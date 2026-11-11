"""One recall question against one arm: the command, the minimal environment, parsing and regex grading.

Standard library only. A fresh non-fork `claude --safe-mode -p` child answers each question with only
Read, Grep and Glob over the arm's memory directory, so nothing it does can change anything. The
parent never trusts the child: the tool list reported by the child must be exactly Glob, Grep, Read,
grading is plain regex (no model judge), and only booleans, a hash and a short withheld-if-secret
excerpt of each answer are kept.
"""

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from collections import namedtuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # the judge may live in scripts/recall/
import memory_common as mc  # noqa: E402
import memory_redact as mr  # noqa: E402

KINDS = ("recall", "drift", "supersede", "negative")
MIN_QUESTIONS, MAX_QUESTIONS = 25, 32
EXPECTED_TOOLS = ["Glob", "Grep", "Read"]
ALLOWED_ENV = (
    "PATH",
    "HOME",
    "CLAUDE_CODE_USE_BEDROCK",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_PROFILE",
)
INDEX_MAX_LINES, INDEX_MAX_UTF16 = 200, 25000
CALL_TIMEOUT_SECONDS = 240
MAX_ANSWER_CHARS = 8_000  # graded text: a key runs against at most this much of an answer, whatever the child returns
EXCERPT_CHARS = 200
ENV_JUDGE_MODEL = "MEMORY_SHEPHERD_JUDGE_MODEL"
# An alias, not a provider-specific id: aliases resolve to different model versions per provider (Anthropic API, Bedrock,
# Vertex, Foundry), so a baseline is only comparable with runs on the same provider. Pin a full model id for reproducibility.
DEFAULT_MODEL = os.environ.get(ENV_JUDGE_MODEL) or "haiku"
WITHHELD = "[withheld]"

Answer = namedtuple("Answer", "text cost tools is_error")
Settings = namedtuple("Settings", "model per_q_usd")


class AskError(Exception):
    """One question could not be answered; it is recorded as an ungraded error."""


class AskTimeout(AskError):
    """The child ran past its time limit; what it spent is unknown, so callers charge their per-call cap."""


class RunAborted(Exception):
    """The whole run must stop."""


class ToolsMismatch(RunAborted):
    pass


class CostCeiling(RunAborted):
    pass


class Spend:
    """Shared spend counter; stopping it makes every later call refuse before it launches."""

    def __init__(self, limit):
        self.limit, self.total, self.stopped = limit, 0.0, None
        self._lock = threading.Lock()

    def check(self):
        with self._lock:
            if self.stopped:
                raise RunAborted(self.stopped)
            if self.total >= self.limit:
                self.stopped = "cost ceiling reached (%.4f of %s USD)" % (
                    self.total,
                    self.limit,
                )
                raise CostCeiling(self.stopped)

    def add(self, cost):
        with self._lock:
            self.total += cost

    def stop(self, reason):
        with self._lock:
            self.stopped = self.stopped or reason


# ------------------------------------------------------------------ question file


def _check_question(rec, number):
    where = "line %d" % number
    if not isinstance(rec, dict):
        raise mc.HygieneError("%s: not an object" % where)
    for key in ("id", "kind", "question"):
        if not isinstance(rec.get(key), str) or not rec[key]:
            raise mc.HygieneError("%s: missing %s" % (where, key))
    if rec["kind"] not in KINDS:
        raise mc.HygieneError("%s: unknown kind %r" % (where, rec["kind"]))
    must, forbid = rec.get("must"), rec.get("forbid", [])
    if (
        not isinstance(must, list)
        or not must
        or not all(isinstance(x, str) for x in must)
    ):
        raise mc.HygieneError("%s: must needs at least one regex" % where)
    if not isinstance(forbid, list) or not all(isinstance(x, str) for x in forbid):
        raise mc.HygieneError("%s: forbid must be a list of regexes" % where)
    if rec["kind"] == "drift" and not rec.get("truth"):
        raise mc.HygieneError(
            "%s: a drift probe names its truth file in 'truth'" % where
        )
    for pattern in must + forbid:
        try:
            re.compile(pattern)
        except re.error as err:
            raise mc.HygieneError("%s: bad regex %r (%s)" % (where, pattern, err))


