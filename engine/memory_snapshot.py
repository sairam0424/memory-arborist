"""Snapshot, verify, restore and prune-plan for the memory directory. Standard library only.

Never deletes anything. os.replace is the only move primitive and is called from exactly three
places (_write_json_private, _archive_file, _swap_in); a test pins that list. A live file is always
archived under a unique name before it is replaced, so a restore cannot destroy content. The window
between "is something there?" and the final os.replace is a few microseconds, not zero: a note
written by a live session inside that window is lost (documented, not hidden).
"""

import contextlib
import json
import os
import shlex
import shutil
import sys
import tempfile
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memory_common as mc  # noqa: E402
import memory_scan as ms  # noqa: E402
from memory_compare import CompareResult, compare_dirs  # noqa: E402,F401  (re-exported for callers and tests)
from memory_registry import (  # noqa: E402,F401  (re-exported for callers and tests)
    _manifest_path,
    _records,
    broken_snapshots,
    list_snapshots,
    load_manifest,
    ops_lock,
    resolve_snapshot,
)

HygieneError = mc.HygieneError
MIN_FREE_BYTES = 2 * 1024**3
KEEP_SNAPSHOTS = 5
MAX_SAME_SECOND = 99
PRIVATE_FILE_MODE = 0o600


# ------------------------------------------------------------------ snapshot


def _tree_hash(meta):
    joined = "\n".join("%s\0%s" % (rel, meta[rel]["sha256"]) for rel in sorted(meta))
    return mc.sha256_bytes(joined.encode("utf-8"))


def _hash_tree(memory_dir, files):
    meta = {}
    for rel in files:
        path = os.path.join(memory_dir, rel)
        try:
            info = os.stat(path)
            meta[rel] = {
                "sha256": mc.sha256_file(path),
                "size": info.st_size,
                "mtime_ns": info.st_mtime_ns,
                "mode": info.st_mode & 0o777,
            }
        except OSError as err:
            raise HygieneError(
                "cannot read %s (%s); refusing a partial snapshot"
                % (mc.printable(rel), type(err).__name__)
            )
    return meta


def _write_json_private(path, data):
    tmp = path + ".part"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _new_snapshot_id(ops, now):
    stamp = mc.utc_stamp(now)
    for n in range(MAX_SAME_SECOND + 1):
        candidate = stamp if n == 0 else "%s-%02d" % (stamp, n)
        if not os.path.exists(os.path.join(ops, "snapshots", candidate)):
            return candidate
    raise HygieneError("too many snapshots within one second")


def _copy_tree(memory_dir, copy_root, meta):
    """Plain content copy (never cp -p, which would carry the uchg flag), then set mode and mtime. Returns verified count."""
    for rel, info in meta.items():
        dest = os.path.join(copy_root, rel)
        mc.makedirs_private(os.path.dirname(dest))
        shutil.copyfile(os.path.join(memory_dir, rel), dest)
        os.chmod(dest, PRIVATE_FILE_MODE)
        os.utime(dest, ns=(info["mtime_ns"], info["mtime_ns"]))
    return sum(
        1
        for rel, info in meta.items()
        if mc.sha256_file(os.path.join(copy_root, rel)) == info["sha256"]
    )


def ensure_ops_outside(memory_dir, ops):
    if mc.is_within(memory_dir, ops):
        raise HygieneError("the ops dir must not live inside the memory dir")


def _check_target(memory_dir, ops):
    if not os.path.isdir(memory_dir):
        raise HygieneError("memory dir not found: %s" % memory_dir)
    ensure_ops_outside(memory_dir, ops)
    walk = mc.walk_files(memory_dir)
    if walk.errors:
        raise HygieneError(
            "unreadable directories, refusing a partial snapshot: %s"
            % ", ".join(mc.printable(rel) for rel, _msg in walk.errors)
        )
    return walk


def _source_matches(source, memory_dir):
    """True only for a non-empty string that names the same directory; a manifest may hold anything else."""
    return isinstance(source, str) and bool(source) and mc.same_dir(source, memory_dir)


