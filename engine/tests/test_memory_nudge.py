"""Tests for the session-start headroom nudge (memory_headroom_nudge.py): silent unless the index is close to its limits, numbers only, never raises, never writes."""

import ast
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import memtest_helpers as th  # noqa: E402

sys.path.insert(0, th.SCRIPTS)
import memory_common as mc  # noqa: E402
import memory_headroom_nudge as nudge  # noqa: E402

SCRIPT = os.path.join(th.SCRIPTS, "memory_headroom_nudge.py")
SENTINEL = "SENTINEL-INDEX-TEXT-MUST-NOT-APPEAR"
SHORT_MARKER = "QZXVWKJ"  # shorter than any prefix a leak would copy


class TempRoot(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        saved, root = tempfile.tempdir, tempfile.mkdtemp(prefix="memtests_")
        tempfile.tempdir = root
        cls.addClassCleanup(shutil.rmtree, root, True)
        cls.addClassCleanup(setattr, tempfile, "tempdir", saved)


def index_with_chars(count):
    """An index of exactly `count` characters made of a few very long lines (so the line count stays small)."""
    text = "".join("\n" if (i + 1) % 1001 == 0 else "x" for i in range(count))
    return text[:-1] + "x" if text.endswith("\n") else text


def index_with_lines(count):
    return "\n".join("- item %d" % i for i in range(count))


def memory_with(index_text):
    return th.make_dir({"MEMORY.md": index_text})


def hook_env(home=None):
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": home or tempfile.gettempdir(),
    }


def run_script(args, stdin_bytes=None, home=None, script=SCRIPT, timeout=20):
    """The script exactly as a hook runs it: a real python3 -I process with a minimal environment."""
    return subprocess.run(
        [sys.executable, "-I", script] + list(args),
        input=stdin_bytes,
        stdin=None if stdin_bytes is not None else subprocess.DEVNULL,
        capture_output=True,
        env=hook_env(home),
        timeout=timeout,
    )


class ThresholdTests(TempRoot):
    def test_a_small_index_is_silent(self):
        self.assertIsNone(nudge.nudge_line(memory_with(index_with_lines(98))))

    def test_the_character_threshold_fires_exactly_at_the_limit(self):
        self.assertIsNone(
            nudge.nudge_line(memory_with(index_with_chars(nudge.NUDGE_CHARS - 1)))
        )
        self.assertIsNotNone(
            nudge.nudge_line(memory_with(index_with_chars(nudge.NUDGE_CHARS)))
        )

    def test_the_line_threshold_fires_exactly_at_the_limit(self):
        self.assertIsNone(
            nudge.nudge_line(memory_with(index_with_lines(nudge.NUDGE_LINES - 1)))
        )
        self.assertIsNotNone(
            nudge.nudge_line(memory_with(index_with_lines(nudge.NUDGE_LINES)))
        )

    def test_the_thresholds_sit_below_the_lint_warning_levels(self):
        import memory_lint as ml

        self.assertLess(nudge.NUDGE_CHARS, ml.INDEX_WARN_CHARS)
        self.assertLess(nudge.NUDGE_LINES, ml.INDEX_WARN_LINES)


class MessageTests(TempRoot):
    def test_the_message_is_one_line_with_the_real_numbers_and_the_next_step(self):
        line = nudge.nudge_line(memory_with(index_with_chars(19000)))
        self.assertNotIn("\n", line)
        self.assertIn("19000", line)
        self.assertIn("lint", line)
        self.assertIn("20000", line)

    def test_the_message_informs_and_does_not_order_the_model_to_act(self):
        line = nudge.nudge_line(memory_with(index_with_chars(19000)))
        self.assertIn("Tell the user", line)
        self.assertIn("unless the user asks", line)

    def test_the_message_names_only_things_this_package_ships_and_no_unit_it_cannot_source(self):
        line = nudge.nudge_line(memory_with(index_with_chars(19000)))
        self.assertIn("memory-arborist:tend", line)
        self.assertNotIn("/memory-dream", line)
        self.assertNotIn("loader caps", line)
        self.assertIn("documented read limit", line)

    def test_the_message_reports_lines_when_lines_trigger_it(self):
        line = nudge.nudge_line(memory_with(index_with_lines(150)))
        self.assertIn("150 lines", line)

    def test_no_index_text_ever_reaches_the_message(self):
        text = SENTINEL + "\n" + index_with_chars(nudge.NUDGE_CHARS + 500)
        line = nudge.nudge_line(memory_with(text))
        self.assertIsNotNone(line)
        self.assertNotIn(SENTINEL, line)
        self.assertTrue(line.isprintable())

    def test_a_huge_index_is_read_in_bounded_form_and_reported_as_at_least(self):
        big = memory_with(SHORT_MARKER + "x" * (nudge.MAX_READ_BYTES * 3))
        started = time.monotonic()
        line = nudge.nudge_line(big)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertIn("at least", line)
        self.assertNotIn(SHORT_MARKER, line)


