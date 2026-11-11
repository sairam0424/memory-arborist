#!/usr/bin/env python3
"""Ground, stage and promote dream hunks. Standard library only; run as python3 -I.

Commands: ground (check a hunks file against a snapshot and the evidence digests, stage the survivors in a
write-once run directory outside the memory directory), promote (dry run by default; with --apply and the digest
the dry run printed it takes a fresh snapshot, archives each touched live note and installs the staged copy).
Promote trusts nothing in the run directory: it re-grounds the hunks against the current digests and pinned list,
rebuilds snapshot-plus-hunks and compares bytes, refuses links, and binds the person's yes to one digest.
Nothing is deleted. Exit codes: 0 ok; 1 failure, refusal or failed checks; 64 usage.
"""

import argparse
import json
import os
import re
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memory_common as mc  # noqa: E402
import memory_dream_ground as dg  # noqa: E402
import memory_dream_stage as ds  # noqa: E402
import memory_registry as reg  # noqa: E402
import memory_snapshot as sn  # noqa: E402

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 64
RUN_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z")
SNAP_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z(?:-[0-9]{2})?")
MARKER = "promoted.json"
DEFAULT_MAX_HUNKS = 10


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, "%s: error: %s\n" % (self.prog, message))


def _paths(args):
    memory = mc.expand(args.dir) if args.dir else mc.DEFAULT_MEMORY_DIR
    root = mc.expand(args.ops_root) if args.ops_root else mc.DEFAULT_OPS_ROOT
    ops = mc.ops_dir_for(memory, root)
    sn.ensure_ops_outside(memory, ops)
    return memory, ops


def _load_hunks(path):
    try:
        with open(mc.expand(path), encoding="utf-8") as fh:
            hunks = json.load(fh)
    except (OSError, ValueError, RecursionError):
        raise mc.HygieneError("the hunks file is missing or is not valid JSON")
    if not isinstance(hunks, list):
        raise mc.HygieneError("the hunks file must hold a JSON list of hunks")
    return hunks


def _load_pinned(ops, allow_draft):
    for name in ("pinned.txt", "pinned.txt.draft") if allow_draft else ("pinned.txt",):
        path = os.path.join(ops, name)
        if os.path.isfile(path):
            lines = [
                line.rstrip("\n")
                for line in mc.read_text(path).split("\n")
                if line.strip()
            ]
            if not lines:
                print("WARNING %s is empty: no index line is protected" % name)
            return lines
    raise mc.HygieneError(
        "pinned.txt is missing: confirm pinned.txt.draft first (or pass --pinned-draft-ok)"
    )


def cmd_ground(args):
    memory, ops = _paths(args)
    hunks = _load_hunks(args.hunks)
    snap = sn.resolve_snapshot(ops, args.snapshot)
    ok, _count, problems = sn.verify_snapshot(ops, snap)
    if not ok:
        raise mc.HygieneError(
            "snapshot %s failed verification (%d problems)" % (snap, len(problems))
        )
    result = dg.ground(
        hunks,
        os.path.join(ops, "snapshots", snap, "memory"),
        sn.load_manifest(ops, snap),
        os.path.join(ops, "evidence"),
        _load_pinned(ops, args.pinned_draft_ok),
        args.max_hunks,
    )
    staged = ds.stage_run(
        ops, snap, result.accepted, result.rejected, result.deferred, now=args.now
    )
    print(
        "GROUND accepted=%d rejected=%d deferred=%d"
        % (len(result.accepted), len(result.rejected), len(result.deferred))
    )
    for label, why in result.rejected:
        print("REJECTED %s: %s" % (mc.printable(label), mc.printable(why)))
    print(
        "STAGE ok=%s problems=%d run=%s"
        % (staged.ok, len(staged.problems), os.path.basename(staged.run_dir))
    )
    for problem in staged.problems:
        print("PROBLEM " + problem)
    print("REPORT " + os.path.join(staged.run_dir, "report.md"))
    mc.append_log(
        ops,
        "dream",
        "ground run=%s accepted=%d rejected=%d"
        % (
            os.path.basename(staged.run_dir),
            len(result.accepted),
            len(result.rejected),
        ),
        ["Snapshot: " + snap, "Stage ok: %s" % staged.ok],
    )
    return EXIT_OK if staged.ok else EXIT_FAIL


# ------------------------------------------------------------------ promote


def _snapshot_of(ops, run_dir):
    match = re.search(
        r"^Snapshot: (\S+)$", mc.read_text(os.path.join(run_dir, "report.md")), re.M
    )
    snap = match.group(1) if match else ""
    if not SNAP_ID.fullmatch(snap) or snap not in sn.list_snapshots(ops):
        raise mc.HygieneError("the run names a snapshot that does not exist")
    if not sn.verify_snapshot(ops, snap)[0]:
        raise mc.HygieneError("the snapshot behind this run failed verification")
    return snap


