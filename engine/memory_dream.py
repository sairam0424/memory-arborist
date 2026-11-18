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
import memory_dream_pin as dp  # noqa: E402
import memory_dream_stage as ds  # noqa: E402
import memory_registry as reg  # noqa: E402
import memory_snapshot as sn  # noqa: E402

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 64
RUN_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z")
SNAP_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z(?:-[0-9]{2})?")
MARKER = "promoted.json"
FAILED_MARKER = "promote-failed.json"  # a promote that failed before it replaced any note: it does not use the run up
DEFAULT_MAX_HUNKS = 10


class _Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("allow_abbrev", False)  # --app must not quietly mean --apply: the confirmation hook matches the full flag
        super().__init__(*args, **kwargs)

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
                for line in mc.read_text(path).lstrip("\ufeff").split("\n")  # an editor may have added a byte-order mark
                if line.strip()
            ]
            if not lines:
                print("WARNING %s is empty: no index line is protected" % name)
            return lines
    raise mc.HygieneError(
        "pinned.txt is missing: create it with `memory_dream.py pin --none` (protect no index line) or `pin --all` "
        "(protect every index line of the newest snapshot), or confirm pinned.txt.draft (or pass --pinned-draft-ok)"
    )


def cmd_pin(args):
    _memory, ops = _paths(args)
    path, count = dp.create(ops, args.all)
    print("PINNED %d index lines written to %s" % (count, path))
    if not count:
        print("WARNING no index line is protected; add lines to that file (copied exactly from MEMORY.md) to protect them")
    return EXIT_OK


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
        ops,
        snap,
        result.accepted,
        result.rejected,
        result.deferred,
        now=args.now,
        incomplete=args.incomplete,
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
        raise mc.HygieneError(ds.marker_message(os.path.join(run_dir, MARKER)))
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
                "%s changed since the snapshot this run was made from: take a fresh snapshot (step 1) and run the dream again (step 4)" % mc.printable(rel)
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
        try:
            if mc.sha256_file(live) != manifest["files"][rel]["sha256"]:
                raise _Partial("%s changed during the promotion" % mc.printable(rel), done, archive_dir)
            sn._swap_in(memory, tmp, live, archive_dir, rel)
            done.append(rel)
            if mc.sha256_file(live) != mc.sha256_file(os.path.join(run_dir, "memory", rel)):
                raise _Partial("read-back check failed for %s" % mc.printable(rel), done, archive_dir)
        except _Partial:
            raise
        except (mc.HygieneError, OSError) as err:  # a live note that vanished, an I/O error: whatever was replaced so far is recorded
            reason = err.strerror if isinstance(err, OSError) and err.strerror else err
            reason = str(reason).replace("nothing was replaced", "this note was not replaced")  # true of that one note, not of the whole promote
            raise _Partial("%s: %s" % (mc.printable(rel), mc.clean_line(reason, 160)), done, archive_dir)
    return done, archive_dir


class _Partial(mc.HygieneError):
    def __init__(self, message, done, archive_dir):
        super().__init__(message)
        self.done, self.archive_dir = list(done), archive_dir


