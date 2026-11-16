#!/usr/bin/env python3
"""Safety net for the Claude Code auto-memory directory. Standard library only; run as python3 -I.

Commands: lint, scan, snapshot, snapshots, verify-snapshot, compare, restore, log, prune-plan.
Writes: snapshot and restore --apply write under the data root (restore also swaps one live note and
archives the old copy first); lint, scan, snapshot-based compare and restore append to
<data root>/<project>/log.md unless --no-log (a failing log write is a warning, never a different exit
code); log appends on request. Nothing here deletes anything.
Exit codes: 0 ok; 1 failure, refusal or lint errors; 2 scan found block-tier secret shapes; 64 usage.
Deferred to the dream phase: promote, verify-live, canary-check.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memory_common as mc  # noqa: E402
import memory_lint as ml  # noqa: E402
import memory_scan as ms  # noqa: E402
import memory_snapshot as sn  # noqa: E402

EXIT_OK, EXIT_FAIL, EXIT_BLOCK, EXIT_USAGE = 0, 1, 2, 64
LIST_CAP = 50


class _Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("allow_abbrev", False)  # --app must not quietly mean --apply: the confirmation hook matches the full flag
        super().__init__(*args, **kwargs)

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, "%s: error: %s\n" % (self.prog, message))


def _require_dir(path):
    if not os.path.isdir(path):
        raise sn.HygieneError("directory not found: %s" % mc.printable(path))


def _ops_path(args):
    return mc.ops_dir_for(args.dir, args.ops_root)


def _storage_ops(args):
    """Commands that keep state need an explicit ops root unless they work on the real memory directory."""
    if not args.log_ok:
        raise sn.HygieneError(
            "pass --ops-root when --dir is not the default memory directory"
        )
    ops = _ops_path(args)
    sn.ensure_ops_outside(args.dir, ops)
    return ops


def _log(args, op, subject, bullets=(), always=False):
    """Append to the log; a failure is a warning because it must never change the command's exit code."""
    if not args.log_ok or (getattr(args, "no_log", False) and not always):
        return
    try:
        ops = _storage_ops(args)
        mc.append_log(ops, op, subject, bullets)
    except (OSError, sn.HygieneError) as err:
        print("WARN could not write the log: %s" % mc.printable(err), file=sys.stderr)


def _last_md_count(ops):
    ids = sn.list_snapshots(ops)
    return (
        sum(1 for rel in sn.load_manifest(ops, ids[-1])["files"] if rel.endswith(".md"))
        if ids
        else None
    )


def cmd_lint(args):
    _require_dir(args.dir)
    last = _last_md_count(_ops_path(args)) if args.log_ok else None
    result = ml.lint_dir(
        args.dir,
        project_dir=os.path.dirname(os.path.abspath(args.dir)),
        last_snapshot_count=last,
    )
    print("\n".join(ml.render(result, full=args.full)))
    counts = {
        sev: sum(1 for f in result.findings if f.severity == sev)
        for sev in ("ERROR", "WARN")
    }
    stats = result.stats
    _log(
        args,
        "lint",
        "errors=%d warn=%d" % (counts["ERROR"], counts["WARN"]),
        [
            "Index: chars=%d lines=%d headroom=%d"
            % (stats["chars"], stats["eff_lines"], stats["headroom"])
        ],
    )
    return ml.exit_code(result, strict=args.strict)


def cmd_scan(args):
    _require_dir(args.dir)
    result = ms.scan_tree(args.dir)
    block, warn = ms.summarize(result.hits)
    ordered = sorted(result.hits, key=lambda h: (h.tier != "block", h.path, h.line))
    warns = [h for h in ordered if h.tier != "block"]
    shown = (
        ordered
        if args.full
        else [h for h in ordered if h.tier == "block"] + warns[: ms.WARN_HITS_SHOWN]
    )
    for hit in shown:
        print("%s %s" % (hit.tier.upper(), ms.format_hit(hit)))
    if len(shown) < len(ordered):
        print("  ... +%d more warn hits (use --full)" % (len(ordered) - len(shown)))
    for path, reason in result.skipped:
        print("SKIPPED %s: %s" % (mc.printable(path), mc.printable(reason)))
    print("SCAN block=%d warn=%d skipped=%d" % (block, warn, len(result.skipped)))
    _log(
        args, "scan", "block=%d warn=%d skipped=%d" % (block, warn, len(result.skipped))
    )
    return EXIT_BLOCK if block else (EXIT_FAIL if result.skipped else EXIT_OK)


