"""Redaction for evidence digests: whole-record secret detection, PII masking, flat safe excerpts.

Standard library only, no I/O. Best effort, in layers, and fail closed:
  1. A record is judged as a whole BEFORE any excerpt is cut, and only after invisible characters
     are deleted (a token split by a zero-width character must not survive the flattening).
  2. Any credential cue word anywhere in a record (password, secret, api key, bearer, authorization,
     access token, ...) drops the whole record, whatever the value looks like: value shapes cannot
     be enumerated, cue words can. On real transcripts this costs about 5% of the text records.
  3. Secret shapes are searched on several detection copies: the text itself, a whitespace-squashed
     copy, a punctuation-stripped copy, an NFKC-normalised copy without combining marks, a
     percent/entity-decoded copy and a reversed copy; wrapped secrets (base64, urlsafe, base32, hex,
     double wrapping) are decoded; header-less PEM bodies, vendor prefixes and unquoted credential
     forms (flags, userinfo, Basic, netrc, quoted JSON keys) are recognised.
Design limits, stated plainly: bare random strings of 16-31 characters, hex keys of 32-63 characters,
rot13, base85 and Cyrillic/Greek look-alike letters carry no recognisable shape and are not caught.
The digests are local, mode 0600, and every proposal built from them is scanned and read by a person.
"""

import base64
import binascii
import html
import os
import re
import sys
import unicodedata
from collections import namedtuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memory_common as mc  # noqa: E402
import memory_scan as ms  # noqa: E402

