"""Found by the first tester with a fake claude: one stray byte, a malformed result, a nan ceiling or a question that errored must not
cost a paid run, switch a ceiling off, or let the staged check report a clean pass."""

import argparse
import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_memory_recall import (  # noqa: E402
    T0,
    MemoryReadingRunner,
    TempRoot,
    fake_output,
    mc,
    memory_with_phrases,
    mrr,
    run_main,
    sn,
    th,
    write_questions,
    questions,
)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "dream"))
import memory_dream_lanes as lanes  # noqa: E402
import memory_dream_run as drun  # noqa: E402


def shim(directory, body):
    path = os.path.join(directory, "claude")
    th.write(path, "#!/bin/sh\n" + body)
    os.chmod(path, stat.S_IRWXU)
    return path


class DecodeTests(TempRoot):
    def test_decode_output_replaces_bad_bytes_and_accepts_text_or_nothing(self):
        self.assertEqual(mrr.decode_output(b"ok \xff\xfe bad"), "ok \ufffd\ufffd bad")
        self.assertEqual(mrr.decode_output("already text"), "already text")
        self.assertEqual(mrr.decode_output(None), "")

    def test_the_judge_runner_survives_a_child_that_prints_non_utf8(self):
        path = shim(
            tempfile.mkdtemp(prefix="shim_"),
            "printf '\\377\\376 bad'\nprintf 'warn \\351\\n' >&2\nexit 1\n",
        )
        code, out, err = mrr.subprocess_runner(
            [path], tempfile.gettempdir(), dict(os.environ)
        )
        self.assertEqual(code, 1)
        self.assertIn("\ufffd", out)
        self.assertIn("warn", err)

    def test_the_lane_runner_does_too_and_still_sends_stdin_as_text(self):
        path = shim(tempfile.mkdtemp(prefix="shim_"), "cat\nprintf ' \\377'\n")
        code, out, _err = lanes.subprocess_runner(
            [path], tempfile.gettempdir(), dict(os.environ), "héllo prompt"
        )
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("héllo prompt"))
        self.assertTrue(out.endswith("\ufffd"))

    def test_a_judge_run_whose_children_print_junk_ends_with_a_message_not_a_codec_error(
        self,
    ):
        mem = memory_with_phrases()
        root = tempfile.mkdtemp(prefix="recops_")
        sn.take_snapshot(mem, mc.ops_dir_for(mem, root), now=T0)
        bin_dir = tempfile.mkdtemp(prefix="shim_")
        shim(bin_dir, "printf '\\377\\376 bad'\n")
        env = dict(os.environ, PATH=bin_dir + os.pathsep + os.environ.get("PATH", ""))
        argv = [
            "--questions",
            write_questions(questions()),
            "--dir",
            mem,
            "--ops-root",
            root,
            "--no-log",
            "--arm",
            "live",
            "--workers",
            "1",
        ]
        previous = os.environ.get("PATH")
        os.environ["PATH"] = env["PATH"]
        try:
            code, out, err = run_main(argv, mrr.subprocess_runner)
        finally:
            os.environ["PATH"] = previous
        self.assertNotIn("codec", out + err)
        self.assertIn("ABORTED", out + err)  # six failed calls in a row stop the run


class ParseOutputTests(unittest.TestCase):
    def test_a_tool_list_with_a_non_string_is_reported_not_a_crash(self):
        out = json.dumps(
            [
                {"type": "system", "subtype": "init", "tools": [1, "Read"]},
                {"type": "result", "result": "x"},
            ]
        )
        self.assertEqual(mrr.parse_output(out).tools, ["1", "Read"])

    def test_deeply_nested_output_is_not_json_instead_of_a_recursion_error(self):
        with self.assertRaises(mrr.AskError) as ctx:
            mrr.parse_output("[" * 300_000)
        self.assertIn("not JSON", str(ctx.exception))

    def test_a_mismatched_tool_list_aborts_the_run_through_the_normal_check(self):
        out = json.dumps(
            [
                {"type": "system", "subtype": "init", "tools": [1, "Read"]},
                {"type": "result", "result": "x", "total_cost_usd": 0.001},
            ]
        )
        spend = mrr.Spend(5.0)
        with self.assertRaises(mrr.ToolsMismatch):
            mrr.ask(
                {
                    "id": "r01",
                    "kind": "recall",
                    "question": "q?",
                    "must": ["x"],
                    "forbid": [],
                },
                lambda argv, cwd, env: (0, out, ""),
                mrr.Settings("m", 0.25),
                "/nonexistent-prompt",
                tempfile.gettempdir(),
                spend,
                tempfile.gettempdir(),
            )


