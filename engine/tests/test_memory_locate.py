"""Tests for memory_locate.py: where the memory directory and the toolkit's own data live by default. No machine-specific paths."""

import ast
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import memtest_helpers as th  # noqa: E402

sys.path.insert(0, th.SCRIPTS)
import memory_locate as loc  # noqa: E402

SOURCE = os.path.join(th.SCRIPTS, "memory_locate.py")


class TempRoot(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        saved, root = tempfile.tempdir, tempfile.mkdtemp(prefix="memtests_")
        tempfile.tempdir = root
        cls.addClassCleanup(shutil.rmtree, root, True)
        cls.addClassCleanup(setattr, tempfile, "tempdir", saved)


def repo_with_subdir():
    base = th.make_dir({})
    repo = os.path.join(base, "repo")
    os.makedirs(os.path.join(repo, ".git"))
    os.makedirs(os.path.join(repo, "src", "deep"))
    return repo


PROJECTS = "/projects-root"  # only ever joined into strings, never touched on disk
# These tests pin the current behaviour of this tool, not a documented Claude Code rule.
# Limits pinned on purpose (MAX_GIT_FILE_BYTES, MAX_WALK_LEVELS): changing either must mean changing these tests.
GIT_FILE_CAP = 4096
WALK_CAP = 64
GITDIR_PREFIX = b"gitdir: "
WORKTREES_MARKER = b"/.git/worktrees/"


def expected_for(path):
    """Where the memory of a repository (or plain folder) at `path` would live under PROJECTS."""
    return os.path.join(PROJECTS, loc._project_slug(path), "memory")


def make_work_tree():
    """<base>/work/src on disk and no .git yet. Returns (base, work, src)."""
    base = th.make_dir({})
    work = os.path.join(base, "work")
    src = os.path.join(work, "src")
    os.makedirs(src)
    return base, work, src


def write_git_file(work, content):
    with open(os.path.join(work, ".git"), "wb") as fh:
        fh.write(content)


def write_pointer(work, main):
    """Make `work` a linked worktree of `main` the way git does; `main` itself is never created."""
    pointer = os.path.join(main, ".git", "worktrees", "wt1")
    write_git_file(work, ("gitdir: %s\n" % pointer).encode("utf-8"))


def linked_line(main_chars):
    """('<main>', .git file content) where <main> is exactly main_chars characters long; nothing is created on disk."""
    main = "/" + "m" * (main_chars - 1)
    return main, GITDIR_PREFIX + main.encode("ascii") + WORKTREES_MARKER + b"wt1\n"


class HomeAndRootTests(TempRoot):
    def test_the_claude_home_defaults_to_dot_claude_and_can_be_overridden(self):
        self.assertEqual(loc.claude_home({}), os.path.expanduser("~/.claude"))
        self.assertEqual(loc.claude_home({loc.ENV_CLAUDE_HOME: "/opt/cc"}), "/opt/cc")
        self.assertEqual(
            loc.claude_home({loc.ENV_CLAUDE_HOME: ""}), os.path.expanduser("~/.claude")
        )

    def test_the_projects_root_sits_under_the_claude_home(self):
        self.assertEqual(
            loc.projects_root({loc.ENV_CLAUDE_HOME: "/opt/cc"}), "/opt/cc/projects"
        )

    def test_the_ops_root_defaults_next_to_the_claude_home_and_never_to_a_plugin_folder(
        self,
    ):
        self.assertEqual(
            loc.default_ops_root({loc.ENV_CLAUDE_HOME: "/opt/cc"}),
            "/opt/cc/memory-arborist",
        )
        self.assertEqual(
            loc.default_ops_root(
                {loc.ENV_HOME: "/data/ms", loc.ENV_CLAUDE_HOME: "/opt/cc"}
            ),
            "/data/ms",
        )
        self.assertEqual(
            loc.default_ops_root({loc.ENV_HOME: "~/ms"}), os.path.expanduser("~/ms")
        )
        self.assertEqual(
            loc.default_ops_root({loc.ENV_HOME: ""}),
            os.path.join(os.path.expanduser("~/.claude"), "memory-arborist"),
        )

    def test_relative_overrides_become_absolute(self):
        self.assertTrue(
            os.path.isabs(loc.default_ops_root({loc.ENV_HOME: "relative/ms"}))
        )
        self.assertTrue(
            os.path.isabs(
                loc.default_memory_dir(environ={loc.ENV_MEMORY_DIR: "relative/mem"})
            )
        )


class DefaultMemoryDirTests(TempRoot):
    def env_for(self, claude_home):
        return {loc.ENV_CLAUDE_HOME: claude_home}

    def test_the_explicit_environment_override_wins_over_everything(self):
        repo = repo_with_subdir()
        claude = th.make_dir({})
        os.makedirs(os.path.join(claude, "projects", loc._project_slug(repo), "memory"))
        env = {
            loc.ENV_MEMORY_DIR: "/somewhere/else/memory",
            loc.ENV_CLAUDE_HOME: claude,
        }
        self.assertEqual(
            loc.default_memory_dir(os.path.join(repo, "src"), env),
            "/somewhere/else/memory",
        )

    def test_an_empty_override_is_ignored(self):
        repo = repo_with_subdir()
        claude = th.make_dir({})
        memory = os.path.join(claude, "projects", loc._project_slug(repo), "memory")
        os.makedirs(memory)
        env = {loc.ENV_MEMORY_DIR: "", loc.ENV_CLAUDE_HOME: claude}
        self.assertEqual(loc.default_memory_dir(repo, env), memory)

    def test_inside_a_repository_the_existing_repository_memory_directory_is_used(self):
        repo = repo_with_subdir()
        claude = th.make_dir({})
        memory = os.path.join(claude, "projects", loc._project_slug(repo), "memory")
        os.makedirs(memory)
        self.assertEqual(
            loc.default_memory_dir(
                os.path.join(repo, "src", "deep"), self.env_for(claude)
            ),
            memory,
        )

    def test_when_no_memory_directory_exists_yet_the_expected_repository_path_is_returned_not_none(
        self,
    ):
        repo = repo_with_subdir()
        claude = th.make_dir({})
        expected = os.path.join(claude, "projects", loc._project_slug(repo), "memory")
        self.assertEqual(
            loc.default_memory_dir(os.path.join(repo, "src"), self.env_for(claude)),
            expected,
        )
        self.assertFalse(os.path.exists(expected))

    def test_outside_a_repository_the_working_directory_names_the_project(self):
        plain = th.make_dir({})
        claude = th.make_dir({})
        expected = os.path.join(claude, "projects", loc._project_slug(plain), "memory")
        self.assertEqual(loc.default_memory_dir(plain, self.env_for(claude)), expected)

    def test_the_default_uses_the_process_working_directory_when_none_is_given(self):
        plain = th.make_dir({})
        claude = th.make_dir({})
        expected = os.path.join(claude, "projects", loc._project_slug(plain), "memory")
        with mock.patch.object(os, "getcwd", return_value=plain):
            self.assertEqual(
                loc.default_memory_dir(environ=self.env_for(claude)), expected
            )

    def test_it_never_raises_whatever_the_working_directory_is(self):
        claude = th.make_dir({})
        with mock.patch.object(os, "getcwd", side_effect=FileNotFoundError("gone")):
            self.assertTrue(
                loc.default_memory_dir(environ=self.env_for(claude)).endswith(
                    os.path.join("unknown-project", "memory")
                )
            )
        self.assertTrue(
            loc.default_memory_dir("/tmp/bad\udc80name", self.env_for(claude)).endswith(
                "memory"
            )
        )

    def test_the_expected_path_of_a_linked_worktree_is_the_main_repository_one(self):
        base = th.make_dir({})
        main = os.path.join(base, "main")
        os.makedirs(os.path.join(main, ".git", "worktrees", "wt1"))
        wt = os.path.join(base, "wt")
        os.makedirs(os.path.join(wt, "sub"))
        with open(os.path.join(wt, ".git"), "w", encoding="utf-8") as fh:
            fh.write("gitdir: %s\n" % os.path.join(main, ".git", "worktrees", "wt1"))
        root = "/r"
        self.assertEqual(
            loc.expected_memory_dir(os.path.join(wt, "sub"), root),
            os.path.join(root, loc._project_slug(main), "memory"),
        )


class GitFileCapTests(TempRoot):
    """Only the first 4096 bytes of a '.git' file are read. The cap bounds the read; it is not a validity rule."""

    FITS = GIT_FILE_CAP - len(GITDIR_PREFIX) - len(WORKTREES_MARKER) - 1  # 4071 characters of <main>

    def resolve(self, main_chars):
        main, content = linked_line(main_chars)
        _base, work, src = make_work_tree()
        write_git_file(work, content)
        return main, work, loc.expected_memory_dir(src, PROJECTS)

    def test_a_line_longer_than_the_cap_still_resolves_when_the_marker_and_first_name_byte_fit(self):
        # Line is 4099 bytes; the 4096th byte is the 'w' of 'wt1', 't1' and the newline are cut off.
        main, _work, found = self.resolve(self.FITS)
        self.assertEqual(found, expected_for(main))

    def test_a_valid_line_whose_marker_does_not_fit_inside_the_cap_is_ignored(self):
        # FITS + 1: the cut falls right after 'worktrees/' and normalising the path drops that last separator.
        # 6000: the marker starts far beyond the cap.
        for main_chars in (self.FITS + 1, 6000):
            with self.subTest(main_chars=main_chars):
                _main, work, found = self.resolve(main_chars)
                self.assertEqual(found, expected_for(work))

class WalkDepthTests(TempRoot):
    """The walk up looks at MAX_WALK_LEVELS (64) directories: the working directory and its 63 nearest ancestors."""

    def test_a_repository_63_levels_above_the_working_directory_is_found(self):
        repo = repo_with_subdir()
        cwd = os.path.join(repo, *["d"] * (WALK_CAP - 1))  # does not exist; only the repository does
        self.assertEqual(loc.expected_memory_dir(cwd, PROJECTS), expected_for(repo))

    def test_a_repository_64_levels_above_the_working_directory_is_not_found(self):
        repo = repo_with_subdir()
        cwd = os.path.join(repo, *["d"] * WALK_CAP)
        self.assertEqual(loc.expected_memory_dir(cwd, PROJECTS), expected_for(cwd))

class LinkedWorktreeFallbackTests(TempRoot):
    NOT_A_POINTER = {
        "submodule, relative": b"gitdir: ../.git/modules/module\n",
        "separate git dir": b"gitdir: /abs/store/project.git\n",
        "worktrees folder itself, no entry": b"gitdir: /abs/main/.git/worktrees\n",
        "look-alike folder name": b"gitdir: /abs/main/.git/worktrees-old/wt1\n",
        "no .git folder before worktrees": b"gitdir: /abs/main/worktrees/wt1\n",
        "main path is empty": b"gitdir: /.git/worktrees/wt1\n",
        "no gitdir prefix": b"/abs/main/.git/worktrees/wt1\n",
        "second line is not read": b"gitdir: ../.git/modules/m\ngitdir: /abs/main/.git/worktrees/wt1\n",
        "empty file": b"",
    }

    def test_a_git_file_that_is_not_a_linked_worktree_pointer_makes_the_work_tree_the_root(self):
        for label, content in self.NOT_A_POINTER.items():
            with self.subTest(label):
                _base, work, src = make_work_tree()
                write_git_file(work, content)
                self.assertEqual(loc.expected_memory_dir(src, PROJECTS), expected_for(work))

    def test_a_relative_gitdir_is_resolved_from_the_work_tree_and_normalised(self):
        base, work, src = make_work_tree()
        cases = (
            ("../main/.git/worktrees/wt1", os.path.join(base, "main")),
            ("./store/main/.git/worktrees/wt1", os.path.join(work, "store", "main")),
        )
        for line, main in cases:
            with self.subTest(line):
                write_git_file(work, ("gitdir: %s\n" % line).encode("utf-8"))
                self.assertEqual(loc.expected_memory_dir(src, PROJECTS), expected_for(main))

    def test_a_main_work_tree_that_no_longer_exists_is_still_named_from_the_pointer(self):
        base, work, src = make_work_tree()
        gone = os.path.join(base, "gone")
        write_pointer(work, gone)
        self.assertFalse(os.path.exists(gone))
        self.assertEqual(loc.expected_memory_dir(src, PROJECTS), expected_for(gone))


class ImportTimeTests(TempRoot):
    """The shared module computes its defaults when it is imported, so the environment is read in a real process."""

    def run_python(self, code, env_extra, cwd):
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": tempfile.gettempdir(),
        }
        env.update(env_extra)
        done = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                "import sys; sys.path.insert(0, %r); %s" % (th.SCRIPTS, code),
            ],
            capture_output=True,
            text=True,
            env=env,
            cwd=cwd,
            timeout=60,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip()

    def test_the_common_module_defaults_follow_the_environment(self):
        claude = th.make_dir({})
        out = self.run_python(
            "import memory_common as mc; print(mc.DEFAULT_OPS_ROOT); print(mc.DEFAULT_MEMORY_DIR)",
            {loc.ENV_CLAUDE_HOME: claude, loc.ENV_MEMORY_DIR: "/data/mem"},
            tempfile.gettempdir(),
        ).split("\n")
        self.assertEqual(out, [os.path.join(claude, "memory-arborist"), "/data/mem"])

    def test_the_common_module_finds_the_repository_memory_from_a_subdirectory(self):
        repo = repo_with_subdir()
        claude = th.make_dir({})
        memory = os.path.join(
            claude, "projects", loc._project_slug(os.path.realpath(repo)), "memory"
        )
        os.makedirs(memory)
        out = self.run_python(
            "import memory_common as mc; print(mc.DEFAULT_MEMORY_DIR)",
            {loc.ENV_CLAUDE_HOME: claude},
            os.path.join(repo, "src", "deep"),
        )
        self.assertEqual(out, memory)

    def test_importing_never_fails_even_when_the_working_directory_was_deleted(self):
        gone = th.make_dir({})
        out = self.run_python(
            "import os, shutil; os.chdir(%r); shutil.rmtree(%r); import memory_common as mc; print('imported')"
            % (gone, gone),
            {},
            tempfile.gettempdir(),
        )
        self.assertEqual(out, "imported")


