"""Registry of snapshots: lock, manifest validation, ordering and lookup. Standard library only."""

import contextlib
import fcntl
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memory_common as mc  # noqa: E402

HygieneError = mc.HygieneError
PRIVATE_FILE_MODE = 0o600
SHA256_HEX = re.compile(
    r"[0-9a-f]{64}"
)  # used with fullmatch: '$' would also accept a trailing newline


@contextlib.contextmanager
def ops_lock(ops):
    """Advisory lock on a persistent private file; released when the handle closes, so a crash never leaves it held."""
    mc.makedirs_private(ops)
    handle = os.fdopen(
        os.open(
            os.path.join(ops, ".lock"), os.O_WRONLY | os.O_CREAT, PRIVATE_FILE_MODE
        ),
        "a",
    )
    try:
        os.chmod(handle.fileno(), PRIVATE_FILE_MODE)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise HygieneError("another memory-arborist run holds the lock")
        yield
    finally:
        handle.close()


# ------------------------------------------------------------------ registry of snapshots


def _snap_key(snap_id):
    stamp, _sep, suffix = snap_id.partition("-")
    return stamp, int(suffix) if suffix.isdigit() else 0


def _manifest_path(ops, snap_id):
    return os.path.join(ops, "snapshots", snap_id, "manifest.json")


def _plain_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_entry(entry):
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("sha256"), str)
        and bool(SHA256_HEX.fullmatch(entry["sha256"]))
        and _plain_int(entry.get("mtime_ns"))
        and _plain_int(entry.get("size"))
    )


def _read_manifest(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    files = data.get("files") if isinstance(data, dict) else None
    return (
        data
        if isinstance(files, dict) and all(_valid_entry(e) for e in files.values())
        else None
    )


def _snapshot_dirs(ops):
    base = os.path.join(ops, "snapshots")
    return (
        [d for d in sorted(os.listdir(base)) if os.path.isdir(os.path.join(base, d))]
        if os.path.isdir(base)
        else []
    )


def _records(ops):
    return [
        (d, m)
        for d in _snapshot_dirs(ops)
        for m in [_read_manifest(_manifest_path(ops, d))]
        if m
    ]


def _order(record):
    snap_id, manifest = record
    seq = manifest.get("seq")
    return (seq if isinstance(seq, int) else 0), _snap_key(snap_id)


def list_snapshots(ops):
    """Valid snapshots in creation order (a counter, so a clock that steps backwards cannot reorder them)."""
    return [snap_id for snap_id, _manifest in sorted(_records(ops), key=_order)]


def broken_snapshots(ops):
    valid = {snap_id for snap_id, _manifest in _records(ops)}
    return [d for d in _snapshot_dirs(ops) if d not in valid]


def resolve_snapshot(ops, snap_id):
    ids = list_snapshots(ops)
    if snap_id in (None, "latest"):
        if not ids:
            raise HygieneError("no snapshots yet; run snapshot first")
        return ids[-1]
    if snap_id not in ids:
        raise HygieneError("no such snapshot: %s" % snap_id)
    return snap_id


def load_manifest(ops, snap_id):
    data = _read_manifest(_manifest_path(ops, snap_id))
    if data is None:
        raise HygieneError(
            "snapshot %s has an unreadable or malformed manifest" % snap_id
        )
    return data