def _write_marker(run_dir, data, name=MARKER):
    try:
        fd = os.open(
            os.path.join(run_dir, name),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
    except FileExistsError:
        raise mc.HygieneError(
            "another promote of this run finished first: check the notes and `memory_hygiene.py snapshots` before doing anything else"
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
    if not err.done and os.path.lexists(os.path.join(run_dir, MARKER)):
        # another promote of this run finished while this one failed: it is not "promotable again", and nothing more is recorded
        print("NOTHING was replaced by this attempt: another promote of this run finished first (%s)" % ds.marker_message(os.path.join(run_dir, MARKER)))
        return
    data = {
        "status": "partial" if err.done else "failed-before-replacing",
        "files": err.done,
        "error": str(err),
        "archive": err.archive_dir,
        "pre_snapshot": pre["id"],
    }
    undo = "`memory_hygiene.py restore --snapshot %s --file <note> --apply --force`" % pre["id"]
    if err.done:  # first: the way back is on screen whatever happens to the marker or the log
        print(
            "PARTIAL replaced %s; the old copies are in %s; pre-promote snapshot %s. To undo, restore each of those notes with %s"
            % (", ".join(map(mc.printable, err.done)), err.archive_dir, pre["id"], undo)
        )
    name = MARKER if err.done else os.path.basename(mc.unique_path(os.path.join(run_dir, FAILED_MARKER)))  # nothing replaced: leave the run promotable
    try:
        _write_marker(run_dir, data, name)
    except OSError as oserr:
        cause = mc.clean_line(oserr.strerror or type(oserr).__name__, 80)
        if not err.done:
            print("NOTHING was replaced (%s); the failure could not be recorded either (%s)" % (mc.clean_line(err, 120), cause))
            return
        raise mc.HygieneError(
            "the notes WERE replaced (see the PARTIAL line above; the failure was: %s) but the run's marker could not be written (%s): do not "
            "promote this run again; to undo, run %s for each replaced note" % (mc.clean_line(err, 100), cause, undo)
        )
    ds.log_or_note(ops, "promote", "PARTIAL run=%s replaced=%d" % (run_id, len(err.done)), ["Error: " + mc.clean_line(err, 200), "Archive: " + err.archive_dir])
    if not err.done:
        print("NOTHING was replaced (%s); the run can be promoted again once that is fixed" % mc.clean_line(err, 120))


def _record_promoted(ops, run_dir, run_id, done, pre, snap, archive_dir):
    print(  # first: the way back is on screen whatever happens to the marker or the log
        "PROMOTED %d notes; pre-promote snapshot %s; old copies in %s"
        % (len(done), pre["id"], archive_dir)
    )
    try:
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
    except OSError as err:
        raise mc.HygieneError(
            "the notes WERE replaced (see the PROMOTED line above) but the run's marker could not be written (%s): do not promote this "
            "run again; to undo, run `memory_hygiene.py restore --snapshot %s --file <note> --apply --force` for each note"
            % (mc.clean_line(err.strerror or type(err).__name__, 80), pre["id"])
        )
    ds.log_or_note(ops, "promote", "run=%s files=%d" % (run_id, len(done)), ["Pre-promote snapshot: " + pre["id"], "Notes: " + ", ".join(done)])


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
    warning = ds.incomplete_warning(run_dir)
    if warning:
        print(warning)
    if not args.apply:
        for rel in touched:
            print("would replace %s (the old copy is archived first)" % mc.printable(rel))
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
    for rel in touched:  # only now: a refused promote must not claim it is replacing anything
        print("replacing %s (the old copy is archived first)" % mc.printable(rel))
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
        help="data root for backups, logs, digests and baselines (default <Claude config dir>/memory-arborist, normally ~/.claude/memory-arborist)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    pin = sub.add_parser("pin", help="create pinned.txt (the index lines a dream run must not edit)")
    mode = pin.add_mutually_exclusive_group(required=True)
    mode.add_argument("--none", action="store_true", help="protect no index line")
    mode.add_argument("--all", action="store_true", help="protect every non-blank line of the newest snapshot's MEMORY.md")
    pin.set_defaults(fn=cmd_pin)
    ground = sub.add_parser("ground", help="check hunks and stage the survivors")
    ground.add_argument("--hunks", required=True)
    ground.add_argument("--snapshot", default="latest")
    ground.add_argument("--max-hunks", type=mc.positive_int, default=DEFAULT_MAX_HUNKS)
    ground.add_argument(
        "--pinned-draft-ok",
        action="store_true",
        help="accept pinned.txt.draft when pinned.txt is missing",
    )
    ground.add_argument("--now", type=float, default=None, help=argparse.SUPPRESS)
    ground.add_argument("--incomplete", type=int, default=0, help=argparse.SUPPRESS)
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
    mc.safe_stdio()
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
