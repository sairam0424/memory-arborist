"""pinned.txt for a first run: the index lines the dream run must never edit. Standard library only.

`memory_dream.py pin --none` writes an empty file (no index line is protected, which is a legitimate stance) and
`pin --all` pins every entry line of the newest verified snapshot's MEMORY.md; afterwards a person edits the file by hand
(one index line per line, copied exactly). Create-only: it refuses when pinned.txt or its draft exists, so it can never
replace lines that someone chose. It writes only the data root, never the memory directory, and prints counts, not text.
"""

import os
import re

import memory_common as mc
import memory_snapshot as sn

ENTRY = re.compile(
    r"^\s*-\s*\[[^\]\n]+\]\([^)\n]+\)"
)  # an index entry: "- [Title](note.md) hook"
PRIVATE_FILE_MODE = 0o600


def entry_lines(index_text):
    """The index's entry lines, exactly as written (the dream run compares them stripped)."""
    return [line.rstrip("\r") for line in index_text.split("\n") if ENTRY.match(line)]


def _write_new(path, text):
    fd = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, PRIVATE_FILE_MODE
    )
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def create(ops, pin_all):
    """Create $OPS/pinned.txt. Returns (path, number of pinned lines). Never replaces an existing pinned.txt or draft."""
    target = os.path.join(ops, "pinned.txt")
    if os.path.lexists(target) or os.path.lexists(target + ".draft"):
        raise mc.HygieneError(
            "pinned.txt or pinned.txt.draft already exists: edit it by hand, this command never replaces it"
        )
    lines = []
    if pin_all:
        snap = sn.resolve_snapshot(ops, "latest")
        ok, _count, problems = sn.verify_snapshot(ops, snap)
        if not ok:
            raise mc.HygieneError(
                "snapshot %s failed verification (%d problems)" % (snap, len(problems))
            )
        index = os.path.join(ops, "snapshots", snap, "memory", "MEMORY.md")
        lines = entry_lines(mc.read_text(index)) if os.path.isfile(index) else []
    mc.makedirs_private(ops)
    _write_new(target, "".join(line + "\n" for line in lines))
    return target, len(lines)