class CostLimitTests(unittest.TestCase):
    def test_the_limit_type_accepts_a_positive_number_only(self):
        self.assertEqual(mrr.positive_usd("0.5"), 0.5)
        for bad in ("nan", "inf", "-inf", "0", "-1", "abc", ""):
            with self.subTest(bad), self.assertRaises(argparse.ArgumentTypeError):
                mrr.positive_usd(bad)

    def test_the_judge_and_the_dream_run_refuse_a_nan_or_negative_ceiling_at_the_command_line(
        self,
    ):
        import memory_recall as mrc

        for build, argv, code in (
            (mrc.build_parser, ["--total-max-usd", "nan"], 64),
            (mrc.build_parser, ["--per-q-usd", "-1"], 64),
            (drun.build_parser, ["run", "--total-max-usd", "nan"], 2),  # the dream run uses plain argparse: usage errors exit 2
            (drun.build_parser, ["run", "--per-lane-usd", "0"], 2),
        ):
            with self.subTest(argv):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as ctx:
                        build().parse_args(argv)
                self.assertEqual(ctx.exception.code, code)


class ScrubbedFailureLineTests(unittest.TestCase):
    def test_an_account_id_and_an_arn_in_a_childs_error_are_replaced(self):
        line = "AccessDenied: User arn:aws:sts::123456789012:assumed-role/Dev/alice is not authorized for account 123456789012"
        text = mrr.failure_message(1, "", line)
        self.assertNotIn("123456789012", text)
        self.assertNotIn("alice", text)
        self.assertIn("<arn>", text)


class StagedUnverifiedTests(TempRoot):
    def setUp(self):
        self.mem = memory_with_phrases()
        self.root = tempfile.mkdtemp(prefix="recops_")
        self.ops = mc.ops_dir_for(self.mem, self.root)
        sn.take_snapshot(self.mem, self.ops, now=T0)
        self.qfile = write_questions(questions())
        self.baseline = os.path.join(
            tempfile.mkdtemp(prefix="recbase_"), "baseline.json"
        )
        code, out, _err = self.cli("--arm", "baseline", "--save", self.baseline)
        self.assertEqual(code, 0, out)

    def cli(self, *extra, runner=None):
        argv = [
            "--questions",
            self.qfile,
            "--dir",
            self.mem,
            "--ops-root",
            self.root,
            "--no-log",
            "--workers",
            "1",
            *extra,
        ]
        return run_main(argv, runner or MemoryReadingRunner())

    def staged(self, runner=None):
        return self.cli(
            "--arm",
            "staged",
            "--staged-dir",
            memory_with_phrases(),
            "--baseline",
            self.baseline,
            runner=runner,
        )

    def test_a_clean_staged_check_prints_zero_unverified_and_exits_zero(self):
        code, out, _err = self.staged()
        self.assertEqual(code, 0, out)
        self.assertIn("UNVERIFIED n=0 ids=-", out)

    def test_a_question_that_errors_on_the_staged_copy_is_unverified_and_the_run_is_incomplete(
        self,
    ):
        good = MemoryReadingRunner()

        def runner(argv, cwd, env):
            if "phrase 3?" in argv[3]:
                return 1, "", "Request timed out"
            return good(argv, cwd, env)

        code, out, _err = self.staged(runner)
        self.assertEqual(code, 5, out)
        self.assertIn("REGRESSIONS n=0", out)
        self.assertIn("UNVERIFIED n=1 ids=r03", out)
        self.assertIn(
            "INCOMPLETE 1 question(s) that passed at baseline could not be graded", out
        )


if __name__ == "__main__":
    unittest.main()
