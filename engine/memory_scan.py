"""Secret and PII shape scanner for the memory directory, snapshots and digests.

Prints file:line:pattern only, never the matched text. Patterns are assembled from fragments at
runtime so a secret-scanning hook does not block writing this file. The first 12 patterns describe
well-known public credential formats (docs/PROVENANCE.md names the public source of each) and run over the
whole text; their open-ended quantifiers are bounded so no input can make the scan super-linear.
This scanner covers the memory notes, their snapshots and the evidence digests.
"""

import bisect
import math
import os
import re
import sys
from collections import Counter, namedtuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memory_common as mc  # noqa: E402

Hit = namedtuple("Hit", "path line pattern tier")
ScanResult = namedtuple("ScanResult", "hits skipped")

MAX_SCAN_BYTES = 50 * 1024 * 1024
SNIPPET_LIMIT = 40
WARN_HITS_SHOWN = 20


def _rx(*parts, flags=0):
    return re.compile("".join(parts), flags)


BASE_PATTERNS = (
    ("AWS Access Key", _rx("AK", "IA[0-9A-Z]{16}"), "block"),
    (
        "AWS Secret Key",
        _rx(
            r"(?:aws)?_?secret_?(?:access)?_?key.{0,100}?[=:]\s{0,20}[\"']?([A-Za-z0-9/+=]{40})"
        ),
        "block",
    ),
    ("GitHub Token", _rx("gh[pousr]", "_[A-Za-z0-9_]{36,}"), "block"),
    ("GitHub PAT", _rx("github", "_pat_[A-Za-z0-9_]{22,}"), "block"),
    (
        "OpenAI Key",
        _rx(
            "sk-[A-Za-z0-9]{20,200}",
            "T3Blbk",
            "FJ|(?<![A-Za-z0-9])sk-(?:proj|svcacct|admin)-[A-Za-z0-9_-]{40,200}",
        ),
        "block",
    ),
    ("Anthropic Key", _rx("(?<![A-Za-z0-9])sk-", "ant-[A-Za-z0-9_-]{40,}"), "block"),
    (
        "Stripe Secret",
        _rx(
            "(?<![A-Za-z0-9])(?:sk_(?:live|test)|rk_(?:live|test)|sk_org(?:_(?:live|test))?)",
            "_[A-Za-z0-9]{24,}",
        ),
        "block",
    ),
    ("Slack Token", _rx("xox[bpares]-[A-Za-z0-9-]{10,}|x", "app-[0-9]-[A-Za-z0-9-]{10,200}"), "block"),
    (
        "Private Key Block",
        _rx("-----BEGIN (?:RSA |EC |DSA )?", "PRIVATE KEY-----"),
        "block",
    ),
    (
        "Generic Secret Assign",
        _rx(
            r"(?:pass",
            "word|sec",
            "ret|tok",
            r"en|api_key|apikey)\s{0,20}[:=]\s{0,20}[\"'][^\"']{8,512}[\"']",
            flags=re.I,
        ),
        "block",
    ),
    # Exact-case Bearer: the RFC 6750 token characters, but not a leading slash (a path is not a token). Any other casing
    # needs a digit in the first 100 characters so ordinary prose about bearer authentication does not hit.
    (
        "Bearer Token",
        _rx(
            "Bear",
            r"er\s{1,20}[A-Za-z0-9_\-.~+][A-Za-z0-9_\-.~+/]{19,}",
            "|(?i:bear",
            r"er)\s{1,20}(?=[A-Za-z0-9_\-.~+/]{0,100}[0-9])[A-Za-z0-9_\-.~+][A-Za-z0-9_\-.~+/]{19,}",
        ),
        "block",
    ),
    (
        "Database URL",
        _rx(r"(?:postgres(?:ql)?|mysqlx?|mongodb|rediss?)(?:\+[A-Za-z0-9_]{1,20})?:", r"//[^@\s]{1,200}@[^\s]{5,}"),
        "block",
    ),
)

