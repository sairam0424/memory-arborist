"""The first-run doctor: every check is read-only and prints names, versions and counts only. No test spends a token."""

import ast
import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ENGINE, "tests"))
import memtest_helpers as th  # noqa: E402

sys.path.insert(0, ENGINE)
sys.path.insert(0, os.path.join(ENGINE, "recall"))
sys.path.insert(0, os.path.dirname(HERE))
import memory_common as mc  # noqa: E402
import memory_doctor as doc  # noqa: E402
import memory_recall_run as mrr  # noqa: E402
import memory_snapshot as sn  # noqa: E402

HELP_ALL = (
    "Options:\n  "
    + "\n  ".join(
        flag for flag in doc.REQUIRED_FLAGS if flag != "--append-system-prompt-file"
    )
    + "\n  --append-system-prompt[-file]\n"
)
LEAK_MARKER = "value-that-must-never-be-printed-1234"


def fake_lane_output(text="{}", tools=None, cost=0.0003, with_init=True):
    items = [
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": text,
            "total_cost_usd": cost,
        }
    ]
    if with_init:
        items.insert(
            0,
            {
                "type": "system",
                "subtype": "init",
                "tools": [] if tools is None else tools,
                "model": "m",
            },
        )
    return json.dumps(items)


def probe_for(
    version="2.1.325 (Claude Code)", help_text=HELP_ALL, version_code=0, help_code=0
):
    def probe(argv, env):
        return (
            (version_code, version, "")
            if argv[1] == "--version"
            else (help_code, help_text, "")
        )

    return probe


def statuses(checks):
    return [check.status for check in checks]


class WithClaudeOnPath(unittest.TestCase):
    """A temporary directory that holds a file named claude, so shutil.which finds it on the PATH the children get."""

    def setUp(self):
        self.bin = tempfile.mkdtemp(prefix="docbin_")
        path = os.path.join(self.bin, "claude")
        th.write(path, "#!/bin/sh\n")
        os.chmod(path, stat.S_IRWXU)
        self.environ = {"PATH": self.bin, "HOME": tempfile.gettempdir()}


class PythonAndVersionTests(unittest.TestCase):
    def test_python_floor(self):
        self.assertEqual(doc.check_python((3, 11, 0)).status, doc.OK)
        self.assertEqual(doc.check_python((3, 10, 9)).status, doc.FAIL)

    def test_version_parsing(self):
        self.assertEqual(doc.parse_version("2.1.325 (Claude Code)"), (2, 1, 325))
        self.assertIsNone(doc.parse_version("no digits"))
        self.assertIsNone(doc.parse_version(""))
        self.assertEqual(doc.dotted((2, 1, 9)), "2.1.9")

    def test_the_manifest_gives_the_name_and_version(self):
        name, version = doc.plugin_info()
        with open(
            os.path.join(os.path.dirname(ENGINE), ".claude-plugin", "plugin.json"),
            encoding="utf-8",
        ) as fh:
            manifest = json.load(fh)
        self.assertEqual((name, version), (manifest["name"], manifest["version"]))

    def test_the_two_floors_match_what_the_docs_promise(self):
        self.assertEqual(doc.HOOK_MIN, (2, 1, 139))
        self.assertEqual(doc.MODEL_MIN, (2, 1, 169))