class FailureTests(TempRoot):
    def test_missing_directory_missing_file_directory_in_place_of_file_and_garbage_are_handled(
        self,
    ):
        self.assertIsNone(
            nudge.nudge_line(os.path.join(tempfile.gettempdir(), "does-not-exist"))
        )
        self.assertIsNone(nudge.nudge_line(th.make_dir({"other.md": "x"})))
        as_dir = th.make_dir({})
        os.mkdir(os.path.join(as_dir, "MEMORY.md"))
        self.assertIsNone(nudge.nudge_line(as_dir))
        garbage = th.make_dir({})
        with open(os.path.join(garbage, "MEMORY.md"), "wb") as fh:
            fh.write(b"\xff\xfe" * 20000)
        self.assertIsNotNone(
            nudge.nudge_line(garbage)
        )  # undecodable bytes still count as a very large index

    def alarm_guard(self, memory):
        def too_slow(signum, frame):
            raise AssertionError("reading the index blocked on a FIFO")

        previous = signal.signal(signal.SIGALRM, too_slow)
        signal.alarm(5)
        try:
            return nudge.nudge_line(memory)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)

    def test_a_fifo_in_place_of_the_index_is_silent_and_does_not_block(self):
        memory = th.make_dir({})
        os.mkfifo(os.path.join(memory, "MEMORY.md"))
        self.assertIsNone(self.alarm_guard(memory))

    def test_a_fifo_with_a_live_writer_and_no_data_is_silent_not_an_error(self):
        memory = th.make_dir({})
        fifo = os.path.join(memory, "MEMORY.md")
        os.mkfifo(fifo)
        holder = os.open(
            fifo, os.O_RDWR
        )  # keeps a writer open without sending anything
        try:
            self.assertIsNone(self.alarm_guard(memory))
        finally:
            os.close(holder)

    def test_a_symlinked_index_is_not_followed(self):
        outside = th.make_dir({"real.md": index_with_chars(nudge.NUDGE_CHARS + 100)})
        memory = th.make_dir({})
        os.symlink(os.path.join(outside, "real.md"), os.path.join(memory, "MEMORY.md"))
        self.assertIsNone(nudge.nudge_line(memory))

    def test_nothing_in_the_memory_directory_changes(self):
        memory = memory_with(index_with_chars(nudge.NUDGE_CHARS + 10))
        before = {
            rel: mc.sha256_file(os.path.join(memory, rel))
            for rel in mc.list_files(memory)
        }
        nudge.nudge_line(memory)
        after = {
            rel: mc.sha256_file(os.path.join(memory, rel))
            for rel in mc.list_files(memory)
        }
        self.assertEqual(before, after)


class ReadOnlyByConstructionTests(unittest.TestCase):
    """The shared tripwire only knows the builtin open(); this one reads the nudge's own source for every other way to change anything."""

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
        "environ",
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
        with open(SCRIPT, encoding="utf-8") as fh:
            return ast.parse(fh.read())

    def test_no_write_capable_name_appears(self):
        tree = self.tree()
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        self.assertEqual(names & self.FORBIDDEN_NAMES, set())

    def test_no_forbidden_module_is_imported(self):
        imported = set()
        for node in ast.walk(self.tree()):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported & self.FORBIDDEN_IMPORTS, set())

    def test_the_only_write_call_goes_to_standard_output(self):
        calls = [
            n
            for n in ast.walk(self.tree())
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "write"
        ]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args[0].value, 1)

    def test_no_builtin_open_call_exists(self):
        opens = [
            n
            for n in ast.walk(self.tree())
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "open"
        ]
        self.assertEqual(opens, [])


