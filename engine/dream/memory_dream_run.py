#!/usr/bin/env python3
"""Run the dream pipeline: find, write, verify (all tool-less lanes), then ground and stage the survivors.

Standard library only; run as python3 -I. The lanes only ever see the evidence digests, the index and the one note
they are asked about. What they return is a proposal: memory_dream.py ground checks it mechanically and stages it
outside the memory directory, and a person reads the report before anything is promoted. Nothing here edits memory.
Exit codes: 0 ok; 1 failure or failed staging checks; 2 usage (argparse); 4 refused before any model call because the
snapshot holds block-tier secret shapes (override: --allow-secret-shapes); 5 the run finished but some model calls failed
(the staged report starts with INCOMPLETE: no hunks then does not mean nothing needed changing).
"""

import argparse
import json
import os
import re
import sys
import tempfile
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, os.path.join(SCRIPTS, "recall"))
import memory_common as mc  # noqa: E402
import memory_dream as dr  # noqa: E402
import memory_dream_ground as dg  # noqa: E402
import memory_dream_lanes as lanes  # noqa: E402
import memory_egress as eg  # noqa: E402
import memory_evidence as me  # noqa: E402
import memory_recall_run as mrr  # noqa: E402
import memory_snapshot as sn  # noqa: E402

ENV_LANE_MODEL = "MEMORY_SHEPHERD_LANE_MODEL"
# An alias, see memory_recall_run.DEFAULT_MODEL: it resolves per provider. Pin a full model id with the environment variable.
DEFAULT_MODEL = os.environ.get(ENV_LANE_MODEL) or "sonnet"
DIGESTS_PER_LANE, MAX_NOTES, MAX_FINDINGS_PER_LANE = 6, 12, 5
# One writer may hand on at most this many hunks (half of memory_dream.DEFAULT_MAX_HUNKS): every hunk costs a paid
# verifier call, so an injected note or digest must not be able to turn one writer reply into a flood of them.
MAX_HUNKS_PER_WRITER = 5
EXIT_INCOMPLETE = 5
INDEX_NAME = "MEMORY.md"
NOTE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.md")
PipelineResult = namedtuple("PipelineResult", "hunks stats trace")


def _pool_map(workers, fn, items):
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(fn, items))


def _valid_finding(item, notes):
    return (
        isinstance(item, dict)
        and isinstance(item.get("note"), str)
        and NOTE_NAME.fullmatch(item["note"])
        and item["note"] in notes
        and item["note"] != INDEX_NAME
        and isinstance(item.get("claim"), str)
        and isinstance(item.get("fact"), str)
        and isinstance(item.get("evidence"), list)
    )


def _finder_stage(digest_texts, index_text, notes, ctx):
    dates = ctx.get("note_dates")
    chunks = [
        digest_texts[i : i + DIGESTS_PER_LANE]
        for i in range(0, len(digest_texts), DIGESTS_PER_LANE)
    ]
    answers = _pool_map(
        ctx["workers"],
        lambda chunk: lanes.run_lane(
            lanes.finder_prompt(index_text, chunk, dates), *ctx["lane"]
        ),
        chunks,
    )
    found, errors = [], 0
    for answer in answers:
        items = answer.get("findings") if isinstance(answer, dict) else None
        if not isinstance(items, list):
            errors += 1
            continue
        found += [f for f in items[:MAX_FINDINGS_PER_LANE] if _valid_finding(f, notes)]
    return found, errors


def _writer_tasks(findings, notes, index_text, max_notes):
    by_note = {}
    for item in findings:
        by_note.setdefault(item["note"], []).append(item)
    ranked = sorted(by_note, key=lambda n: (-len(by_note[n]), n))[:max_notes]
    tasks = [
        (name, lanes.writer_prompt(name, notes[name], by_note[name]), "edit")
        for name in ranked
    ]
    index_findings = [
        f
        for name in ranked
        for f in by_note[name]
        if f["claim"] and f["claim"] in index_text
    ]
    if index_findings:
        tasks.append(
            (
                INDEX_NAME,
                lanes.index_writer_prompt(index_text, index_findings),
                "index_edit",
            )
        )
    return tasks


def _valid_hunk(item):
    """A writer hunk with the types the later stages rely on; anything else is dropped here, not crashed on."""
    evidence = item.get("evidence") if isinstance(item, dict) else None
    return (
        isinstance(item, dict)
        and isinstance(item.get("old"), str)
        and isinstance(item.get("new"), str)
        and isinstance(item.get("reason"), str)
        and isinstance(evidence, list)
        and bool(evidence)
        and all(isinstance(e, dict) and isinstance(e.get("session"), str) and isinstance(e.get("quote"), str) for e in evidence)
    )