class ClaudeChecksTests(WithClaudeOnPath):
    def test_a_current_claude_with_every_flag_is_ok(self):
        checks = doc.check_claude(probe_for(), self.environ)
        self.assertEqual(statuses(checks), [doc.OK, doc.OK])
        self.assertIn("claude 2.1.325", checks[0].detail)

    def test_the_bracket_shorthand_counts_as_listing_the_file_flag(self):
        self.assertTrue(
            doc._listed(
                "--append-system-prompt-file", "x --append-system-prompt[-file] y"
            )
        )
        self.assertFalse(
            doc._listed("--append-system-prompt-file", "x --append-system-prompt y")
        )

    def test_between_the_two_floors_is_a_warning_and_below_both_is_a_failure(self):
        self.assertEqual(
            doc.check_claude(probe_for("2.1.150"), self.environ)[0].status, doc.WARN
        )
        failing = doc.check_claude(probe_for("2.1.100"), self.environ)[0]
        self.assertEqual(failing.status, doc.FAIL)
        self.assertIn("2.1.139", failing.detail)

    def test_a_missing_flag_is_named_in_a_warning(self):
        checks = doc.check_claude(probe_for(help_text="--model --print"), self.environ)
        self.assertEqual(checks[1].status, doc.WARN)
        self.assertIn("--safe-mode", checks[1].detail)

    def test_a_failing_help_is_a_warning_not_a_failure(self):
        checks = doc.check_claude(probe_for(help_code=1), self.environ)
        self.assertEqual(statuses(checks), [doc.OK, doc.WARN])

    def test_no_claude_on_the_path_is_a_failure_that_does_not_print_the_path(self):
        checks = doc.check_claude(
            probe_for(), {"PATH": tempfile.mkdtemp(prefix="emptybin_")}
        )
        self.assertEqual(statuses(checks), [doc.FAIL])
        self.assertIn("not found", checks[0].detail)
        self.assertNotIn("emptybin_", checks[0].detail)

    def test_a_version_command_that_prints_nothing_useful_is_a_failure(self):
        checks = doc.check_claude(probe_for(version="hello"), self.environ)
        self.assertEqual(statuses(checks), [doc.FAIL])
        checks = doc.check_claude(probe_for(version_code=1), self.environ)
        self.assertEqual(statuses(checks), [doc.FAIL])

    def test_the_real_probe_reports_a_missing_program_as_127(self):
        code, out, err = doc.subprocess_probe(
            ["/nonexistent/claude-for-doctor-test"], {}
        )
        self.assertEqual(code, 127)
        self.assertEqual(out, "")


class LoginEnvTests(unittest.TestCase):
    def test_nothing_set_is_ok(self):
        checks = doc.check_login_env({"PATH": "/bin", "HOME": "/h"})
        self.assertEqual(statuses(checks), [doc.OK])

    def test_variables_that_are_set_but_not_passed_are_named_with_the_exact_fix_line(
        self,
    ):
        environ = {
            "PATH": "/bin",
            "ANTHROPIC_API_KEY": LEAK_MARKER,
            "HTTPS_PROXY": "http://u:" + LEAK_MARKER + "@p:1",
            "ANTHROPIC_BASE_URL": "https://gw.example",
        }
        warn = [c for c in doc.check_login_env(environ) if c.status == doc.WARN][0]
        for name in ("ANTHROPIC_API_KEY", "HTTPS_PROXY", "ANTHROPIC_BASE_URL"):
            self.assertIn(name, warn.detail)
        self.assertIn(
            "MEMORY_ARBORIST_PASS_ENV=ANTHROPIC_API_KEY,ANTHROPIC_BASE_URL,HTTPS_PROXY",
            warn.fix,
        )
        self.assertIn("secrets", warn.fix)
        self.assertNotIn(LEAK_MARKER, warn.detail + warn.fix)
        self.assertNotIn(
            "ANTHROPIC_BASE_URL, which", warn.fix
        )  # only secret-bearing names are called secrets

    def test_a_variable_the_user_already_listed_is_not_reported(self):
        environ = {
            "PATH": "/bin",
            "ANTHROPIC_API_KEY": LEAK_MARKER,
            mrr.ENV_PASS: "ANTHROPIC_API_KEY",
        }
        checks = doc.check_login_env(environ)
        self.assertEqual(statuses(checks), [doc.OK])
        self.assertIn(
            "ANTHROPIC_API_KEY", checks[0].detail
        )  # shown as passed, by name only
        self.assertNotIn(LEAK_MARKER, checks[0].detail)

    def test_an_empty_value_counts_as_not_set(self):
        self.assertEqual(
            statuses(doc.check_login_env({"PATH": "/bin", "ANTHROPIC_API_KEY": ""})),
            [doc.OK],
        )

    def test_the_variables_the_children_get_by_default_are_not_in_the_watch_list(self):
        names = {name for name, _secret in doc.LOGIN_ENV}
        self.assertEqual(names & set(mrr.ALLOWED_ENV), set())