def cmd_snapshot(args):
    ops = _storage_ops(args)
    res = sn.take_snapshot(args.dir, ops)
    if res["status"] == "noop":
        print("NOOP identical to %s" % res["id"])
        return EXIT_OK
    if res["previous_problems"]:
        print(
            "WARN the previous snapshot failed verification (%d problems); this one replaces it as the reference"
            % len(res["previous_problems"])
        )
    if res["scan"][0]:
        print(
            "WARN the copied notes contain %d block-tier secret shapes; run scan to see where"
            % res["scan"][0]
        )
    if res.get("unscanned"):
        print(
            "WARN %d note(s) were not scanned for secrets (a note over %s, or a file that could not be read): scan names them. "
            "The paid steps refuse to send notes that were not scanned (exit 4) until you shrink them or pass --allow-secret-shapes"
            % (res["unscanned"], ms.limit_text())
        )
    print(
        "SNAPSHOT ok files=%d manifest_sha256_ok=%d id=%s scan_block=%d scan_warn=%d skipped_symlinks=%d"
        % (
            res["files"],
            res["ok_count"],
            res["id"],
            res["scan"][0],
            res["scan"][1],
            len(res["skipped"]),
        )
    )
    _log(
        args,
        "snapshot",
        res["id"],
        [
            "Files: %d" % res["files"],
            "Scan: block=%d warn=%d" % res["scan"],
            "Skipped (symlinks, special files): %d" % len(res["skipped"]),
        ],
        always=True,
    )
    return EXIT_OK


def cmd_snapshots(args):
    ops = _storage_ops(args)
    for snap_id in sn.list_snapshots(ops):
        print(
            "%s files=%d"
            % (mc.printable(snap_id), len(sn.load_manifest(ops, snap_id)["files"]))
        )
    for snap_id in sn.broken_snapshots(ops):
        print("%s incomplete (no valid manifest)" % mc.printable(snap_id))
    return EXIT_OK


def cmd_verify(args):
    ops = _storage_ops(args)
    snap_id = sn.resolve_snapshot(ops, args.snapshot)
    ok, checked, problems = sn.verify_snapshot(ops, snap_id)
    print("VERIFY %s files=%d id=%s" % ("ok" if ok else "FAIL", checked, snap_id))
    for problem in problems:
        print("  " + mc.printable(problem))
    return EXIT_OK if ok else EXIT_FAIL


def _compare_baseline(args):
    """(baseline directory, whether it came from a snapshot). A snapshot id wins over a directory of the same name."""
    if args.log_ok:
        ops = _storage_ops(args)
        if args.baseline == "latest" or args.baseline in sn.list_snapshots(ops):
            return os.path.join(
                ops, "snapshots", sn.resolve_snapshot(ops, args.baseline), "memory"
            ), True
    if os.path.isdir(args.baseline):
        if mc.same_dir(args.baseline, args.dir):
            raise sn.HygieneError(
                "the live directory changes under you; compare against a snapshot id instead"
            )
        return args.baseline, False
    raise sn.HygieneError(
        "not a snapshot id or a directory: %s" % mc.printable(args.baseline)
    )


def cmd_compare(args):
    baseline, from_snapshot = _compare_baseline(args)
    files = [name for name in args.files.split(",") if name] or None
    res = sn.compare_dirs(baseline, args.staging, files)
    for rel, number in res.lost[:LIST_CAP]:
        print("LOST %s:%d" % (mc.printable(rel), number))
    if len(res.lost) > LIST_CAP:
        print("  ... +%d more" % (len(res.lost) - LIST_CAP))
    unverifiable = res.baseline_files == 0 or res.lines_checked == 0
    print(
        "COMPARE baseline_files=%d lines_checked=%d lost_lines=%d%s"
        % (
            res.baseline_files,
            res.lines_checked,
            len(res.lost),
            " (cannot verify: nothing to compare)" if unverifiable else "",
        )
    )
    if from_snapshot:
        _log(
            args,
            "compare",
            "lost_lines=%d of %d lines" % (len(res.lost), res.lines_checked),
        )
    return EXIT_FAIL if (res.lost or unverifiable) else EXIT_OK


