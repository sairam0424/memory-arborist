"""Lint for the Claude Code auto-memory directory. Standard library only; read-only (never writes).

Entry point is memory_hygiene.py (lint command). Claude Code's docs say MEMORY.md is read up to 200
lines or 25KB and name no unit for the size; this tool counts UTF-16 code units against 25,000 and warns
well below that, which is its own conservative policy. The 30-line frontmatter limit, the 4 KB long-note
size and the symlink finding are also this tool's own policy, not documented Claude Code behaviour.
Messages never contain raw note text: anything quoted goes through safe_snippet.
"""

import os
import re
import sys
from collections import Counter, namedtuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memory_common as mc  # noqa: E402
import memory_links as mk  # noqa: E402
import memory_frontmatter as mf  # noqa: E402

Finding = mc.Finding
description_hazards = mf.description_hazards  # re-exported for callers and tests
parse_frontmatter = mf.parse_frontmatter
LintResult = namedtuple("LintResult", "findings stats")

INDEX_NAME = mk.INDEX_NAME
INDEX_CAP_CHARS = 25000  # documented as 25KB with no stated unit; counted here in UTF-16 code units
INDEX_WARN_CHARS, INDEX_ERROR_CHARS = (
    20000,
    22500,
)  # own thresholds: warn at 80% of the cap, error keeps a 2.5k margin
INDEX_WARN_LINES, INDEX_ERROR_LINES = 160, 190  # the documented read cap is 200 lines
LINE_WARN, LINE_FLAG, LINE_ERROR = 150, 200, 300
BIG_BYTES, LONG_NOTE_BYTES = 100 * 1024, 4096  # own heuristics for a big note and a long line
# Findings that stop a staged edit even though lint only warns about them: a proposed edit must never be what
# breaks a note's frontmatter or the index. The yaml-* codes are every hazard memory_frontmatter.py can emit for a
# name or description line (a dream hunk may rewrite the description line). The staging gate compares counts per
# (code, path), so a hazard the snapshot already had never blocks. missing-type and bad-type stay warnings.
STAGING_BLOCKING_CODES = frozenset(
    {
        "frontmatter-unclosed",
        "missing-description",
        "index-is-symlink",
        "yaml-bad-escape",
        "yaml-colon",
        "yaml-comment",
        "yaml-indicator",
        "yaml-multiline",
        "yaml-nonstring",
        "yaml-tab-indent",
        "yaml-trailing-text",
        "yaml-unterminated",
    }
)
PER_CODE_SHOWN = 10
ORIGIN_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
SEVERITY_ORDER = {"ERROR": 0, "WARN": 1, "INFO": 2}


# ------------------------------------------------------------------ index


def _strip_comments(text):
    """Remove <!-- ... --> in one linear pass, keeping newlines so line numbers stay valid; an unclosed comment is left alone."""
    parts, pos = [], 0
    while True:
        start = text.find("<!--", pos)
        end = text.find("-->", start + 4) if start >= 0 else -1
        if start < 0 or end < 0:
            parts.append(text[pos:])
            return "".join(parts)
        parts.append(text[pos:start])
        parts.append("\n" * text.count("\n", start, end + 3))
        pos = end + 3


def index_visible_text(text):
    """The index as Claude Code reads it (comments removed, frontmatter blanked) with line numbers unchanged."""
    lines = _strip_comments(text).split("\n")
    if lines[0].rstrip() == "---":
        close = next((i for i in range(1, len(lines)) if lines[i].rstrip() == "---"), None)
        if close is not None:
            lines = [""] * (close + 1) + lines[close + 1 :]
    return "\n".join(lines)


def _counted_lines(text):
    """(original line number, text) for the span that counts toward the cap."""
    numbered = list(enumerate(index_visible_text(text).split("\n"), start=1))
    used = [i for i, (_n, line) in enumerate(numbered) if line.strip()]
    return numbered[used[0] : used[-1] + 1] if used else []


def index_stats(text):
    counted = _counted_lines(text)
    lengths = [(number, mc.utf16_len(line)) for number, line in counted]
    chars = mc.utf16_len("\n".join(line for _n, line in counted))
    over = lambda limit: sum(length > limit for _n, length in lengths)  # noqa: E731
    return {
        "bytes": len(text.encode("utf-8")),
        "chars": chars,
        "lines": text.count("\n"),
        "eff_lines": len(counted),
        "headroom": INDEX_CAP_CHARS - chars,
        "max_len": max((length for _n, length in lengths), default=0),
        "numbered_lengths": lengths,
        "over150": over(LINE_WARN),
        "over200": over(LINE_FLAG),
        "over300": over(LINE_ERROR),
    }