def _writer_stage(tasks, ctx):
    answers = _pool_map(
        ctx["workers"], lambda task: lanes.run_lane(task[1], *ctx["lane"]), tasks
    )
    hunks, errors, over_cap = [], 0, 0
    for (name, _prompt, kind), answer in zip(tasks, answers):
        items = answer.get("hunks") if isinstance(answer, dict) else None
        if not isinstance(items, list):
            errors += 1
            continue
        kept = 0
        for item in items:
            if not _valid_hunk(item):
                errors += 1
            elif kept < MAX_HUNKS_PER_WRITER:
                hunks.append(dict(item, kind=kind, file=name))
                kept += 1
            else:
                over_cap += 1
    return hunks, errors, over_cap


def _evidence_lines(hunk, digests_by_session):
    lines = []
    for item in hunk.get("evidence", []):
        text = digests_by_session.get(
            item.get("session") if isinstance(item, dict) else None, ""
        )
        quote = (
            " ".join(str(item.get("quote", "")).split())
            if isinstance(item, dict)
            else ""
        )
        stamp = re.search(r"^date: (\S+)", text, re.M)
        label = "(digest dated %s) " % (stamp.group(1) if stamp else "unknown")
        lines += [label + line for line in text.split("\n") if quote and quote in " ".join(line.split())][:3]
    return lines or ["(no matching digest line)"]


def _verifier_stage(hunks, notes, digests_by_session, ctx):
    """[(kept, why)] in hunk order; anything but an explicit keep is a rejection."""

    def verify(hunk):
        prompt = lanes.verifier_prompt(
            hunk, _evidence_lines(hunk, digests_by_session), notes.get(hunk["file"], ""), ctx.get("note_dates", {}).get(hunk["file"])
        )
        answer = lanes.run_lane(prompt, *ctx["lane"])
        if not isinstance(answer, dict):
            return False, "no usable verdict"
        return answer.get("verdict") == "keep", str(answer.get("why", ""))[:300]

    return _pool_map(ctx["workers"], verify, hunks)


