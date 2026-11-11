"""Find the Claude Code auto-memory directory for a working directory, and the default places this toolkit keeps its own data.

Standard library only; read-only (reads directory entries and one small `.git` file, never writes). It is a leaf module that
every entry point imports, including the session-start hook, so nothing here may raise at import time.

Public sources (docs/PROVENANCE.md lists them all): the auto-memory directory is ~/.claude/projects/<project>/memory/ and
<project> is derived from the git repository, so all worktrees and subdirectories of one repository share one directory
(https://code.claude.com/docs/en/memory); <project> is the working directory path with non-alphanumeric characters replaced by
'-', and a name over 200 characters is truncated with a hash appended (https://code.claude.com/docs/en/sessions).
Observed, not documented: how non-ASCII characters are counted (each UTF-16 code unit that is not an ASCII letter or digit
becomes one '-'), and that a linked worktree uses the main work tree's path. Known limits: a name over 200 characters is not
found here; the autoMemoryDirectory setting is not read (settings can hold secrets). CLAUDE_CONFIG_DIR, documented on
the sessions page, is followed (MEMORY_SHEPHERD_CLAUDE_HOME overrides it), and this tool's own data root moves with it. The
headless children do not receive CLAUDE_CONFIG_DIR. CLAUDE_CODE_PROJECT_DIR_NAME, which names the project directory when
CLAUDE_CONFIG_DIR is also set, is not read: point MEMORY_SHEPHERD_MEMORY_DIR at that directory instead.

Environment (all optional):
    MEMORY_SHEPHERD_MEMORY_DIR   memory directory to work on, overriding detection
    MEMORY_SHEPHERD_HOME         where backups, logs, digests and baselines live (default <claude home>/memory-shepherd)
    MEMORY_SHEPHERD_CLAUDE_HOME  the Claude Code configuration directory (default CLAUDE_CONFIG_DIR, else ~/.claude)
"""

import os

ENV_MEMORY_DIR = "MEMORY_SHEPHERD_MEMORY_DIR"
ENV_HOME = "MEMORY_SHEPHERD_HOME"
ENV_CLAUDE_HOME = "MEMORY_SHEPHERD_CLAUDE_HOME"
ENV_CLAUDE_CONFIG = "CLAUDE_CONFIG_DIR"  # documented by Claude Code (sessions page): it moves every ~/.claude path
MAX_SLUG_CHARS = 200
MAX_WALK_LEVELS = 64
MAX_GIT_FILE_BYTES = 4096
SOFT_ERRORS = (OSError, ValueError, KeyError, RecursionError)


def _expand(path):
    expanded = os.path.expanduser(path)
    try:
        return os.path.abspath(expanded)
    except OSError:  # a relative path while the working directory has been deleted
        return expanded


def claude_home(environ=None):
    """The Claude Code configuration directory: this tool's override, then Claude Code's own CLAUDE_CONFIG_DIR, then ~/.claude."""
    env = os.environ if environ is None else environ
    return _expand(env.get(ENV_CLAUDE_HOME) or env.get(ENV_CLAUDE_CONFIG) or "~/.claude")


def projects_root(environ=None):
    return os.path.join(claude_home(environ), "projects")


def default_ops_root(environ=None):
    """Where this toolkit keeps its own data. Outside any plugin folder, so it survives an uninstall."""
    env = os.environ if environ is None else environ
    return _expand(
        env.get(ENV_HOME) or os.path.join(claude_home(environ), "memory-shepherd")
    )


def _project_slug(cwd):
    """Claude Code's directory name for a path: each UTF-16 code unit that is not an ASCII letter or digit becomes '-'."""
    return "".join(
        c if (c.isascii() and c.isalnum()) else "-" * (len(c.encode("utf-16-le")) // 2)
        for c in cwd
    )


def _read_small(path, limit):
    """Up to `limit` bytes of a small file, or b"" when it cannot be read. Never follows a symlink, never blocks."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as fh:
            return fh.read(limit) or b""
    except OSError:
        return b""


def _linked_main_root(work_tree, git_file):
    """Main work tree of a linked worktree whose .git file reads 'gitdir: <main>/.git/worktrees/<name>'; None for anything else."""
    first_line = _read_small(git_file, MAX_GIT_FILE_BYTES).split(b"\n", 1)[0]
    text = first_line.decode("utf-8", errors="replace")
    if not text.startswith("gitdir:"):
        return None
    gitdir = os.path.normpath(os.path.join(work_tree, text[len("gitdir:") :].strip()))
    marker = os.sep + ".git" + os.sep + "worktrees" + os.sep
    at = gitdir.find(marker)
    return gitdir[:at] if at > 0 else None


def _repo_root(cwd):
    """The git work tree that holds cwd (the main work tree for a linked worktree), or None outside a repository."""
    current = cwd
    for _ in range(MAX_WALK_LEVELS):
        marker = os.path.join(current, ".git")
        if os.path.isdir(marker):
            return current
        if os.path.isfile(marker):
            return _linked_main_root(current, marker) or current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent
    return None


def _memory_dir_of(path, root):
    try:
        slug = _project_slug(path)
        if len(slug) > MAX_SLUG_CHARS:
            return None
        candidate = os.path.join(root, slug, "memory")
        return candidate if os.path.isdir(candidate) else None
    except SOFT_ERRORS:
        return None


def memory_dir_for_cwd(cwd, root=None):
    """The existing project memory directory for a working directory, or None.

    The repository root is tried first (Claude Code shares one directory across a repository), then the working directory
    itself (a folder that is not in a repository).
    """
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        return None
    root = projects_root() if root is None else root
    repo = _repo_root(cwd)
    for path in ([repo] if repo else []) + [cwd]:
        found = _memory_dir_of(path, root)
        if found:
            return found
    return None


def expected_memory_dir(cwd, root=None):
    """Where Claude Code would keep the memory for cwd even if it does not exist yet (so an error can name it)."""
    root = projects_root() if root is None else root
    base = _repo_root(cwd) or cwd
    return os.path.join(root, _project_slug(base), "memory")


def default_memory_dir(cwd=None, environ=None):
    """The memory directory to work on when none is given. Never raises."""
    env = os.environ if environ is None else environ
    explicit = env.get(ENV_MEMORY_DIR)
    if explicit:
        return _expand(explicit)
    root = projects_root(environ)
    try:
        where = os.getcwd() if cwd is None else cwd
        return memory_dir_for_cwd(where, root) or expected_memory_dir(where, root)
    except SOFT_ERRORS:
        return os.path.join(root, "unknown-project", "memory")
