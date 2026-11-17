"""Found by the independent code review of the doctor. Its output is pasted into public issues, so text that comes from a
config folder name, a question file, a model string or a child process must not reach it, and one odd file or byte must not
end the report. No test spends a token."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ENGINE, "tests"))
sys.path.insert(0, ENGINE)
sys.path.insert(0, os.path.join(ENGINE, "recall"))
sys.path.insert(0, os.path.dirname(HERE))
import memtest_helpers as th  # noqa: E402
import memory_common as mc  # noqa: E402
import memory_doctor as doc  # noqa: E402
import memory_locate as locate  # noqa: E402
import memory_recall_run as mrr  # noqa: E402
from test_doctor_checks import (  # noqa: E402
    WithClaudeOnPath,
    fake_lane_output,
    probe_for,
    statuses,
    valid_question,
)

# built at runtime: the secret scanner of this workspace refuses the literal shape
TOKEN = "sk-" + "ant-" + "api03-" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
ARN = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/acme-prod"


def text_of(checks):
    return " ".join(c.detail + " " + c.fix for c in checks)


class CustomConfigDirTests(unittest.TestCase):
    """A Claude config directory that is not ~/.claude can sit under a client or employer folder."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="dochome_")
        self.client = os.path.join(self.home, "clients", "acme-corp-merger", "claude")
        self.mem = os.path.join(
            self.client, "projects", "-Users-alice-acme-merger", "memory"
        )
        os.makedirs(self.mem)
        th.write(os.path.join(self.mem, "MEMORY.md"), "# Index\n")
        self.root = os.path.join(self.client, "memory-arborist")

    def checks_with(self, variable):
        with mock.patch.dict(
            os.environ, {"HOME": self.home, variable: self.client}, clear=True
        ):
            return doc.check_dirs(
                self.mem, self.root, mc.ops_dir_for(self.mem, self.root)
            )

    def test_a_config_dir_set_by_either_variable_is_shown_as_a_custom_path(self):
        for variable in (locate.ENV_CLAUDE_CONFIG, locate.ENV_CLAUDE_HOME):
            with self.subTest(variable):
                text = text_of(self.checks_with(variable))
                for word in ("acme", "alice", "clients", "merger"):
                    self.assertNotIn(word, text)
                self.assertIn(doc.CUSTOM_PATH, text)

    def test_the_standard_layout_is_still_shown_with_a_tilde(self):
        standard = os.path.join(self.home, ".claude")
        mem = os.path.join(standard, "projects", "-Users-alice-acme-merger", "memory")
        os.makedirs(mem)
        th.write(os.path.join(mem, "MEMORY.md"), "# Index\n")
        root = os.path.join(standard, "memory-arborist")
        with mock.patch.dict(os.environ, {"HOME": self.home}, clear=True):
            text = text_of(doc.check_dirs(mem, root, mc.ops_dir_for(mem, root)))
        self.assertIn("~/.claude/projects/<project>/memory", text)
        self.assertIn("~/.claude/memory-arborist", text)


