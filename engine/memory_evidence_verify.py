#!/usr/bin/env python3
"""Strict verification of evidence digests: shape, size, permissions, forbidden characters and secret shapes. Standard library only.

Split out of memory_evidence.py (which re-exports these names). The builder asks would_verify() before it writes a digest, so it
never leaves a file that its own verify step rejects; verify_dir() names the first files at fault.
"""

import os
import re
import stat
import unicodedata
from collections import namedtuple

import memory_common as mc
import memory_redact as mr
from memory_digest import HEADER, LINE_FORMAT, MAX_DIGEST_BYTES

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
MAX_LINE_CHARS = 412
Verify = namedtuple("Verify", "digests over_limit missing_header untrusted_lines ok bad_names", defaults=((),))
MAX_BAD_NAMES = 5

HEADER_LINES = (
    re.compile("^" + re.escape(HEADER) + "$"),
    re.compile("^session: ([0-9a-f-]{36})$"),
    re.compile(r"^date: ([0-9]{4}-[0-9]{2}-[0-9]{2}|unknown) \(times are UTC\)$"),
    re.compile(r"^cwd: (/.{0,200}|unknown|\[redacted\])$"),
    re.compile(r"^records: user=([0-9]{1,9})/([0-9]{1,9}) assistant=([0-9]{1,9})/([0-9]{1,9}) dropped_secret=[0-9]{1,9} masked_pii=[0-9]{1,9}$"),
    re.compile("^---$"),
)


# ------------------------------------------------------------------ verify


def _digest_entries(out):
    return sorted(os.listdir(out)) if os.path.isdir(out) else []


def _forbidden_character(text):
    return any(
        (unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") and c != "\n") or mr.is_invisible(c) for c in text
    )


def _clock_ok(line):
    shape = LINE_FORMAT.match(line)
    if not shape:
        return False
    stamp = shape.group(1)
    return stamp == "--:--" or (int(stamp[:2]) < 24 and int(stamp[3:]) < 60)


def _body_problems(lines, header):
    body = lines[6:-1]
    problems = int(not body)
    problems += sum(1 for line in body if len(line) > MAX_LINE_CHARS or not _clock_ok(line))
    problems += sum(1 for line in body if mr.has_secret(line[8:])) + mr.has_secret(lines[3][5:])
    users, assistants = (sum(1 for line in body if line.startswith(tag)) for tag in ("U ", "A "))
    kept_u, seen_u, kept_a, seen_a = (int(header.group(i)) for i in (1, 2, 3, 4))
    if (users, assistants) != (kept_u, kept_a) or kept_u > seen_u or kept_a > seen_a:
        problems += 1
    return problems


def _text_problems(text, stem, over):
    """(over_limit, header problems, untrusted problems) for a digest's text; the file-level checks come before this."""
    untrusted = _forbidden_character(text)
    lines = text.split("\n")
    matches = [rx.match(line) for rx, line in zip(HEADER_LINES, lines)]
    if over or len(lines) < 7 or lines[-1] != "" or not all(matches) or matches[1].group(1) != stem:
        return over, int(not over), untrusted
    return over, 0, untrusted + _body_problems(lines, matches[4])


def would_verify(text, session_id):
    """True when a digest with this text would pass verify. The builder asks before it writes, so it never leaves a file its own
    verify step rejects."""
    over = int(len(text.encode("utf-8")) > MAX_DIGEST_BYTES)
    return not any(_text_problems(text, session_id, over))


def _check_digest(out, name):
    """(over_limit, header problems, untrusted problems) for one entry."""
    path = os.path.join(out, name)
    stem = name[: -len(".md")] if name.endswith(".md") else ""
    if os.path.islink(path) or not os.path.isfile(path) or not UUID.fullmatch(stem):
        return 0, 0, 1
    info = os.stat(path)
    untrusted = int(bool(stat.S_IMODE(info.st_mode) & 0o077) or info.st_nlink > 1)
    over = int(info.st_size > MAX_DIGEST_BYTES)
    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_DIGEST_BYTES + 1)
    except OSError:
        return over, 0, untrusted + 1
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return over, 0, untrusted + 1
    over, missing, extra = _text_problems(text, stem, over)
    return over, missing, untrusted + extra


def verify_dir(out):
    if os.path.islink(out) or (os.path.isdir(out) and stat.S_IMODE(os.stat(out).st_mode) & 0o077):
        return Verify(0, 0, 0, 1, False)
    entries = _digest_entries(out)
    over = missing = bad = 0
    names = []
    for name in entries:
        a, b, c = _check_digest(out, name)
        over, missing, bad = over + a, missing + b, bad + c
        if (a or b or c) and len(names) < MAX_BAD_NAMES:
            names.append(name)
    return Verify(len(entries), over, missing, bad, bool(entries) and not (over or missing or bad), tuple(names))


def failure_text(result):
    """Why a caller refused the evidence folder, in words that name the first files at fault (or say there are none yet)."""
    if not result.digests:
        return "there are no evidence digests yet; run memory_evidence.py build (a session counts once it has been idle for 6 hours)"
    names = ", ".join(mc.printable(name)[:80] for name in result.bad_names)
    return "the evidence digests do not verify%s; delete those files and run memory_evidence.py build again (memory_evidence.py verify shows the counts)" % (
        " (first: %s)" % names if names else ""
    )
