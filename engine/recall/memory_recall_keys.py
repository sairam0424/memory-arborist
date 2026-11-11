"""The answer-key grammar of the recall-question wizard: which regular expressions a model may propose as keys.

Standard library only. A key the judge later runs against model answers must not be able to hang it, so a key is accepted only
if it stays inside a small whitelist: literal characters, \\d and \\s, simple character classes, a | between alternatives,
`?` after any item, bounded repeats up to 20 and at most ONE open-ended repeat (on a digit or a space only), at most three
unnested groups that may only be followed by `?`, no lookarounds, anchors, flags or backreferences. Every top-level
alternative must carry at least three literal letters or digits, so no alternative can match almost anything. Inside that
grammar the worst case is quadratic in the length of the text, and the judge cuts answers to a fixed length before grading.
Control, format, line-separator, invisible and surrogate characters are refused everywhere.
"""

import os
import re
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import memory_redact as mr  # noqa: E402

PATTERN_MIN_CHARS, PATTERN_MAX_CHARS, PATTERN_MIN_LITERALS = 3, 160, 3
MAX_GROUPS, MAX_OPEN_REPEATS, MAX_BOUNDED_REPEATS, MAX_REPEAT_COUNT = 3, 1, 3, 20
ESCAPABLE = frozenset(".-_/:@#%&=~',;!\"()[]{}|*+?^$<>\\")
BOUNDED = re.compile(r"\{([0-9]{1,2})(?:,([0-9]{1,2}))?\}")


def _plain_char(ch):
    if ch == " ":
        return True
    # C: control, format (bidi, zero-width), surrogate, unassigned. Z: every space and separator but the plain space,
    # including U+2028 and U+2029. The redactor's own list adds variation selectors and similar marks.
    return unicodedata.category(ch)[0] not in "CZ" and not mr.is_invisible(ch)


def plain_text(text):
    """True when the text holds only characters a person can see: no control, format, separator, invisible or surrogate characters."""
    return all(_plain_char(ch) for ch in text)


class _Scan:
    """What the key grammar walk has counted so far."""

    def __init__(self):
        self.groups = self.open_repeats = self.bounded = self.alt_literals = 0
        self.depth = 0
        self.last = None  # the kind of the previous item: char, digit, space, any, class, group, quantified, or None


def _escape(body, i, scan):
    if i + 1 >= len(body):
        return None, "a pattern ends with a backslash"
    escaped = body[i + 1]
    if escaped in "ds":
        scan.last = "digit" if escaped == "d" else "space"
    elif escaped in ESCAPABLE:
        scan.last = "char"
    else:
        return (
            None,
            "a pattern uses an escape that is not allowed (backreferences, word and boundary escapes)",
        )
    return i + 2, None


def _char_class(body, i, scan):
    end = body.find("]", i + 1)
    inner = body[i + 1 : end] if end != -1 else ""
    if (
        end == -1
        or end - i > 41
        or "[" in inner
        or "\\" in inner
        or inner.startswith("^")
    ):
        return None, "a pattern has a character class that is not allowed"
    scan.last = "class"
    return end + 1, None


def _group_open(body, i, scan):
    if scan.depth:
        return None, "nested groups are not allowed"
    scan.groups += 1
    if scan.groups > MAX_GROUPS:
        return None, "a pattern has too many groups"
    scan.depth, scan.last = 1, None
    if body.startswith("(?:", i):
        return i + 3, None
    if body.startswith("(?", i):
        return None, "a group flag, lookaround or comment is not allowed"
    return i + 1, None


def _repeat(body, i, scan):
    mark = body[i]
    if scan.last in (None, "quantified"):
        return None, "a repeat has nothing to repeat, or follows another repeat"
    if mark == "?":
        scan.last = "quantified"
        return i + 1, None
    if scan.last == "group":
        return None, "only ? may follow a group"
    if mark in "*+":
        if scan.last not in ("digit", "space"):
            return None, "an open-ended repeat is only allowed on a digit or a space"
        scan.open_repeats += 1
        if scan.open_repeats > MAX_OPEN_REPEATS:
            return None, "a pattern has too many open-ended repeats"
        scan.last = "quantified"
        return i + 1, None
    found = BOUNDED.match(body, i)
    if not found:
        return None, "a repeat count is not allowed"
    low, high = int(found.group(1)), int(found.group(2) or found.group(1))
    scan.bounded += 1
    if low > high or high > MAX_REPEAT_COUNT or scan.bounded > MAX_BOUNDED_REPEATS:
        return None, "a repeat count is too large or there are too many"
    scan.last = "quantified"
    return found.end(), None


def _scan_syntax(body):
    """None when the pattern body stays inside the grammar, else why not. Linear in the pattern length."""
    scan, i = _Scan(), 0
    while i < len(body):
        c = body[i]
        if c == "\\":
            i, problem = _escape(body, i, scan)
        elif c == "[":
            i, problem = _char_class(body, i, scan)
        elif c == "(":
            i, problem = _group_open(body, i, scan)
        elif c in "?*+{":
            i, problem = _repeat(body, i, scan)
        elif c == ")":
            problem = None if scan.depth else "unbalanced parenthesis"
            scan.depth, scan.last, i = 0, "group", i + 1
        elif c == "|":
            problem = _alternative_problem(scan) if not scan.depth else None
            scan.last, i = None, i + 1
        elif c in "^$]}":
            problem = "an anchor or a stray bracket is not allowed"
        elif c == ".":
            scan.last, i, problem = "any", i + 1, None
        else:
            scan.alt_literals += c.isalnum()
            scan.last, i, problem = "char", i + 1, None
        if problem:
            return problem
    if scan.depth:
        return "unbalanced parenthesis"
    return _alternative_problem(scan)


def _alternative_problem(scan):
    """Called at the end of every top-level alternative: it must be anchored by literal text, then the count starts again."""
    if scan.alt_literals < PATTERN_MIN_LITERALS:
        return "a pattern has too few literal letters or digits"
    scan.alt_literals = 0
    return None


def pattern_problem(pattern):
    """Why a regex cannot be an answer key, or None. Whitelist first (no catastrophic backtracking is possible inside it), then a compile."""
    if not isinstance(pattern, str):
        return "a pattern is not text"
    if not PATTERN_MIN_CHARS <= len(pattern) <= PATTERN_MAX_CHARS:
        return "a pattern has the wrong length"
    if not plain_text(pattern):
        return "a pattern holds control or invisible characters"
    problem = _scan_syntax(pattern[4:] if pattern.startswith("(?i)") else pattern)
    if problem:
        return problem
    try:
        compiled = re.compile(pattern, re.S)
    except re.error:
        return "a pattern is not a valid regex"
    if compiled.search("") is not None:
        return "a pattern matches the empty text"
    return None