class QuestionFileTextTests(unittest.TestCase):
    """The loader's own messages quote the line (a regex, a kind, an id); the doctor shows the line number and the kind of problem."""

    def state_of(self, lines):
        ops = tempfile.mkdtemp(prefix="docops_")
        os.makedirs(os.path.join(ops, "recall"))
        th.write(
            os.path.join(ops, "recall", "questions.jsonl"), "\n".join(lines) + "\n"
        )
        return doc._questions_state(ops)

    def line(self, **changes):
        rec = dict(valid_question(1), **changes)
        return json.dumps(rec)

    def test_a_problem_names_its_line_and_kind_but_never_quotes_the_file(self):
        cases = (
            (
                "bad regex",
                self.line(must=["Priya Raman (Acme Corp"]),
                "bad regex",
                "Priya",
            ),
            (
                "unknown kind",
                self.line(kind="Falcon-Acme-merger"),
                "unknown kind",
                "Falcon",
            ),
        )
        for label, text, kind, secret in cases:
            with self.subTest(label):
                check = self.state_of([text])
                self.assertEqual(check.status, doc.WARN)
                self.assertIn("line 1", check.detail)
                self.assertIn(kind, check.detail)
                self.assertNotIn(secret, check.detail + check.fix)

    def test_a_duplicate_id_does_not_print_the_id(self):
        first = self.line(id="Priya-Raman-salary")
        second = self.line(id="Priya-Raman-salary", question="another")
        check = self.state_of([first, second])
        self.assertEqual(check.status, doc.WARN)
        self.assertIn("line 2", check.detail)
        self.assertIn("duplicate id", check.detail)
        self.assertNotIn("Priya", check.detail + check.fix)

    def test_the_fixed_phrases_and_counts_are_kept(self):
        self.assertEqual(doc.question_problem("line 2: missing id"), "line 2: missing id")
        self.assertEqual(
            doc.question_problem("line 4: must needs at least one regex"),
            "line 4: must needs at least one regex",
        )
        self.assertEqual(
            doc.question_problem("3 questions; the set needs 20 to 80"),
            "3 questions; the set needs 20 to 80",
        )

    def test_a_message_the_doctor_does_not_know_is_not_echoed(self):
        text = doc.question_problem("line 3: some future message quoting SecretWord")
        self.assertIn("line 3", text)
        self.assertNotIn("SecretWord", text)

    def test_a_file_that_is_not_utf8_is_a_warning_not_a_failure_about_permissions(self):
        ops = tempfile.mkdtemp(prefix="docops_")
        os.makedirs(os.path.join(ops, "recall"))
        with open(os.path.join(ops, "recall", "questions.jsonl"), "wb") as fh:
            fh.write(b"\xff\xfe{}\n")
        checks = doc.check_first_run(ops)
        question = [c for c in checks if c.name == "questions"][0]
        self.assertEqual(question.status, doc.WARN)
        self.assertNotIn("permissions", question.fix)


class OversizeRepeatTests(unittest.TestCase):
    """A regex like a{4294967296} raises OverflowError, which ended the whole report."""

    def test_the_judges_loader_calls_it_a_bad_regex(self):
        text = '{"id":"q1","kind":"recall","question":"x","must":["a{4294967296}"]}\n'
        with self.assertRaises(mc.HygieneError):
            mrr.load_questions_text(text, require_count=False)

    def test_the_doctor_reports_it_as_a_warning_and_keeps_going(self):
        ops = tempfile.mkdtemp(prefix="docops_")
        os.makedirs(os.path.join(ops, "recall"))
        th.write(
            os.path.join(ops, "recall", "questions.jsonl"),
            '{"id":"q1","kind":"recall","question":"x","must":["a{4294967296}"]}\n',
        )
        checks = doc.check_first_run(ops)
        self.assertEqual([c.name for c in checks].count("snapshots"), 1)
        question = [c for c in checks if c.name == "questions"][0]
        self.assertEqual(question.status, doc.WARN)
        self.assertIn("bad regex", question.detail)

    def test_one_check_that_raises_any_arithmetic_error_is_one_failure(self):
        def boom():
            raise OverflowError("too large")

        self.assertEqual(statuses(doc._guarded("name", boom)), [doc.FAIL])


class ChildTextTests(WithClaudeOnPath):
    """What a child process says is untrusted: secrets are withheld and paths outside the home directory are not printed."""

    def failing_probe(self, stderr):
        return lambda argv, env: (127, "", stderr)

    def test_a_token_in_the_stderr_of_a_program_that_exits_127_is_withheld(self):
        stderr = "wrapper: line 4: %s: command not found" % TOKEN
        checks = doc.check_claude(self.failing_probe(stderr), self.environ)
        self.assertEqual(checks[0].status, doc.FAIL)
        self.assertNotIn(TOKEN[:12], text_of(checks))
        self.assertIn("withheld", checks[0].detail)

    def test_a_path_outside_the_home_directory_is_not_printed(self):
        stderr = "env: node: No such file or directory /srv/client-acme/tools/wrap.sh"
        checks = doc.check_claude(self.failing_probe(stderr), self.environ)
        self.assertNotIn("client-acme", text_of(checks))
        self.assertIn("<path>", checks[0].detail)

    def test_a_path_in_a_live_failure_is_not_printed_either(self):
        def runner(argv, cwd, env, stdin_text):
            return 1, "", "cannot open /srv/client-acme/notes/x.md"

        checks = doc.check_live(runner, "m")
        self.assertEqual(checks[0].status, doc.FAIL)
        self.assertNotIn("client-acme", checks[0].detail)

    def test_a_slash_command_is_not_a_path(self):
        self.assertEqual(doc.scrub_paths("Not logged in - run /login"), "Not logged in - run /login")

    def test_a_failed_check_does_not_print_a_path_from_its_error(self):
        def boom():
            raise mc.HygieneError("unreadable directories under /srv/client-acme/notes")

        checks = doc._guarded("memory", boom)
        self.assertEqual(statuses(checks), [doc.FAIL])
        self.assertNotIn("client-acme", checks[0].detail)

    def test_a_home_path_still_becomes_a_tilde_and_a_url_is_left_alone(self):
        with mock.patch.dict(os.environ, {"HOME": "/Users/example"}):
            text = doc.scrub_child(
                "see ~/x, /Users/example/.claude/x and https://example.com/docs"
            )
        self.assertIn("~/.claude/x", text)
        self.assertIn("https://example.com/docs", text)
        self.assertNotIn("/Users/example", text)