def load_questions(path, require_count=True):
    with open(path, encoding="utf-8") as fh:
        return load_questions_text(fh.read(), require_count)


def load_questions_text(text, require_count=True):
    """The questions in one JSONL text, checked. The wizard validates the exact bytes it installs with this."""
    questions, seen = [], set()
    for number, raw in enumerate(io.StringIO(text), start=1):
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            raise mc.HygieneError("line %d: not valid JSON" % number)
        _check_question(rec, number)
        if rec["id"] in seen:
            raise mc.HygieneError("line %d: duplicate id %s" % (number, rec["id"]))
        seen.add(rec["id"])
        questions.append(rec)
    if require_count and not MIN_QUESTIONS <= len(questions) <= MAX_QUESTIONS:
        raise mc.HygieneError(
            "%d questions; the set needs %d to %d"
            % (len(questions), MIN_QUESTIONS, MAX_QUESTIONS)
        )
    return questions


# ------------------------------------------------------------------ grading


def grade(question, answer):
    """PASS iff every must regex matches and no forbid regex does."""
    return all(re.search(p, answer, re.S) for p in question["must"]) and not forbid_hit(
        question, answer
    )


def forbid_hit(question, answer):
    return any(re.search(p, answer, re.S) for p in question.get("forbid", []))


# ------------------------------------------------------------------ the child


