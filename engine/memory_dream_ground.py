"""Mechanical grounding of dream hunks: no model decides whether a proposal is allowed through. Standard library only.

A hunk is {id, kind, file, old, new, evidence: [{session, quote}], reason}. Version 1 supports two kinds:
`edit` (replace one exact span of one note) and `index_edit` (replace one span inside one MEMORY.md line).
A hunk is accepted only when every rule below holds; the rest are rejected with a reason, and the best
supported accepted hunks beyond the cap are deferred. Read-only: nothing here writes or deletes.
Anything the new text adds that could act on a reader (a link, address, path, command, figure or an instruction
aimed at a model) must come from the old text or from a quote a person typed.
"""

import os
import re
import sys
from collections import namedtuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memory_common as mc  # noqa: E402
import memory_evidence as me  # noqa: E402
import memory_redact as mr  # noqa: E402
import memory_scan as ms  # noqa: E402

KINDS = ("edit", "index_edit")
INDEX_NAME = "MEMORY.md"
MIN_OLD, MIN_QUOTE = 12, 20
GROWTH_FACTOR, GROWTH_SLACK = 1.5, 200
SHRINK_FLOOR, SHRINK_CHECK_FROM = 0.3, 100
FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.md")
HUNK_ID = re.compile(r"[A-Za-z0-9_-]{1,32}")
LINE_LINK = re.compile(r"\]\(([A-Za-z0-9][A-Za-z0-9._-]*\.md)\)")
URLISH = (
    re.compile(r"\b[a-z][a-z0-9+.-]{1,20}://[^\s)>\]\"'`]+", re.I),
    re.compile(r"\b(?:javascript|data|vbscript|mailto|tel|file):\S+", re.I),
    re.compile(r"\bgit@[\w.-]+:\S+"),
    re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
    re.compile(r"\b(?:[\w-]+\.)+[a-z]{2,24}/\S*", re.I),
    re.compile(r"(?<![\w/])~?/(?:[\w.-]+/)+[\w.-]+"),
    re.compile(r"~/[\w./-]+"),
)
COMMAND = re.compile(r"`([^`\n]{2,200})`")
DOMAIN = re.compile(r"\b(?:[a-z0-9-]+\.)+([a-z]{2,24})\b", re.I)
FIGURE = re.compile(r"\b\d[\w.:-]*\w\b|\b\d{2,}\b")
FILE_EXTENSIONS = frozenset(
    "md js mjs cjs ts tsx jsx py rb go rs java c h cpp hpp json yml yaml toml ini cfg lock log sql csv xml html css txt sh zsh bash "
    "png jpg jpeg gif svg pdf zip tar gz tgz jsonl env conf plist swift kt php cs".split()
)
INSTRUCTION_LIKE = re.compile(
    r"(?i)ignore (?:all |any |the )?(?:previous|prior|above)|from now on|always approve|disregard (?:all |the )?(?:previous|prior|above)"
    r"|system prompt|you are now|as an ai\b"
)
MAX_REASON_CHARS = 200  # the writer prompt asks for this; it is enforced here
Digest = namedtuple("Digest", "date users assistants")
GroundResult = namedtuple("GroundResult", "accepted rejected deferred")


def _squash(text):
    return " ".join(str(text).split())


def load_digests(directory):
    """{session: Digest}. Refuses to read a directory that does not pass the strict digest verification."""
    if not me.verify_dir(directory).ok:
        raise mc.HygieneError(
            "the evidence digests do not verify; rebuild or inspect them before grounding anything"
        )
    digests = {}
    for name in sorted(os.listdir(directory)):
        lines = mc.read_text(os.path.join(directory, name)).split("\n")
        date = re.match(r"date: (\S+) ", lines[2]).group(1)
        body = [line for line in lines[6:] if line]
        digests[name[: -len(".md")]] = Digest(
            date,
            [_squash(line[8:]) for line in body if line[0] == "U"],
            [_squash(line[8:]) for line in body if line[0] == "A"],
        )
    return digests


def read_notes(snapshot_dir):
    """({name: text}, {names that are not valid UTF-8}); a note that does not decode strictly can never be edited."""
    files, undecodable = {}, set()
    for rel in mc.list_md(snapshot_dir):
        with open(os.path.join(snapshot_dir, rel), "rb") as fh:
            raw = fh.read()
        try:
            files[rel] = raw.decode("utf-8")
        except UnicodeDecodeError:
            undecodable.add(rel)
    return files, undecodable


