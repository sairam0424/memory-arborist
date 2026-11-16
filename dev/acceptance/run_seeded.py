"""Run the seeded-bad-note acceptance test against the real pipeline.

usage: python3 -I dev/acceptance/run_seeded.py            (nothing is sent: builds the fixture, prints the plan and the ceiling)
       python3 -I dev/acceptance/run_seeded.py --live     (runs the real lanes with your claude login, up to --max-usd)

The fixture is synthetic (see seeded.py) and lives in a new folder under the system temp directory; nothing is deleted
afterwards, the path is printed. Your own notes and sessions are never read.
"""

import argparse
import contextlib
import glob
import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(os.path.dirname(os.path.dirname(HERE)), "engine")
for path in (
    HERE,
    ENGINE,
    os.path.join(ENGINE, "dream"),
    os.path.join(ENGINE, "recall"),
):
    sys.path.insert(0, path)
import memory_common as mc  # noqa: E402
import memory_dream_lanes as lanes  # noqa: E402
import memory_dream_pin as dp  # noqa: E402
import memory_dream_run as drun  # noqa: E402
import memory_evidence as me  # noqa: E402
import memory_recall_run as mrr  # noqa: E402
import memory_snapshot as sn  # noqa: E402
import seeded  # noqa: E402

DEFAULT_MAX_USD = 3.0


def staged_hunks(ops):
    """(accepted, deferred) hunks of the newest staged run, or ([], []) when the run staged nothing."""
    runs = sorted(glob.glob(os.path.join(ops, "dream", "2*", "hunks.accepted.json")))
    if not runs:
        return [], []
    folder = os.path.dirname(runs[-1])
    return json.loads(mc.read_text(runs[-1])), json.loads(mc.read_text(os.path.join(folder, "hunks.deferred.json")))


def run(root, runner, model, max_usd, workers=2):
    """Build the fixture under root, run the real dream pipeline with `runner`, and return the counts."""
    manifest = seeded.build_fixture(root)
    ops_root = os.path.join(root, "ops")
    ops = mc.ops_dir_for(manifest["memory"], ops_root)
    snap = sn.take_snapshot(manifest["memory"], ops)["id"]
    built = me.build_all(manifest["transcripts"], os.path.join(ops, "evidence"))
    dp.create(ops, False)
    argv = [
        "--dir",
        manifest["memory"],
        "--ops-root",
        ops_root,
        "run",
        "--snapshot",
        snap,
        "--workers",
        str(workers),
        "--model",
        model,
        "--total-max-usd",
        str(max_usd),
    ]
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = drun.main(argv, runner=runner)
    pipeline = next(
        (line for line in out.getvalue().splitlines() if line.startswith("PIPELINE ")),
        "",
    )
    accepted, deferred = staged_hunks(ops)
    return {
        "exit": code,
        "digests": len(built.written),
        "pipeline": pipeline,
        "score": seeded.score(manifest, accepted, deferred),
        "ops": ops,
    }


def render(result):
    s = result["score"]
    lines = [
        "SEEDED digests=%d exit=%d" % (result["digests"], result["exit"]),
        result["pipeline"],
        "stale notes: %d  fixed: %d  deferred past the hunk cap: %d  missed: %d  edited but not to the new value: %d"
        % (s["stale"], s["fixed"], s["deferred"], s["missed"], s["off_target"]),
        "other notes edited (a steady note, a trap, or a stray index line): %d %s"
        % (s["edited_other_notes"], s["edited_other_note_names"]),
        "injected instruction followed: %d" % s["injection_followed"],
        "accepted hunks: %d (good: %d, of which index lines: %d)  recall: %s (with deferred: %s)  precision: %s"
        % (s["hunks"], s["good_hunks"], s["index_hunks_good"], s["recall"], s["recall_with_deferred"], s["precision"]),
    ]
    for label, key in (("deferred", "deferred_notes"), ("missed", "missed_notes")):
        if s[key]:
            lines.append("%s: %s" % (label, ", ".join(s[key])))
    return "\n".join(lines)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="run_seeded.py", description=__doc__.split("\n")[0], allow_abbrev=False
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="make real model calls (default: print the plan only)",
    )
    parser.add_argument("--model", default=drun.DEFAULT_MODEL)
    parser.add_argument("--max-usd", type=float, default=DEFAULT_MAX_USD)
    return parser


def main(argv=None, runner=None):
    args = build_parser().parse_args(argv)
    root = tempfile.mkdtemp(prefix="seeded_")
    if not args.live:
        manifest = seeded.build_fixture(root)
        print(
            "PLAN %d stale notes, %d steady notes, %d traps; fixture in %s"
            % (
                len(manifest["stale"]),
                len(manifest["steady"]),
                len(manifest["traps"]),
                root,
            )
        )
        print(
            mrr.estimate_line(
                "seeded acceptance (finders, writers, verifiers)",
                "an open number of",
                0.6,
                args.max_usd,
            )
        )
        print("Nothing was sent. Add --live to run it with your claude login.")
        return 0
    result = run(root, runner or lanes.subprocess_runner, args.model, args.max_usd)
    print(render(result))
    if result["exit"] == drun.EXIT_INCOMPLETE:
        print("INCOMPLETE: some model calls failed, so these numbers under-report what the lanes can do; fix the cause and run again")
    print("work folder: %s" % root)
    return 0 if result["exit"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
