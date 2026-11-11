"""Shared helpers for the memory-shepherd scripts. Standard library only; run through python3 -I.

Safety contract: nothing in memory_*.py deletes files. The only move primitive is os.replace, and
tests pin exactly which functions may call it (atomic writes, archiving, restore).
"""

import hashlib
import os
import re
import shutil
import time
from collections import namedtuple

import memory_locate as _locate

# Defaults are computed when this module is imported, from the environment and the working directory; see memory_locate.py.
DEFAULT_MEMORY_DIR = _locate.default_memory_dir()
DEFAULT_OPS_ROOT = _locate.default_ops_root()
VALID_OPS = frozenset(
    {
        "snapshot",
        "lint",
        "scan",
        "evidence",
        "recall",
        "dream",
        "promote",
        "restore",
        "trim",
        "ablate",
        "decision",
        "compare",
    }
)
READ_CHUNK_BYTES = 1 << 20
LOG_SUBJECT_LIMIT, LOG_BULLET_LIMIT = 200, 400
CONTROL_CHARS = re.compile(
    "[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]"
)


class HygieneError(Exception):
    """A refusal or failure the user should read; the CLI prints it and exits 1."""


WalkResult = namedtuple("WalkResult", "files symlinks errors special")
Finding = namedtuple("Finding", "severity code path line message")


def utf16_len(text):
    """Length in UTF-16 code units: this tool's own reading of the \"25KB\" index limit, whose unit the docs do not state."""
    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(READ_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_stamp(now=None):
    return time.strftime(
        "%Y%m%dT%H%M%SZ", time.gmtime(time.time() if now is None else now)
    )


def local_date(now=None):
    return time.strftime(
        "%Y-%m-%d", time.localtime(time.time() if now is None else now)
    )


def slug_for(memory_dir):
    """The project directory name that owns a memory dir (<projects>/<slug>/memory)."""
    return os.path.basename(os.path.dirname(os.path.abspath(memory_dir)))


def ops_dir_for(memory_dir, ops_root=DEFAULT_OPS_ROOT):
    return os.path.join(ops_root, slug_for(memory_dir))


def is_within(base, path):
    """True when path (after resolving symlinks) is base or lies inside it."""
    real_base, real_path = os.path.realpath(base), os.path.realpath(path)
    return real_path == real_base or real_path.startswith(real_base + os.sep)


def same_dir(first, second):
    return os.path.realpath(first) == os.path.realpath(second)


def expand(path):
    """~ and relative paths resolved to an absolute path."""
    return os.path.abspath(os.path.expanduser(path))


def printable(text):
    """Text safe to print: control, bidi and zero-width characters become '?'."""
    return CONTROL_CHARS.sub("?", str(text))


def makedirs_private(path):
    """Create path and any missing parents with mode 0700; tighten the leaf if it already exists."""
    path = os.path.abspath(path)
    missing, cur = [], path
    while not os.path.isdir(cur):
        missing.append(cur)
        cur = os.path.dirname(cur)
    for directory in reversed(missing):
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            continue
        os.chmod(directory, 0o700)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass  # not ours to tighten


def walk_files(root):
    """Regular files, symlinks (never followed) and directory errors under root, as relative paths."""
    if not os.path.isdir(root):
        raise FileNotFoundError(root)
    files, links, errors, special = [], [], [], []

    def on_error(err):
        errors.append(
            (
                os.path.relpath(err.filename, root) if err.filename else "?",
                err.strerror or str(err),
            )
        )

    for base, dirs, names in os.walk(root, followlinks=False, onerror=on_error):
        file_names = set(names)
        for name in dirs + names:
            full = os.path.join(base, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if os.path.islink(full):
                links.append(rel)
            elif name in file_names:
                (files if os.path.isfile(full) else special).append(rel)
    return WalkResult(sorted(files), sorted(links), sorted(errors), sorted(special))


def list_files(root):
    return list(walk_files(root).files)


def list_md(root):
    return [p for p in list_files(root) if p.endswith(".md")]


def free_bytes(path):
    probe = os.path.abspath(path)
    while not os.path.exists(probe):
        probe = os.path.dirname(probe)
    return shutil.disk_usage(probe).free


def read_text(path):
    with open(path, "rb") as fh:
        return fh.read().decode("utf-8", errors="replace")


def unique_path(dest):
    """dest, or dest.1, dest.2 ... so that an existing file is never overwritten."""
    candidate, counter = dest, 0
    while os.path.lexists(candidate):
        counter += 1
        candidate = "%s.%d" % (dest, counter)
    return candidate


def clean_line(text, limit):
    flat = " ".join(CONTROL_CHARS.sub(" ", str(text)).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def append_log(ops, op, subject, bullets=(), now=None):
    """Append one entry to ops/log.md: '## [YYYY-MM-DD] op | subject' plus bullets. Append-only."""
    if op not in VALID_OPS:
        raise ValueError("unknown log op: %r" % (op,))
    makedirs_private(ops)
    path = os.path.join(ops, "log.md")
    entry = "\n## [%s] %s | %s\n" % (
        local_date(now),
        op,
        clean_line(subject, LOG_SUBJECT_LIMIT),
    )
    entry += "".join("- %s\n" % clean_line(b, LOG_BULLET_LIMIT) for b in bullets)
    fd = os.open(
        path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )  # never follow a planted symlink
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, entry.encode("utf-8"))
    finally:
        os.close(fd)
    return path
