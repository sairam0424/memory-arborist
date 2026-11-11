"""The answer-key grammar of the recall-question wizard: which regular expressions a model may propose as keys.

Standard library only. The judge runs every key with re.search on model answers, so a key must not be able to hang it. A key
is therefore accepted only if it is written in a small grammar in which matching cannot backtrack much:

    key         := ["(?i)"] alternative ("|" alternative)*
    alternative := item+
    item        := a literal character | an escaped punctuation mark | \\d | \\s | "." | a character class [a-z0-9_-]
                 | a group "(" branch ("|" branch)* ")" | a single character, \\d or \\s followed by "?" | \\d or \\s followed by "+" or "*"
    branch      := literal characters only; branches are distinct and none is the start of another, so at most one can match

Limits: at most 2 optional marks, 1 open-ended repeat (which must be the last item of its alternative) and 3 groups per key;
no bounded repeats, lookarounds, anchors, flags, backreferences, nested groups, or marks after a group or a class. Each
top-level alternative must carry at least three letters or digits that have to match, at least two of them outside groups
(an optional character counts for nothing, and a group counts its shortest branch), so no alternative can match almost
anything. Control, format, line-separator, invisible and surrogate characters are refused.

Why it is cheap: every item consumes one character or picks the one branch that fits, except the optional marks (at most
4 paths together). An open-ended repeat that ends its alternative has nothing after it to backtrack into. For a text of n
characters the cost is therefore a few passes over the text per alternative, not a power of n. The judge also cuts answers to
a fixed length before grading.
"""

import os
import re
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import memory_redact as mr  # noqa: E402

PATTERN_MIN_CHARS, PATTERN_MAX_CHARS, PATTERN_MIN_LITERALS, PATTERN_MIN_OUTSIDE = 3, 160, 3, 2
MAX_GROUPS, MAX_OPEN_REPEATS, MAX_OPTIONAL_MARKS = 3, 1, 2
ESCAPABLE = frozenset(".-_/:@#%&=~',;!\"()[]{}|*+?^$<>\\")
GROUP_META = frozenset(
    "([{?*+.^$]}"
)  # none of these may appear unescaped inside a group branch


def _plain_char(ch):
    if ch == " ":
        return True
    # C: control, format (bidi, zero-width), surrogate, unassigned. Z: every space and separator but the plain space,
    # including U+2028 and U+2029. The redactor's own list adds variation selectors and similar marks.
    return unicodedata.category(ch)[0] not in "CZ" and not mr.is_invisible(ch)


def plain_text(text):
    """True when the text holds only characters a person can see: no control, format, separator, invisible or surrogate characters."""
    return all(_plain_char(ch) for ch in text)


def _branch_problem(branches):
    lowered = [b.lower() for b in branches]
    if any(not b for b in lowered) or len(set(lowered)) != len(lowered):
        return "a group has an empty branch or a repeated branch"
    if any(a != b and b.startswith(a) for a in lowered for b in lowered):
        return "a group has a branch that is the start of another branch"
    return None


def _read_group(body, start):
    """(branches, problem, index after the closing parenthesis) for the group whose text starts at `start`."""
    branches, current, i = [], [], start
    while i < len(body):
        c = body[i]
        if c == ")":
            branches.append("".join(current))
            problem = _branch_problem(branches)
            return branches, problem, i + 1
        if c == "|":
            branches.append("".join(current))
            current = []
        elif c == "\\":
            if i + 1 >= len(body) or body[i + 1] not in ESCAPABLE:
                return None, "a group branch may hold only literal characters", i
            current.append(body[i + 1])
            i += 1
        elif c == "(":
            return None, "nested groups are not allowed", i
        elif c in GROUP_META:
            return None, "a group branch may hold only literal characters", i
        else:
            current.append(c)
        i += 1
    return None, "unbalanced parenthesis", i