def _previous_state(ops, memory_dir, tree):
    """(latest id or None, whether it is intact and identical, problems found)."""
    ids = list_snapshots(ops)
    if not ids:
        return None, False, []
    manifest = load_manifest(ops, ids[-1])
    if not _source_matches(manifest.get("source"), memory_dir):
        raise HygieneError(
            "this ops dir already holds snapshots of a different memory dir (%s)"
            % mc.printable(manifest.get("source"))
        )
    if manifest.get("tree_sha256") != tree or _tree_hash(manifest["files"]) != tree:
        return ids[-1], False, []
    ok, _count, problems = verify_snapshot(ops, ids[-1])
    return ids[-1], ok, problems


def _next_seq(ops):
    return (
        max(
            [m["seq"] for _d, m in _records(ops) if isinstance(m.get("seq"), int)] + [0]
        )
        + 1
    )


def _write_snapshot(memory_dir, ops, walk, meta, tree, now):
    """Copy, verify, scan and write the manifest last. Returns (snapshot id, verified count, (block, warn), unscanned count)."""
    snap_id = _new_snapshot_id(ops, now)
    copy_root = os.path.join(ops, "snapshots", snap_id, "memory")
    mc.makedirs_private(copy_root)
    try:
        ok_count = _copy_tree(memory_dir, copy_root, meta)
    except OSError as err:
        raise HygieneError("copy failed (%s); no manifest written" % type(err).__name__)
    if ok_count != len(meta):
        raise HygieneError(
            "source changed or copy corrupted (%d of %d verified); no manifest written, retry"
            % (ok_count, len(meta))
        )
    scanned = ms.scan_tree(copy_root)
    scan, unscanned = ms.summarize(scanned.hits), len(scanned.skipped)
    manifest = {
        "version": 1,
        "id": snap_id,
        "seq": _next_seq(ops),
        "created_utc": mc.utc_stamp(now),
        "source": os.path.realpath(memory_dir),
        "files": meta,
        "tree_sha256": tree,
        "scan": dict({"block": scan[0], "warn": scan[1]}, **({"unscanned": unscanned} if unscanned else {})),
        "skipped_symlinks": list(walk.symlinks),
        "skipped_special": list(walk.special),
    }
    _write_json_private(_manifest_path(ops, snap_id), manifest)
    return snap_id, ok_count, scan, unscanned


def _result(status, snap_id, meta, ok_count, scan, skipped, problems, unscanned=0):
    """The snapshot result; "unscanned" (entries the secret scan could not read or was too big for) is present only when above zero."""
    return {
        **({"unscanned": unscanned} if unscanned else {}),
        "status": status,
        "id": snap_id,
        "files": len(meta),
        "ok_count": ok_count,
        "scan": scan,
        "skipped": skipped,
        "previous_problems": problems,
    }


def take_snapshot(memory_dir, ops, now=None, free_fn=None, min_free=MIN_FREE_BYTES):
    walk = _check_target(memory_dir, ops)
    with ops_lock(ops):
        meta = _hash_tree(memory_dir, walk.files)
        tree = _tree_hash(meta)
        latest, intact, problems = _previous_state(ops, memory_dir, tree)
        if intact:
            return _result("noop", latest, meta, len(meta), (0, 0), [], [])
        free = (free_fn or mc.free_bytes)(ops)
        if free < min_free:
            raise HygieneError(
                "refusing: only %d bytes free, need %d" % (free, min_free)
            )
        snap_id, ok_count, scan, unscanned = _write_snapshot(
            memory_dir, ops, walk, meta, tree, now
        )
        return _result(
            "ok",
            snap_id,
            meta,
            ok_count,
            scan,
            sorted(walk.symlinks + walk.special),
            problems,
            unscanned,
        )


def verify_snapshot(ops, snap_id):
    manifest = load_manifest(ops, snap_id)
    root = os.path.join(ops, "snapshots", snap_id, "memory")
    problems = []
    for rel, info in sorted(manifest["files"].items()):
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            problems.append("missing: %s" % mc.printable(rel))
        elif mc.sha256_file(path) != info["sha256"]:
            problems.append("sha256 mismatch: %s" % mc.printable(rel))
    problems += [
        "unexpected file: %s" % mc.printable(rel)
        for rel in (mc.list_files(root) if os.path.isdir(root) else [])
        if rel not in manifest["files"]
    ]
    return (not problems, len(manifest["files"]), problems)