class LocateTests(TempRoot):
    def test_a_working_directory_maps_to_its_project_memory_directory(self):
        root = th.make_dir({})
        expected = os.path.join(root, "-Users-example-Projects-demo", "memory")
        os.makedirs(expected)
        found = nudge.memory_dir_for_cwd("/Users/example/Projects/demo", root)
        self.assertEqual(found, expected)

    def test_the_default_projects_root_follows_the_shared_defaults(self):
        claude = th.make_dir({})
        plain = th.make_dir({})
        memory = os.path.join(claude, "projects", nudge._project_slug(plain), "memory")
        os.makedirs(memory)
        with mock.patch.dict(os.environ, {"MEMORY_ARBORIST_CLAUDE_HOME": claude}):
            self.assertEqual(nudge.memory_dir_for_cwd(plain), memory)

    def test_unusual_or_missing_working_directories_find_nothing(self):
        root = th.make_dir({})
        for cwd in (None, "", 7, "relative/path", "/no/such/project/at/all"):
            with self.subTest(cwd=cwd):
                self.assertIsNone(nudge.memory_dir_for_cwd(cwd, root))

    def test_an_empty_or_relative_working_directory_never_finds_a_stray_directory(self):
        root = th.make_dir({})
        os.makedirs(os.path.join(root, "memory"))  # what an empty slug would resolve to
        os.makedirs(
            os.path.join(root, "relative", "memory")
        )  # what a relative cwd would resolve to
        self.assertIsNone(nudge.memory_dir_for_cwd("", root))
        self.assertIsNone(nudge.memory_dir_for_cwd("relative", root))

    def test_digits_in_a_working_directory_are_kept_in_the_project_name(self):
        root = th.make_dir({})
        expected = os.path.join(root, "-tmp-p4", "memory")
        os.makedirs(expected)
        self.assertEqual(nudge.memory_dir_for_cwd("/tmp/p4", root), expected)

    def test_a_character_outside_the_basic_plane_counts_as_two_dashes_like_claude_code(
        self,
    ):
        root = th.make_dir({})
        expected = os.path.join(
            root, "-tmp-proj--x", "memory"
        )  # JavaScript replaces each UTF-16 code unit
        os.makedirs(expected)
        self.assertEqual(
            nudge.memory_dir_for_cwd("/tmp/proj\U0001f600x", root), expected
        )

    def test_a_working_directory_with_a_lone_surrogate_finds_nothing_and_does_not_raise(
        self,
    ):
        self.assertIsNone(
            nudge.memory_dir_for_cwd("/tmp/bad\udc80name", th.make_dir({}))
        )

    def test_a_project_name_over_the_length_limit_finds_nothing(self):
        root = th.make_dir({})
        os.makedirs(os.path.join(root, "-" + "a" * 250, "memory"))
        self.assertIsNone(nudge.memory_dir_for_cwd("/" + "a" * 250, root))


class RunTests(TempRoot):
    def project_root(self):
        projects = th.make_dir({})
        slug_dir = os.path.join(projects, "-tmp-demo-project", "memory")
        os.makedirs(slug_dir)
        with open(os.path.join(slug_dir, "MEMORY.md"), "w", encoding="utf-8") as fh:
            fh.write(index_with_chars(19000))
        return projects

    def test_run_prints_the_line_for_an_explicit_directory(self):
        code, out = nudge.run(["--dir", memory_with(index_with_chars(19000))])
        self.assertEqual(code, 0)
        self.assertIn("19000", out)

    def test_run_is_silent_and_exits_zero_for_bad_input(self):
        for stdin_text in (
            "",
            "not json",
            "[]",
            '{"cwd": 5}',
            '{"cwd": "/nope"}',
            "{" * 100000,
        ):
            with self.subTest(stdin=stdin_text[:12]):
                self.assertEqual(nudge.run([], stdin_text), (0, ""))

    def test_run_never_raises_on_a_broken_argument_list(self):
        for argv in (["--bogus"], ["--dir"], ["--dir", "/no/such/dir"]):
            with self.subTest(argv=argv):
                self.assertEqual(nudge.run(argv), (0, ""))

    def test_run_reads_the_working_directory_from_hook_json(self):
        code, out = nudge.run(
            [], '{"cwd": "/tmp/demo-project"}', projects_root=self.project_root()
        )
        self.assertEqual(code, 0)
        self.assertIn("19000", out)

    def test_an_oversized_hook_payload_is_ignored(self):
        padded = (
            '{"pad": "'
            + "x" * (nudge.MAX_STDIN_CHARS + 10)
            + '", "cwd": "/tmp/demo-project"}'
        )
        self.assertEqual(
            nudge.run([], padded, projects_root=self.project_root()), (0, "")
        )


