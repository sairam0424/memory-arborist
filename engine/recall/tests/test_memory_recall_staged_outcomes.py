"""Found by the independent test review: the staged check's mixed outcomes. A question that errors in only ONE of the staged runs is
still unverified, and a real regression next to an unverified question must keep the failing exit code, not the 'run again' one."""

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_memory_recall import (  # noqa: E402
    T0,
    MemoryReadingRunner,
    TempRoot,
    mc,
    memory_with_phrases,
    run_main,
    sn,
    write_questions,
    questions,
)


class StagedOutcomeTests(TempRoot):
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

    def staged(self, staged_dir, runner):
        return self.cli(
            "--arm",
            "staged",
            "--staged-dir",
            staged_dir,
            "--baseline",
            self.baseline,
            runner=runner,
        )

    def test_a_question_that_errors_in_only_one_staged_run_is_unverified(self):
        good, failures = MemoryReadingRunner(), {"left": 1}

        def runner(argv, cwd, env):
            if "phrase 3?" in argv[3] and failures["left"]:
                failures["left"] -= 1
                return 1, "", "Request timed out"
            return good(argv, cwd, env)

        code, out, _err = self.staged(memory_with_phrases(), runner)
        self.assertEqual(code, 5, out)
        self.assertIn("UNVERIFIED n=1 ids=r03", out)

    def test_a_regression_next_to_an_unverified_question_keeps_the_failing_exit_code(
        self,
    ):
        good = MemoryReadingRunner()

        def runner(argv, cwd, env):
            if "phrase 3?" in argv[3]:
                return 1, "", "Request timed out"
            return good(argv, cwd, env)

        code, out, _err = self.staged(memory_with_phrases(skip=(4,)), runner)
        self.assertEqual(code, 1, out)
        self.assertIn("REGRESSIONS n=1 ids=r04", out)
        self.assertIn("UNVERIFIED n=1 ids=r03", out)
        self.assertIn("INCOMPLETE 1 question(s)", out)


if __name__ == "__main__":
    unittest.main()
