"""One evidence digest per Claude Code session: which records may enter, how they are picked and shaped.

Standard library only; reads a transcript, never writes. Only human-typed text and the assistant's own
text enter a digest. A user record is admitted only when every one of its keys is one a person-typed
record is known to carry (an allowlist observed in the author's own transcript files, not a documented
format: a record with an unknown key is left out, which fails closed), so scheduled fires, wake-ups,
agent and plugin messages, hook feedback and anything new the harness invents stay out. Tool results,
any block containing an HTML-like tag, compact summaries, sidechain records, synthetic or provenance-
tagged assistant records and headless (non-cli) sessions never enter either. A session whose records
never name an entrypoint is treated as not human. Every record is judged whole by memory_redact after
invisible characters are deleted and before any excerpt is cut.
"""

import html
import json
import os
import re
import sys
import unicodedata
from collections import Counter, deque, namedtuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memory_redact as mr  # noqa: E402

HEADER = "DATA ONLY - UNTRUSTED TRANSCRIPT EXCERPTS"
MAX_DIGEST_BYTES = 8192
HEADER_RESERVE = 1024
USER_SHARE = 0.6
KEEP_LINES = 40
MAX_LINE_BYTES = 8 * 1024 * 1024
HUMAN_PROBE_LINES = 300
CWD_CHARS = 120
MARKER = b"MEMORY-ARBORIST-RUN"
SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
LINE_FORMAT = re.compile(r"^[UA] ([0-9]{2}:[0-9]{2}|--:--) \S")
CLOCK = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T([0-9]{2}:[0-9]{2})")
DATE_ONLY = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
# Assumes harness-generated text inside user records is wrapped in HTML-like tags; observed, not documented. Untagged
# harness text would pass this filter.
ANY_TAG = re.compile(r"<[A-Za-z/!?][\w:.-]*[\s>/]")
USER_KEYS = frozenset(
    {
        "parentUuid",
        "isSidechain",
        "type",
        "message",
        "uuid",
        "timestamp",
        "userType",
        "entrypoint",
        "cwd",
        "sessionId",
        "version",
        "gitBranch",
        "promptId",
        "slug",
        "origin",
        "promptSource",
        "permissionMode",
        "turnOrigin",
        "turnPosition",
        "imagePasteIds",
        "queuePriority",
        "queueSkipAttachments",
        "interruptedMessageId",
        "interruptedBySendNow",
    }
)
ASSISTANT_PROVENANCE_KEYS = frozenset(
    {
        "agentId",
        "sourceToolUseID",
        "sourceToolAssistantUUID",
        "isVisibleInTranscriptOnly",
        "scheduledTaskId",
        "scheduledFireId",
        "wakeupSource",
        "mcpMeta",
        "origin",
        "isMeta",
        "isCompactSummary",
        "toolUseResult",
    }
)
NOT_HUMAN_PREFIXES = ("This session is being continued", "Stop hook feedback")
OBSERVER_DIRS = frozenset({"claude-mem", ".claude-mem"})

DEFAULT_ENTRYPOINTS = frozenset({"cli"})  # the entrypoints that count as a person at a terminal
SAFE_NAME = re.compile(r"[^A-Za-z0-9_-]+")
MAX_UNKNOWN_KEYS_PER_RECORD = 8
MAX_UNKNOWN_KEY_NAMES = 50  # distinct names kept per session; the rest are counted together as "other"
Digest = namedtuple(
    "Digest",
    "session_id text skip kept secret_records pii_records oversize_records entrypoint unknown_keys",
    defaults=(None, ()),
)


def _safe_name(value):
    """A name from a transcript, cut to letters, digits, underscore and hyphen so it can be printed safely."""
    return SAFE_NAME.sub("", str(value))[:30] or "unnamed"
Line = namedtuple("Line", "index text")


# ------------------------------------------------------------------ extracting text


def _blocks(content):
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [
            block["text"]
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ]
    return []


def _between(text, open_tag, close_tag):
    """Every span between an opener and the next closer; stops at the first opener without one (linear)."""
    spans, pos = [], 0
    while True:
        start = text.find(open_tag, pos)
        if start < 0:
            return spans
        end = text.find(close_tag, start + len(open_tag))
        if end < 0:
            return spans
        spans.append(text[start + len(open_tag) : end])
        pos = end + len(close_tag)


def _has_tag(text):
    probe = html.unescape(unicodedata.normalize("NFKC", text))
    return bool(ANY_TAG.search(probe))