def _regrounded(ops, snap, accepted):
    """The hunks that still pass grounding today; any difference from the stored list is a refusal."""
    snapshot_dir = os.path.join(ops, "snapshots", snap, "memory")
    plain = [
        {k: v for k, v in h.items() if k not in ("start", "score")}
        for h in accepted
        if isinstance(h, dict)
    ]
    result = dg.ground(
        plain,
        snapshot_dir,
        sn.load_manifest(ops, snap),
        os.path.join(ops, "evidence"),
        _load_pinned(ops, True),
        max(len(plain), 1),
    )

    def key(hunks):
        return sorted(
            (h["id"], h["file"], h["start"], h["old"], h["new"]) for h in hunks
        )

    if (
        result.rejected
        or result.deferred
        or len(plain) != len(accepted)
        or key(result.accepted) != key(accepted)
    ):
        raise mc.HygieneError(
            "the stored hunks no longer pass grounding as they are; run the dream again"
        )
    return result.accepted


def _promotion_plan(memory, ops, run_dir):
    """(snapshot id, accepted hunks, touched files, digest); refuses unless the run is intact and the live notes are unchanged."""
    if os.path.lexists(os.path.join(run_dir, MARKER)):
        raise mc.HygieneError("this run was already promoted")
    walk = mc.walk_files(run_dir)
    if walk.symlinks or walk.special or walk.errors:
        raise mc.HygieneError("the run directory holds links or special files")
    snap = _snapshot_of(ops, run_dir)
    manifest = sn.load_manifest(ops, snap)
    if not sn._source_matches(manifest.get("source"), memory):
        raise mc.HygieneError(
            "the snapshot behind this run was taken from a different directory"
        )
    try:
        stored = json.loads(mc.read_text(os.path.join(run_dir, "hunks.accepted.json")))
    except (ValueError, RecursionError):
        raise mc.HygieneError("hunks.accepted.json is not valid JSON")
    accepted = _regrounded(ops, snap, stored)
    snapshot_dir, staged_dir = (
        os.path.join(ops, "snapshots", snap, "memory"),
        os.path.join(run_dir, "memory"),
    )
    problems = ds.run_checks(snapshot_dir, staged_dir, accepted)
    if problems:
        raise mc.HygieneError(
            "the staged copy fails its checks: " + "; ".join(problems)
        )
    touched = sorted({h["file"] for h in accepted})
    for rel in touched:
        live = os.path.join(memory, rel)
        if (
            not os.path.isfile(live)
            or os.path.islink(live)
            or mc.sha256_file(live) != manifest["files"][rel]["sha256"]
        ):
            raise mc.HygieneError(
                "%s changed since the snapshot; run the dream again" % mc.printable(rel)
            )
    return snap, accepted, touched, ds.run_digest(staged_dir, accepted)


def _prepare_install(memory, ops, run_dir, touched, manifest):
    """Copies of the staged notes under the run directory (never inside the memory directory), checked before anything moves."""
    install = os.path.join(run_dir, "install")
    mc.makedirs_private(install)
    prepared = {}
    for rel in touched:
        reason = sn._unsafe_name(memory, rel)
        if reason:
            raise mc.HygieneError(
                "%s cannot be replaced: %s" % (mc.printable(rel), reason)
            )
        staged = os.path.join(run_dir, "memory", rel)
        fd, tmp = tempfile.mkstemp(dir=install, prefix=".install-", suffix=".part")
        os.close(fd)
        shutil.copyfile(staged, tmp)
        os.chmod(tmp, os.stat(os.path.join(memory, rel)).st_mode & 0o777)
        if mc.sha256_file(tmp) != mc.sha256_file(staged):
            raise mc.HygieneError(
                "the copy of %s failed its checksum" % mc.printable(rel)
            )
        prepared[rel] = tmp
    return prepared


def _install(memory, ops, run_dir, prepared, manifest, now):
    archive_dir = os.path.join(ops, "archive", mc.utc_stamp(now))
    done = []
    for rel, tmp in prepared.items():
        live = os.path.join(memory, rel)
        if mc.sha256_file(live) != manifest["files"][rel]["sha256"]:
            raise _Partial(
                "%s changed during the promotion" % mc.printable(rel), done, archive_dir
            )
        try:
            sn._swap_in(memory, tmp, live, archive_dir, rel)
        except mc.HygieneError as err:
            raise _Partial(str(err), done, archive_dir)
        done.append(rel)
        if mc.sha256_file(live) != mc.sha256_file(os.path.join(run_dir, "memory", rel)):
            raise _Partial(
                "read-back check failed for %s" % mc.printable(rel), done, archive_dir
            )
    return done, archive_dir


class _Partial(mc.HygieneError):
    def __init__(self, message, done, archive_dir):
        super().__init__(message)
        self.done, self.archive_dir = list(done), archive_dir