def cmd_restore(args):
    ops = _storage_ops(args)
    code, messages = sn.restore_file(
        args.dir,
        ops,
        sn.resolve_snapshot(ops, args.snapshot),
        args.file,
        apply=args.apply,
        force=args.force,
    )
    print("\n".join(messages))
    if args.apply:
        _log(args, "restore", "%s (exit %d)" % (args.file, code), messages, always=True)
    return code


def cmd_log(args):
    print(mc.append_log(_storage_ops(args), args.op, args.subject, args.bullet))
    return EXIT_OK


def cmd_prune_plan(args):
    plan = sn.prune_plan(_storage_ops(args), args.keep)
    print("# plan only, nothing is removed; run each line as its own command")
    print("\n".join(plan) if plan else "# nothing to prune")
    return EXIT_OK


def _add_common(parser_cls):
    common = parser_cls(add_help=False)
    common.add_argument(
        "--dir",
        default=argparse.SUPPRESS,
        help="memory directory (default: this project's)",
    )
    common.add_argument(
        "--ops-root",
        default=argparse.SUPPRESS,
        help="data root for backups, logs, digests and baselines (default <Claude config dir>/memory-arborist, normally ~/.claude/memory-arborist)",
    )
    return common


def _register_commands(sub, common):
    def command(name, fn, *flags):
        parser = sub.add_parser(name, parents=[common])
        for flag, kwargs in flags:
            parser.add_argument(flag, **kwargs)
        parser.set_defaults(fn=fn)

    switch = {"action": "store_true"}
    command(
        "lint", cmd_lint, ("--strict", switch), ("--full", switch), ("--no-log", switch)
    )
    command("scan", cmd_scan, ("--full", switch), ("--no-log", switch))
    command("snapshot", cmd_snapshot)
    command("snapshots", cmd_snapshots)
    command("verify-snapshot", cmd_verify, ("--snapshot", {"default": "latest"}))
    command(
        "compare",
        cmd_compare,
        ("baseline", {"help": "snapshot id or a directory"}),
        ("staging", {}),
        ("--files", {"default": ""}),
        ("--no-log", switch),
    )
    command(
        "restore",
        cmd_restore,
        ("--snapshot", {"required": True}),
        ("--file", {"required": True}),
        (
            "--apply",
            {"action": "store_true", "help": "write; without it only a dry run"},
        ),
        (
            "--force",
            {
                "action": "store_true",
                "help": "allow replacing a live file that differs (archived first)",
            },
        ),
    )
    command(
        "log",
        cmd_log,
        ("op", {"choices": sorted(mc.VALID_OPS)}),
        ("subject", {}),
        ("--bullet", {"action": "append", "default": []}),
    )
    command(
        "prune-plan",
        cmd_prune_plan,
        ("--keep", {"type": int, "default": sn.KEEP_SNAPSHOTS}),
    )


def build_parser():
    parser = _Parser(prog="memory_hygiene.py", description=__doc__.split("\n")[0])
    parser.add_argument(
        "--dir", default=None, help="memory directory (default: this project's)"
    )
    parser.add_argument(
        "--ops-root",
        default=None,
        help="data root for backups, logs, digests and baselines (default <Claude config dir>/memory-arborist, normally ~/.claude/memory-arborist)",
    )
    _register_commands(
        parser.add_subparsers(dest="cmd", required=True), _add_common(_Parser)
    )
    return parser


def _resolve_paths(args):
    for flag in ("dir", "ops_root"):
        if getattr(args, flag) == "":
            raise sn.HygieneError("--%s must not be empty" % flag.replace("_", "-"))
    explicit_dir, explicit_ops = args.dir is not None, args.ops_root is not None
    args.dir = mc.expand(args.dir) if explicit_dir else mc.DEFAULT_MEMORY_DIR
    args.ops_root = mc.expand(args.ops_root) if explicit_ops else mc.DEFAULT_OPS_ROOT
    # a staged copy must never write into the real project's ops dir
    args.log_ok = (
        explicit_ops or not explicit_dir or mc.same_dir(args.dir, mc.DEFAULT_MEMORY_DIR)
    )
    return args


def main(argv=None):
    mc.safe_stdio()
    args = build_parser().parse_args(argv)
    try:
        return args.fn(_resolve_paths(args))
    except (sn.HygieneError, OSError, ValueError) as err:
        print("ERROR: %s" % mc.printable(err), file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