class _Walk:
    """One pass over a key body, keeping the counts the limits need."""

    def __init__(self, body):
        self.body, self.i = body, 0
        self.groups = self.optionals = self.opens = 0
        self.open_pending = False  # an open-ended repeat was just written: nothing may follow it in this alternative
        self.outside = 0  # of `mandatory`, the letters and digits that sit outside groups
        self.mandatory = 0  # letters and digits the current top-level alternative must match
        self.last = None  # None, char, digit, space, other, group or quantified
        self.last_weight = 0  # what the last item added to `mandatory`, so a following ? can take it back

    def run(self):
        while self.i < len(self.body):
            problem = self._step(self.body[self.i])
            if problem:
                return problem
        return self._end_alternative()

    def _step(self, c):
        if self.open_pending and c != "|":
            return "an open-ended repeat must be the last item of its alternative"
        if c == "\\":
            return self._escape()
        if c == "[":
            return self._char_class()
        if c == "(":
            return self._group()
        if c in "?*+":
            return self._mark(c)
        if c == "{":
            return "a repeat count is not allowed"
        if c == "|":
            problem = self._end_alternative()
            self.last, self.last_weight, self.open_pending = None, 0, False
            self.i += 1
            return problem
        if c == ")":
            return "unbalanced parenthesis"
        if c in "^$]}":
            return "an anchor or a stray bracket is not allowed"
        if c == ".":
            return self._item("other", 0, 1)
        return self._item("char", 1 if c.isalnum() else 0, 1)

    def _item(self, kind, weight, width):
        self.mandatory += weight
        self.outside += weight
        self.last, self.last_weight = kind, weight
        self.i += width
        return None

    def _escape(self):
        if self.i + 1 >= len(self.body):
            return "a pattern ends with a backslash"
        escaped = self.body[self.i + 1]
        if escaped == "d":
            return self._item("digit", 0, 2)
        if escaped == "s":
            return self._item("space", 0, 2)
        if escaped in ESCAPABLE:
            return self._item("char", 0, 2)
        return "a pattern uses an escape that is not allowed (backreferences, word and boundary escapes)"

    def _char_class(self):
        body = self.body
        end = body.find("]", self.i + 1)
        inner = body[self.i + 1 : end] if end != -1 else ""
        if (
            end == -1
            or end - self.i > 41
            or "[" in inner
            or "\\" in inner
            or inner.startswith("^")
        ):
            return "a pattern has a character class that is not allowed"
        self.last, self.last_weight, self.i = "other", 0, end + 1
        return None

    def _group(self):
        body = self.body
        self.groups += 1
        if self.groups > MAX_GROUPS:
            return "a pattern has too many groups"
        start = self.i + 1
        if body.startswith("(?:", self.i):
            start = self.i + 3
        elif body.startswith("(?", self.i):
            return "a group flag, lookaround or comment is not allowed"
        branches, problem, end = _read_group(body, start)
        if problem:
            return problem
        self.mandatory += min(sum(1 for ch in b if ch.isalnum()) for b in branches)
        self.last, self.last_weight, self.i = "group", 0, end
        return None

    def _mark(self, mark):
        if self.last in (None, "quantified"):
            return "a repeat has nothing to repeat, or follows another repeat"
        if mark == "?":
            if self.last not in ("char", "digit", "space"):
                return "an optional mark is only allowed on a single character, a digit or a space"
            self.optionals += 1
            if self.optionals > MAX_OPTIONAL_MARKS:
                return "a pattern has too many optional marks"
            self.mandatory -= self.last_weight
            self.outside -= self.last_weight
        else:
            if self.last not in ("digit", "space"):
                return "an open-ended repeat is only allowed on a digit or a space"
            self.opens += 1
            if self.opens > MAX_OPEN_REPEATS:
                return "a pattern has too many open-ended repeats"
            self.open_pending = True
        self.last, self.last_weight, self.i = "quantified", 0, self.i + 1
        return None

    def _end_alternative(self):
        """At the end of every top-level alternative: it must be anchored by letters or digits that have to match."""
        if self.mandatory < PATTERN_MIN_LITERALS or self.outside < PATTERN_MIN_OUTSIDE:
            return "a pattern has too few literal letters or digits"
        self.mandatory = self.outside = 0
        return None


def _scan_syntax(body):
    """None when the key body stays inside the grammar, else why not. Linear in the length of the key."""
    return _Walk(body).run()


def pattern_problem(pattern):
    """Why a regex cannot be an answer key, or None. The grammar first (every alternative needs three letters or digits that must match, so none can match the empty text), then a compile."""
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
        re.compile(pattern, re.S)
    except re.error:
        return "a pattern is not a valid regex"
    return None
