"""PreToolUse hook: makes Claude Code ask the person before a memory-arborist command that changes the live notes.

It recognises the plain commands the skill runs: `memory_dream.py ... promote ... --apply`, `memory_hygiene.py ... restore ...
--apply`, and any memory_*.py command given `--allow-secret-shapes` (which sends notes the secret scan refused). For those it
answers {"permissionDecision": "ask"}; for everything else it prints nothing. It never denies, never edits, never reads a file
and always exits 0, so a problem in the hook can never block a session.

What this is and is not: Claude Code shows the prompt in its default and accept-edits permission modes and an "ask" beats a
settings allow rule; in bypassPermissions mode, which skips prompts, and in auto mode, where a classifier answers, it cannot
force a person to look. It also matches the command text, so a command assembled at run time (eval, a variable holding the
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
STDIN_WAIT_SECONDS = 1.0
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


def logical_commands(command):
    """The commands of a shell line, split at | ; & and newline that are outside quotes, $( ) and backticks.

    A backslash-newline is a continuation, not a separator. This is a text scan, not a shell parser: it is meant to keep one
    command's words together and two commands' words apart, in the forms a model writes."""
    text = CONTINUATION.sub(" ", command)
    commands, current = [], []
    quote, depth, backtick, i = "", 0, False, 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and quote != "'" and i + 1 < len(text):
            current.append(text[i : i + 2])
            i += 2
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
        if ch in SEPARATORS and not quote and not depth and not backtick and not (ch == "&" and current and current[-1][-1:] in "<>"):
            commands.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
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


def decision(stdin_text):
    """The JSON line to print, or '' when the hook has nothing to say."""
    try:
        payload = json.loads(stdin_text[:MAX_STDIN_CHARS])
    except SOFT_ERRORS:
        return ""
    if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        return ""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict) or not needs_confirmation(
        tool_input.get("command")
    ):
        return ""
    answer = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": REASON,
        }
    }
    return json.dumps(answer) + "\n"


def _read_stdin(limit=MAX_STDIN_CHARS, wait=STDIN_WAIT_SECONDS):
    """Whatever the host wrote to stdin within the deadline; empty on a closed or silent stdin."""
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
        return ""
    return b"".join(chunks).decode("utf-8", errors="replace")


def main():
    try:
        out = decision(_read_stdin())
        if out:
            os.write(1, out.encode("utf-8"))
    except Exception:  # noqa: BLE001 - the hook contract is silence and exit 0, whatever happens
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