def _evidence_shape_problem(hunk):
    items = hunk.get("evidence")
    if not isinstance(items, list) or not items:
        return "a reason and at least one piece of evidence are required"
    if not all(
        isinstance(i, dict)
        and isinstance(i.get("session"), str)
        and isinstance(i.get("quote"), str)
        for i in items
    ):
        return "evidence items must be {session, quote} strings"
    return None


def _shape_problem(hunk, seen):
    if (
        not isinstance(hunk, dict)
        or not isinstance(hunk.get("id"), str)
        or not HUNK_ID.fullmatch(hunk["id"])
    ):
        return "bad hunk id"
    if hunk["id"] in seen:
        return "duplicate hunk id"
    if hunk.get("kind") not in KINDS:
        return "kind must be one of %s" % ", ".join(KINDS)
    name = hunk.get("file")
    if not isinstance(name, str) or not FILE_NAME.fullmatch(name):
        return "file must be a plain note name ending in .md"
    if (hunk["kind"] == "index_edit") != (name == INDEX_NAME):
        return "index_edit edits only MEMORY.md and edit never does"
    old, new = hunk.get("old"), hunk.get("new")
    if (
        not isinstance(old, str)
        or len(old) < MIN_OLD
        or not isinstance(new, str)
        or old == new
    ):
        return (
            "old must be at least %d characters and new must differ from it" % MIN_OLD
        )
    reason = hunk.get("reason")
    if (
        not isinstance(reason, str)
        or not reason.strip()
        or mr.normalize(reason) != reason
        or "\n" in reason
        or len(reason) > MAX_REASON_CHARS
    ):
        return (
            "a single-line plain-text reason (at most %d characters) and at least one piece of evidence are required"
            % MAX_REASON_CHARS
        )
    # the reason is model output that ends up in the report a person (or a session with tools) reads
    if INSTRUCTION_LIKE.search(reason):
        return "reason holds instruction-like text"
    if ms.summarize(ms.scan_text(reason))[0]:
        return "reason holds a secret shape"
    return _evidence_shape_problem(hunk)


def _frontmatter_end(text):
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip() != "---":
        return 0
    offset = len(lines[0])
    for line in lines[1:]:
        offset += len(line)
        if line.rstrip() == "---":
            return offset
    return 0


def _line_around(text, start):
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", start)
    return line_start, text[line_start : line_end if line_end >= 0 else len(text)]


def _size_problem(old, new):
    if len(new) > len(old) * GROWTH_FACTOR + GROWTH_SLACK:
        return "new text grows too much"
    if len(old) > SHRINK_CHECK_FROM and len(new) < len(old) * SHRINK_FLOOR:
        return "new text removes most of the old text"
    return None


def _text_problem(hunk, text, start, pinned):
    old, new = hunk["old"], hunk["new"]
    problem = _size_problem(old, new)
    if problem:
        return problem
    if mr.normalize(new) != new:
        return "new text holds control or invisible characters"
    if ms.summarize(ms.scan_text(new))[0]:
        return "new text holds a secret shape"
    if INSTRUCTION_LIKE.search(new) and not INSTRUCTION_LIKE.search(old):
        return "new text reads like an instruction to a model"
    line_start, line = _line_around(text, start)
    if hunk["kind"] == "index_edit":
        if "\n" in old or "\n" in new:
            return "an index edit must stay inside one line"
        if line.strip() in {p.strip() for p in pinned}:
            return "the line is pinned"
    elif start < _frontmatter_end(text):
        offset = start - line_start
        edited = line[:offset] + new + line[offset + len(old) :]
        if (
            not line.startswith("description:")
            or "\n" in old + new
            or not edited.startswith("description:")
        ):
            return "touches frontmatter other than the description"
    return None


def _evidence_problem(hunk, digests, note_date):
    """(problem or None, the quotes a person typed, the dates of the evidence digests)."""
    typed, dates = [], set()
    for item in hunk["evidence"]:
        digest = digests.get(item["session"])
        quote = _squash(item["quote"])
        if digest is None:
            return "evidence names an unknown session", [], set()
        if len(quote) < MIN_QUOTE:
            return "evidence quote is shorter than %d characters" % MIN_QUOTE, [], set()
        in_user = any(quote in line for line in digest.users)
        if not (in_user or any(quote in line for line in digest.assistants)):
            return "evidence quote is not in the digest", [], set()
        if digest.date == "unknown" or digest.date < note_date:
            return "evidence is older than the note", [], set()
        dates.add(digest.date)
        if in_user:
            typed.append(quote)
    return (
        (None if typed else "evidence needs a quote that a person typed"),
        typed,
        dates,
    )


