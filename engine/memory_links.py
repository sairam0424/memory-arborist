"""Link extraction and resolution for memory notes. Standard library only; read-only."""

import os
import re
import sys
import urllib.parse
from collections import namedtuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memory_common as mc  # noqa: E402
import memory_scan as ms  # noqa: E402

INDEX_NAME = "MEMORY.md"
NOTE_TYPES = ("user", "feedback", "project", "reference")
TYPE_PREFIX = re.compile(r"^(?:%s)-" % "|".join(NOTE_TYPES))
WIKI_LINK = re.compile(r"\[\[([^\[\]\n]{1,200}?)\]\]")
MD_LINK = re.compile(
    r"\]\(([^)\s#]{1,200}\.md)(?:#[^)\s]{0,100})?(?:\s+\"[^\"]{0,200}\")?\)"
)
REF_DEF = re.compile(r"^\s{0,3}\[[^\]\n]{1,100}\]:\s*(\S{1,200}\.md)(?:\s|$)")
CODE_SPAN = re.compile(r"```[^\n]*?```|``[^\n]*?``|`[^`\n]*`")
FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
ESCAPED_OR_PLAIN_PIPE = re.compile(r"\\?\|")
Resolution = namedtuple("Resolution", "status files via")
LinkReport = namedtuple("LinkReport", "findings stats")


def _closes(line, fence):
    stripped = line.strip()
    return (
        bool(stripped) and set(stripped) == {fence[0]} and len(stripped) >= len(fence)
    )


def _visible_lines(text):
    """Lines with fenced blocks and inline code removed; numbering is preserved."""
    visible, fence = [], None
    for line in text.split("\n"):
        opener = FENCE.match(line)
        if fence:
            visible.append("")
            fence = None if _closes(line, fence) else fence
        elif (
            opener and line.count(opener.group(1)) < 2
        ):  # a lone fence opens a block; ```x``` on one line is a span
            visible.append("")
            fence = opener.group(1)
        else:
            visible.append(CODE_SPAN.sub("", line))
    return visible


def _wiki_target(raw):
    return ESCAPED_OR_PLAIN_PIPE.split(raw)[0].split("#")[0].strip()


def _md_target(raw):
    target = urllib.parse.unquote(raw)
    while target.startswith("./"):
        target = target[2:]
    return target if "/" not in target and ":" not in target else None


def extract_links(text):
    """[[wiki]], (file.md) and '[ref]: file.md' links as (target, 1-based line); code is ignored, ./ is normalised."""
    links = []
    for number, line in enumerate(_visible_lines(text), start=1):
        links += [
            (t, number)
            for t in (_wiki_target(m.group(1)) for m in WIKI_LINK.finditer(line))
            if t
        ]
        links += [
            (t, number)
            for t in (_md_target(m.group(1)) for m in MD_LINK.finditer(line))
            if t
        ]
        reference = REF_DEF.match(line)
        if reference and _md_target(reference.group(1)):
            links.append((_md_target(reference.group(1)), number))
    return links


def alias_tiers(names):
    """exact: file name, stem, frontmatter name. loose: stem without its type prefix. folded: case-folded exact."""
    exact, loose, folded = {}, {}, {}
    for rel, name in names.items():
        stem = os.path.basename(rel)[:-3]
        for alias in {os.path.basename(rel), stem, name} - {""}:
            exact.setdefault(alias, set()).add(rel)
            folded.setdefault(alias.casefold(), set()).add(rel)
        loose.setdefault(TYPE_PREFIX.sub("", stem), set()).add(rel)
    return exact, loose, folded


def _verdict(found, via):
    return Resolution("ok" if len(found) == 1 else "ambiguous", frozenset(found), via)


def resolve(target, exact, loose, folded=None):
    bare = target[:-3] if target.endswith(".md") else target
    if bare == INDEX_NAME[:-3]:
        return Resolution("index", frozenset(), "index")
    for key in dict.fromkeys((target, bare)):
        if exact.get(key):
            return _verdict(exact[key], "exact")
    if not TYPE_PREFIX.match(bare) and loose.get(
        bare
    ):  # a typed target must match exactly
        return _verdict(loose[bare], "loose")
    for key in dict.fromkeys((target.casefold(), bare.casefold())):
        if folded and folded.get(key):
            return _verdict(folded[key], "case")
    return Resolution("dangling", frozenset(), None)


def _finding(res, target, source, number, counts, inbound):
    shown = ms.safe_snippet(target)
    if res.status == "index":
        return None
    if res.status == "ok":
        inbound[next(iter(res.files))].add(source)
        if res.via in ("loose", "case"):
            counts[res.via] += 1
            code, severity = (
                ("alias-resolved-link", "INFO")
                if res.via == "loose"
                else ("case-mismatch-link", "WARN")
            )
            return mc.Finding(
                severity,
                code,
                source,
                number,
                "%s matched only through a looser spelling" % shown,
            )
        return None
    if res.status == "ambiguous":
        counts["ambiguous"] += 1
        return mc.Finding(
            "ERROR",
            "ambiguous-link",
            source,
            number,
            "%s matches %d notes" % (shown, len(res.files)),
        )
    if not SLUG.match(
        target
    ):  # prose or placeholder text inside [[ ]], not a reference to a note
        counts["non_note"] += 1
        return mc.Finding(
            "INFO",
            "non-note-link",
            source,
            number,
            "%s is not shaped like a note name" % shown,
        )
    counts["dangling"] += 1
    return mc.Finding(
        "ERROR", "dangling-link", source, number, "%s resolves to no note" % shown
    )


def check_links(texts, index_text, names):
    exact, loose, folded = alias_tiers(names)
    inbound = {rel: set() for rel in names}
    counts = {"dangling": 0, "ambiguous": 0, "loose": 0, "case": 0, "non_note": 0}
    findings, dangling_targets = [], set()
    for source, text in sorted({**texts, INDEX_NAME: index_text}.items()):
        for target, number in extract_links(text):
            res = resolve(target, exact, loose, folded)
            finding = _finding(res, target, source, number, counts, inbound)
            if finding:
                findings.append(finding)
                dangling_targets |= (
                    {target} if finding.code == "dangling-link" else set()
                )
    orphans = sorted(rel for rel, srcs in inbound.items() if not srcs - {rel})
    findings += [
        mc.Finding("INFO", "orphan", rel, 0, "no note or index line links here")
        for rel in orphans
    ]
    stats = dict(
        counts,
        dangling_targets=len(dangling_targets),
        orphans=len(orphans),
        not_in_index=sum(1 for srcs in inbound.values() if INDEX_NAME not in srcs),
    )
    return LinkReport(tuple(findings), stats)
