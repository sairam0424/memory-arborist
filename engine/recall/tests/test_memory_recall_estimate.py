"""Every paid command prints an ESTIMATE line (calls, cap per call, ceiling) before its first model call."""

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
    questions,
    run_main,
    sn,
    write_questions,
)
from test_memory_recall_wizard import Base as WizardBase  # noqa: E402

import memory_recall_run as mrr  # noqa: E402


class EstimateLineTests(unittest.TestCase):
    def test_the_line_names_the_calls_the_cap_and_the_ceiling(self):
        line = mrr.estimate_line("judge", 50, 0.25, 5.0)
        self.assertEqual(
            line,
            "ESTIMATE judge: 50 model calls, each capped at 0.25 USD, run ceiling 5.0 USD (the run stops there); "
            "the real cost is usually well below the ceiling",
        )


class JudgeEstimateTests(TempRoot):
    def setUp(self):
        self.mem = memory_with_phrases()
        self.root = tempfile.mkdtemp(prefix="recops_")
        self.ops = mc.ops_dir_for(self.mem, self.root)
        sn.take_snapshot(self.mem, self.ops, now=T0)
        self.qfile = write_questions(questions())

    def cli(self, *extra):
        argv = ["--questions", self.qfile, "--dir", self.mem, "--ops-root", self.root, "--no-log", "--workers", "1", *extra]
        return run_main(argv, MemoryReadingRunner())

    def test_a_single_live_run_counts_one_call_per_question(self):
        code, out, _err = self.cli("--arm", "live")
        self.assertEqual(code, 0, out)
        self.assertIn("ESTIMATE judge: 25 model calls, each capped at 0.25 USD, run ceiling 5.0 USD", out)
        self.assertLess(out.index("ESTIMATE"), out.index("RECALL arm="))

    def test_repeats_multiply_the_calls_and_a_changed_ceiling_is_shown(self):
        _code, out, _err = self.cli("--arm", "live", "--repeat", "2", "--total-max-usd", "3")
        self.assertIn("ESTIMATE judge: 50 model calls", out)
        self.assertIn("run ceiling 3.0 USD", out)


class WizardEstimateTests(WizardBase):
    def test_the_wizard_prints_the_batches_and_the_ceiling_before_asking(self):
        code, out, err, runner = self.propose()
        self.assertEqual(code, 0, out + err)
        self.assertIn("ESTIMATE question wizard: 2 model calls", out)
        self.assertLess(out.index("ESTIMATE"), out.index("PROPOSAL "))


if __name__ == "__main__":
    unittest.main()