class ReadOnlyByConstructionTests(unittest.TestCase):
    FORBIDDEN_NAMES = {
        "O_WRONLY",
        "O_RDWR",
        "O_CREAT",
        "O_APPEND",
        "O_TRUNC",
        "write_text",
        "write_bytes",
        "mkdir",
        "makedirs",
        "remove",
        "unlink",
        "rename",
        "replace",
        "rmdir",
        "chmod",
        "chown",
        "utime",
        "symlink",
        "link",
        "system",
        "popen",
        "Popen",
        "check_call",
        "check_output",
        "copy",
        "copyfile",
        "move",
        "putenv",
        "unsetenv",
        "write",
    }
    FORBIDDEN_IMPORTS = {
        "subprocess",
        "shutil",
        "pathlib",
        "socket",
        "urllib",
        "http",
        "tempfile",
        "ctypes",
    }

    def tree(self):
        with open(SOURCE, encoding="utf-8") as fh:
            return ast.parse(fh.read())

    def test_no_write_capable_name_appears(self):
        tree = self.tree()
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        self.assertEqual(names & self.FORBIDDEN_NAMES, set())

    def test_no_forbidden_module_is_imported_and_no_builtin_open_is_called(self):
        imported, opens = set(), []
        for node in ast.walk(self.tree()):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "open"
            ):
                opens.append(node)
        self.assertEqual(imported & self.FORBIDDEN_IMPORTS, set())
        self.assertEqual(opens, [])