def _graded(code, value, warn, error):
    if value >= error:
        return [
            Finding(
                "ERROR",
                code,
                INDEX_NAME,
                0,
                "%d is at or above %d (the hard cap truncates the tail silently)"
                % (value, error),
            )
        ]
    return (
        [Finding("WARN", code, INDEX_NAME, 0, "%d is at or above %d" % (value, warn))]
        if value >= warn
        else []
    )


def _check_index(text):
    stats = index_stats(text)
    findings = _graded(
        "index-chars", stats["chars"], INDEX_WARN_CHARS, INDEX_ERROR_CHARS
    )
    findings += _graded(
        "index-lines", stats["eff_lines"], INDEX_WARN_LINES, INDEX_ERROR_LINES
    )
    for number, length in stats["numbered_lengths"]:
        if length > LINE_ERROR:
            findings.append(
                Finding(
                    "ERROR", "index-line-300", INDEX_NAME, number, "%d chars" % length
                )
            )
        elif length > LINE_FLAG:
            findings.append(
                Finding(
                    "WARN",
                    "index-line-200",
                    INDEX_NAME,
                    number,
                    "%d chars, candidate for trimming" % length,
                )
            )
        elif length > LINE_WARN:
            findings.append(
                Finding(
                    "WARN", "index-line-150", INDEX_NAME, number, "%d chars" % length
                )
            )
    return findings, stats


# ------------------------------------------------------------------ files


def bad_filename(name):
    if name == "memory.md":
        return "lowercase memory.md shadows the index on a case-insensitive filesystem"
    if mc.CONTROL_CHARS.search(name):
        return "control character in file name"
    if name.endswith((".tmp.md", ".tmp", "~", ".swp")) or name.startswith((".#", ".restore-")):
        return "temporary file inside the memory directory"
    return None


def _check_walk(walk):
    out = [
        Finding(
            "WARN", "symlink", rel, 0, "symlink: not snapshotted, scanned or linted"
        )
        for rel in walk.symlinks
    ]
    out += [Finding("WARN", "special-file", rel, 0, "FIFO or socket: not snapshotted or scanned") for rel in walk.special]
    out += [Finding("ERROR", "unreadable", rel, 0, msg) for rel, msg in walk.errors]
    if INDEX_NAME in walk.symlinks:
        out.append(Finding("WARN", "index-is-symlink", INDEX_NAME, 0, "MEMORY.md is a symlink and this tool does not follow it, so it reads the index as empty"))
    for rel in walk.files:
        base = os.path.basename(rel)
        reason = bad_filename(base)
        if reason:
            out.append(Finding("ERROR", "bad-filename", rel, 0, reason))
        elif not rel.endswith(".md") and not base.startswith("."):
            out.append(Finding("WARN", "non-note-file", rel, 0, "not a markdown note"))
    return out