def visible_index(text):
    """MEMORY.md as a session would load it, approximately: Claude Code's docs say 200 lines or 25KB and name no unit, so
    this keeps at most 200 lines and 25,000 UTF-16 code units (this tool's own reading of "25KB")."""
    kept = "".join(text.splitlines(keepends=True)[:INDEX_MAX_LINES])
    while mc.utf16_len(kept) > INDEX_MAX_UTF16:
        kept = kept[: len(kept) - max(1, (mc.utf16_len(kept) - INDEX_MAX_UTF16) // 2)]
    return kept


def write_system_prompt(path, memory_dir, index_text):
    header = (
        "You answer from the user's persistent memory only. The memory directory is %s. Its index file "
        "MEMORY.md is shown below as a session would load it (long indexes are truncated). Use Read, Grep "
        "and Glob inside that directory to open the notes the index links to. If the answer is not in "
        "memory, say you do not know. Answer briefly.\n\n--- MEMORY.md ---\n"
        % memory_dir
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(header + index_text)


def build_argv(question_text, model, prompt_file, memory_dir, per_q_usd):
    """An argument list, never a shell string; the question comes before the variadic flags and --add-dir is last."""
    return [
        "claude",
        "--safe-mode",
        "-p",
        question_text,
        "--model",
        model,
        "--no-session-persistence",
        "--permission-mode",
        "dontAsk",
        "--tools",
        "Read,Grep,Glob",
        "--append-system-prompt-file",
        prompt_file,
        "--max-budget-usd",
        str(per_q_usd),
        "--output-format",
        "json",
        "--add-dir",
        memory_dir,
    ]


ENV_PASS = "MEMORY_SHEPHERD_PASS_ENV"
MAX_PASS_NAMES = 32
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


def child_env(environ=None):
    """The environment a headless child gets: a short fixed list, plus the variable NAMES the user listed in MEMORY_SHEPHERD_PASS_ENV.

    Passing more is an explicit opt-in (for example the variable that holds an API key); the listed values go to the child and
    from there to the model provider, so only list what that session needs. Malformed names and anything past the first 32
    entries are ignored, and the pass-through variable itself is never forwarded.
    """
    source = os.environ if environ is None else environ
    names = list(ALLOWED_ENV)
    for entry in source.get(ENV_PASS, "").split(",")[:MAX_PASS_NAMES]:
        entry = entry.strip()
        if ENV_NAME.fullmatch(entry) and entry != ENV_PASS:
            names.append(entry)
    return {key: source[key] for key in names if key in source}


def subprocess_runner(argv, cwd, env):
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=CALL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise AskTimeout("timed out")
    except OSError as err:
        raise AskError("could not start claude: %s" % type(err).__name__)
    return proc.returncode, proc.stdout, proc.stderr


def parse_output(stdout):
    try:
        data = json.loads(stdout)
    except ValueError:
        raise AskError("output is not JSON")
    items = [
        i for i in (data if isinstance(data, list) else [data]) if isinstance(i, dict)
    ]
    result = next((i for i in items if i.get("type") == "result"), None)
    if result is None:
        raise AskError("no result element")
    init = next(
        (i for i in items if i.get("type") == "system" and i.get("subtype") == "init"),
        None,
    )
    tools = (
        sorted(init["tools"]) if init and isinstance(init.get("tools"), list) else None
    )
    return Answer(
        str(result.get("result", "")),
        _clean_cost(result.get("total_cost_usd")),
        tools,
        bool(result.get("is_error")),
    )


def _clean_cost(value):
    """A cost the ceiling can trust: a finite, non-negative number. Anything else (bool, NaN, negative, absurdly large) is 0.0."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return 0.0
    try:
        cost = float(value)
    except OverflowError:
        return 0.0
    return cost if 0 <= cost < float("inf") else 0.0


def failed_run_answer(stdout):
    """What a failed child still printed on stdout (its result and tool list), or None when nothing parses.
    The caller raises or records the failure itself, so no error is hidden here."""
    try:
        return parse_output(stdout)
    except AskError:
        return None


def failed_run_cost(stdout):
    answer = failed_run_answer(stdout)
    return answer.cost if answer else 0.0


def _errored(question, spend, err, cost):
    spend.add(cost)
    return {"id": question["id"], "error": mc.clean_line(err, 120), "cost": cost}


def _excerpt(text):
    return mr.sanitize(text, EXCERPT_CHARS).excerpt or WITHHELD


def ask(question, runner, settings, prompt_file, memory_dir, spend, cwd=None):
    """One graded record: {id, pass, forbid_hit, cost, sha, excerpt} or {id, error, cost}."""
    spend.check()
    cwd = cwd or tempfile.mkdtemp(prefix="recall_cwd_")
    argv = build_argv(
        question["question"],
        settings.model,
        prompt_file,
        memory_dir,
        settings.per_q_usd,
    )
    stdout = ""
    try:
        code, stdout, _stderr = runner(argv, cwd, child_env())
        if code != 0:
            raise AskError("claude exited with %d" % code)
        out = parse_output(stdout)
    except AskTimeout as err:  # first: it is an AskError. The real spend is unknown, so charge the cap.
        return _errored(question, spend, err, settings.per_q_usd)
    except AskError as err:
        failed = failed_run_answer(stdout)
        record = _errored(question, spend, err, failed.cost if failed else 0.0)
        if failed and failed.tools is not None and failed.tools != EXPECTED_TOOLS:
            spend.stop("the child reported tools %s, expected %s" % (failed.tools, EXPECTED_TOOLS))
            raise ToolsMismatch(spend.stopped)  # a child that failed is still held to the tool list it reported
        return record
    spend.add(out.cost)
    if out.tools != EXPECTED_TOOLS:
        spend.stop(
            "the child reported tools %s, expected %s" % (out.tools, EXPECTED_TOOLS)
        )
        raise ToolsMismatch(spend.stopped)
    if out.is_error:
        return {
            "id": question["id"],
            "error": "the child reported an error",
            "cost": out.cost,
        }
    return {
        "id": question["id"],
        "pass": grade(question, out.text[:MAX_ANSWER_CHARS]),
        "forbid_hit": forbid_hit(question, out.text[:MAX_ANSWER_CHARS]),
        "cost": out.cost,
        "sha": hashlib.sha256(out.text.encode("utf-8")).hexdigest()[:16],
        "excerpt": _excerpt(out.text),
    }