# ------------------------------------------------------------------ restore


def _unsafe_name(memory_dir, clean):
    if clean in ("", ".") or os.path.isabs(clean) or ".." in clean.split(os.sep):
        return "unsafe file name"
    current = memory_dir
    for part in clean.split(os.sep):
        current = os.path.join(current, part)
        if os.path.islink(current):
            return "path crosses a symlink"
    if not mc.is_within(memory_dir, os.path.dirname(os.path.join(memory_dir, clean))):
        return "path leaves the memory directory"
    return None


def _archive_file(live, archive_dir, clean):
    dest = mc.unique_path(os.path.join(archive_dir, clean))
    try:
        mc.makedirs_private(os.path.dirname(dest))
        os.replace(live, dest)
    except OSError as err:  # the folder could not be made, or the note could not be moved: either way nothing was replaced
        raise HygieneError(
            "could not archive %s: %s" % (mc.printable(clean), err.strerror or type(err).__name__)
        )
    return dest


def _stage(copy, live, entry, mode):
    """Copy the snapshot bytes next to the live file under an exclusive, unpredictable name and verify them."""
    parent = os.path.dirname(live)
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".restore-", suffix=".part")
    os.close(fd)
    try:
        shutil.copyfile(copy, tmp)
        os.chmod(tmp, mode)
        os.utime(tmp, ns=(entry["mtime_ns"], entry["mtime_ns"]))
        if mc.sha256_file(tmp) != entry["sha256"]:
            raise HygieneError("the staged copy failed its sha256 check")
    except (OSError, HygieneError) as err:
        raise HygieneError(
            "could not stage the restore (%s); the live file was not touched; leftover temp file: %s"
            % (getattr(err, "strerror", None) or err, tmp)
        )
    return tmp


def _swap_in(memory_dir, tmp, live, archive_dir, clean):
    """Re-check the path, archive whatever is there now (it may have appeared during staging), then replace."""
    reason = _unsafe_name(memory_dir, clean)
    if reason:
        raise HygieneError(
            "the path changed during the restore (%s); nothing was replaced; staged copy: %s"
            % (reason, tmp)
        )
    try:
        archived = (
            _archive_file(live, archive_dir, clean) if os.path.lexists(live) else None
        )
    except HygieneError as err:
        raise HygieneError(
            "%s; the live note is untouched, and the staged copy %s is safe to remove by hand (this tool never deletes)"
            % (err, mc.printable(tmp))
        )
    try:
        os.replace(tmp, live)
    except OSError as err:
        if archived:
            os.replace(archived, live)  # put the original back
        raise HygieneError(
            "could not place the restored file (%s); the original was put back"
            % err.strerror
        )
    return ["archived live copy -> %s" % mc.printable(archived)] if archived else []


def _restore_absent(live, archive_dir, clean, apply, force):
    if not os.path.isfile(live):
        return 1, [
            "%s is in neither the snapshot nor the live directory" % mc.printable(clean)
        ]
    if not apply:
        return 0, [
            "would move %s (absent from the snapshot) into the archive"
            % mc.printable(clean)
        ]
    if not force:
        return 1, [
            "refused: %s is absent from the snapshot; pass --force to archive it"
            % mc.printable(clean)
        ]
    return 0, [
        "archived %s -> %s"
        % (mc.printable(clean), mc.printable(_archive_file(live, archive_dir, clean)))
    ]


def _canonical(name):
    return unicodedata.normalize("NFC", name).casefold()


def _refusal(memory_dir, manifest, snap_id, clean):
    """Why this restore must not proceed, or None."""
    source = manifest.get("source")
    if not _source_matches(source, memory_dir):
        return "snapshot %s was taken from a different directory (%s)" % (
            snap_id,
            mc.printable(source),
        )
    stored = manifest["files"]
    twin = next(
        (
            key
            for key in stored
            if key != clean and _canonical(key) == _canonical(clean)
        ),
        None,
    )
    if clean not in stored and twin:
        return "the snapshot stores this note as %s, not %s" % (
            mc.printable(twin),
            mc.printable(clean),
        )
    return None


