#!/usr/bin/env python3
"""Outside recall judge for the memory directory. Standard library only; run as python3 -I.

Asks a fixed question set of fresh `claude --safe-mode -p` children (Read, Grep, Glob only) against one
arm of the memory and grades the answers with regexes. Arms: live (a frozen, verified snapshot copy),
staged (a directory you give), empty (negative control), baseline (live twice + empty once, saved to a
file). Nothing here edits memory. Exit codes: 0 ok; 1 failure, regression or a set that measures model
priors; 3 fewer graded questions than --min; 4 refused before any model call because the notes hold block-tier
secret shapes (override: --allow-secret-shapes); 64 usage. --selftest spends nothing.
"""

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # the judge may live in scripts/recall/
import memory_common as mc  # noqa: E402
import memory_egress as eg  # noqa: E402
import memory_recall_run as mrr  # noqa: E402
import memory_snapshot as sn  # noqa: E402

EXIT_OK, EXIT_FAIL, EXIT_FLOOR, EXIT_USAGE = 0, 1, 3, 64
EMPTY_PASS_LIMIT, NOISE_LIMIT = 3, 2
DEFAULT_MIN, DEFAULT_WORKERS, DEFAULT_TOTAL_USD = 20, 4, 5.0
ARMS = ("live", "staged", "empty", "baseline")
SELFTEST_QUESTIONS = (
    {
        "id": "t1",
        "kind": "recall",
        "question": "q",
        "must": [r"alpha-\d+", "(?i)beta"],
        "forbid": ["stale"],
    },
    {
        "id": "t2",
        "kind": "supersede",
        "question": "q",
        "must": ["gamma"],
        "forbid": ["(?i)old-delta"],
    },
    {
        "id": "t3",
        "kind": "negative",
        "question": "q",
        "must": ["(?i)do not know"],
        "forbid": [],
    },
)
SELFTEST_GOOD = {
    "t1": "alpha-12 and BETA",
    "t2": "gamma, replacing the rest",
    "t3": "I do not know.",
}
SELFTEST_BAD = {
    "t1": ["", "alpha-12 only", "alpha-12 beta stale"],
    "t2": ["", "delta", "gamma and OLD-delta"],
    "t3": ["", "it is 42", "know not"],
}


class _Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, allow_abbrev=False, **kwargs)  # an abbreviation must not switch the override flag on

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, "%s: error: %s\n" % (self.prog, message))


# ------------------------------------------------------------------ pure helpers


def flips(first, second):
    """How many questions graded in both runs got a different verdict."""
    return sum(1 for key in first if key in second and first[key] != second[key])


def stable_passers(baseline):
    runs = baseline["live_runs"]
    return [key for key in runs[0] if all(run.get(key) is True for run in runs)]


def regressions(baseline, staged_runs):
    """Questions that passed every baseline run and failed every staged run (one lucky flip never blocks)."""
    return sorted(
        key
        for key in stable_passers(baseline)
        if all(run.get(key) is False for run in staged_runs)
    )


def verdicts(records):
    return {rec["id"]: rec["pass"] for rec in records if "pass" in rec}


def kind_counts(questions, records):
    kinds = {q["id"]: q["kind"] for q in questions}
    total, passed = Counter(), Counter()
    for rec in records:
        if "pass" in rec:
            total[kinds[rec["id"]]] += 1
            passed[kinds[rec["id"]]] += rec["pass"]
    return " ".join(
        "%s=%d/%d" % (kind, passed[kind], total[kind])
        for kind in mrr.KINDS
        if total[kind]
    )


# ------------------------------------------------------------------ arms and runs


