"""The recall judge refuses to send notes that hold secret shapes, unless the flag says so (memory_egress)."""

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
    th,
    write_questions,
)

LEAK = th.fake_github_token()


class EgressTests(TempRoot):
    def setUp(self):
        self.mem = memory_with_phrases()
        with open(os.path.join(self.mem, "feedback-n1.md"), "a", encoding="utf-8") as fh:
            fh.write("\nthe key is %s\n" % LEAK)
        self.root = tempfile.mkdtemp(prefix="recops_")
        self.ops = mc.ops_dir_for(self.mem, self.root)
        self.snap = sn.take_snapshot(self.mem, self.ops, now=T0)["id"]
        self.qfile = write_questions(questions())

    def cli(self, *argv, runner=None):
        runner = runner or MemoryReadingRunner()
        base = ["--questions", self.qfile, "--dir", self.mem, "--ops-root", self.root, "--no-log"]
        code, out, err = run_main(base + list(argv), runner)
        return code, out, err, runner

    def test_the_live_arm_is_refused_before_any_call(self):
        code, _out, err, runner = self.cli("--arm", "live")
        self.assertEqual(code, 4)
        self.assertEqual(runner.calls, [])
        self.assertIn("REFUSED", err)
        self.assertIn("feedback-n1.md", err)
        self.assertNotIn(LEAK, err)

    def test_the_baseline_arm_is_refused_before_any_call(self):
        code, _out, err, runner = self.cli("--arm", "baseline", "--save", os.path.join(self.root, "b.json"))
        self.assertEqual(code, 4)
        self.assertEqual(runner.calls, [])
        self.assertIn("REFUSED", err)

    def test_the_staged_arm_scans_the_directory_it_is_given(self):
        code, _out, err, runner = self.cli("--arm", "staged", "--staged-dir", self.mem, "--baseline", os.path.join(self.root, "b.json"))
        self.assertEqual(code, 4)
        self.assertEqual(runner.calls, [])
        self.assertIn("REFUSED", err)

    def test_the_empty_arm_reads_no_notes_so_it_is_not_gated(self):
        code, out, _err, runner = self.cli("--arm", "empty")
        self.assertEqual(code, 0, out)
        self.assertTrue(runner.calls)

    def test_the_flag_lets_the_live_arm_go_ahead_with_one_warning_line(self):
        code, out, _err, runner = self.cli("--arm", "live", "--allow-secret-shapes")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.count("WARN --allow-secret-shapes"), 1)
        self.assertTrue(runner.calls)

    def test_the_override_flag_cannot_be_given_as_an_abbreviation(self):
        for abbreviation in ("--allow", "--allow-s"):
            with self.subTest(abbreviation):
                runner = MemoryReadingRunner()
                with self.assertRaises(SystemExit) as caught:
                    self.cli("--arm", "live", abbreviation, runner=runner)
                self.assertEqual(caught.exception.code, 64)
                self.assertEqual(runner.calls, [])

    def test_a_damaged_snapshot_is_reported_as_damaged_not_as_a_refusal(self):
        damaged = os.path.join(self.ops, "snapshots", self.snap, "memory", "feedback-n2.md")
        os.chmod(damaged, 0o600)
        th.write(damaged, "tampered\n")
        code, _out, err, runner = self.cli("--arm", "live")
        self.assertEqual(code, 1)
        self.assertIn("snapshot", err)
        self.assertNotIn("REFUSED", err)
        self.assertEqual(runner.calls, [])


if __name__ == "__main__":
    unittest.main()