def _human_piece(text):
    """The part of one text block that a person typed, or '' when the block is harness text."""
    if len(text) > mr.MAX_RECORD_CHARS:
        return text[
            : mr.MAX_RECORD_CHARS + 1
        ]  # judged oversize and dropped by the redactor; never scanned
    text = mr.normalize(text).strip()
    if not text or text.startswith(NOT_HUMAN_PREFIXES):
        return ""
    if text.startswith("<bash-input>"):
        joined = " ".join(_between(text, "<bash-input>", "</bash-input>"))
    elif text.startswith("<command-name>"):
        joined = " ".join(
            _between(text, "<command-name>", "</command-name>")
            + _between(text, "<command-args>", "</command-args>")
        )
    else:
        joined = text
    return "" if _has_tag(joined) else joined


def _is_tool_result(rec):
    content = rec.get("message", {}).get("content")
    return "toolUseResult" in rec or (
        isinstance(content, list)
        and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    )


def _typed_by_a_person(rec):
    if set(rec) - USER_KEYS or _is_tool_result(rec):
        return False
    return rec.get("origin") in (None, {"kind": "human"})


def _record_text(rec):
    """(origin 'U' or 'A', text) for a record that may enter a digest, else (None, '')."""
    if rec.get("isSidechain") or rec.get("isMeta") or rec.get("isCompactSummary"):
        return None, ""
    message = rec.get("message")
    if not isinstance(message, dict):
        return None, ""
    kind = rec.get("type")
    if kind == "user" and _typed_by_a_person(rec):
        pieces = map(_human_piece, _blocks(message.get("content")))
        return "U", "\n".join(piece for piece in pieces if piece)
    if (
        kind == "assistant"
        and not rec.get("isApiErrorMessage")
        and message.get("model") != "<synthetic>"
        and not ASSISTANT_PROVENANCE_KEYS & rec.keys()
    ):
        return "A", "\n".join(
            piece.strip() for piece in _blocks(message.get("content")) if piece.strip()
        )
    return None, ""


def _clock(rec):
    match = CLOCK.match(str(rec.get("timestamp", "")))
    return match.group(1) if match else "--:--"


# ------------------------------------------------------------------ one digest


def _observer_cwd(cwd):
    return any(part.casefold() in OBSERVER_DIRS for part in re.split(r"[\\/]+", cwd))


class _Session:
    """What one pass over a transcript collects; only the first and last KEEP_LINES lines per origin are kept."""

    def __init__(self, allowed=DEFAULT_ENTRYPOINTS):
        self.allowed = allowed
        self.entrypoint = None
        self.unknown_keys = Counter()
        self.head = {"U": [], "A": []}
        self.tail = {"U": deque(maxlen=KEEP_LINES), "A": deque(maxlen=KEEP_LINES)}
        self.seen = Counter()
        self.secret = self.pii = self.oversize = 0
        self.marker = self.headless = self.observer = self.saw_cli = False
        self.date = self.cwd = None

    @property
    def lines(self):
        return {
            origin: self.head[origin] + list(self.tail[origin]) for origin in ("U", "A")
        }

    def note_session_fields(self, rec):
        entry = rec.get("entrypoint")
        name = entry if isinstance(entry, str) else None
        self.saw_cli = self.saw_cli or name in self.allowed
        foreign = entry is not None and name not in self.allowed
        self.headless = self.headless or foreign
        if foreign and self.entrypoint is None:
            self.entrypoint = _safe_name(entry)
        cwd = rec.get("cwd")
        if isinstance(cwd, str):
            self.cwd = self.cwd or cwd
            self.observer = self.observer or _observer_cwd(cwd)
        stamp = str(rec.get("timestamp", ""))[:10]
        self.date = self.date or (stamp if DATE_ONLY.match(stamp) else None)

    def note_unknown_keys(self, rec):
        """Count, by name, the keys that make a person's record untrusted (a newer Claude Code may add some)."""
        if rec.get("type") != "user" or not isinstance(rec.get("message"), dict):
            return
        if rec.get("isSidechain") or rec.get("isMeta") or rec.get("isCompactSummary") or _is_tool_result(rec):
            return
        if rec.get("origin") not in (None, {"kind": "human"}):
            return
        for key in sorted(set(rec) - USER_KEYS)[:MAX_UNKNOWN_KEYS_PER_RECORD]:
            name = _safe_name(key)
            if name not in self.unknown_keys and len(self.unknown_keys) >= MAX_UNKNOWN_KEY_NAMES:
                name = "other"
            self.unknown_keys[name] += 1

    def add(self, rec, index):
        self.note_unknown_keys(rec)
        origin, text = _record_text(rec)
        if not origin or not text:
            return
        self.seen[origin] += 1
        red = mr.sanitize(text)
        self.secret += red.secret
        self.pii += bool(red.pii and red.excerpt)
        self.oversize += red.oversize
        if red.excerpt:
            line = Line(index, "%s %s %s" % (origin, _clock(rec), red.excerpt))
            (
                self.head[origin]
                if len(self.head[origin]) < KEEP_LINES
                else self.tail[origin]
            ).append(line)


