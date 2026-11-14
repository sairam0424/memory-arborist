"""Session-start nudge: one line when the memory index is close to its limits, silence otherwise. Standard library only.

Meant for a Claude Code SessionStart hook, whose stdout is added to the model's context. Because of that the output is a
fixed two-sentence message with four numbers filled in (no index text, no paths) that informs the model and tells it not to act unless the
user asks. It is empty unless the index is within about 1,500 characters or 20 lines of lint's own warning levels. Every
failure is silent: this program always exits 0, writes no file (not even bytecode) and never waits more than a second for
stdin. Known limits are those of memory_locate (a project directory name over 200 characters, the autoMemoryDirectory
setting and CLAUDE_CODE_PROJECT_DIR_NAME are not read; CLAUDE_CONFIG_DIR is followed), and MEMORY_ARBORIST_MEMORY_DIR is ignored here (the hook follows the session's working directory); the thresholds are this tool's own, not Claude Code's.

    hook:    python3 -B -I ${CLAUDE_PLUGIN_ROOT}/engine/memory_headroom_nudge.py     (reads the hook JSON on stdin; uses its "cwd")
    manual:  python3 -B -I engine/memory_headroom_nudge.py --dir <memory dir>
"""

import json
import os
import select
import sys
import time

sys.dont_write_bytecode = True  # read-only also means no .pyc files
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import memory_lint as ml  # noqa: E402
    import memory_locate as loc  # noqa: E402
except Exception:  # noqa: BLE001 - a hook must stay silent whatever state the sibling modules are in
    ml = None
    loc = None

INDEX_NAME = "MEMORY.md"
NUDGE_CHARS, NUDGE_LINES = 18_500, 140  # lint warns at 20,000 characters / 160 lines
MAX_READ_BYTES = 1 << 20
MAX_STDIN_CHARS = 65_536
STDIN_WAIT_SECONDS = 1.0
SOFT_ERRORS = (OSError, ValueError, KeyError, RecursionError)


def _project_slug(path):
    """Claude Code's directory name for a path (see memory_locate)."""
    return loc._project_slug(path)


def memory_dir_for_cwd(cwd, projects_root=None):
    """The existing project memory directory for a working directory, or None (see memory_locate)."""
    return loc.memory_dir_for_cwd(cwd, projects_root) if loc is not None else None


def _read_index(memory_dir):
    """(text, truncated) of the index. Never follows a symlink; O_NONBLOCK keeps a FIFO from blocking (an idle one reads as empty)."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    fd = os.open(os.path.join(memory_dir, INDEX_NAME), flags)
    with os.fdopen(fd, "rb") as fh:
        data = fh.read(MAX_READ_BYTES + 1) or b""
    return data[:MAX_READ_BYTES].decode("utf-8", errors="replace"), len(
        data
    ) > MAX_READ_BYTES


def nudge_line(memory_dir):
    """The one-line message, or None when the index is comfortable or cannot be read."""
    if ml is None:
        return None
    try:
        text, truncated = _read_index(memory_dir)
        stats = ml.index_stats(text)
    except SOFT_ERRORS:
        return None
    chars, lines = int(stats["chars"]), int(stats["eff_lines"])
    if chars < NUDGE_CHARS and lines < NUDGE_LINES:
        return None
    return (
        "Memory index is %s%d characters and %d lines (Claude Code's documented read limit is 200 lines or 25KB; lint warns at %d characters and %d lines). "
        "Tell the user it is getting full and suggest the memory-arborist:tend skill, memory_hygiene.py lint or hand-trimming; "
        "do not run any of them unless the user asks."
        % (
            "at least " if truncated else "",
            chars,
            lines,
            ml.INDEX_WARN_CHARS,
            ml.INDEX_WARN_LINES,
        )
    )


def _cwd_from_hook_json(stdin_text):
    try:
        payload = json.loads(stdin_text[:MAX_STDIN_CHARS])
    except (ValueError, RecursionError):
        return None
    return payload.get("cwd") if isinstance(payload, dict) else None


def _target_dir(argv, stdin_text, projects_root):
    if "--dir" in argv:
        at = argv.index("--dir") + 1
        return argv[at] if at < len(argv) else None
    return memory_dir_for_cwd(_cwd_from_hook_json(stdin_text), projects_root)


def run(argv, stdin_text="", projects_root=None):
    """(exit code, text to print). The exit code is always 0 so a hook problem can never block a session."""
    try:
        memory_dir = _target_dir(argv, stdin_text, projects_root)
        line = nudge_line(memory_dir) if memory_dir else None
    except SOFT_ERRORS:
        line = None
    return 0, (line + "\n") if line else ""


def _read_stdin(limit=MAX_STDIN_CHARS, wait=STDIN_WAIT_SECONDS):
    """Whatever the host wrote to stdin within the deadline (hook JSON is tiny); empty on a closed or silent stdin."""
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


def _emit(text):
    if not text:
        return
    try:
        os.write(1, text.encode("utf-8"))
    except OSError:
        pass  # a closed or broken stdout must not turn into a failing hook


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        stdin_text = "" if "--dir" in argv else _read_stdin()
        _code, out = run(argv, stdin_text)
    except Exception:  # noqa: BLE001 - the hook contract is silence and exit 0, whatever happens
        return 0
    _emit(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