EXCERPT_CHARS = 400
MAX_RECORD_CHARS = 200_000
MAX_WRAPPED_RUNS = 400
WRAP_BUDGET_CHARS = 1_000_000
MAX_UNWRAP_DEPTH = 2
PEM_MIN_LINES, NARROW_MIN_LINES = 3, 6
PII_PATTERNS = frozenset({"Email", "Phone", "Aadhaar", "PAN"})
INVISIBLE_RANGES = ((0xFE00, 0xFE0F), (0xE0100, 0xE01EF), (0x180B, 0x180D))
INVISIBLE_SINGLES = frozenset(
    {0x034F, 0x180E, 0x3164, 0x115F, 0x1160, 0xFFA0, 0x2800, 0xFFFC, 0x17B4, 0x17B5}
)
WRAPPED_RUN = re.compile(r"[A-Za-z0-9+/_-]{24,4096}={0,2}")
B32_RUN = re.compile(r"[A-Z2-7]{24,4096}={0,6}")
HEX_RUN = re.compile(r"(?:[0-9a-fA-F]{2}){12,2048}")
PEM_LINE = re.compile(r"^[A-Za-z0-9+/]{56,100}={0,2}$")
NARROW_LINE = re.compile(r"^[A-Za-z0-9+/]{16,100}={0,2}$")
CUE_ANY = re.compile(
    r"(?i)pass(?:word|wd|phrase|code)|secret|credential|api[ _-]?key|private[ _-]?key|bearer|authorization"
    r"|(?<![A-Za-z])(?:access|auth|refresh|session|bot|id|api)[ _-]?token|(?<=[a-z0-9])(?-i:(?:Access|Auth|Refresh|Session|Bot|Api)Token)"
    r"|ssh[ _-]?key|sshpass|docker\s+login|(?<![\w-])--token\b|(?:set-)?cookie\s*:|(?<![A-Za-z])(?:pw|pwd|pass)\s*[:=]|recovery[ _-]?(?:codes?|keys?|phrase)|backup[ _-]?codes?|seed[ _-]?(?:phrase|words?)|mnemonic"
)
PIN_VALUE = re.compile(r"(?i)\bpin\s*[:=]\s*[0-9]{4,8}\b")
CUE = (
    r"(?:pass(?:word|wd|phrase)?|pwd|secret|token|api[_-]?key|apikey|client[_-]?secret|access[_-]?key"
    r"|private[_-]?key|credentials?|auth(?:orization)?)"
)
CUE_VALUE = re.compile(
    r"(?i)(?:(?<![A-Za-z])|(?<=[a-z0-9])(?-i:(?=[A-Z])))"
    + CUE
    + r"[\"']?\s{0,5}(?::|=|\bis\b|\bare\b)\s{0,5}[\"']?([^\s\"']{8,256})"
)
SHORT_P_FLAG = re.compile(r"(?<![\w-])-p(?=[^\s]{0,128}[0-9])([^\s\"']{6,128})")
QUOTED_P_FLAG = re.compile(r"(?<![\w-])-p(?:'[^']{4,128}'|\"[^\"]{4,128}\")")
USER_COLON_PASS = re.compile(
    r"(?<![\w-])(?:-u|--user)(?:=|\s{0,3})[^\s:\"']{1,128}:([^\s\"']{3,128})"
)
BASIC_AUTH = re.compile(r"(?i)\bBasic\s{1,3}[A-Za-z0-9+/]{16,512}={0,2}")
URL_USERINFO = re.compile(
    r"[A-Za-z][A-Za-z0-9+.-]{1,20}://[^/\s:@]{0,128}:[^/\s@]{3,128}@"
)
BARE_USERINFO = re.compile(
    r"(?<![\w/@.:-])(?!mailto:)[A-Za-z0-9._-]{2,32}:[^\s/@:]{4,64}@[A-Za-z0-9][A-Za-z0-9.-]{0,63}"
)
VENDOR_TOKEN = re.compile(
    r"(?:\bglpat-|\bhf_|\bshpat_|\bshpca_|\bxapp-|\bxoxe\.|\bdop_v1_|\bsq0atp-|\bsq0csp-|\brk_live_|\bpk_live_|\bAIza|\bghu_|\bghr_|\bgho_"
    r"|\bpypi-|\bnpm_|\bSG\.)[A-Za-z0-9_.-]{16,200}|\bSK[0-9a-f]{32}\b|\b[a-z]{2,8}[-_][0-9a-f]{32,64}\b|\b[0-9a-f]{32}-us[0-9]{1,3}\b"
)
EMAIL = re.compile(
    r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){0,5}\.[A-Za-z]{2,24}"
)
PHONE = re.compile(
    r"(?<![\d.])(?:\+\d{1,3}[\s-]?)?\d{5}[\s-]\d{5}(?![\d.])"
    r"|(?<![\d.])\d{3}[-.\s]\d{3}[-.\s]\d{4}(?![\d.])"
    r"|(?<![\d.\w])\(\d{3}\)\s?\d{3}[-.\s]\d{4}(?![\d.])"
    r"|(?<![\d.\w])\+\d{1,3}(?:[ -]\d{2,5}){2,4}(?![\d.])"
    r"|(?<![\d.\w])\+\d{10,14}(?![\d.])"
    r"|(?<![\d.])[6-9]\d{9}(?![\d.])"
)
PAN = re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b")
IFSC = re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b")
UPI = re.compile(r"\b[A-Za-z0-9._-]{3,64}@(?:ok[a-z]{2,6}|ybl|paytm|ibl|axl|upi|apl)\b")
LONG_ID = re.compile(r"(?<![\d-])(?:[2-9][0-9]{11}|[2-9][0-9]{3}-[0-9]{4}-[0-9]{4})(?![\d-])")
SSN = re.compile(r"(?<![\d-])\d{3}-\d{2}-\d{4}(?![\d-])")
TWELVE_DIGITS = re.compile(r"(?<![\d-])(?<!\d\s)\d{4}\s\d{4}\s\d{4}(?![\d-])(?!\s\d)")
CARD = re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])")
Redaction = namedtuple("Redaction", "excerpt secret pii oversize")


# ------------------------------------------------------------------ normalising


def is_invisible(char):
    code = ord(char)
    return code in INVISIBLE_SINGLES or any(
        lo <= code <= hi for lo, hi in INVISIBLE_RANGES
    )


