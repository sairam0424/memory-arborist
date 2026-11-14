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
SEGMENT = r"[^|;&\n]*"  # both words must sit in the same shell command, not in two commands of one line
GATED = (
    re.compile(
        r"memory_dream\.py\b" + SEGMENT + r"\bpromote\b" + SEGMENT + r"--apply\b"
    ),
    re.compile(
        r"memory_hygiene\.py\b" + SEGMENT + r"\brestore\b" + SEGMENT + r"--apply\b"
    ),
    re.compile(r"\bmemory_[a-z_]+\.py\b" + SEGMENT + r"--allow-secret-shapes\b"),
)
SOFT_ERRORS = (OSError, ValueError, KeyError, RecursionError)


def needs_confirmation(command):
    return isinstance(command, str) and any(rx.search(command) for rx in GATED)


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
