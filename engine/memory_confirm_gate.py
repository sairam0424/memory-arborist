"""PreToolUse hook: makes Claude Code ask the person before a memory-arborist command that changes the live notes.

It recognises the plain commands the skill runs: `memory_dream.py ... promote ... --apply`, `memory_hygiene.py ... restore ...
--apply`, and any memory_*.py command given `--allow-secret-shapes` (which sends notes the secret scan refused). For those it
answers {"permissionDecision": "ask"}; for everything else it prints nothing. It never denies, never edits, never reads a file
and always exits 0, so a problem in the hook can never block a session.

What this is and is not: Claude Code shows the prompt in its default and accept-edits permission modes and an "ask" beats a
settings allow rule, and its documentation says auto mode normally shows it too (with exceptions); a headless run counts an ask as a refusal (and one was still
enforced under bypassPermissions in a test on 2.1.325; an interactive bypass session is untested), and the disableAllHooks
setting switches the hook off, so it cannot force a person to look. It also matches the command text, so a command assembled at run time (eval, a variable holding the
script name) is not recognised: it is a seat belt against a slip, not a sandbox against an attacker.

    hook:  python3 -B -I ${CLAUDE_PLUGIN_ROOT}/engine/memory_confirm_gate.py     (reads the PreToolUse JSON on stdin)
"""

import json
import os
import re
import select
import sys
import time

sys.dont_write_bytecode = True  # a hook writes nothing, not even bytecode

MAX_STDIN_CHARS = 1 << 20
STDIN_WAIT_SECONDS = 3.5  # the host's hook timeout is 5 s; a slow writer must not get a free pass
REASON = (
    "memory-arborist is about to change your live memory notes, or to send notes that hold secret-shaped text. "
    "Go ahead only if you reviewed the diff and the digest it shows."
)
CONTINUATION = re.compile(r"\\\r?\n")  # a backslash at the end of a line continues the command
SEPARATORS = "|;&\n"
SCRIPT = re.compile(r"\bmemory_[a-z_]+\.py\b")
DREAM, HYGIENE = re.compile(r"\bmemory_dream\.py\b"), re.compile(r"\bmemory_hygiene\.py\b")
PROMOTE, RESTORE = re.compile(r"\bpromote\b"), re.compile(r"\brestore\b")
APPLY, ALLOW = re.compile(r"--apply\b"), re.compile(r"--allow-secret-shapes\b")
SOFT_ERRORS = (OSError, ValueError, KeyError, RecursionError)


def _splits(ch, text, i, prev):
    """True when this | ; & or newline is a command separator and not part of a redirection (&> >& <& >|)."""
    if ch not in SEPARATORS:
        return False
    if ch == "&" and ((prev and prev in "<>") or text[i + 1 : i + 2] == ">"):
        return False
    return not (ch == "|" and prev == ">")


def logical_commands(command):
    """The commands of a shell line, split at | ; & and newline that are outside quotes, $( ), ${ } and backticks.

    A backslash-newline is removed (bash joins the lines, even inside a word), a comment is dropped, and redirections such as
    &> and >| do not split. This is a text scan, not a shell parser, so it falls back to judging the WHOLE text as one command
    whenever it cannot trust itself (a here-document, or a quote, bracket or brace left open): a false prompt is the safe
    direction, a missed one is not."""
    text = CONTINUATION.sub("", command)
    if "<<" in text:
        return [text]
    commands, current = [], []
    quote, depth, braces, backtick, i = "", 0, 0, False, 0
    while i < len(text):
        ch = text[i]
        prev = current[-1][-1:] if current else ""
        if ch == "\\" and quote != "'" and i + 1 < len(text):
            current.append(text[i : i + 2])
            i += 2
            continue
        if ch == "#" and not quote and (not prev or prev in " \t\n;|&("):
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
            continue
        if ch in "'\"" and quote in ("", ch):
            quote = "" if quote else ch
        elif not quote:
            if ch == "`":
                backtick = not backtick
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth = max(0, depth - 1)
            elif ch == "{" and prev == "$":
                braces += 1
            elif ch == "}" and braces:
                braces -= 1
        if _splits(ch, text, i, prev) and not (quote or depth or braces or backtick):
            commands.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    if quote or depth or braces or backtick:
        return [text]
    commands.append("".join(current))
    return commands


def _gated(one):
    return bool(
        (DREAM.search(one) and PROMOTE.search(one) and APPLY.search(one))
        or (HYGIENE.search(one) and RESTORE.search(one) and APPLY.search(one))
        or (SCRIPT.search(one) and ALLOW.search(one))
    )


def needs_confirmation(command):
    return isinstance(command, str) and any(_gated(one) for one in logical_commands(command))


def decision(stdin_text, cut=False):
    """The JSON line to print, or '' when the hook has nothing to say.

    `cut` says the input filled the read limit, so the command may continue past what was read. The host only starts this hook for
    a command that already matches one of the plugin's gated shapes, so a payload this long is asked about rather than waved through."""
    if cut:
        return _ask()
    try:
        payload = json.loads(stdin_text)
    except SOFT_ERRORS:
        # unreadable input: judge the raw text rather than let a gated command through
        return _ask() if _gated(stdin_text) else ""
    if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        return ""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict) or not needs_confirmation(
        tool_input.get("command")
    ):
        return ""
    return _ask()


def _ask():
    answer = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask", "permissionDecisionReason": REASON}}
    return json.dumps(answer) + "\n"


def _read_stdin(limit=MAX_STDIN_CHARS, wait=STDIN_WAIT_SECONDS):
    """(text, cut): whatever the host wrote to stdin within the deadline, and whether it filled the limit (so there may be more)."""
    chunks, total = [], 0
    deadline = time.monotonic() + wait
    try:
        while total < limit:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([0], [], [], remaining)[0]:
                break
            chunk = os.read(0, limit - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    except (OSError, ValueError):
        return "", False
    return b"".join(chunks).decode("utf-8", errors="replace"), total >= limit


def main():
    try:
        out = decision(*_read_stdin())
        if out:
            os.write(1, out.encode("utf-8"))
    except Exception:  # noqa: BLE001 - the hook contract is silence and exit 0, whatever happens
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