def _check_sizes(texts):
    out, stats = [], Counter()
    for rel, text in sorted(texts.items()):
        size = len(text.encode("utf-8"))
        stats["over4096"] += size > LONG_NOTE_BYTES
        if size > BIG_BYTES:
            stats["big"] += 1
            out.append(Finding("WARN", "big-file", rel, 0, "%d KB" % (size // 1024)))
        width, number = max(
            (len(line.encode("utf-8")), n)
            for n, line in enumerate(text.split("\n"), start=1)
        )
        if width > LONG_NOTE_BYTES:
            stats["long_line"] += 1
            out.append(
                Finding(
                    "WARN", "long-line", rel, number, "%d bytes in one line" % width
                )
            )
    return out, dict(stats)


def _check_origin(texts, parsed, project_dir):
    if not project_dir:
        return [], 0
    dead = 0
    for rel in texts:
        origin = (
            parsed[rel]["meta"].get("originSessionId")
            or parsed[rel]["fields"].get("originSessionId")
            or ""
        ).strip("\"' ")
        if origin and not (
            ORIGIN_ID.match(origin)
            and os.path.exists(os.path.join(project_dir, origin + ".jsonl"))
        ):
            dead += 1
    note = (
        [
            Finding(
                "INFO",
                "dead-origin",
                "",
                0,
                "%d notes cite a transcript that no longer exists (unverifiable by transcript)"
                % dead,
            )
        ]
        if dead
        else []
    )
    return note, dead


def _check_tripwire(memory_dir, files, last_snapshot_count):
    """Warn when the markdown file count drops below the last snapshot's: a silent mass deletion is the failure that matters most."""
    md_count = sum(1 for rel in files if rel.endswith(".md"))
    if last_snapshot_count is not None and md_count < last_snapshot_count:
        return [
            Finding(
                "WARN",
                "memory-shrink-tripwire",
                "",
                0,
                "%d markdown files now vs %d in the last snapshot"
                % (md_count, last_snapshot_count),
            )
        ]
    return []


def _load(memory_dir, walk):
    """Note texts and index text; one unreadable file becomes a finding, never a crash."""
    texts, errors = {}, []
    for rel in walk.files:
        if rel.endswith(".md") and rel != INDEX_NAME:
            try:
                texts[rel] = mc.read_text(os.path.join(memory_dir, rel))
            except OSError as err:
                errors.append(Finding("ERROR", "unreadable-file", rel, 0, type(err).__name__))
    if INDEX_NAME not in walk.files:
        return texts, "", errors + [Finding("WARN", "no-index", INDEX_NAME, 0, "MEMORY.md is missing")]
    try:
        return texts, mc.read_text(os.path.join(memory_dir, INDEX_NAME)), errors
    except OSError as err:
        return texts, "", errors + [Finding("ERROR", "unreadable-file", INDEX_NAME, 0, type(err).__name__)]


def lint_dir(memory_dir, project_dir=None, last_snapshot_count=None):
    walk = mc.walk_files(memory_dir)
    texts, index_text, load_errors = _load(memory_dir, walk)
    index_findings, index_numbers = _check_index(index_text)
    parsed = {rel: mf.parse_frontmatter(text) for rel, text in texts.items()}
    note_findings = [f for rel in sorted(parsed) for f in mf.check_note(rel, parsed[rel])] + mf.check_duplicate_names(parsed)
    names = {rel: mf.note_name(p) for rel, p in parsed.items()}
    unreadable = [f.path for f in load_errors if f.code == "unreadable-file"]
    names.update({rel: "" for rel in list(walk.symlinks) + unreadable if rel.endswith(".md") and rel != INDEX_NAME})
    links = mk.check_links(texts, index_visible_text(index_text), names)
    size_findings, size_stats = _check_sizes(texts)
    origin_findings, dead = _check_origin(texts, parsed, project_dir)
    wire = _check_tripwire(memory_dir, walk.files, last_snapshot_count)
    findings = tuple(
        _check_walk(walk) + load_errors + index_findings + note_findings + list(links.findings)
        + size_findings + origin_findings + wire
    )
    stats = {
        **index_numbers, **links.stats, **size_stats, "notes": len(texts), "dead_origin": dead, "tripwire": len(wire),
        "symlinks": len(walk.symlinks), "yamlfail": len({f.path for f in findings if f.code.startswith("yaml-")}),
        "nofm": sum(1 for f in findings if f.code == "no-frontmatter"),
    }
    return LintResult(findings, stats)


# ------------------------------------------------------------------ output


def blocks_staging(finding):
    """True for an ERROR and for the structural and YAML-hazard warnings in STAGING_BLOCKING_CODES."""
    return finding.severity == "ERROR" or finding.code in STAGING_BLOCKING_CODES


def exit_code(result, strict=False):
    counts = Counter(f.severity for f in result.findings)
    return 1 if counts["ERROR"] or (strict and counts["WARN"]) else 0


def render(result, full=False, per_code=PER_CODE_SHOWN):
    s, counts = result.stats, Counter(f.severity for f in result.findings)
    get = lambda key: s.get(key, 0)  # noqa: E731
    out = [
        "INDEX bytes=%d chars=%d lines=%d headroom=%d over150=%d over200=%d over300=%d max=%d"
        % (
            s["bytes"],
            s["chars"],
            s["lines"],
            s["headroom"],
            s["over150"],
            s["over200"],
            s["over300"],
            s["max_len"],
        ),
        "NOTES n=%d DANGLING=%d targets=%d LOOSE=%d AMBIGUOUS=%d ORPHANS=%d NOT_IN_INDEX=%d BIG=%d LONG_LINE=%d FILES_OVER_4KB=%d "
        "YAMLFAIL=%d NO_FRONTMATTER=%d DEAD_ORIGIN=%d SYMLINKS=%d TRIPWIRE=%d"
        % (
            get("notes"),
            get("dangling"),
            get("dangling_targets"),
            get("loose"),
            get("ambiguous"),
            get("orphans"),
            get("not_in_index"),
            get("big"),
            get("long_line"),
            get("over4096"),
            get("yamlfail"),
            get("nofm"),
            get("dead_origin"),
            get("symlinks"),
            get("tripwire"),
        ),
        "LINT errors=%d warn=%d info=%d"
        % (counts["ERROR"], counts["WARN"], counts["INFO"]),
    ]
    groups = {}
    for f in result.findings:
        groups.setdefault((f.severity, f.code), []).append(f)
    for severity, code in sorted(groups, key=lambda k: (SEVERITY_ORDER[k[0]], k[1])):
        items = sorted(groups[(severity, code)], key=lambda f: (f.path, f.line))
        for f in items if full else items[:per_code]:
            out.append(
                "%s %s %s:%d %s" % (severity, code, mc.printable(f.path or "-"), f.line, mc.printable(f.message))
            )
        if not full and len(items) > per_code:
            out.append("  ... +%d more %s (use --full)" % (len(items) - per_code, code))
    return out