class HookProcessTests(TempRoot):
    """The program as the hook runs it: a real process, a minimal environment, exit codes and streams checked."""

    def project_home(self, index_text):
        home = th.make_dir({})
        memory = os.path.join(
            home, ".claude", "projects", "-tmp-demo-project", "memory"
        )
        os.makedirs(memory)
        with open(os.path.join(memory, "MEMORY.md"), "w", encoding="utf-8") as fh:
            fh.write(index_text)
        return home

    def spawn(self, args):
        return subprocess.Popen(
            [sys.executable, "-I", SCRIPT] + list(args),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=hook_env(),
        )

    def finish(self, proc, wait):
        try:
            code = proc.wait(timeout=wait)
            return code, proc.stdout.read()
        finally:
            proc.stdin.close()
            proc.stdout.close()
            proc.stderr.close()
            if proc.poll() is None:
                proc.kill()

    def test_the_hook_path_prints_the_sentence_from_stdin_json(self):
        home = self.project_home(index_with_chars(19000))
        done = run_script(
            [],
            b'{"cwd": "/tmp/demo-project", "hook_event_name": "SessionStart"}',
            home=home,
        )
        self.assertEqual(done.returncode, 0)
        self.assertIn(b"19000", done.stdout)
        self.assertEqual(done.stderr, b"")

    def test_the_hook_follows_claude_codes_relocated_config_directory(self):
        home = th.make_dir({})  # an empty home: nothing under ~/.claude
        cfg = th.make_dir({})
        memory = os.path.join(cfg, "projects", "-tmp-demo-project", "memory")
        os.makedirs(memory)
        with open(os.path.join(memory, "MEMORY.md"), "w", encoding="utf-8") as fh:
            fh.write(index_with_chars(19000))
        env = hook_env(home)
        env["CLAUDE_CONFIG_DIR"] = cfg
        done = subprocess.run(
            [sys.executable, "-I", SCRIPT],
            input=b'{"cwd": "/tmp/demo-project"}',
            capture_output=True,
            env=env,
            timeout=20,
        )
        self.assertEqual(done.returncode, 0)
        self.assertIn(b"19000", done.stdout)

    def test_a_comfortable_index_prints_nothing(self):
        home = self.project_home(index_with_lines(50))
        done = run_script([], b'{"cwd": "/tmp/demo-project"}', home=home)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, b"", b""))

    def test_garbage_empty_and_missing_stdin_all_exit_zero_silently(self):
        home = self.project_home(index_with_chars(19000))
        for payload in (b"not json", b"", b"\xff\xfe\x00", b"[1, 2]"):
            with self.subTest(payload=payload[:8]):
                done = run_script([], payload, home=home)
                self.assertEqual((done.returncode, done.stdout), (0, b""))
        done = run_script([], None, home=home)  # stdin is /dev/null
        self.assertEqual((done.returncode, done.stdout), (0, b""))

    def test_the_manual_form_with_dir_prints_the_sentence(self):
        done = run_script(["--dir", memory_with(index_with_chars(19000))])
        self.assertEqual(done.returncode, 0)
        self.assertIn(b"19000", done.stdout)

    def test_dir_mode_does_not_wait_for_an_open_stdin(self):
        code, out = self.finish(
            self.spawn(["--dir", memory_with(index_with_chars(19000))]), 5
        )
        self.assertEqual(code, 0)
        self.assertIn(b"19000", out)

    def test_the_hook_path_gives_up_waiting_on_a_silent_open_stdin(self):
        code, out = self.finish(self.spawn([]), 8)
        self.assertEqual((code, out), (0, b""))

    def test_a_closed_standard_output_still_exits_zero(self):
        memory = memory_with(index_with_chars(19000))
        done = subprocess.run(
            [
                "/bin/sh",
                "-c",
                'exec "$0" -I "$1" --dir "$2" >&-',
                sys.executable,
                SCRIPT,
                memory,
            ],
            capture_output=True,
            env=hook_env(),
            timeout=20,
        )
        self.assertEqual(done.returncode, 0)

    def test_a_missing_sibling_module_still_exits_zero_silently(self):
        alone = th.make_dir({})
        shutil.copy(SCRIPT, os.path.join(alone, "memory_headroom_nudge.py"))
        done = run_script(
            ["--dir", memory_with(index_with_chars(19000))],
            script=os.path.join(alone, "memory_headroom_nudge.py"),
        )
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, b"", b""))

    def test_running_it_writes_no_bytecode_files(self):
        copy = th.make_dir({})
        for name in os.listdir(th.SCRIPTS):
            if name.startswith("memory_") and name.endswith(".py"):
                shutil.copy(os.path.join(th.SCRIPTS, name), os.path.join(copy, name))
        done = run_script(
            ["--dir", memory_with(index_with_chars(19000))],
            script=os.path.join(copy, "memory_headroom_nudge.py"),
        )
        self.assertEqual(done.returncode, 0)
        self.assertIn(b"19000", done.stdout)
        self.assertNotIn("__pycache__", os.listdir(copy))

    def test_it_is_fast_enough_for_every_session_start(self):
        started = time.monotonic()
        run_script(["--dir", memory_with(index_with_chars(19000))])
        self.assertLess(time.monotonic() - started, 3.0)