class DirectoryChecksTests(unittest.TestCase):
    def setUp(self):
        self.mem = th.make_dir(
            {
                "MEMORY.md": "# Index\n- [A](a.md) a\n- [B](b.md) b\n",
                "a.md": th.note("a"),
                "b.md": th.note("b"),
            }
        )
        self.root = tempfile.mkdtemp(prefix="docops_")
        self.ops = mc.ops_dir_for(self.mem, self.root)

    def test_an_existing_memory_directory_is_counted(self):
        checks = doc.check_dirs(self.mem, self.root, self.ops)
        self.assertEqual(statuses(checks), [doc.OK, doc.OK])
        self.assertIn("2 notes, 3 index lines", checks[0].detail)
        self.assertIn("is created by the first snapshot", checks[1].detail)

    def test_a_missing_memory_directory_is_a_failure_with_the_fix(self):
        checks = doc.check_dirs(os.path.join(self.mem, "nope"), self.root, self.ops)
        self.assertEqual(statuses(checks), [doc.FAIL])
        self.assertIn("MEMORY_ARBORIST_MEMORY_DIR", checks[0].fix)

    def test_a_data_root_on_another_filesystem_is_a_warning(self):
        real_stat = os.stat
        notes_dev = real_stat(self.mem).st_dev

        def fake_stat(path, *args, **kwargs):
            result = real_stat(path, *args, **kwargs)
            if os.path.abspath(path) == os.path.abspath(self.root):
                return os.stat_result(
                    (result.st_mode, result.st_ino, notes_dev + 1) + tuple(result)[3:]
                )
            return result

        with mock.patch.object(doc.os, "stat", fake_stat):
            check = doc._data_root_check(self.mem, self.root, self.ops)
        self.assertEqual(check.status, doc.WARN)
        self.assertIn("different filesystem", check.detail)
        self.assertIn("--ops-root", check.fix)

    def test_a_data_root_that_does_not_exist_yet_is_checked_through_its_nearest_parent(
        self,
    ):
        check = doc._data_root_check(
            self.mem,
            os.path.join(self.root, "a", "b", "c"),
            os.path.join(self.root, "a", "b", "c", "x"),
        )
        self.assertEqual(check.status, doc.OK)


class FirstRunTests(unittest.TestCase):
    def setUp(self):
        self.mem = th.make_dir({"MEMORY.md": "# Index\n", "a.md": th.note("a")})
        self.root = tempfile.mkdtemp(prefix="docops_")
        self.ops = mc.ops_dir_for(self.mem, self.root)
        os.makedirs(self.ops, mode=0o700)

    def by_name(self):
        return {check.name: check for check in doc.check_first_run(self.ops)}

    def test_a_fresh_data_root_warns_about_pinned_and_questions(self):
        found = self.by_name()
        self.assertEqual(found["pinned"].status, doc.WARN)
        self.assertIn("stops at once", found["pinned"].detail)
        self.assertEqual(found["questions"].status, doc.WARN)
        self.assertEqual(found["evidence"].status, doc.INFO)
        self.assertEqual(found["snapshots"].status, doc.INFO)

    def test_pinned_states(self):
        th.write(os.path.join(self.ops, "pinned.txt.draft"), "- [A](a.md) a\n")
        self.assertIn("only pinned.txt.draft", self.by_name()["pinned"].detail)
        th.write(
            os.path.join(self.ops, "pinned.txt"), "- [A](a.md) a\n\n- [B](b.md) b\n"
        )
        found = self.by_name()["pinned"]
        self.assertEqual(found.status, doc.OK)
        self.assertIn("2 protected", found.detail)

    def test_the_question_count_must_be_inside_the_judges_range(self):
        os.makedirs(os.path.join(self.ops, "recall"), mode=0o700)
        path = os.path.join(self.ops, "recall", "questions.jsonl")
        for count, expected in (
            (24, doc.WARN),
            (25, doc.OK),
            (32, doc.OK),
            (33, doc.WARN),
        ):
            with self.subTest(count):
                th.write(path, "".join('{"id": "q%d"}\n' % i for i in range(count)))
                self.assertEqual(self.by_name()["questions"].status, expected)

    def test_digests_and_snapshots_are_counted(self):
        os.makedirs(os.path.join(self.ops, "evidence"), mode=0o700)
        th.write(os.path.join(self.ops, "evidence", "s1.md"), "x")
        th.write(os.path.join(self.ops, "evidence", "s2.md"), "x")
        sn.take_snapshot(self.mem, self.ops, now=1_700_000_000)
        found = self.by_name()
        self.assertIn("2 evidence digests", found["evidence"].detail)
        self.assertEqual(found["snapshots"].status, doc.OK)
        self.assertIn("1 snapshots", found["snapshots"].detail)