def prepare_arm(arm, args, ops, tmp):
    """(memory directory, label) for an arm."""
    if arm == "empty":
        memory = os.path.join(tmp, "empty-memory")
        mc.makedirs_private(memory)
        os.close(
            os.open(
                os.path.join(memory, "MEMORY.md"),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        )
        return memory, "empty"
    if arm == "staged":
        if not args.staged_dir or not os.path.isfile(
            os.path.join(args.staged_dir, "MEMORY.md")
        ):
            raise mc.HygieneError(
                "--staged-dir must be a directory that holds a MEMORY.md"
            )
        staged = mc.expand(args.staged_dir)
        eg.require_clear(staged, args.allow_secret_shapes)  # a directory, not a snapshot: scanned directly
        return staged, "staged"
    snap = sn.resolve_snapshot(ops, args.snapshot)
    ok, _count, problems = sn.verify_snapshot(ops, snap)
    if not ok:
        raise mc.HygieneError(
            "snapshot %s failed verification (%d problems); take a fresh snapshot first"
            % (snap, len(problems))
        )
    memory = os.path.join(ops, "snapshots", snap, "memory")
    eg.require_clear(memory, args.allow_secret_shapes)  # covers --arm live and the live half of --arm baseline
    return memory, snap


EARLY_FAIL_LIMIT = 6  # this many failed calls before a single answer: the setup is broken, stop paying for doomed calls


class _EarlyFail:
    """Stops a judge run whose first calls all fail (logged out, unknown flag, no network) instead of making every doomed call."""

    def __init__(self, spend, limit=EARLY_FAIL_LIMIT):
        self.spend, self.limit, self.failed, self.answered, self.first = spend, limit, 0, False, None
        self._lock = threading.Lock()

    def note(self, record):
        with self._lock:
            if "error" not in record:
                self.answered = True
                return
            self.failed += 1
            self.first = self.first or record["error"]
            if not self.answered and self.failed >= self.limit:
                self.spend.stop("the first %d model calls all failed, so nothing was graded (first error: %s); run doctor/memory_doctor.py --live --model <the judge model> to see why" % (self.failed, self.first))


def run_arm(questions, runner, settings, memory, tmp, label, spend, workers):
    prompt = os.path.join(tmp, "prompt-%s.txt" % label)
    index = (
        mc.read_text(os.path.join(memory, "MEMORY.md"))
        if os.path.isfile(os.path.join(memory, "MEMORY.md"))
        else ""
    )
    mrr.write_system_prompt(prompt, memory, mrr.visible_index(index))
    cwd = os.path.join(tmp, "child-cwd")
    mc.makedirs_private(cwd)
    early = _EarlyFail(spend)

    def one(question):
        record = mrr.ask(question, runner, settings, prompt, memory, spend, cwd)
        early.note(record)
        return record

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(one, questions))


def report_run(arm, number, questions, records):
    graded = sum(1 for rec in records if "pass" in rec)
    passed = sum(1 for rec in records if rec.get("pass"))
    print(
        "RECALL arm=%s run=%d graded=%d pass=%d/%d errors=%d"
        % (arm, number, graded, passed, graded, len(records) - graded)
    )
    print("KINDS " + kind_counts(questions, records))
    failed = [rec["id"] for rec in records if rec.get("pass") is False]
    broken = [rec["id"] for rec in records if "error" in rec]
    print("FAILED " + (",".join(failed) or "-") + " ERRORED " + (",".join(broken) or "-"))
    return graded, passed


def execute(args, runner, questions, ops, tmp, plan, spend):
    """Run every (arm, repeat) of the plan; returns {arm: [records per run]} and the memory label of the live arm."""
    settings = mrr.Settings(args.model, args.per_q_usd)
    results, labels = {}, {}
    for arm, repeat in plan:
        memory, labels[arm] = prepare_arm(arm, args, ops, tmp)
        for number in range(1, repeat + 1):
            records = run_arm(
                questions,
                runner,
                settings,
                memory,
                tmp,
                "%s%d" % (arm, number),
                spend,
                args.workers,
            )
            results.setdefault(arm, []).append(records)
            report_run(arm, number, questions, records)
    if any("pass" in rec for runs in results.values() for run in runs for rec in run):
        print("TOOLS ok=" + ",".join(mrr.EXPECTED_TOOLS))  # only when a child really answered and reported them
    return results, labels


# ------------------------------------------------------------------ the arms' verdicts


def empty_verdict(questions, records):
    kinds = {q["id"]: q["kind"] for q in questions}
    graded = [r for r in records if "pass" in r and kinds[r["id"]] != "negative"]
    passed = sum(1 for r in graded if r["pass"])
    print(
        "EMPTY pass=%d of %d non-negative graded (limit %d)"
        % (passed, len(graded), EMPTY_PASS_LIMIT)
    )
    if passed > EMPTY_PASS_LIMIT:
        print(
            "the empty memory already passes %d questions: the set measures model priors and must be rewritten"
            % passed
        )
    return passed, len(graded)


def noise_line(runs):
    if len(runs) < 2:
        return 0
    count = flips(verdicts(runs[0]), verdicts(runs[1]))
    print("NOISE flips=%d limit=%d" % (count, NOISE_LIMIT))
    return count


