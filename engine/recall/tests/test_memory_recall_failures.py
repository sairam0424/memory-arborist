"""A judge run whose model calls keep failing stops early and says why; it never reports the child as having answered."""

import contextlib
import io
import os
import sys
import tempfile
import types
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
    mrc,
    mrr,
    question,
    questions,
    run_main,
    sn,
    write_questions,
)

LOGIN_ERROR = "Not logged in - run /login"


def always_failing(calls):
    def runner(argv, cwd, env):
        calls.append(argv)
        return 1, "", LOGIN_ERROR

    return runner


class EarlyFailTests(unittest.TestCase):
    def record(self, ok):
        return (
            {"id": "r01", "pass": True}
            if ok
            else {"id": "r01", "error": "claude exited with 1: " + LOGIN_ERROR}
        )

    def test_it_stops_the_run_after_the_limit_when_nothing_has_answered(self):
        spend = mrr.Spend(5.0)
        early = mrc._EarlyFail(spend, limit=3)
        for _ in range(2):
            early.note(self.record(False))
        self.assertIsNone(spend.stopped)
        early.note(self.record(False))
        self.assertIn("first 3 model calls all failed", spend.stopped)
        self.assertIn(LOGIN_ERROR, spend.stopped)

    def test_it_never_stops_a_run_that_has_had_an_answer(self):
        spend = mrr.Spend(5.0)
        early = mrc._EarlyFail(spend, limit=3)
        early.note(self.record(True))
        for _ in range(10):
            early.note(self.record(False))
        self.assertIsNone(spend.stopped)

    def test_the_default_limit_is_six(self):
        self.assertEqual(mrc.EARLY_FAIL_LIMIT, 6)


class FailedJudgeRunTests(TempRoot):
    def setUp(self):
        self.mem = memory_with_phrases()
        self.root = tempfile.mkdtemp(prefix="recops_")
        self.ops = mc.ops_dir_for(self.mem, self.root)
        self.snap = sn.take_snapshot(self.mem, self.ops, now=T0)["id"]
        self.qfile = write_questions(questions())

    def cli(self, runner):
        argv = [
            "--questions",
            self.qfile,
            "--dir",
            self.mem,
            "--ops-root",
            self.root,
            "--no-log",
            "--arm",
            "live",
            "--workers",
            "1",
        ]
        return run_main(argv, runner)

    def test_a_run_where_every_call_fails_aborts_after_six_calls_and_names_the_cause(
        self,
    ):
        calls = []
        code, out, err = self.cli(always_failing(calls))
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 6)
        self.assertIn("ABORTED", out + err)
        self.assertIn("first 6 model calls all failed", out + err)
        self.assertIn(LOGIN_ERROR, out + err)
        self.assertNotIn("TOOLS ok=", out)

    def test_a_run_with_some_answers_is_not_cut_short(self):
        good = MemoryReadingRunner()
        state = {"n": 0}

        def runner(argv, cwd, env):
            state["n"] += 1
            if state["n"] > 1 and state["n"] <= 9:
                return 1, "", LOGIN_ERROR
            return good(argv, cwd, env)

        code, out, err = self.cli(runner)
        self.assertNotIn("ABORTED", out + err)
        self.assertIn("TOOLS ok=", out)
        self.assertEqual(state["n"], 25)


class ToolsLineTests(TempRoot):
    def args(self):
        return types.SimpleNamespace(model="m", per_q_usd=0.5, workers=1)

    def execute(self, runner, items):
        tmp = tempfile.mkdtemp(prefix="recrun_")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            mrc.execute(
                self.args(), runner, items, tmp, tmp, [("empty", 1)], mrr.Spend(5.0)
            )
        return out.getvalue()

    def test_no_tools_line_when_no_child_answered(self):
        out = self.execute(always_failing([]), [question(i) for i in range(1, 4)])
        self.assertIn("errors=3", out)
        self.assertNotIn("TOOLS ok=", out)

    def test_the_tools_line_appears_once_a_child_answered(self):
        def runner(argv, cwd, env):
            return 0, fake_output("nothing"), ""

        out = self.execute(runner, [question(i) for i in range(1, 4)])
        self.assertIn("TOOLS ok=Glob,Grep,Read", out)


class ToolListTests(TempRoot):
    def test_a_missing_tool_list_is_explained_instead_of_printed_as_none(self):
        text = mrr.tools_reason("the child", None, mrr.EXPECTED_TOOLS)
        self.assertIn("did not report its tool list", text)
        self.assertNotIn("None", text)

    def test_a_wrong_tool_list_keeps_its_wording(self):
        self.assertEqual(
            mrr.tools_reason("the child", ["Bash"], ["Read"]),
            "the child reported tools ['Bash'], expected ['Read']",
        )
        self.assertEqual(mrr.tools_reason("a lane", ["Bash"]), "a lane reported tools ['Bash']")

    def test_a_judge_run_whose_children_print_no_init_element_aborts_with_that_explanation(self):
        mem = memory_with_phrases()
        root = tempfile.mkdtemp(prefix="recops_")
        ops = mc.ops_dir_for(mem, root)
        sn.take_snapshot(mem, ops, now=T0)
        argv = ["--questions", write_questions(questions()), "--dir", mem, "--ops-root", root, "--no-log", "--arm", "live", "--workers", "1"]
        code, out, err = run_main(argv, lambda a, cwd, env: (0, fake_output("x", init=False), ""))
        self.assertEqual(code, 1)
        self.assertIn("did not report its tool list", out + err)


class ShowFailedTests(TempRoot):
    def setUp(self):
        self.mem = memory_with_phrases()
        self.root = tempfile.mkdtemp(prefix="recops_")
        self.ops = mc.ops_dir_for(self.mem, self.root)
        sn.take_snapshot(self.mem, self.ops, now=T0)
        self.qfile = write_questions(questions())

    def cli(self, *extra):
        def runner(argv, cwd, env):
            if "phrase 3?" in argv[3]:
                return 0, fake_output("the answer is something else entirely"), ""
            return MemoryReadingRunner()(argv, cwd, env)

        argv = ["--questions", self.qfile, "--dir", self.mem, "--ops-root", self.root, "--no-log", "--arm", "live", "--workers", "1", *extra]
        return run_main(argv, runner)

    def test_a_failed_answer_is_shown_only_when_asked_for(self):
        _code, out, _err = self.cli()
        self.assertIn("FAILED r03", out)
        self.assertNotIn("FAILED-ANSWER", out)
        _code, out, _err = self.cli("--show-failed")
        self.assertIn("FAILED-ANSWER r03 the answer is something else entirely", out)
        self.assertEqual(out.count("FAILED-ANSWER"), 1)


if __name__ == "__main__":
    unittest.main()