class MainSafetyNetTests(TempRoot):
    def test_dir_mode_never_reads_stdin_and_hook_mode_reads_it_once(self):
        calls = []
        real = nudge._read_stdin
        nudge._read_stdin = lambda *args, **kwargs: calls.append(1) or ""
        try:
            nudge.main(["--dir", "/no/such/dir"])
            self.assertEqual(calls, [])
            nudge.main([])
            self.assertEqual(calls, [1])
        finally:
            nudge._read_stdin = real

    def test_an_unexpected_error_anywhere_still_gives_exit_zero_and_no_output(self):
        real = nudge.run

        def boom(*args, **kwargs):
            raise RuntimeError("unexpected")

        nudge.run = boom
        try:
            self.assertEqual(nudge.main(["--dir", "/no/such/dir"]), 0)
        finally:
            nudge.run = real

    def test_a_sibling_module_that_fails_while_importing_still_exits_zero_silently(self):
        fake = th.make_dir({"memory_lint.py": "raise RuntimeError('boom')\n"})
        shutil.copy(SCRIPT, os.path.join(fake, "memory_headroom_nudge.py"))
        done = run_script(
            ["--dir", memory_with(index_with_chars(19000))], script=os.path.join(fake, "memory_headroom_nudge.py")
        )
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, b"", b""))