def finish_baseline(args, questions, results, labels, spend, ops):
    live, empty = results["live"], results["empty"][0]
    empty_pass, empty_graded = empty_verdict(questions, empty)
    noise = noise_line(live)
    graded = min(sum(1 for r in run if "pass" in r) for run in live + [empty])
    data = {
        "date": time.strftime("%Y-%m-%d"),
        "model": args.model,
        "snapshot": labels["live"],
        "questions": len(questions),
        "graded": graded,
        "empty_pass": empty_pass,
        "empty_graded": empty_graded,
        "noise_flips": noise,
        "live_runs": [verdicts(run) for run in live],
        "empty_run": verdicts(empty),
        "total_usd": round(spend.total, 4),
    }
    ok = empty_pass <= EMPTY_PASS_LIMIT and noise <= NOISE_LIMIT
    if ok and args.save:
        fd = os.open(mc.expand(args.save), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
        print("BASELINE saved " + mc.expand(args.save))
    return ok, "baseline snapshot=%s graded=%d empty_pass=%d flips=%d" % (
        labels["live"],
        graded,
        empty_pass,
        noise,
    )


def finish_staged(args, questions, results):
    with open(mc.expand(args.baseline), encoding="utf-8") as fh:
        baseline = json.load(fh)
    runs = results["staged"]
    lost = regressions(baseline, [verdicts(run) for run in runs])
    print("REGRESSIONS n=%d ids=%s" % (len(lost), ",".join(lost) or "-"))
    kinds = {q["id"]: q["kind"] for q in questions}
    back = sorted(
        {
            r["id"]
            for run in runs
            for r in run
            if kinds[r["id"]] == "supersede" and r.get("forbid_hit")
        }
    )
    print("RESURRECTED n=%d ids=%s" % (len(back), ",".join(back) or "-"))
    drift = [k for k, kind in kinds.items() if kind == "drift"]
    fixed = [
        k
        for k in drift
        if all(run.get(k) is False for run in baseline["live_runs"])
        and all(verdicts(run).get(k) is True for run in runs)
    ]
    print("DRIFT fixed=%d of %d" % (len(fixed), len(drift)))
    noise_line(runs)
    return (
        not lost and not back,
        "staged regressions=%d resurrected=%d drift_fixed=%d"
        % (len(lost), len(back), len(fixed)),
    )


# ------------------------------------------------------------------ selftest


def selftest(args):
    good = sum(mrr.grade(q, SELFTEST_GOOD[q["id"]]) for q in SELFTEST_QUESTIONS)
    bad_total = sum(len(SELFTEST_BAD[q["id"]]) for q in SELFTEST_QUESTIONS)
    bad = sum(
        mrr.grade(q, a) for q in SELFTEST_QUESTIONS for a in SELFTEST_BAD[q["id"]]
    )
    same = {q["id"]: True for q in SELFTEST_QUESTIONS}
    flipped = dict(same, t1=False)
    control = flips(same, dict(same)) == 0 and flips(same, flipped) == 1
    print(
        "selftest good=%d/%d bad=%d/%d flip-control=%s"
        % (good, len(SELFTEST_QUESTIONS), bad, bad_total, "OK" if control else "FAIL")
    )
    ok = good == len(SELFTEST_QUESTIONS) and bad == 0 and control
    if args.questions and os.path.isfile(mc.expand(args.questions)):
        count = len(mrr.load_questions(mc.expand(args.questions)))
        print("questions=%d file=ok" % count)
    elif args.questions_given:
        print("questions=missing")
        ok = False
    return EXIT_OK if ok else EXIT_FAIL


# ------------------------------------------------------------------ command line


def build_parser():
    p = _Parser(prog="memory_recall.py", description=__doc__.split("\n")[0])
    p.add_argument("--arm", choices=ARMS, default="live")
    p.add_argument(
        "--selftest", action="store_true", help="grade canned answers; spends nothing"
    )
    p.add_argument(
        "--questions",
        default=None,
        help="question file (default: <ops>/recall/questions.jsonl)",
    )
    p.add_argument(
        "--dir", default=None, help="memory directory (default: this project's)"
    )
    p.add_argument(
        "--ops-root",
        default=None,
        help="data root for backups, logs, digests and baselines (default <Claude config dir>/memory-arborist, normally ~/.claude/memory-arborist)",
    )
    p.add_argument(
        "--staged-dir", default=None, help="memory directory to test for --arm staged"
    )
    p.add_argument(
        "--baseline", default=None, help="baseline file to compare a staged run with"
    )
    p.add_argument(
        "--save", default=None, help="where --arm baseline writes the baseline file"
    )
    p.add_argument("--snapshot", default="latest", help="snapshot id for the live arm")
    p.add_argument(
        "--repeat",
        type=int,
        default=None,
        help="runs per arm (live 1, staged 2, baseline 2+1)",
    )
    p.add_argument(
        "--min",
        type=int,
        default=DEFAULT_MIN,
        dest="floor",
        help="fewest graded questions allowed",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="only the first N questions (smoke test)",
    )
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    p.add_argument("--model", default=mrr.DEFAULT_MODEL)
    p.add_argument("--per-q-usd", type=float, default=0.25)
    p.add_argument("--total-max-usd", type=float, default=DEFAULT_TOTAL_USD)
    p.add_argument("--no-log", action="store_true")
    p.add_argument("--allow-secret-shapes", action="store_true", help="send the notes even though they hold block-tier secret shapes or entries that could not be scanned (default: refuse, exit 4)")
    return p


def make_plan(args):
    if args.arm == "baseline":
        return [("live", args.repeat or 2), ("empty", 1)]
    return [(args.arm, args.repeat or (2 if args.arm == "staged" else 1))]


def judge(args, runner):
    dirs = mc.expand(args.dir) if args.dir else mc.DEFAULT_MEMORY_DIR
    ops = mc.ops_dir_for(
        dirs, mc.expand(args.ops_root) if args.ops_root else mc.DEFAULT_OPS_ROOT
    )
    path = (
        mc.expand(args.questions)
        if args.questions
        else os.path.join(ops, "recall", "questions.jsonl")
    )
    questions = mrr.load_questions(path)[: args.limit]
    if args.arm == "staged" and not args.baseline:
        raise mc.HygieneError("--arm staged needs --baseline FILE")
    spend = mrr.Spend(args.total_max_usd)
    with tempfile.TemporaryDirectory(prefix="recall_run_") as tmp:  # holds a copy of the index
        return _judge_in(args, runner, questions, ops, tmp, spend)


def _judge_in(args, runner, questions, ops, tmp, spend):
    runs = sum(repeat for _arm, repeat in make_plan(args))
    print(mrr.estimate_line("judge", len(questions) * runs, args.per_q_usd, args.total_max_usd))
    try:
        results, labels = execute(
            args, runner, questions, ops, tmp, make_plan(args), spend
        )
    except mrr.RunAborted as err:
        print("ABORTED %s" % mc.printable(err))
        return EXIT_FAIL
    print("COST total_usd=%.4f limit=%s" % (spend.total, args.total_max_usd))
    graded = min(
        sum(1 for r in run if "pass" in r) for runs in results.values() for run in runs
    )
    if graded < args.floor:
        print("FLOOR graded=%d is below --min %d" % (graded, args.floor))
        return EXIT_FLOOR
    ok, subject = True, "%s run" % args.arm
    if args.arm == "baseline":
        ok, subject = finish_baseline(args, questions, results, labels, spend, ops)
    elif args.arm == "staged":
        ok, subject = finish_staged(args, questions, results)
    elif args.arm == "empty":
        ok = empty_verdict(questions, results["empty"][0])[0] <= EMPTY_PASS_LIMIT
    else:
        noise_line(results["live"])
    if not args.no_log:
        mc.append_log(
            ops,
            "recall",
            subject,
            [
                "Model: %s" % args.model,
                "Spend: %.4f USD" % spend.total,
                "Questions: %d" % len(questions),
            ],
        )
    return EXIT_OK if ok else EXIT_FAIL


def main(argv=None, runner=None):
    args = build_parser().parse_args(argv)
    args.questions_given = args.questions is not None
    try:
        if args.selftest:
            return selftest(args)
        return judge(args, runner or mrr.subprocess_runner)
    except eg.SecretShapesRefused as err:
        print("REFUSED: %s" % mc.printable(err), file=sys.stderr)
        return eg.EXIT_SECRET_SHAPES
    except (mc.HygieneError, OSError, ValueError) as err:
        print("ERROR: %s" % mc.printable(err), file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