def run_pipeline(ops, snap_id, runner, settings, spend, workers=4, max_notes=MAX_NOTES):
    digest_dir = os.path.join(ops, "evidence")
    if not me.verify_dir(digest_dir).ok:
        raise mc.HygieneError(
            "the evidence digests do not verify; rebuild or inspect them first"
        )
    snapshot_dir = os.path.join(ops, "snapshots", snap_id, "memory")
    notes = {
        rel: mc.read_text(os.path.join(snapshot_dir, rel))
        for rel in mc.list_md(snapshot_dir)
    }
    index_text = notes.get(INDEX_NAME, "")
    names = sorted(n for n in os.listdir(digest_dir) if n.endswith(".md"))
    texts = [mc.read_text(os.path.join(digest_dir, n)) for n in names]
    by_session = {n[: -len(".md")]: t for n, t in zip(names, texts)}
    manifest = sn.load_manifest(ops, snap_id)
    note_dates = {rel: dg._utc_date(info["mtime_ns"]) for rel, info in manifest["files"].items() if rel.endswith(".md")}
    with tempfile.TemporaryDirectory(prefix="dream_lanes_") as workdir:  # the system prompt and the children's empty cwd
        system_file = os.path.join(workdir, "system.txt")
        fd = os.open(system_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(lanes.SYSTEM_PROMPT)
        empty_cwd = os.path.join(workdir, "cwd")
        mc.makedirs_private(empty_cwd)
        ctx = {"workers": workers, "lane": (system_file, settings, runner, spend, empty_cwd), "note_dates": note_dates}
        return _run_stages(ctx, texts, notes, index_text, by_session, max_notes, spend)


def _run_stages(ctx, texts, notes, index_text, by_session, max_notes, spend):
    findings, finder_errors = _finder_stage(texts, index_text, notes, ctx)
    tasks = _writer_tasks(findings, notes, index_text, max_notes)
    proposed, writer_errors, over_cap = _writer_stage(tasks, ctx)
    if over_cap:
        print("HUNK-CAP dropped=%d (at most %d per writer)" % (over_cap, MAX_HUNKS_PER_WRITER))
    verdicts = _verifier_stage(proposed, notes, by_session, ctx)
    kept = [dict(h, id="h%d" % (i + 1)) for i, (h, (ok, _why)) in enumerate(zip(proposed, verdicts)) if ok]
    trace = {"findings": findings, "proposed": [dict(h, kept=ok, verifier_why=why) for h, (ok, why) in zip(proposed, verdicts)]}
    stats = {
        "findings": len(findings),
        "writers": len(tasks),
        "proposed": len(proposed),
        "rejected_by_verifier": len(proposed) - len(kept),
        "lane_errors": finder_errors + writer_errors,
        "over_cap": over_cap,
        "cost": round(spend.total, 4),
    }
    return PipelineResult(kept, stats, trace)


def write_hunks_file(ops, hunks, now=None, suffix=".json"):
    directory = os.path.join(ops, "dream", "inputs")
    mc.makedirs_private(directory)
    path = os.path.join(directory, mc.utc_stamp(now) + suffix)
    if os.path.lexists(path):
        raise mc.HygieneError("a hunks file for this second already exists")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(hunks, fh, indent=1, sort_keys=True)
    return path


# ------------------------------------------------------------------ command line


def _verified_snapshot(ops, args):
    """The snapshot id once it is verified and its notes may be sent: verify first, so the scan reads verified bytes."""
    snap = sn.resolve_snapshot(ops, args.snapshot)
    ok, _count, problems = sn.verify_snapshot(ops, snap)
    if not ok:
        raise mc.HygieneError(
            "snapshot %s failed verification (%d problems)" % (snap, len(problems))
        )
    eg.require_clear(os.path.join(ops, "snapshots", snap, "memory"), args.allow_secret_shapes)
    return snap


def cmd_run(args, runner):
    memory, ops = dr._paths(args)
    dr._load_pinned(ops, args.pinned_draft_ok)  # a missing pinned.txt is found now, not after the paid lanes have run
    snap = _verified_snapshot(ops, args)
    print(mrr.estimate_line("dream lanes (finders, writers, verifiers)", "an open number of", args.per_lane_usd, args.total_max_usd))
    spend = mrr.Spend(args.total_max_usd)
    try:
        result = run_pipeline(
            ops,
            snap,
            runner,
            lanes.Settings(args.model, args.per_lane_usd),
            spend,
            args.workers,
            args.max_notes,
        )
    except mrr.RunAborted as err:
        print("ABORTED %s" % mc.printable(err))
        return dr.EXIT_FAIL
    s = result.stats
    print(
        "PIPELINE findings=%d writers=%d proposed=%d rejected_by_verifier=%d kept=%d lane_errors=%d cost_usd=%.4f"
        % (
            s["findings"],
            s["writers"],
            s["proposed"],
            s["rejected_by_verifier"],
            len(result.hunks),
            s["lane_errors"],
            s["cost"],
        )
    )
    incomplete = s["lane_errors"]
    if incomplete:
        print(
            "INCOMPLETE %d model call(s) failed (see the LANE-ERROR lines above): this run may be missing proposals, so "
            "'accepted 0' does not mean there was nothing to change" % incomplete
        )
    path = write_hunks_file(ops, result.hunks, args.now)
    print("HUNKS " + path)
    print("TRACE " + write_hunks_file(ops, result.trace, args.now, ".trace.json"))
    mc.append_log(
        ops,
        "dream",
        "lanes kept=%d proposed=%d" % (len(result.hunks), s["proposed"]),
        ["Model: " + args.model, "Spend: %.4f USD" % s["cost"]],
    )
    ground_args = [
        "--dir",
        memory,
        "--ops-root",
        args.ops_root or mc.DEFAULT_OPS_ROOT,
        "ground",
        "--hunks",
        path,
        "--snapshot",
        snap,
    ]
    if args.pinned_draft_ok:
        ground_args.append("--pinned-draft-ok")
    if incomplete:
        ground_args += ["--incomplete", str(incomplete)]
    code = dr.main(ground_args)
    return EXIT_INCOMPLETE if code == dr.EXIT_OK and incomplete else code


def build_parser():
    parser = argparse.ArgumentParser(
        prog="memory_dream_run.py", description=__doc__.split("\n")[0], allow_abbrev=False
    )
    parser.add_argument("--dir", default=None)
    parser.add_argument("--ops-root", default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)
    cmd = sub.add_parser(
        "run", help="run the lanes, then ground and stage what they proposed", allow_abbrev=False
    )
    cmd.add_argument("--snapshot", default="latest")
    cmd.add_argument("--model", default=DEFAULT_MODEL)
    cmd.add_argument("--per-lane-usd", type=float, default=0.6)
    cmd.add_argument("--total-max-usd", type=float, default=8.0)
    cmd.add_argument("--workers", type=int, default=4)
    cmd.add_argument("--max-notes", type=int, default=MAX_NOTES)
    cmd.add_argument("--pinned-draft-ok", action="store_true")
    cmd.add_argument("--allow-secret-shapes", action="store_true", help="send the notes even though they hold block-tier secret shapes or entries that could not be scanned (default: refuse, exit 4)")
    cmd.add_argument("--now", type=float, default=None, help=argparse.SUPPRESS)
    return parser


def main(argv=None, runner=None):
    args = build_parser().parse_args(argv)
    try:
        return cmd_run(args, runner or lanes.subprocess_runner)
    except eg.SecretShapesRefused as err:
        print("REFUSED: %s" % mc.printable(err), file=sys.stderr)
        return eg.EXIT_SECRET_SHAPES
    except (mc.HygieneError, OSError, ValueError) as err:
        print("ERROR: %s" % mc.printable(err), file=sys.stderr)
        return dr.EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