class GitRootTests(TempRoot):
    """Claude Code derives the project from the git repository, so every subdirectory and linked worktree shares one memory directory."""

    def project_memory(self, work_tree):
        root = th.make_dir({})
        memory = os.path.join(root, nudge._project_slug(work_tree), "memory")
        os.makedirs(memory)
        return root, memory

    def repo(self):
        base = th.make_dir({})
        repo = os.path.join(base, "repo")
        os.makedirs(os.path.join(repo, ".git"))
        os.makedirs(os.path.join(repo, "src", "pkg"))
        return base, repo

    def test_a_subdirectory_of_a_git_repository_maps_to_the_repository_memory_directory(self):
        _base, repo = self.repo()
        projects, memory = self.project_memory(repo)
        self.assertEqual(nudge.memory_dir_for_cwd(os.path.join(repo, "src", "pkg"), projects), memory)
        self.assertEqual(nudge.memory_dir_for_cwd(repo, projects), memory)

    def test_the_nearest_repository_wins_when_repositories_are_nested(self):
        _base, outer = self.repo()
        inner = os.path.join(outer, "vendor", "inner")
        os.makedirs(os.path.join(inner, ".git"))
        os.makedirs(os.path.join(inner, "src"))
        projects, inner_memory = self.project_memory(inner)
        os.makedirs(os.path.join(projects, nudge._project_slug(outer), "memory"))
        self.assertEqual(nudge.memory_dir_for_cwd(os.path.join(inner, "src"), projects), inner_memory)

    def worktree(self, base, repo, gitdir_line):
        os.makedirs(os.path.join(repo, ".git", "worktrees", "wt1"))
        worktree = os.path.join(base, "wt")
        os.makedirs(os.path.join(worktree, "sub"))
        with open(os.path.join(worktree, ".git"), "w", encoding="utf-8") as fh:
            fh.write(gitdir_line)
        return worktree

    def test_a_linked_worktree_maps_to_the_main_repository_memory_directory(self):
        base = th.make_dir({})
        repo = os.path.join(base, "main")
        os.makedirs(repo)
        worktree = self.worktree(base, repo, "gitdir: %s\n" % os.path.join(repo, ".git", "worktrees", "wt1"))
        projects, memory = self.project_memory(repo)
        self.assertEqual(nudge.memory_dir_for_cwd(os.path.join(worktree, "sub"), projects), memory)

    def test_a_relative_gitdir_in_a_worktree_file_is_resolved_from_the_worktree(self):
        base = th.make_dir({})
        repo = os.path.join(base, "main")
        os.makedirs(repo)
        worktree = self.worktree(base, repo, "gitdir: ../main/.git/worktrees/wt1\n")
        projects, memory = self.project_memory(repo)
        self.assertEqual(nudge.memory_dir_for_cwd(os.path.join(worktree, "sub"), projects), memory)

    def test_a_git_file_that_is_not_a_linked_worktree_makes_its_own_directory_the_root(self):
        base = th.make_dir({})
        module = os.path.join(base, "super", "module")
        os.makedirs(os.path.join(module, "src"))
        with open(os.path.join(module, ".git"), "w", encoding="utf-8") as fh:
            fh.write("gitdir: ../.git/modules/module\n")
        projects, memory = self.project_memory(module)
        self.assertEqual(nudge.memory_dir_for_cwd(os.path.join(module, "src"), projects), memory)

    def test_garbage_and_oversized_git_files_make_their_own_directory_the_root(self):
        for content in (b"\xff\xfe\x00" * 50, b"not a gitdir line\n", b"gitdir: " + b"x" * (1 << 20)):
            with self.subTest(size=len(content)):
                base = th.make_dir({})
                work = os.path.join(base, "w")
                os.makedirs(os.path.join(work, "src"))
                with open(os.path.join(work, ".git"), "wb") as fh:
                    fh.write(content)
                projects, memory = self.project_memory(work)
                self.assertEqual(nudge.memory_dir_for_cwd(os.path.join(work, "src"), projects), memory)

    def test_the_repository_memory_directory_is_preferred_over_the_working_directory_one(self):
        _base, repo = self.repo()
        projects, repo_memory = self.project_memory(repo)
        sub = os.path.join(repo, "src")
        os.makedirs(os.path.join(projects, nudge._project_slug(sub), "memory"))
        self.assertEqual(nudge.memory_dir_for_cwd(sub, projects), repo_memory)

    def test_the_working_directory_memory_is_used_when_the_repository_one_does_not_exist(self):
        _base, repo = self.repo()
        projects = th.make_dir({})
        sub = os.path.join(repo, "src")
        own = os.path.join(projects, nudge._project_slug(sub), "memory")
        os.makedirs(own)
        self.assertEqual(nudge.memory_dir_for_cwd(sub, projects), own)

    def test_a_deep_path_without_any_repository_is_cheap_and_finds_nothing(self):
        deep = "/" + "/".join("d%d" % i for i in range(120))
        started = time.monotonic()
        self.assertIsNone(nudge.memory_dir_for_cwd(deep, th.make_dir({})))
        self.assertLess(time.monotonic() - started, 1.0)

    def test_a_symlinked_git_file_is_not_followed_so_it_cannot_redirect_the_lookup(self):
        base = th.make_dir({})
        main = os.path.join(base, "main")
        os.makedirs(os.path.join(main, ".git", "worktrees", "wt1"))
        elsewhere = os.path.join(base, "elsewhere.txt")
        with open(elsewhere, "w", encoding="utf-8") as fh:
            fh.write("gitdir: %s\n" % os.path.join(main, ".git", "worktrees", "wt1"))
        work = os.path.join(base, "work")
        os.makedirs(os.path.join(work, "src"))
        os.symlink(elsewhere, os.path.join(work, ".git"))
        projects, own_memory = self.project_memory(work)
        os.makedirs(os.path.join(projects, nudge._project_slug(main), "memory"))
        self.assertEqual(nudge.memory_dir_for_cwd(os.path.join(work, "src"), projects), own_memory)

    def test_the_hook_process_finds_the_memory_of_a_repository_from_a_subdirectory(self):
        home = th.make_dir({})
        repo = os.path.join(home, "work", "repo")
        os.makedirs(os.path.join(repo, ".git"))
        os.makedirs(os.path.join(repo, "src", "pkg"))
        memory = os.path.join(home, ".claude", "projects", nudge._project_slug(repo), "memory")
        os.makedirs(memory)
        with open(os.path.join(memory, "MEMORY.md"), "w", encoding="utf-8") as fh:
            fh.write(index_with_chars(19000))
        payload = ('{"cwd": "%s"}' % os.path.join(repo, "src", "pkg")).encode("utf-8")
        done = run_script([], payload, home=home)
        self.assertEqual(done.returncode, 0)
        self.assertIn(b"19000", done.stdout)


if __name__ == "__main__":
    unittest.main()