class LiveProbeTests(unittest.TestCase):
    def test_a_good_call_is_ok_and_describes_the_output_shape(self):
        def runner(argv, cwd, env, stdin_text):
            self.assertIn("--tools", argv)
            self.assertEqual(
                argv[argv.index("--max-budget-usd") + 1], str(doc.LIVE_PROBE_USD)
            )
            return 0, fake_lane_output('{"ok": true}'), ""

        checks = doc.check_live(runner, "m1")
        self.assertEqual(statuses(checks), [doc.OK])
        self.assertIn("list of 2 events", checks[0].detail)
        self.assertIn("model m1", checks[0].detail)

    def test_a_child_that_cannot_log_in_is_a_failure_with_its_own_message(self):
        checks = doc.check_live(
            lambda argv, cwd, env, stdin: (1, "", "Not logged in - run /login"), "m"
        )
        self.assertEqual(statuses(checks), [doc.FAIL])
        self.assertIn("Not logged in - run /login", checks[0].detail)

    def test_output_without_a_tool_list_says_so_in_plain_words(self):
        checks = doc.check_live(
            lambda argv, cwd, env, stdin: (0, fake_lane_output(with_init=False), ""),
            "m",
        )
        self.assertEqual(statuses(checks), [doc.FAIL])
        self.assertIn("did not report its tool list", checks[0].detail)
        self.assertNotIn("None", checks[0].detail)

    def test_a_child_that_reports_a_tool_is_a_failure(self):
        checks = doc.check_live(
            lambda argv, cwd, env, stdin: (0, fake_lane_output(tools=["Bash"]), ""), "m"
        )
        self.assertEqual(statuses(checks), [doc.FAIL])
        self.assertIn("Bash", checks[0].detail)

    def test_a_timeout_is_a_failure(self):
        def runner(argv, cwd, env, stdin_text):
            raise mrr.AskTimeout("timed out")

        checks = doc.check_live(runner, "m")
        self.assertEqual(statuses(checks), [doc.FAIL])
        self.assertIn("timed out", checks[0].detail)

    def test_the_probe_leaves_no_temporary_folder_behind(self):
        before = set(os.listdir(tempfile.gettempdir()))
        doc.check_live(lambda argv, cwd, env, stdin: (0, fake_lane_output(), ""), "m")
        self.assertEqual(
            {
                n
                for n in set(os.listdir(tempfile.gettempdir())) - before
                if n.startswith("doctor_")
            },
            set(),
        )


