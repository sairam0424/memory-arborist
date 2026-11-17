#!/usr/bin/env python3
"""First-run doctor: read-only checks of what a first paid run needs. Standard library only; run as python3 -I.

Checks Python, Claude Code (on the PATH the children get, its version and the flags the engine passes), which login-route
variables are set here but never reach the children, the memory directory, the data root and the first-run files.
Nothing is spent unless --live is given: that makes ONE real call (at most 0.05 USD, normally a fraction of a cent) with the
engine's exact invocation, which proves the login and the output format. The output holds names, versions and counts only
(the standard folders are shown with ~ and <project>, any other path as <custom path>, cloud account ids and role ARNs in a child's
error are replaced, and no variable value is printed), so it is safe to paste into an issue.
Exit codes: 0 no FAIL; 1 at least one FAIL (a refused or failed --live call included); 2 usage (argparse).
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import namedtuple

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, os.path.join(SCRIPTS, "recall"))
sys.path.insert(0, os.path.join(SCRIPTS, "dream"))
import memory_common as mc  # noqa: E402
import memory_dream_lanes as lanes  # noqa: E402
import memory_locate as locate  # noqa: E402
import memory_redact as mr  # noqa: E402
import memory_recall_run as mrr  # noqa: E402
import memory_snapshot as sn  # noqa: E402

OK, INFO, WARN, FAIL = "OK", "INFO", "WARN", "FAIL"
Check = namedtuple("Check", "status name detail fix")
PYTHON_MIN = (3, 11)
HOOK_MIN, MODEL_MIN = (
    (2, 1, 139),
    (2, 1, 169),
)  # the session-start hook; --safe-mode for the model-backed half
PROBE_TIMEOUT_SECONDS = 20
LIVE_PROBE_USD = 0.05
ENV_LANE_MODEL = "MEMORY_ARBORIST_LANE_MODEL"
REQUIRED_FLAGS = (
    "--safe-mode",
    "--print",
    "--model",
    "--no-session-persistence",
    "--permission-mode",
    "--tools",
    "--append-system-prompt-file",
    "--max-budget-usd",
    "--output-format",
    "--add-dir",
)
# Variables a login route may need that the children do not get by default. True = the value is (or may carry) a secret,
# so passing it sends it to the child and from there to the model provider: opt in only for what the session needs.
LOGIN_ENV = (
    ("ANTHROPIC_API_KEY", True),
    ("ANTHROPIC_AUTH_TOKEN", True),
    ("CLAUDE_CODE_OAUTH_TOKEN", True),
    ("AWS_BEARER_TOKEN_BEDROCK", True),
    ("AWS_ACCESS_KEY_ID", True),
    ("AWS_SECRET_ACCESS_KEY", True),
    ("AWS_SESSION_TOKEN", True),
    ("ANTHROPIC_FOUNDRY_API_KEY", True),
    ("HTTPS_PROXY", True),  # a proxy URL can carry a password
    ("HTTP_PROXY", True),
    ("ALL_PROXY", True),
    ("https_proxy", True),
    ("http_proxy", True),
    ("all_proxy", True),
    ("GOOGLE_APPLICATION_CREDENTIALS", False),
    ("CLAUDE_CODE_USE_VERTEX", False),
    ("CLAUDE_CODE_USE_FOUNDRY", False),
    ("ANTHROPIC_BASE_URL", False),
    ("ANTHROPIC_VERTEX_PROJECT_ID", False),
    ("CLOUD_ML_REGION", False),
    ("ANTHROPIC_FOUNDRY_RESOURCE", False),
    ("ANTHROPIC_MODEL", False),
    ("ANTHROPIC_DEFAULT_SONNET_MODEL", False),
    ("ANTHROPIC_DEFAULT_HAIKU_MODEL", False),
    ("ANTHROPIC_DEFAULT_OPUS_MODEL", False),
    ("ANTHROPIC_SMALL_FAST_MODEL", False),
    ("ANTHROPIC_BEDROCK_BASE_URL", False),
    ("ANTHROPIC_VERTEX_BASE_URL", False),
    ("ANTHROPIC_FOUNDRY_BASE_URL", False),
    ("CLAUDE_CODE_SKIP_BEDROCK_AUTH", False),
    ("CLAUDE_CODE_SKIP_VERTEX_AUTH", False),
    ("CLAUDE_CODE_SKIP_FOUNDRY_AUTH", False),
    ("ANTHROPIC_CUSTOM_HEADERS", True),
    ("AWS_CONFIG_FILE", False),
    ("AWS_SHARED_CREDENTIALS_FILE", False),
    (
        "CLAUDE_CONFIG_DIR",
        False,
    ),  # where a non-default login lives: children look under HOME otherwise
    ("NO_PROXY", False),
    ("no_proxy", False),
    ("NODE_EXTRA_CA_CERTS", False),
    ("SSL_CERT_FILE", False),
    ("SSL_CERT_DIR", False),
)


def tilde(path):
    home = os.path.expanduser("~")
    text = str(path)
    if text == home or text.startswith(home + os.sep):
        return "~" + text[len(home) :]
    return text


PROJECT_FOLDER = re.compile(r"(/projects/)[^/]+")


def shown(path):
    """A path for the output: the home directory as ~, and the project folder (its name spells the working directory path,
    user name included) as <project>, so the output stays safe to paste."""
    return PROJECT_FOLDER.sub(r"\1<project>", tilde(path))


CUSTOM_PATH = "<custom path>"


def _is_default_claude_home():
    """True only for ~/.claude itself: a config directory chosen with CLAUDE_CONFIG_DIR can sit under a client or employer folder."""
    default = os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.abspath(locate.claude_home()) == os.path.abspath(default)


def memory_label(path):
    """The memory directory for the output: the standard ~/.claude/projects/<project>/memory is shown with ~ and <project>;
    any other path can spell a user, client or employer name, so it is shown as <custom path>."""
    parent, leaf = os.path.split(os.path.abspath(path))
    standard = _is_default_claude_home() and leaf == "memory" and os.path.dirname(parent) == locate.projects_root()
    label = shown(path) if standard else CUSTOM_PATH
    return label if label.startswith("~") else CUSTOM_PATH


def data_root_label(path):
    """The data root for the output: the standard ~/.claude/memory-arborist with ~, anything else as <custom path>."""
    standard = _is_default_claude_home() and os.path.abspath(path) == os.path.join(locate.claude_home(), "memory-arborist")
    label = tilde(path) if standard else CUSTOM_PATH
    return label if label.startswith("~") else CUSTOM_PATH


def scrub(text):
    """Text from a child process with every home directory path shortened to ~ and cloud account ids and role ARNs replaced
    (an error line from a Bedrock or Vertex login can carry them, and this output is pasted into public issues)."""
    text = str(text)
    home = os.path.expanduser("~")
    if home and home != "/":
        text = re.sub(re.escape(home) + r"(?=/|$)", "~", text)
    return mrr.ARN.sub("<arn>", mrr.ACCOUNT_ID.sub("<account-id>", text))


# An absolute path of two or more parts that is not already shortened to ~ . A single word after a slash (`/login`, `/help`) is a
# slash command, not a folder; a URL, a ~ path and a relative path are left alone.
ABSOLUTE_PATH = re.compile(r"(?<![\w~:/.])/[^\s/:;,'\"()<>\[\]]+(?:/[^\s:;,'\"()<>\[\]]+)+")


def scrub_paths(text):
    """scrub(), then every absolute path outside the home directory as <path>: a child's error line can name a client folder."""
    return ABSOLUTE_PATH.sub("<path>", scrub(text))