def _write_marker(run_dir, data):
    fd = os.open(
        os.path.join(run_dir, MARKER),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def _run_dir_of(ops, run_id):
    if not RUN_ID.fullmatch(run_id):
        raise mc.HygieneError("not a dream run id: %s" % mc.printable(run_id))
    run_dir = os.path.join(ops, "dream", run_id)
    if not os.path.isdir(run_dir) or os.path.islink(run_dir):
        raise mc.HygieneError("no such dream run: %s" % run_id)
    return run_dir


def _record_partial(ops, run_dir, run_id, err, pre):
    _write_marker(
        run_dir,
        {
            "status": "partial",
            "files": err.done,
            "error": str(err),
            "archive": err.archive_dir,
            "pre_snapshot": pre["id"],
        },
    )
    mc.append_log(
        ops,
        "promote",
        "PARTIAL run=%s replaced=%d" % (run_id, len(err.done)),
        ["Error: " + mc.clean_line(err, 200), "Archive: " + err.archive_dir],
    )
    print(
        "PARTIAL replaced %s; the old copies are in %s"
        % (", ".join(map(mc.printable, err.done)) or "nothing", err.archive_dir)
    )


def _record_promoted(ops, run_dir, run_id, done, pre, snap, archive_dir):
    _write_marker(
        run_dir,
        {
            "status": "promoted",
            "files": done,
            "pre_snapshot": pre["id"],
            "from_snapshot": snap,
            "archive": archive_dir,
        },
    )
    mc.append_log(
        ops,
        "promote",
        "run=%s files=%d" % (run_id, len(done)),
        ["Pre-promote snapshot: " + pre["id"], "Notes: " + ", ".join(done)],
    )
    print(
        "PROMOTED %d notes; pre-promote snapshot %s; old copies in %s"
        % (len(done), pre["id"], archive_dir)
    )


def _apply_promotion(args, memory, ops, run_dir, snap, touched):
    manifest = sn.load_manifest(ops, snap)
    prepared = _prepare_install(memory, ops, run_dir, touched, manifest)
    pre = sn.take_snapshot(memory, ops, now=args.now)
    try:
        with reg.ops_lock(ops):
            done, archive_dir = _install(
                memory, ops, run_dir, prepared, manifest, args.now
            )
    except _Partial as err:
        _record_partial(ops, run_dir, args.run, err, pre)
        raise
    _record_promoted(ops, run_dir, args.run, done, pre, snap, archive_dir)


def cmd_promote(args):
    memory, ops = _paths(args)
    run_dir = _run_dir_of(ops, args.run)
    snap, accepted, touched, digest = _promotion_plan(memory, ops, run_dir)
    for rel in touched:
        print(
            "%s %s (the old copy is archived first)"
            % ("replacing" if args.apply else "would replace", mc.printable(rel))
        )
    print("DIGEST " + digest)
    if not args.apply:
        print(
            "DRY RUN: nothing changed; add --apply --expect %s to promote %d hunks in %d notes"
            % (digest, len(accepted), len(touched))
        )
        return EXIT_OK
    if args.expect != digest:
        raise mc.HygieneError(
            "--expect must equal the DIGEST a dry run printed for exactly this content"
        )
    _apply_promotion(args, memory, ops, run_dir, snap, touched)
    return EXIT_OK


def build_parser():
    parser = _Parser(prog="memory_dream.py", description=__doc__.split("\n")[0])
    parser.add_argument(
        "--dir", default=None, help="memory directory (default: this project's)"
    )
    parser.add_argument(
        "--ops-root",
        default=None,
        help="data root for backups, logs, digests and baselines (default <Claude config dir>/memory-shepherd, normally ~/.claude/memory-shepherd)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    ground = sub.add_parser("ground", help="check hunks and stage the survivors")
    ground.add_argument("--hunks", required=True)
    ground.add_argument("--snapshot", default="latest")
    ground.add_argument("--max-hunks", type=int, default=DEFAULT_MAX_HUNKS)
    ground.add_argument(
        "--pinned-draft-ok",
        action="store_true",
        help="accept pinned.txt.draft when pinned.txt is missing",
    )
    ground.add_argument("--now", type=float, default=None, help=argparse.SUPPRESS)
    ground.set_defaults(fn=cmd_ground)
    promote = sub.add_parser(
        "promote", help="install a staged run (dry run unless --apply)"
    )
    promote.add_argument("run")
    promote.add_argument("--apply", action="store_true")
    promote.add_argument(
        "--expect",
        default=None,
        help="the DIGEST a dry run printed; required with --apply",
    )
    promote.add_argument("--now", type=float, default=None, help=argparse.SUPPRESS)
    promote.set_defaults(fn=cmd_promote)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except (
        mc.HygieneError,
        OSError,
        ValueError,
        KeyError,
        AttributeError,
        TypeError,
        RecursionError,
    ) as err:
        print("ERROR: %s" % mc.printable(err), file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
