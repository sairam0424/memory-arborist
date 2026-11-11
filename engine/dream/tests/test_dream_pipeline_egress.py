"""The dream run refuses to send notes that hold secret shapes, unless the flag says so (memory_egress)."""

import contextlib
import io
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_dream_pipeline import T0, Base, ScriptedRunner, mc, run, sn, th  # noqa: E402

LEAK = th.fake_github_token()


class EgressTests(Base):
    def setUp(self):
        super().setUp()
        th.write(os.path.join(self.ops, "pinned.txt"), "- [Style](feedback-style.md) how to answer\n")
        with open(os.path.join(self.mem, "feedback-other.md"), "a", encoding="utf-8") as fh:
            fh.write("\nthe key is %s\n" % LEAK)
        self.leaky = sn.take_snapshot(self.mem, self.ops, now=T0 + 10)["id"]

    def main(self, *extra, snapshot=None):
        runner = ScriptedRunner()
        out, err = io.StringIO(), io.StringIO()
        argv = ["--dir", self.mem, "--ops-root", self.root, "run", "--snapshot", snapshot or self.leaky, "--workers", "1", *extra]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = run.main(argv, runner=runner)
        return code, out.getvalue(), err.getvalue(), runner

    def test_a_snapshot_with_a_secret_shape_is_refused_before_any_lane_call(self):
        code, _out, err, runner = self.main()
        self.assertEqual(code, 4)
        self.assertEqual(runner.calls, [])
        self.assertIn("REFUSED", err)
        self.assertIn("feedback-other.md", err)
        self.assertNotIn(LEAK, err)

    def test_the_flag_lets_the_run_go_ahead_with_one_warning_line(self):
        code, out, _err, runner = self.main("--allow-secret-shapes")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.count("WARN --allow-secret-shapes"), 1)
        self.assertTrue(runner.calls)

    def test_the_snapshot_that_was_selected_is_the_one_scanned_not_the_latest(self):
        code, out, _err, runner = self.main(snapshot=self.snap)
        self.assertEqual(code, 0, out)
        self.assertTrue(runner.calls)

    def test_a_damaged_snapshot_is_reported_as_damaged_not_as_a_refusal(self):
        damaged = os.path.join(self.ops, "snapshots", self.leaky, "memory", "project-deploys.md")
        os.chmod(damaged, 0o600)
        th.write(damaged, "tampered\n")
        code, _out, err, runner = self.main()
        self.assertEqual(code, 1)
        self.assertIn("failed verification", err)
        self.assertNotIn("REFUSED", err)
        self.assertEqual(runner.calls, [])

    def test_the_override_flag_cannot_be_given_as_an_abbreviation(self):
        for abbreviation in ("--allow", "--allow-s", "--allow-secret"):
            with self.subTest(abbreviation):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    run.build_parser().parse_args(["--dir", self.mem, "run", abbreviation])

    def test_the_environment_does_not_switch_the_override_on(self):
        saved = os.environ.get("MEMORY_SHEPHERD_ALLOW_SECRET_SHAPES")
        os.environ["MEMORY_SHEPHERD_ALLOW_SECRET_SHAPES"] = "1"
        try:
            args = run.build_parser().parse_args(["--dir", self.mem, "run"])
        finally:
            if saved is None:
                del os.environ["MEMORY_SHEPHERD_ALLOW_SECRET_SHAPES"]
            else:
                os.environ["MEMORY_SHEPHERD_ALLOW_SECRET_SHAPES"] = saved
        self.assertFalse(args.allow_secret_shapes)

    def test_a_secret_shape_in_the_index_is_refused_too(self):
        with open(os.path.join(self.mem, "MEMORY.md"), "a", encoding="utf-8") as fh:
            fh.write("- key %s\n" % LEAK)
        newest = sn.take_snapshot(self.mem, self.ops, now=T0 + 20)["id"]
        code, _out, err, _runner = self.main(snapshot=newest)
        self.assertEqual(code, 4)
        self.assertIn("MEMORY.md", err)


if __name__ == "__main__":
    unittest.main()