def _added_tokens(text):
    found = [m.group(0) for rx in URLISH for m in rx.finditer(text)] + [
        m.group(1) for m in COMMAND.finditer(text)
    ]
    found += [
        m.group(0)
        for m in DOMAIN.finditer(text)
        if m.group(1).lower() not in FILE_EXTENSIONS
    ]
    found += [m.group(0) for m in FIGURE.finditer(text)]
    return found


def _invented(hunk, typed_quotes, dates):
    """Links, addresses, paths, commands and figures in the new text that neither the old text nor a typed quote contains."""
    allowed = hunk["old"] + " " + " ".join(typed_quotes) + " " + " ".join(dates)
    return [token for token in _added_tokens(hunk["new"]) if token not in allowed]


def _score(hunk, digests):
    typed = sum(
        1
        for item in hunk["evidence"]
        if any(
            _squash(item["quote"]) in line for line in digests[item["session"]].users
        )
    )
    return (-typed, -len({item["session"] for item in hunk["evidence"]}), hunk["id"])


def _utc_date(mtime_ns):
    stamp = mc.utc_stamp(mtime_ns / 1e9)[:8]
    return "%s-%s-%s" % (stamp[:4], stamp[4:6], stamp[6:])


def _dating_file(hunk, text, start, manifest):
    """The file whose age bounds the evidence: for an index edit the note its line links to, because the index is touched constantly."""
    if hunk["kind"] != "index_edit":
        return hunk["file"]
    link = LINE_LINK.search(_line_around(text, start)[1])
    return (
        link.group(1) if link and link.group(1) in manifest["files"] else hunk["file"]
    )


def _occurrences(text, needle):
    """1 or 2: how many (possibly overlapping) times the needle occurs, stopping at two."""
    count, pos = 0, text.find(needle)
    while pos >= 0 and count < 2:
        count, pos = count + 1, text.find(needle, pos + 1)
    return count


def _check(hunk, notes, manifest, digests, pinned, seen):
    files, undecodable = notes
    problem = _shape_problem(hunk, seen)
    if problem:
        return problem, None
    if hunk["file"] in undecodable:
        return "the note is not valid UTF-8 and cannot be edited", None
    text = files.get(hunk["file"])
    if text is None:
        return "the note is not in the snapshot", None
    count = _occurrences(text, hunk["old"])
    if count != 1:
        return (
            "old text not found in the note"
            if not count
            else "old text occurs more than once"
        ), None
    start = text.index(hunk["old"])
    problem = _text_problem(hunk, text, start, pinned)
    if problem:
        return problem, None
    note_date = _utc_date(
        manifest["files"][_dating_file(hunk, text, start, manifest)]["mtime_ns"]
    )
    problem, typed, dates = _evidence_problem(hunk, digests, note_date)
    if problem:
        return problem, None
    invented = _invented(hunk, typed, dates)
    if invented:
        return (
            "%d new link, address, path, command or figure not in the typed evidence (%s)"
            % (len(invented), mc.printable(invented[0])[:60]),
            None,
        )
    return None, start


def ground(hunks, snapshot_dir, manifest, digests_dir, pinned=(), max_hunks=10):
    notes = read_notes(snapshot_dir)
    digests = load_digests(digests_dir)
    passed, rejected, seen = [], [], set()
    for hunk in hunks:
        reason, start = _check(hunk, notes, manifest, digests, pinned, seen)
        label = (
            hunk.get("id")
            if isinstance(hunk, dict) and isinstance(hunk.get("id"), str)
            else repr(hunk)[:40]
        )
        if reason:
            rejected.append((label, reason))
            continue
        seen.add(hunk["id"])
        passed.append(dict(hunk, start=start, score=_score(hunk, digests)))
    accepted, spans = [], {}
    for hunk in sorted(passed, key=lambda h: h["score"]):
        taken = spans.setdefault(hunk["file"], [])
        end = hunk["start"] + len(hunk["old"])
        if any(hunk["start"] < b and a < end for a, b in taken):
            rejected.append((hunk["id"], "overlaps a stronger hunk"))
            continue
        taken.append((hunk["start"], end))
        accepted.append(hunk)
    return GroundResult(accepted[:max_hunks], rejected, accepted[max_hunks:])
