"""Frontmatter parsing and per-note checks for memory notes. Standard library only; read-only.

No public page says how many lines Claude Code reads for a note's frontmatter, so the 30-line limit
below is this tool's own policy and its findings are warnings. Messages never contain raw note text:
anything quoted goes through safe_snippet.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memory_common as mc  # noqa: E402
import memory_links as mk  # noqa: E402
import memory_scan as ms  # noqa: E402

Finding = mc.Finding
FM_CLOSE_LIMIT = 30
NOTE_TYPES = mk.NOTE_TYPES
KEY_LINE = re.compile(r"^([A-Za-z_][\w-]*):\s*(.*)$")
NESTED_KEY_LINE = re.compile(r"^\s+([A-Za-z_][\w-]*):\s*(.*)$")
NON_STRING = re.compile(r"^(?:yes|no|true|false|null|on|off|~|[-+]?\d+(?:\.\d+)?|\d{4}-\d{2}-\d{2}(?:[ T][\d:.]+Z?)?)$", re.I)
BAD_ESCAPE = re.compile(r'\\[^0abt\tnvfre "/\\N_LPxuU\n]')
EMPTY = {
    "has": False,
    "closed": False,
    "close_line": None,
    "fields": {},
    "meta": {},
    "line_of": {},
    "multiline": (),
    "tab_lines": (),
}


def _flow_pairs(value):
    pairs = (item.split(":", 1) for item in value[1:-1].split(",") if ":" in item)
    return {key.strip(): val.strip() for key, val in pairs}


def _strip_comment(value):
    """Value without a trailing ' # comment', in one linear pass (a leading # makes the whole value a comment)."""
    if value.startswith("#"):
        return ""
    for pos in range(1, len(value)):
        if value[pos] == "#" and value[pos - 1].isspace():
            return value[: pos - 1].rstrip()
    return value.strip()


def parse_frontmatter(text):
    lines = text.split("\n")
    if lines[0].rstrip() != "---":
        return {**EMPTY, "fields": {}, "meta": {}, "line_of": {}}
    close = next(
        (i + 1 for i in range(1, len(lines)) if lines[i].rstrip() == "---"), None
    )
    fields, meta, line_of, multiline, tab_lines = {}, {}, {}, [], []
    in_meta, meta_indent, last_plain = False, None, None
    for number in range(2, close or len(lines) + 1):
        raw = lines[number - 1]
        top, nested = KEY_LINE.match(raw), NESTED_KEY_LINE.match(raw)
        if top:
            key, value = top.group(1), top.group(2).strip()
            fields[key], line_of[key] = value, number
            bare = _strip_comment(value)
            in_meta, meta_indent = key == "metadata" and not bare, None
            last_plain = key if value and key != "metadata" else None
            if key == "metadata" and bare.startswith("{") and bare.endswith("}"):
                meta = {**meta, **_flow_pairs(bare)}
        elif in_meta and nested:
            indent = len(raw) - len(raw.lstrip())
            meta_indent = indent if meta_indent is None else meta_indent
            if indent == meta_indent:
                meta = {**meta, nested.group(1): nested.group(2).strip()}
                line_of[nested.group(1)] = number
        elif (
            raw[:1] in (" ", "\t")
            and raw.strip()
            and last_plain in ("name", "description")
        ):
            multiline.append(last_plain)
        if raw[:1] == "\t":
            tab_lines.append(number)
    return {
        "has": True,
        "closed": close is not None and close <= FM_CLOSE_LIMIT,
        "close_line": close,
        "fields": fields,
        "meta": meta,
        "line_of": line_of,
        "multiline": tuple(multiline),
        "tab_lines": tuple(tab_lines),
    }


def _closing_quote(value):
    quote, pos = value[0], 1
    while pos < len(value):
        if quote == '"' and value[pos] == "\\":
            pos += 2
        elif value[pos] == quote and not (
            quote == "'" and value[pos + 1 : pos + 2] == "'"
        ):
            return pos
        else:
            pos += 2 if value[pos] == quote else 1
    return None


def description_hazards(raw):
    """Ways a one-line YAML scalar silently misparses (a ' #' truncates the value)."""
    value = raw.strip()
    if not value:
        return []
    if value[0] in "\"'":
        end = _closing_quote(value)
        if end is None:
            return ["yaml-unterminated"]
        rest = value[end + 1 :].strip()
        found = ["yaml-trailing-text"] if rest and not rest.startswith("#") else []
        return found + (["yaml-bad-escape"] if value[0] == '"' and BAD_ESCAPE.search(value[1:end]) else [])
    checks = (
        ("yaml-colon", ": " in value or ":\t" in value or value.endswith(":")),
        ("yaml-comment", value.startswith("#") or bool(re.search(r"\s#", value))),
        (
            "yaml-indicator",
            value[0] in "[{*&!|>%@`" or value == "-" or value.startswith(("- ", "? ")),
        ),
        ("yaml-nonstring", bool(NON_STRING.match(value))),
    )
    return [code for code, hit in checks if hit]


def note_type(fm):
    return (fm["meta"].get("type") or fm["fields"].get("type") or "").strip("\"' ")


def note_name(fm):
    return fm["fields"].get("name", "").strip("\"' ")


def _problems(fm):
    """(severity, code, line, message) rows for one note's frontmatter."""
    fields, line_of, kind = fm["fields"], fm["line_of"], note_type(fm)
    rows = []
    if not fm["closed"]:
        rows.append(
            (
                "WARN",
                "frontmatter-unclosed",
                1,
                "no closing --- within the first %d lines (this tool's own limit)"
                % FM_CLOSE_LIMIT,
            )
        )
    if not note_name(fm):
        rows.append(("ERROR", "missing-name", 1, "name is missing"))
    if not fields.get("description", "").strip().strip("\"'").strip():  # "" and '' are an emptied description
        rows.append(
            (
                "WARN",
                "missing-description",
                1,
                "description is missing or not on one line",
            )
        )
    if not kind:
        rows.append(("WARN", "missing-type", 1, "no type"))
    elif kind not in NOTE_TYPES:
        rows.append(
            (
                "WARN",
                "bad-type",
                line_of.get("type", 1),
                "type %s is not one of %s"
                % (ms.safe_snippet(kind), "/".join(NOTE_TYPES)),
            )
        )
    rows += [("WARN", "yaml-tab-indent", number, "tab indentation is a YAML syntax error") for number in fm["tab_lines"][:1]]
    for key in ("description", "name"):
        rows += [
            (
                "WARN",
                hazard,
                line_of.get(key, 1),
                "%s line would misparse as YAML" % key,
            )
            for hazard in description_hazards(fields.get(key, ""))
        ]
    rows += [
        (
            "WARN",
            "yaml-multiline",
            line_of.get(key, 1),
            "%s continues on the next line" % key,
        )
        for key in dict.fromkeys(fm["multiline"])
    ]
    return rows


def check_note(rel, fm):
    if not fm["has"]:
        return [
            Finding("ERROR", "no-frontmatter", rel, 1, "file does not start with ---")
        ]
    return [
        Finding(sev, code, rel, line, message)
        for sev, code, line, message in _problems(fm)
    ]


def check_duplicate_names(parsed):
    owner, out = {}, []
    for rel, fm in sorted(parsed.items()):
        name = note_name(fm)
        if name and name in owner:
            out.append(
                Finding(
                    "ERROR",
                    "duplicate-name",
                    rel,
                    fm["line_of"].get("name", 1),
                    "same name as %s" % owner[name],
                )
            )
        elif name:
            owner[name] = rel
    return out
