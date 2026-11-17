"""Found by the independent code review: the judge opened --baseline and --save only AFTER its paid calls, so a typo in a path cost a
whole run (and a malformed baseline ended in a traceback), and count options accepted negative numbers."""

import argparse
import contextlib
import io
import json
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
    mrr,
    run_main,
    sn,
    th,
    write_questions,
    questions,
)

import memory_recall as mrc  # noqa: E402


class PreflightTests(TempRoot):
    def setUp(self):
        self.mem = memory_with_phrases()
        self.root = tempfile.mkdtemp(prefix="recops_")
        sn.take_snapshot(self.mem, mc.ops_dir_for(self.mem, self.root), now=T0)
        self.qfile = write_questions(questions())
        self.scratch = tempfile.mkdtemp(prefix="recpre_")

    def cli(self, *extra, runner):
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
        return run_main(argv, runner)

    def staged(self, baseline, runner):
        return self.cli(
            "--arm",
            "staged",
            "--staged-dir",
            memory_with_phrases(),
            "--baseline",
            baseline,
            runner=runner,
        )

    def test_a_missing_baseline_file_is_refused_before_any_paid_call(self):
        runner = MemoryReadingRunner()
        code, out, err = self.staged(os.path.join(self.scratch, "nope.json"), runner)
        self.assertEqual(code, 1, out + err)
        self.assertEqual(runner.calls, [])
        self.assertIn("--baseline", err)
        self.assertNotIn("Traceback", out + err)

    def test_a_file_that_is_not_a_baseline_is_refused_with_a_message_and_no_calls(self):
        for label, text in (
            ("empty object", "{}"),
            ("a list", "[]"),
            ("not json", "this is not json"),
            ("empty runs", '{"live_runs": []}'),
            ("runs of the wrong type", '{"live_runs": [1]}'),
        ):
            with self.subTest(label):
                path = os.path.join(self.scratch, label.replace(" ", "_") + ".json")
                th.write(path, text)
                runner = MemoryReadingRunner()
                code, out, err = self.staged(path, runner)
                self.assertEqual(code, 1, out + err)
                self.assertEqual(runner.calls, [])
                self.assertIn("not a baseline", err)
                self.assertNotIn("Traceback", out + err)

    def test_a_save_path_that_cannot_be_written_is_refused_before_any_paid_call(self):
        for label, path in (
            ("missing folder", os.path.join(self.scratch, "no-such-folder", "b.json")),
            ("a directory", self.scratch),
        ):
            with self.subTest(label):
                runner = MemoryReadingRunner()
                code, out, err = self.cli(
                    "--arm", "baseline", "--save", path, runner=runner
                )
                self.assertEqual(code, 1, out + err)
                self.assertEqual(runner.calls, [])
                self.assertIn("--save", err)

    def test_a_good_baseline_and_a_good_save_path_still_work(self):
        save = os.path.join(self.scratch, "baseline.json")
        code, out, err = self.cli(
            "--arm", "baseline", "--save", save, runner=MemoryReadingRunner()
        )
        self.assertEqual(code, 0, out + err)
        self.assertTrue(os.path.isfile(save))
        code, out, err = self.staged(save, MemoryReadingRunner())
        self.assertEqual(code, 0, out + err)


class CountOptionTests(unittest.TestCase):
    def parse(self, build, argv):
        with contextlib.redirect_stderr(io.StringIO()):
            return build().parse_args(argv)

    def test_the_helper_accepts_only_positive_whole_numbers(self):
        self.assertEqual(mc.positive_int("3"), 3)
        for bad in ("0", "-1", "2.5", "abc", "", "nan"):
            with self.subTest(bad), self.assertRaises(argparse.ArgumentTypeError):
                mc.positive_int(bad)

    def test_the_judge_refuses_a_non_positive_limit_repeat_or_workers(self):
        for flag in ("--limit", "--repeat", "--workers"):
            for bad in ("0", "-1", "-20"):
                with self.subTest(flag=flag, bad=bad), self.assertRaises(SystemExit) as ctx:
                    self.parse(mrc.build_parser, [flag, bad])
                self.assertEqual(ctx.exception.code, 64)

    def test_the_dream_run_and_ground_refuse_non_positive_counts(self):
        sys.path.insert(
            0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "dream")
        )
        import memory_dream as dr
        import memory_dream_run as drun

        for build, argv in (
            (drun.build_parser, ["run", "--max-notes", "-1"]),
            (drun.build_parser, ["run", "--workers", "0"]),
            (dr.build_parser, ["ground", "--hunks", "x.json", "--max-hunks", "-1"]),
        ):
            with self.subTest(argv), self.assertRaises(SystemExit) as ctx:
                self.parse(build, argv)
            self.assertNotEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