def normalize(text):
    """Delete format characters and invisible marks, blank other control characters, replace lone surrogates."""
    kept = []
    for char in str(text):
        category = unicodedata.category(char)
        if category == "Cf" or is_invisible(char):
            continue
        if category == "Cs":
            kept.append("?")
        elif category == "Cc" and char not in "\n\t":
            kept.append(" ")
        else:
            kept.append(char)
    return "".join(kept)


def _detection_copy(text):
    """NFKD-folded text without combining marks or invisible characters; used for detection only."""
    folded = unicodedata.normalize("NFKD", text)
    if len(folded) > 4 * len(text) + 64:
        return text
    return "".join(
        c
        for c in folded
        if unicodedata.category(c) not in ("Mn", "Me", "Cf") and not is_invisible(c)
    )


def _squash(text):
    return re.sub(r"\s+", "", text)


def _core(text):
    return re.sub(r"[^A-Za-z0-9_-]", "", text)


# ------------------------------------------------------------------ secret shapes


def _scanner_hit(text):
    return any(hit.pattern not in PII_PATTERNS for hit in ms.scan_text(text))


def _block_hit(text):
    return ms.summarize(ms.scan_text(text))[0] > 0


def _strong_value(value):
    return (
        len(value) >= 8
        and bool(re.search("[A-Za-z]", value))
        and bool(re.search("[0-9]", value))
    )


def _credential_form(text):
    if any(_strong_value(m.group(1)) for m in CUE_VALUE.finditer(text)):
        return True
    return any(
        rx.search(text)
        for rx in (
            SHORT_P_FLAG,
            QUOTED_P_FLAG,
            USER_COLON_PASS,
            BASIC_AUTH,
            URL_USERINFO,
            BARE_USERINFO,
            VENDOR_TOKEN,
            CUE_ANY,
            PIN_VALUE,
        )
    )


def _streak(text, line_re, needed):
    streak = 0
    for line in text.split("\n"):
        streak = streak + 1 if line_re.match(line.strip()) else 0
        if streak >= needed:
            return True
    return False


def _pem_body(text):
    return _streak(text, PEM_LINE, PEM_MIN_LINES) or _streak(
        text, NARROW_LINE, NARROW_MIN_LINES
    )


def _base64_variants(run):
    for trim in range(4):
        part = run[trim:].rstrip("=")
        if len(part) < 16:
            return
        padded = part + "=" * (-len(part) % 4)
        urlsafe, standard = bool(re.search("[-_]", part)), bool(re.search("[+/]", part))
        if urlsafe and standard:
            continue  # valid in neither alphabet
        for altchars in (b"-_",) if urlsafe else (None,) if standard else (None, b"-_"):
            try:
                yield base64.b64decode(padded, altchars=altchars, validate=True)
            except (binascii.Error, ValueError):
                continue


def _other_variants(text):
    for run in B32_RUN.findall(text):
        try:
            yield base64.b32decode(run.rstrip("=") + "=" * (-len(run.rstrip("=")) % 8))
        except (binascii.Error, ValueError):
            continue
    for run in HEX_RUN.findall(text):
        yield bytes.fromhex(run)


def _wrapped_secret(text, depth=0):
    """True when a decoding of some run in the text holds a secret; fails closed when there are too many runs."""
    runs = WRAPPED_RUN.findall(text)
    if len(runs) > MAX_WRAPPED_RUNS:
        return True
    budget = WRAP_BUDGET_CHARS
    variants = [raw for run in runs for raw in _base64_variants(run)] if runs else []
    for raw in variants + list(_other_variants(text)):
        budget -= len(raw)
        if budget < 0:
            return True
        decoded = raw.decode("utf-8", errors="ignore")
        if _scanner_hit(decoded) or _credential_form(decoded):
            return True
        if depth + 1 < MAX_UNWRAP_DEPTH and _wrapped_secret(decoded, depth + 1):
            return True
    return False