def scrub_child(text, limit=160):
    """One line a child process printed, made safe to paste: paths and account ids replaced, a line that looks like a secret withheld."""
    return mr.sanitize(scrub_paths(mc.clean_line(text, 400)), limit).excerpt or mrr.WITHHELD_FAILURE_LINE


def plugin_info():
    """(name, version) from the plugin manifest next to the engine, or ('memory-arborist', 'unknown')."""
    manifest = os.path.join(os.path.dirname(SCRIPTS), ".claude-plugin", "plugin.json")
    try:
        with open(manifest, encoding="utf-8") as fh:
            data = json.load(fh)
        return str(data.get("name") or "memory-arborist"), str(
            data.get("version") or "unknown"
        )
    except (OSError, ValueError):
        return "memory-arborist", "unknown"


def parse_version(text):
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(part) for part in match.groups()) if match else None


def dotted(version):
    return ".".join(str(part) for part in version)


def subprocess_probe(argv, env):
    """(exit code, stdout, stderr) of a short read-only command; 127 when it cannot start, 124 on a timeout."""
    try:
        proc = subprocess.run(
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    except OSError as err:
        return 127, "", err.strerror or type(err).__name__
    return proc.returncode, mrr.decode_output(proc.stdout), mrr.decode_output(proc.stderr)


def _no_answer(code, err, command, default):
    """Why a probe of the claude binary failed, in words: a timeout or a failure to start is not an exit code of the program."""
    if code == 124:
        return "%s did not answer within %d seconds" % (command, PROBE_TIMEOUT_SECONDS)
    if code == 127:
        return "%s could not be started (%s)" % (command, scrub_child(err, 120))
    return default


def check_python(version_info=None):
    current = tuple(version_info or sys.version_info[:3])
    text = "Python " + dotted(current[:3])
    if current[:2] >= PYTHON_MIN:
        return Check(OK, "python", text, "")
    return Check(
        FAIL,
        "python",
        text + ", needs %d.%d or newer" % PYTHON_MIN,
        "run the scripts with a newer python3",
    )


def check_claude(probe, environ):
    """The claude binary on the PATH the children get, its version against the two floors, and the flags the engine passes."""
    env = mrr.child_env(environ)
    found = shutil.which("claude", path=env.get("PATH", ""))
    if not found:
        return [
            Check(
                FAIL,
                "claude",
                "not found on the PATH the children get (%d entries)"
                % len([p for p in env.get("PATH", "").split(os.pathsep) if p]),
                "install Claude Code, or start this from a shell whose PATH includes it",
            )
        ]
    code, out, err = probe([found, "--version"], env)
    version = parse_version(out) if code == 0 else None
    if version is None:
        return [
            Check(
                FAIL,
                "claude",
                "found, but "
                + _no_answer(code, err, "`claude --version`", "`claude --version` gave no version (exit %d)" % code),
                "run `claude --version` yourself and see what it says",
            )
        ]
    checks = [_version_check(version)]
    code, helptext, err = probe([found, "--help"], env)
    if code != 0:
        return checks + [
            Check(
                WARN,
                "flags",
                _no_answer(code, err, "`claude --help`", "`claude --help` failed (exit %d)" % code)
                + ", so the flags could not be checked",
                "run `claude --help` yourself; if it works there, run the doctor again from the same shell",
            )
        ]
    missing = [flag for flag in REQUIRED_FLAGS if not _listed(flag, helptext)]
    if missing:
        return checks + [
            Check(
                WARN,
                "flags",
                "not listed by `claude --help`: " + ", ".join(missing),
                "a hidden flag may still work; the first paid call (or --live) shows it",
            )
        ]
    return checks + [
        Check(
            OK,
            "flags",
            "all %d flags the engine passes are listed" % len(REQUIRED_FLAGS),
            "",
        )
    ]


def _listed(flag, helptext):
    """True when `claude --help` shows the flag, either as is or in the shorthand `--append-system-prompt[-file]`."""
    return flag in helptext or (flag.endswith("-file") and flag[: -len("-file")] + "[-file]" in helptext)


def _version_check(version):
    text = "claude " + dotted(version)
    if version < HOOK_MIN:
        return Check(
            FAIL,
            "claude",
            text
            + ", the session-start hook needs %s and the model-backed steps %s"
            % (dotted(HOOK_MIN), dotted(MODEL_MIN)),
            "update Claude Code",
        )
    if version < MODEL_MIN:
        return Check(
            WARN,
            "claude",
            text + ", the model-backed steps (--safe-mode) need %s" % dotted(MODEL_MIN),
            "update Claude Code to use the paid half",
        )
    return Check(OK, "claude", text, "")


def check_login_env(environ):
    """Login-route variables that are set here but not passed to the children, with the exact line to pass them."""
    passed = mrr.child_env(environ)
    unpassed = [
        (name, secret)
        for name, secret in LOGIN_ENV
        if environ.get(name) and name not in passed
    ]
    listed = [name for name in passed if name not in mrr.ALLOWED_ENV]
    base = "passed to the children: %s" % ", ".join(sorted(passed))
    if not unpassed:
        return [Check(OK, "env", base, "")]
    names = ",".join(name for name, _secret in unpassed)
    secrets = [name for name, secret in unpassed if secret]
    detail = "set here but not passed to the children: %s" % ", ".join(
        name for name, _s in unpassed
    )
    fix = (
        "if the first paid call cannot log in, or answers with a different model than you expect, set %s=%s in the environment that starts these scripts"
        % (
            mrr.ENV_PASS,
            ",".join(sorted(set(listed) | set(n for n, _s in unpassed))) or names,
        )
    )
    if secrets:
        fix += (
            "; the values of %s are secrets (or can carry one) and go to the child and on to the model provider, so list only what this session needs"
            % ", ".join(secrets)
        )
    return [Check(INFO, "env", base, ""), Check(WARN, "env", detail, fix)]


def _count_notes(memory_dir):
    names = [
        n for n in os.listdir(memory_dir) if n.endswith(".md") and n != "MEMORY.md"
    ]
    index = os.path.join(memory_dir, "MEMORY.md")
    lines = len(mc.read_text(index).splitlines()) if os.path.isfile(index) else 0
    return len(names), lines


def _guarded(name, function, *args):
    """Run one check as a list of Checks. A file or folder that cannot be read makes THIS check a FAIL; it never hides the others."""
    try:
        result = function(*args)
    except (OSError, ValueError, ArithmeticError, RecursionError, mc.HygieneError) as err:
        reason = getattr(err, "strerror", None) or mc.clean_line(err, 160) or type(err).__name__
        return [Check(FAIL, name, "could not read: %s" % scrub_paths(reason), "check the permissions of that file or folder")]
    return result if isinstance(result, list) else [result]


def _memory_check(memory_dir):
    text = memory_label(memory_dir)
    notes, lines = _count_notes(memory_dir)
    return Check(
        OK,
        "memory",
        "%s: %d notes, %d index lines (Claude Code reads the first %d)" % (text, notes, lines, mrr.INDEX_MAX_LINES),
        "",
    )


def check_dirs(memory_dir, ops_root, ops):
    if not os.path.isdir(memory_dir):
        return [
            Check(
                FAIL,
                "memory",
                "directory not found: " + memory_label(memory_dir),
                "run this from the project whose memory you want to check, or pass --dir (or set MEMORY_ARBORIST_MEMORY_DIR); Claude Code creates the folder on its first memory write",
            )
        ]
    return _guarded("memory", _memory_check, memory_dir) + _guarded("data-root", _data_root_check, memory_dir, ops_root, ops)


def _data_root_check(memory_dir, ops_root, ops):
    if mc.is_within(memory_dir, ops):
        return Check(
            FAIL,
            "data-root",
            "the data root lies inside the memory directory, which every command refuses",
            "use --ops-root (or MEMORY_ARBORIST_HOME) on a folder outside the notes, on the same disk",
        )
    probe_path = _nearest_existing(ops_root)
    problem = _data_root_problem(probe_path)
    if problem:
        return problem
    label = data_root_label(ops_root)
    state = "exists" if os.path.isdir(ops) else "is created by the first snapshot"
    if os.stat(_nearest_existing(memory_dir)).st_dev != os.stat(probe_path).st_dev:
        return Check(
            WARN,
            "data-root",
            "%s %s, but it is on a different filesystem than the notes"
            % (label, state),
            "restore and promote archive a note with a rename that fails across filesystems; use --ops-root on the same disk as the notes",
        )
    return Check(
        OK,
        "data-root",
        "%s %s, on the same filesystem as the notes" % (label, state),
        "",
    )


def _nearest_existing(path):
    """The path itself or the closest folder above it that exists (a data root or a memory folder is created later)."""
    probe = os.path.abspath(path)
    while not os.path.exists(probe) and os.path.dirname(probe) != probe:
        probe = os.path.dirname(probe)
    return probe


def _data_root_problem(nearest):
    """A FAIL when the nearest existing folder above the data root cannot hold it: a file, read-only, or under the 2 GB the snapshot needs."""
    fix = "use --ops-root (or MEMORY_ARBORIST_HOME) on a writable folder outside the notes, on the same disk, with %.0f GB free" % (sn.MIN_FREE_BYTES / 1024**3)
    if not os.path.isdir(nearest):
        return Check(FAIL, "data-root", "the data folder cannot be created: a file is in the way", fix)
    if not os.access(nearest, os.W_OK | os.X_OK):
        return Check(FAIL, "data-root", "the data folder cannot be created: the nearest existing folder above it is not writable", fix)
    free = mc.free_bytes(nearest)
    if free < sn.MIN_FREE_BYTES:
        return Check(FAIL, "data-root", "only %.1f GB free where the data folder goes; the snapshot step refuses below %.0f GB" % (free / 1024**3, sn.MIN_FREE_BYTES / 1024**3), fix)
    return None


def _pinned_check(pinned, memory_dir):
    text = mc.read_text(pinned)
    text = text[1:] if text.startswith("\ufeff") else text  # a BOM added by an editor is not part of a line
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    index = os.path.join(memory_dir, "MEMORY.md") if memory_dir else ""
    if not lines or not os.path.isfile(index):
        return Check(OK, "pinned", "pinned.txt has %d protected index lines" % len(lines), "")
    current = {ln.strip() for ln in mc.read_text(index).split("\n")}
    matching = sum(1 for ln in lines if ln in current)
    if matching == 0:
        return Check(WARN, "pinned", "none of the %d pinned lines matches a line of MEMORY.md, so nothing is protected" % len(lines), "copy each line exactly as it appears in MEMORY.md; or move this pinned.txt aside and run memory_dream.py pin --all (it never replaces an existing file), or pin --none if you want no line protected")
    return Check(OK, "pinned", "pinned.txt has %d lines, %d of them match a line of MEMORY.md now" % (len(lines), matching), "")


def _pinned_state(ops, memory_dir):
    pinned, draft = os.path.join(ops, "pinned.txt"), os.path.join(ops, "pinned.txt.draft")
    if os.path.isfile(pinned):
        return _pinned_check(pinned, memory_dir)
    if os.path.isfile(draft):
        return Check(
            WARN,
            "pinned",
            "only pinned.txt.draft exists",
            "confirm it by copying it to pinned.txt, or pass --pinned-draft-ok to the dream run",
        )
    return Check(
        WARN,
        "pinned",
        "pinned.txt is missing: the dream run stops at once without it",
        "run memory_dream.py pin --none (protect no index line) or pin --all (protect every index line of the newest snapshot), then edit the file by hand if you like",
    )


# The loader's messages quote the line they refuse (a regex, a kind, an id); only these fixed phrases are ever shown.
QUESTION_PROBLEMS = (
    "not an object",
    "not valid JSON",
    "unknown kind",
    "bad regex",
    "duplicate id",
    "must needs at least one regex",
    "forbid must be a list of regexes",
    "a drift probe names its truth file",
)
UNKNOWN_PROBLEM = "a problem the doctor cannot describe safely (run the judge to read it)"


def question_problem(text):
    """'line N: <kind of problem>' from a question-loader message, without the text of the line it refused."""
    text = str(text)
    match = re.match(r"line (\d+): (.*)", text, re.S)
    if not match:
        return text if re.fullmatch(r"\d+ questions; the set needs \d+ to \d+", text) else UNKNOWN_PROBLEM
    number, rest = match.groups()
    named = re.fullmatch(r"missing (id|kind|question)", rest)
    found = named.group(0) if named else next((p for p in QUESTION_PROBLEMS if rest.startswith(p)), UNKNOWN_PROBLEM)
    return "line %s: %s" % (number, found)


QUESTIONS_FIX = "the question wizard proposes a set from your notes (about 1 USD); see Preconditions in the skill, or run recall/memory_recall_wizard.py propose after a snapshot"


def _questions_state(ops):
    """The question file, read with the judge's own loader: a count alone passes a file the judge refuses to load."""
    path = os.path.join(ops, "recall", "questions.jsonl")
    if not os.path.isfile(path):
        count = 0
    else:
        try:
            count = len(mrr.load_questions(path, require_count=False))
        except mc.HygieneError as err:
            return Check(WARN, "questions", "the question file does not load: %s" % question_problem(err), "fix that line, or let the wizard write a new set. " + QUESTIONS_FIX)
        except UnicodeDecodeError:
            return Check(WARN, "questions", "the question file is not valid UTF-8 text", "save it as plain UTF-8, or let the wizard write a new set. " + QUESTIONS_FIX)
    if mrr.MIN_QUESTIONS <= count <= mrr.MAX_QUESTIONS:
        return Check(OK, "questions", "%d recall questions" % count, "")
    return Check(
        WARN,
        "questions",
        "%d recall questions (the judge needs %d to %d)" % (count, mrr.MIN_QUESTIONS, mrr.MAX_QUESTIONS),
        QUESTIONS_FIX,
    )


def _evidence_state(ops):
    evidence = os.path.join(ops, "evidence")
    digests = len([n for n in os.listdir(evidence) if n.endswith(".md")]) if os.path.isdir(evidence) else 0
    return Check(OK if digests else INFO, "evidence", "%d evidence digests" % digests, "")


def _snapshots_state(ops):
    snapshots = len(sn.list_snapshots(ops)) if os.path.isdir(ops) else 0
    return Check(OK if snapshots else INFO, "snapshots", "%d snapshots" % snapshots, "")


def check_first_run(ops, memory_dir=None):
    """The files a first model-backed run needs: pinned index lines, recall questions, evidence digests, a snapshot.
    Each is read on its own, so one unreadable file is one FAIL and the others still report."""
    return (
        _guarded("pinned", _pinned_state, ops, memory_dir)
        + _guarded("questions", _questions_state, ops)
        + _guarded("evidence", _evidence_state, ops)
        + _guarded("snapshots", _snapshots_state, ops)
    )


PROBE_SYSTEM = "Answer with exactly one JSON value and nothing else."


def check_live(runner, model):
    """ONE real call with the engine's exact lane invocation: proves the login, the model alias and the output format."""
    seen = []

    def tap(argv, cwd, env, stdin_text):
        result = runner(argv, cwd, env, stdin_text)
        seen.append(result[1])
        return result

    spend = mrr.Spend(LIVE_PROBE_USD)
    with tempfile.TemporaryDirectory(prefix="doctor_") as work:
        system_file = os.path.join(work, "system.txt")
        fd = os.open(system_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(PROBE_SYSTEM)
        try:
            lanes.ask_lane(
                'LANE:probe\nAnswer with {"ok": true}.',
                system_file,
                lanes.Settings(model, LIVE_PROBE_USD),
                tap,
                spend,
                work,
            )
        except (mrr.AskError, mrr.RunAborted) as err:  # a reported tool stops the run: say so, do not crash
            return [
                Check(
                    FAIL,
                    "live",
                    "the call failed: %s" % scrub_paths(mc.clean_line(err, 300)),
                    "fix the cause above, then run --live again",
                )
            ]
    shape = _shape(seen[0] if seen else "")
    checks = [
        Check(
            OK,
            "live",
            "one call answered (model %s, %s, tools reported: none, cost about %.4f USD)"
            % (scrub(model), shape, spend.total),
            "",
        )
    ]
    if spend.total > LIVE_PROBE_USD:
        checks.append(
            Check(
                WARN,
                "live",
                "the call cost %.4f USD, more than the %.2f USD cap passed to it" % (spend.total, LIVE_PROBE_USD),
                "the paid steps also stop at their own total ceiling, but check what your login route and model really charge before approving them (try --model with a smaller model)",
            )
        )
    return checks


def _shape(stdout):
    try:
        data = json.loads(stdout)
    except ValueError:
        return "output not JSON"
    return (
        "output is a list of %d events" % len(data)
        if isinstance(data, list)
        else "output is one object"
    )


def render(checks):
    lines = []
    for check in checks:
        lines.append(
            "%-4s %-10s %s" % (check.status, check.name, mc.printable(check.detail))
        )
        if check.fix and check.status in (WARN, FAIL):
            lines.append("     fix: " + mc.printable(check.fix))
    counts = {
        status: sum(1 for c in checks if c.status == status)
        for status in (OK, INFO, WARN, FAIL)
    }
    lines.append(
        "DOCTOR ok=%d warn=%d fail=%d info=%d"
        % (counts[OK], counts[WARN], counts[FAIL], counts[INFO])
    )
    return "\n".join(lines)


def run_checks(args, environ, probe, runner):
    memory_dir = mc.expand(args.dir) if args.dir else mc.DEFAULT_MEMORY_DIR
    ops_root = mc.expand(args.ops_root) if args.ops_root else mc.DEFAULT_OPS_ROOT
    ops = mc.ops_dir_for(memory_dir, ops_root)
    name, version = plugin_info()
    checks = [Check(INFO, "plugin", "%s %s" % (name, version), ""), check_python()]
    checks += check_claude(probe, environ) + check_login_env(environ)
    checks += _guarded("files", check_dirs, memory_dir, ops_root, ops)
    has_memory = os.path.isdir(memory_dir)
    if not has_memory:  # the data root and the first-run files only need the data root
        checks += _guarded("data-root", _data_root_check, memory_dir, ops_root, ops)
    checks += _guarded("files", check_first_run, ops, memory_dir if has_memory else None)
    if args.live:
        print(mrr.estimate_line("doctor probe", 1, LIVE_PROBE_USD, LIVE_PROBE_USD))
        lane_model = args.model or environ.get(ENV_LANE_MODEL) or "sonnet"
        checks += check_live(runner, lane_model)
        checks.append(_judge_model_note(environ))
    return checks


def _judge_model_note(environ):
    """--live proves the lane model only; the recall judge defaults to a different model, so say which one was not tried."""
    judge = environ.get(mrr.ENV_JUDGE_MODEL) or mrr.DEFAULT_MODEL
    return Check(
        INFO,
        "live",
        "the recall judge uses model %s (set %s to change it), which this call did not try; a login route can serve one model alias and refuse another"
        % (scrub(judge), mrr.ENV_JUDGE_MODEL),
        "",
    )


def build_parser():
    parser = argparse.ArgumentParser(
        prog="memory_doctor.py", description=__doc__.split("\n")[0], allow_abbrev=False
    )
    parser.add_argument(
        "--dir", default=None, help="memory directory (default: this project's)"
    )
    parser.add_argument(
        "--ops-root",
        default=None,
        help="data root (default: <Claude config dir>/memory-arborist)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="make ONE real capped call (at most 0.05 USD) to prove the login and the output format",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="model for --live (default: %s or sonnet)" % ENV_LANE_MODEL,
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="print the plugin name and version, then exit",
    )
    return parser


def main(argv=None, environ=None, probe=None, runner=None):
    mc.safe_stdio()
    args = build_parser().parse_args(argv)
    if args.version:
        print("%s %s" % plugin_info())
        return 0
    checks = run_checks(
        args,
        os.environ if environ is None else environ,
        probe or subprocess_probe,
        runner or lanes.subprocess_runner,
    )
    print(render(checks))
    return 1 if any(check.status == FAIL for check in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