class MainTests(WithClaudeOnPath):
    def setUp(self):
        super().setUp()
        self.mem = th.make_dir({"MEMORY.md": "# Index\n", "a.md": th.note("a")})
        self.root = tempfile.mkdtemp(prefix="docops_")

    def run_main(self, *argv, environ=None, runner=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = doc.main(
                ["--dir", self.mem, "--ops-root", self.root, *argv],
                environ=environ or self.environ,
                probe=probe_for(),
                runner=runner,
            )
        return code, out.getvalue()

    def test_without_a_failure_the_exit_code_is_zero_and_the_summary_line_is_last(self):
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertRegex(
            out.splitlines()[-1], r"^DOCTOR ok=\d+ warn=\d+ fail=0 info=\d+$"
        )

    def test_a_failure_gives_exit_one_and_a_fix_line(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = doc.main(
                ["--dir", os.path.join(self.mem, "gone"), "--ops-root", self.root],
                environ=self.environ,
                probe=probe_for(),
            )
        self.assertEqual(code, 1)
        self.assertIn("     fix: ", out.getvalue())

    def test_the_output_has_no_variable_values_and_shows_the_home_directory_as_a_tilde(
        self,
    ):
        home = tempfile.mkdtemp(prefix="dochome_")
        environ = dict(self.environ, HOME=home)
        environ["ANTHROPIC_API_KEY"] = LEAK_MARKER
        with mock.patch.dict(os.environ, {"HOME": home}):
            code, out = self.run_main(environ=environ)
            self.assertEqual(doc.tilde(os.path.join(home, "x", "y")), "~/x/y")
            self.assertEqual(doc.tilde("/elsewhere/x"), "/elsewhere/x")
        self.assertNotIn(LEAK_MARKER, out)
        self.assertIn("ANTHROPIC_API_KEY", out)

    def test_version_prints_the_manifest_name_and_version_and_nothing_else(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = doc.main(["--version"])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().strip(), "%s %s" % doc.plugin_info())

    def test_live_is_off_unless_asked_for(self):
        def runner(argv, cwd, env, stdin_text):
            self.fail("a model call was made without --live")

        self.run_main(runner=runner)

    def test_live_makes_exactly_one_call_with_the_lane_model_from_the_environment(self):
        calls = []

        def runner(argv, cwd, env, stdin_text):
            calls.append(argv)
            return 0, fake_lane_output(), ""

        code, out = self.run_main(
            "--live",
            environ=dict(self.environ, **{doc.ENV_LANE_MODEL: "pinned-model-id"}),
            runner=runner,
        )
        self.assertEqual(code, 0, out)
        self.assertEqual(len(calls), 1)
        self.assertIn("ESTIMATE doctor probe: 1 model calls", out)
        self.assertLess(out.index("ESTIMATE"), out.index("live "))
        self.assertEqual(calls[0][calls[0].index("--model") + 1], "pinned-model-id")

    def test_an_unknown_option_is_a_usage_error(self):
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as ctx,
        ):
            doc.main(["--no-such-option"])
        self.assertEqual(ctx.exception.code, 2)


class SafetyTests(unittest.TestCase):
    def test_the_doctor_never_deletes_moves_or_uses_a_shell(self):
        with open(doc.__file__, encoding="utf-8") as fh:
            source = fh.read()
        names = {
            n.func.attr
            for n in ast.walk(ast.parse(source))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        self.assertEqual(
            names
            & {
                "remove",
                "unlink",
                "rmtree",
                "rename",
                "replace",
                "system",
                "popen",
                "chmod",
                "symlink",
                "link",
            },
            set(),
        )
        self.assertNotIn("shell=True", source)

    def test_every_flag_the_engine_passes_is_in_the_doctors_list(self):
        import memory_dream_lanes as lanes

        argv = lanes.build_argv("m", "/f", 0.1) + mrr.build_argv(
            "q", "m", "/f", "/d", 0.1
        )
        passed = {a for a in argv if a.startswith("--")} | {"--print"}
        self.assertLessEqual(passed, set(doc.REQUIRED_FLAGS))


if __name__ == "__main__":
    unittest.main()