def _apply_restore(memory_dir, ops, snap_id, clean, entry, exists, archive_dir):
    live = os.path.join(memory_dir, clean)
    copy = os.path.join(ops, "snapshots", snap_id, "memory", clean)
    with ops_lock(ops):
        mode = entry.get(
            "mode", os.stat(live).st_mode & 0o777 if exists else PRIVATE_FILE_MODE
        )
        tmp = _stage(copy, live, entry, mode)
        messages = _swap_in(memory_dir, tmp, live, archive_dir, clean)
    if mc.sha256_file(live) != entry["sha256"]:
        raise HygieneError(
            "restored file failed read-back verification: %s" % mc.printable(clean)
        )
    return 0, messages + [
        "restored %s from snapshot %s" % (mc.printable(clean), snap_id)
    ]


def restore_file(memory_dir, ops, snap_id, name, apply=False, force=False, now=None):
    clean = os.path.normpath(name)
    reason = _unsafe_name(memory_dir, clean)
    if reason:
        return 1, ["refused: %s (%s)" % (reason, mc.printable(name))]
    manifest = load_manifest(ops, snap_id)
    refusal = _refusal(memory_dir, manifest, snap_id, clean)
    if refusal:
        return 1, ["refused: " + refusal]
    live, archive_dir = (
        os.path.join(memory_dir, clean),
        os.path.join(ops, "archive", mc.utc_stamp(now)),
    )
    entry, exists = (
        manifest["files"].get(clean),
        os.path.isfile(os.path.join(memory_dir, clean)),
    )
    if entry is None:
        with ops_lock(ops) if apply else contextlib.nullcontext():
            return _restore_absent(live, archive_dir, clean, apply, force)
    if os.path.isdir(live):
        return 1, ["refused: a directory sits where the note belongs (%s); move it away yourself first" % mc.printable(clean)]
    copy = os.path.join(ops, "snapshots", snap_id, "memory", clean)
    if not os.path.isfile(copy) or mc.sha256_file(copy) != entry["sha256"]:
        return 1, [
            "refused: snapshot copy of %s failed its sha256 check" % mc.printable(clean)
        ]
    if exists and mc.sha256_file(live) == entry["sha256"]:
        return 0, ["already identical: %s" % mc.printable(clean)]
    if not apply:
        return 0, [
            "would restore %s from snapshot %s (live file %s)"
            % (mc.printable(clean), snap_id, "differs" if exists else "is missing")
        ]
    if exists and not force:
        return 1, [
            "refused: live %s differs (a live session may have written it); pass --force, the live copy is archived first"
            % mc.printable(clean)
        ]
    return _apply_restore(memory_dir, ops, snap_id, clean, entry, exists, archive_dir)


# ------------------------------------------------------------------ prune


def _is_intact(ops, snap_id):
    try:
        return verify_snapshot(ops, snap_id)[0]
    except (HygieneError, OSError):
        return False


def prune_plan(ops, keep=KEEP_SNAPSHOTS):
    """Commands to remove old snapshots; the newest `keep` INTACT ones are retained. Printed only, never executed."""
    if keep < 1:
        raise HygieneError("--keep must be at least 1")
    base = os.path.abspath(ops)
    ids = list_snapshots(base)
    retained = set([i for i in ids if _is_intact(base, i)][-keep:])
    lines = []
    for snap_id in (i for i in ids if i not in retained):
        path = os.path.join(base, "snapshots", snap_id)
        lines.append(
            "rm -r %s" % shlex.quote(path)
            if mc.printable(path) == path
            else "# skipped, unusual name: %s" % mc.printable(path)
        )
    lines += [
        "# incomplete snapshot (no valid manifest), review then remove: %s"
        % mc.printable(os.path.join(base, "snapshots", i))
        for i in broken_snapshots(base)
    ]
    return lines