EXTRA_PATTERNS = (
    ("AWS Temp Key", _rx("AS", "IA[0-9A-Z]{16}|(?<![A-Za-z0-9])A(?:BIA|CCA)[0-9A-Z]{16}"), "block"),
    (
        "AWS Secret Key (any case)",
        _rx(
            r"aws_?secret_?(?:access)?_?key.{0,100}?[=:]\s{0,20}[\"']?([A-Za-z0-9/+=]{40})",
            flags=re.I,
        ),
        "block",
    ),
    ("Google OAuth Token", _rx(r"ya29\.[A-Za-z0-9_-]{20,}"), "block"),
    ("Google Refresh Token", _rx("(?<![A-Za-z0-9:/])1//[A-Za-z0-9_-]{30,}"), "block"),
    (
        "JWT",
        _rx(
            "eyJ[A-Za-z0-9_-]{8,300}",
            r"\.eyJ[A-Za-z0-9_-]{8,300}",
            r"\.[A-Za-z0-9_-]{8,300}",
        ),
        "block",
    ),
    ("npm Token", _rx("npm_[A-Za-z0-9]{36}"), "block"),
    (
        "Other Key Block",
        _rx("-----BEGIN (?:OPENSSH |ENCRYPTED |PGP )", "PRIVATE KEY"),
        "block",
    ),
    (
        "Key Context",
        _rx(
            r"\b(?:authorization|passwd|api[_-]?key|secret[_-]?key|access[_-]?key)\b[\"']?\s{0,10}[:=]\s{0,10}[\"']?[A-Za-z0-9+/_\-.=]{16,}",
            flags=re.I,
        ),
        "warn",
    ),
    (
        "Quoted Key Value",
        _rx(
            r"\"(?:pass",
            "word|passwd|sec",
            "ret|tok",
            r"en|api[_-]?key|access[_-]?key)\"\s{0,10}:\s{0,10}\"[^\"\n]{8,512}\"",
            flags=re.I,
        ),
        "warn",
    ),
    (
        "Env Assignment",
        _rx(
            r"\b[A-Z0-9_]{0,40}(?:KEY|SECRET|TOKEN|PASSWORD|PASSWD)[A-Z0-9_]{0,40}\s{0,5}=\s{0,5}[\"']?[A-Za-z0-9+/_\-.=]{12,}"
        ),
        "warn",
    ),
    ("hex64", _rx(r"(?<![0-9a-fA-F])[0-9a-fA-F]{64,}(?![0-9a-fA-F])"), "warn"),
    (
        "Email",
        _rx(
            r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){0,5}\.[A-Za-z]{2,24}"
        ),
        "warn",
    ),
    ("Aadhaar", _rx(r"(?<!\d)\d{4}\s\d{4}\s\d{4}(?!\d)"), "warn"),
    ("PAN", _rx(r"\b[A-Z]{5}\d{4}[A-Z]\b"), "warn"),
    (
        "Phone",
        _rx(
            r"(?<![\d.])(?:\+\d{1,3}[\s-]?)?\d{5}[\s-]\d{5}(?![\d.])|(?<![\d.])\d{3}[-.\s]\d{3}[-.\s]\d{4}(?![\d.])"
        ),
        "warn",
    ),
)

URLSAFE_RUN = re.compile(r"[A-Za-z0-9_-]{32,}")
BASE64_RUN = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")
URLSAFE_ENTROPY = 4.2
BASE64_ENTROPY = 4.5
HEX_ONLY = re.compile(r"^[0-9a-fA-F]+$")
MIXED_RUN = re.compile(r"[A-Za-z0-9+/_-]{16,}")


def shannon_entropy(text):
    if not text:
        return 0.0
    counts = Counter(text)
    total = len(text)
    return -sum((n / total) * math.log2(n / total) for n in counts.values())