class LiveModelTextTests(unittest.TestCase):
    def runner(self, argv, cwd, env, stdin_text):
        return 0, fake_lane_output('{"ok": true}'), ""

    def test_an_arn_or_account_id_given_as_the_model_is_not_printed(self):
        for model in (ARN, "123456789012"):
            with self.subTest(model):
                checks = doc.check_live(self.runner, model)
                self.assertEqual(statuses(checks), [doc.OK])
                text = doc.render(checks)
                self.assertNotIn("123456789012", text)
                self.assertNotIn("acme-prod", text)


class ProbeBytesTests(unittest.TestCase):
    def test_a_program_that_prints_a_byte_that_is_not_utf8_does_not_crash_the_doctor(
        self,
    ):
        script = "import sys; sys.stdout.buffer.write(b'\\xff ok\\n'); sys.stderr.buffer.write(b'\\xfe err\\n')"
        code, out, err = doc.subprocess_probe(
            [sys.executable, "-c", script], dict(os.environ)
        )
        self.assertEqual(code, 0)
        self.assertIn("ok", out)
        self.assertIn("err", err)


class MissingMemoryDirTests(WithClaudeOnPath):
    def test_a_missing_memory_directory_does_not_hide_the_data_root_and_first_run_checks(
        self,
    ):
        root = tempfile.mkdtemp(prefix="docops_")
        missing = os.path.join(tempfile.mkdtemp(prefix="docmem_"), "no-such-memory")
        args = doc.build_parser().parse_args(["--dir", missing, "--ops-root", root])
        checks = doc.run_checks(args, self.environ, probe_for(), None)
        names = [c.name for c in checks]
        for name in (
            "memory",
            "data-root",
            "pinned",
            "questions",
            "evidence",
            "snapshots",
        ):
            self.assertIn(name, names)
        self.assertEqual([c.status for c in checks if c.name == "memory"], [doc.FAIL])
        self.assertEqual([c.status for c in checks if c.name == "data-root"], [doc.OK])


class JudgeModelNoteTests(WithClaudeOnPath):
    def run_live(self, environ):
        mem = th.make_dir({"MEMORY.md": "# Index\n"})
        root = tempfile.mkdtemp(prefix="docops_")
        args = doc.build_parser().parse_args(
            ["--dir", mem, "--ops-root", root, "--live", "--model", "lane-model"]
        )

        def runner(argv, cwd, env, stdin_text):
            return 0, fake_lane_output('{"ok": true}'), ""

        with contextlib.redirect_stdout(io.StringIO()):
            return doc.run_checks(
                args, dict(self.environ, **environ), probe_for(), runner
            )

    def test_live_says_which_model_it_proved_and_that_the_judge_model_was_not(self):
        live = [c for c in self.run_live({}) if c.name == "live"]
        self.assertEqual([c.status for c in live], [doc.OK, doc.INFO])
        self.assertIn("lane-model", live[0].detail)
        self.assertIn(mrr.DEFAULT_MODEL, live[1].detail)
        self.assertIn("not", live[1].detail)

    def test_the_judge_model_from_the_environment_is_shown_without_an_account_id(self):
        live = [
            c for c in self.run_live({mrr.ENV_JUDGE_MODEL: ARN}) if c.name == "live"
        ]
        text = text_of(live)
        self.assertNotIn("123456789012", text)
        self.assertNotIn("acme-prod", text)


if __name__ == "__main__":
    unittest.main()