def _percent_decode(text):
    return re.sub(r"%([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), text)


def _unescaped(text):
    """Percent- and entity-decoded twice, tags removed, then folded again (decoding can reveal invisible characters)."""
    once = html.unescape(_percent_decode(_percent_decode(text)))
    return _detection_copy(re.sub(r"<[A-Za-z/][^<>]{0,40}>", "", once))


def _hidden_in_variants(text):
    """Block-tier shapes in the squashed, punctuation-stripped, folded, decoded and reversed copies."""
    folded = _detection_copy(text)
    decoded = _unescaped(folded)
    cores = [_core(folded), _core(decoded)]
    copies = (
        [_squash(text), _squash(folded), _squash(decoded)]
        + cores
        + [core[::-1] for core in cores]
    )
    return any(_block_hit(copy) for copy in dict.fromkeys(copies) if copy)


def has_secret(text):
    """True when the text holds a secret shape or credential cue in any of the forms the layers above know."""
    text = normalize(text)
    folded = _detection_copy(text)
    decoded = _unescaped(folded)
    return (
        _scanner_hit(text)
        or _credential_form(text)
        or _credential_form(decoded)
        or (decoded != text and _scanner_hit(decoded))
        or _hidden_in_variants(text)
        or _pem_body(text)
        or _wrapped_secret(text)
    )


# ------------------------------------------------------------------ PII and excerpts


def _luhn_ok(digits):
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2:
            value = value * 2 - 9 if value > 4 else value * 2
        total += value
    return total % 10 == 0


def _mask_card(match):
    text = match.group(0)
    digits = re.sub(r"\D", "", text)
    if 13 <= len(digits) <= 19 and _luhn_ok(digits):
        return "[card]"
    return _mask_card_window(text)


def _mask_card_window(text):
    """The whole run failed the Luhn check, as it does when a card is followed or preceded by more digits (an expiry date, a
    security code). Replace the longest stretch of whole digit groups of 13 to 19 digits that passes, and keep the rest."""
    groups = [(found.start(), found.end()) for found in re.finditer(r"\d+", text)]
    best = None
    for first in range(len(groups)):
        for last in range(first, len(groups)):
            span = (groups[first][0], groups[last][1])
            digits = re.sub(r"\D", "", text[span[0] : span[1]])
            if len(digits) > 19:
                break
            if len(digits) >= 13 and _luhn_ok(digits) and (best is None or len(digits) > best[0]):
                best = (len(digits), span)
    return text if best is None else text[: best[1][0]] + "[card]" + text[best[1][1] :]


def mask_pii(text):
    """(text with e-mail, phone, card, SSN and twelve-digit-group shapes masked, whether anything changed)."""
    masked = UPI.sub("[upi]", text)
    masked = EMAIL.sub("[email]", masked)
    masked = PAN.sub("[pan]", masked)
    masked = IFSC.sub("[ifsc]", masked)
    masked = LONG_ID.sub("[id]", masked)
    masked = PHONE.sub("[phone]", masked)
    masked = SSN.sub("[ssn]", masked)
    masked = TWELVE_DIGITS.sub("[id]", masked)
    masked = CARD.sub(_mask_card, masked)
    return masked, masked != text


def cut_excerpt(text, limit=EXCERPT_CHARS):
    """One printable line of at most `limit` characters, cut at whitespace with a trailing ellipsis."""
    flat = " ".join(mc.CONTROL_CHARS.sub(" ", str(text)).split())
    if len(flat) <= limit:
        return flat
    head = flat[: limit - 1]
    if flat[limit - 1] != " " and " " in head:
        head = head[: head.rfind(" ")]
    return head.rstrip() + "…"


def sanitize(text, limit=EXCERPT_CHARS):
    """Judge the whole record (size first, then normalised), mask PII, cut, then judge the cut once more."""
    if len(text) > MAX_RECORD_CHARS:
        return Redaction(None, False, False, True)
    text = normalize(text)
    if has_secret(text):
        return Redaction(None, True, False, False)
    masked, pii = mask_pii(text)
    excerpt = cut_excerpt(masked, limit)
    if not excerpt:
        return Redaction(None, False, False, False)
    if has_secret(excerpt):
        return Redaction(None, True, pii, False)
    return Redaction(excerpt, False, pii, False)