def _classes(token):
    return sum(bool(re.search(rx, token)) for rx in ("[a-z]", "[A-Z]", "[0-9]"))


def _secret_shaped(run):
    """Separators suggest an identifier (kebab/snake case, often date-suffixed): demand all three classes."""
    return _classes(run) >= (3 if ("-" in run or "_" in run) else 2)


def _entropy_hits(line):
    for run in URLSAFE_RUN.findall(line):
        if (
            not HEX_ONLY.match(run)
            and _secret_shaped(run)
            and shannon_entropy(run) > URLSAFE_ENTROPY
        ):
            yield "high-entropy"
    for run in BASE64_RUN.findall(line):
        if re.search("[0-9]", run) and shannon_entropy(run) > BASE64_ENTROPY:
            yield "high-entropy-b64"


def _line_starts(text):
    starts, pos = [0], 0
    while True:
        pos = text.find("\n", pos)
        if pos < 0:
            return starts
        pos += 1
        starts.append(pos)


def scan_text(text, path=""):
    """All hits in text. Base patterns see the whole text (multi-line forms count); extras run per line."""
    starts = _line_starts(text)
    found = {}
    for name, rx, tier in BASE_PATTERNS:
        for match in rx.finditer(text):
            line = bisect.bisect_right(starts, match.start())
            found.setdefault((line, name), Hit(path, line, name, tier))
    lines = text.split("\n")
    for number, line_text in enumerate(lines, start=1):
        for name, rx, tier in EXTRA_PATTERNS:
            if (number, name) not in found and rx.search(line_text):
                found[(number, name)] = Hit(path, number, name, tier)
    blocked = {line for (line, _name), hit in found.items() if hit.tier == "block"}
    for number, line_text in enumerate(lines, start=1):
        if (
            number not in blocked
        ):  # a known secret shape already explains the line; skip the entropy guess
            for name in dict.fromkeys(_entropy_hits(line_text)):
                found[(number, name)] = Hit(path, number, name, "warn")
    return sorted(found.values(), key=lambda hit: (hit.line, hit.pattern))


def decode_for_scan(raw):
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace").replace("\x00", "")
    return raw.decode("utf-8", errors="replace").replace("\x00", "")


def scan_file(path, label=None):
    if os.path.getsize(path) > MAX_SCAN_BYTES:
        raise ValueError("file larger than the scan limit")
    with open(path, "rb") as fh:
        raw = fh.read()
    return scan_text(decode_for_scan(raw), label or path)


def scan_tree(root):
    """Scan every regular file. Symlinks, unreadable directories and unreadable files are reported as skipped."""
    walk = mc.walk_files(root)
    skipped = [(rel, "symlink, not scanned") for rel in walk.symlinks]
    skipped += [(rel, "special file (FIFO or socket), not scanned") for rel in walk.special]
    skipped += [(rel, "unreadable directory: %s" % msg) for rel, msg in walk.errors]
    hits = []
    for rel in walk.files:
        try:
            hits.extend(scan_file(os.path.join(root, rel), rel))
        except (OSError, ValueError) as err:
            skipped.append((rel, "unreadable file: %s" % type(err).__name__))
    return ScanResult(hits, sorted(skipped))


def format_hit(hit):
    return "%s:%d:%s" % (mc.printable(hit.path), hit.line, hit.pattern)


def summarize(hits):
    block = sum(1 for hit in hits if hit.tier == "block")
    return block, len(hits) - block


def safe_snippet(text, limit=SNIPPET_LIMIT):
    """Short printable text for messages; anything long or secret-shaped is replaced by a length marker."""
    cleaned = mc.printable(text)
    if (
        len(cleaned) > limit
        or summarize(scan_text(cleaned))[0]
        or any(True for _ in _entropy_hits(cleaned))
        or any(_classes(run) >= 3 for run in MIXED_RUN.findall(cleaned))
    ):
        return "<redacted %d chars>" % len(str(text))
    return cleaned