class ConfigDirTests(unittest.TestCase):
    """Claude Code's documented CLAUDE_CONFIG_DIR moves every ~/.claude path (sessions page); this tool follows it."""

    def test_claude_code_config_dir_is_the_claude_home_when_no_override_is_given(self):
        self.assertEqual(loc.claude_home({"CLAUDE_CONFIG_DIR": "/srv/cfg"}), "/srv/cfg")

    def test_the_toolkit_override_wins_over_claude_codes_variable(self):
        env = {"MEMORY_ARBORIST_CLAUDE_HOME": "/srv/mine", "CLAUDE_CONFIG_DIR": "/srv/cfg"}
        self.assertEqual(loc.claude_home(env), "/srv/mine")

    def test_an_empty_value_counts_as_unset(self):
        self.assertEqual(loc.claude_home({"CLAUDE_CONFIG_DIR": ""}), os.path.expanduser("~/.claude"))

    def test_assumption_a_tilde_or_relative_value_is_expanded_like_the_toolkit_override(self):
        self.assertEqual(loc.claude_home({"CLAUDE_CONFIG_DIR": "~/cfg"}), os.path.expanduser("~/cfg"))
        self.assertEqual(loc.claude_home({"CLAUDE_CONFIG_DIR": "rel/cfg"}), os.path.abspath("rel/cfg"))

    def test_the_projects_root_and_the_data_root_follow_it(self):
        env = {"CLAUDE_CONFIG_DIR": "/srv/cfg"}
        self.assertEqual(loc.projects_root(env), "/srv/cfg/projects")
        self.assertEqual(loc.default_ops_root(env), "/srv/cfg/memory-arborist")
        self.assertEqual(loc.default_ops_root(dict(env, MEMORY_ARBORIST_HOME="/srv/data")), "/srv/data")

    def test_the_default_memory_directory_is_found_under_the_relocated_config(self):
        repo = repo_with_subdir()
        cfg = th.make_dir({})
        found = loc.default_memory_dir(cwd=repo, environ={"CLAUDE_CONFIG_DIR": cfg})
        self.assertEqual(found, os.path.join(cfg, "projects", loc._project_slug(repo), "memory"))


class DeletedWorkingDirectoryTests(unittest.TestCase):
    def test_importing_the_toolkit_survives_a_relative_override_and_a_deleted_cwd(self):
        gone = tempfile.mkdtemp(prefix="memtests_gone_")
        code = (
            "import os, sys; os.chdir(%r); os.rmdir(%r); sys.path.insert(0, %r); "
            "import memory_locate as loc, memory_common as mc; "
            "print(bool(loc.default_memory_dir()), bool(mc.DEFAULT_OPS_ROOT))"
            % (gone, gone, th.SCRIPTS)
        )
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/nonexistent-home",
               "MEMORY_ARBORIST_HOME": "relative-data", "MEMORY_ARBORIST_MEMORY_DIR": "relative-mem"}
        done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "True True")


if __name__ == "__main__":
    unittest.main()