def _lines(fh):
    """Lines of at most MAX_LINE_BYTES; a longer line yields None and is skipped in bounded chunks."""
    while True:
        raw = fh.readline(MAX_LINE_BYTES + 1)
        if not raw:
            return
        if len(raw) > MAX_LINE_BYTES and not raw.endswith(b"\n"):
            while True:
                rest = fh.readline(MAX_LINE_BYTES)
                if not rest or rest.endswith(b"\n"):
                    break
            yield None
        else:
            yield raw


def _read_session(path, allowed=DEFAULT_ENTRYPOINTS):
    session = _Session(allowed)
    with open(path, "rb") as fh:
        for index, raw in enumerate(_lines(fh)):
            if raw is None:
                continue
            if MARKER in raw:
                session.marker = True
                break
            try:
                rec = json.loads(raw)
            except (ValueError, RecursionError):
                continue
            if isinstance(rec, dict):
                session.note_session_fields(rec)
                if session.headless or session.observer:
                    break  # such a session never yields a digest; do not parse the rest of a large file
                session.add(rec, index)
    return session


def _edge_pick(items, budget):
    """Take items alternately from both ends while they fit, so a long session keeps its start and its end."""
    chosen, used, lo, hi, front = [], 0, 0, len(items) - 1, True
    while lo <= hi:
        item = items[lo] if front else items[hi]
        cost = len(item.text.encode("utf-8")) + 1
        if used + cost > budget:
            break
        chosen.append(item)
        used += cost
        lo, hi, front = (lo + 1, hi, False) if front else (lo, hi - 1, True)
    return chosen, used


def _select(session):
    budget = MAX_DIGEST_BYTES - HEADER_RESERVE
    lines = session.lines
    users, used = _edge_pick(lines["U"], int(budget * USER_SHARE))
    assistants, _ = _edge_pick(lines["A"], budget - used)
    return (
        sorted(users + assistants, key=lambda line: line.index),
        len(users),
        len(assistants),
    )


def _header(session_id, session, kept_users, kept_assistants):
    cwd = mr.sanitize(session.cwd or "unknown", CWD_CHARS).excerpt or "[redacted]"
    return "\n".join(
        [
            HEADER,
            "session: " + session_id,
            "date: %s (times are UTC)" % (session.date or "unknown"),
            "cwd: " + cwd,
            "records: user=%d/%d assistant=%d/%d dropped_secret=%d masked_pii=%d"
            % (
                kept_users,
                session.seen["U"],
                kept_assistants,
                session.seen["A"],
                session.secret,
                session.pii,
            ),
            "---",
        ]
    )


def _skip_reason(session, chosen, kept_users=1):
    for flag, reason in (
        (session.marker, "marker"),
        (session.headless or not session.saw_cli, "non_cli"),
        (session.observer, "observer"),
    ):
        if flag:
            return reason
    if not chosen:
        return "empty"
    return None if kept_users else "no_user"  # a digest nobody typed in cannot support a quote


def build_digest(path, allowed=DEFAULT_ENTRYPOINTS):
    """Digest for one transcript file. `skip` names why no digest is wanted; counters are still reported."""
    session_id = os.path.basename(path)[: -len(".jsonl")]
    if not SESSION_ID.fullmatch(session_id):
        return Digest(session_id, None, "not_uuid", 0, 0, 0, 0)
    session = _read_session(path, allowed)
    chosen, kept_users, kept_assistants = _select(session)
    skip = _skip_reason(session, chosen, kept_users)
    text = None
    if not skip:
        body = "\n".join(line.text for line in chosen)
        text = (
            _header(session_id, session, kept_users, kept_assistants)
            + "\n"
            + body
            + "\n"
        )
    return Digest(
        session_id,
        text,
        skip,
        len(chosen),
        session.secret,
        session.pii,
        session.oversize,
        session.entrypoint,
        tuple(sorted(session.unknown_keys.items())),
    )


def is_human_session(path, allowed=DEFAULT_ENTRYPOINTS):
    """Cheap check from the first records: False for headless (sdk-*) and observer sessions, and when no record names an entrypoint."""
    with open(path, "rb") as fh:
        for index, raw in enumerate(_lines(fh)):
            if index >= HUMAN_PROBE_LINES:
                break
            if raw is None:
                continue
            try:
                rec = json.loads(raw)
            except (ValueError, RecursionError):
                continue
            if isinstance(rec, dict) and "entrypoint" in rec:
                entry = rec["entrypoint"]
                return isinstance(entry, str) and entry in allowed
    return False
