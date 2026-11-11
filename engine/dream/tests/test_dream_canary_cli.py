"""The canary spends money, so asking it for help or giving it a wrong argument must never start a run.

Each case runs it as a real command with a fake `claude` first on PATH. The fake only records that it was called; the
real binary is not on that PATH, so a failing test cannot spend anything either.
"""

import glob
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CANARY = os.path.join(os.path.dirname(HERE), "memory_dream_canary.py")


class CanaryCommandLineTests(unittest.TestCase):
    def run_canary(self, args, reply=None):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = os.path.join(tmp, "bin")
            os.makedirs(bin_dir)
            marker = os.path.join(tmp, "claude-was-called")
            fake = os.path.join(bin_dir, "claude")
            body = "exit 1" if reply is None else "cat <<'JSON'\n%s\nJSON" % reply
            with open(fake, "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\ntouch '%s'\n%s\n" % (marker, body))
            os.chmod(fake, 0o755)
            env = {"PATH": bin_dir + ":/usr/bin:/bin", "HOME": tmp, "TMPDIR": tmp}
            done = subprocess.run(
                [sys.executable, "-I", CANARY] + args,
                capture_output=True,
                text=True,
                env=env,
                timeout=60,
            )
            return (
                done,
                os.path.exists(marker),
                glob.glob(os.path.join(tmp, "dream_canary_*")),
            )

    def test_help_prints_usage_and_starts_nothing(self):
        for flag in ("--help", "-h"):
            with self.subTest(flag):
                done, claude_called, fixtures = self.run_canary([flag])
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertIn("usage", done.stdout.lower())
                self.assertIn("spends", done.stdout.lower())
                self.assertFalse(claude_called)
                self.assertEqual(fixtures, [])

    def test_a_pipeline_that_aborts_before_writing_anything_ends_in_a_canary_line_not_a_traceback(self):
        # a child that reports a tool makes the pipeline abort before it writes any hunk file
        reply = '[{"type":"system","subtype":"init","tools":["Bash"]},{"type":"result","subtype":"success","result":"{}","total_cost_usd":0.0,"is_error":false}]'
        done, claude_called, _fixtures = self.run_canary([], reply=reply)
        self.assertTrue(claude_called)
        self.assertEqual(done.returncode, 1)
        self.assertNotIn("Traceback", done.stderr)
        self.assertIn("CANARY", done.stdout)
        self.assertIn("positive=0", done.stdout)

    def test_an_unknown_argument_is_refused_before_anything_runs(self):
        done, claude_called, fixtures = self.run_canary(["--no-such-option"])
        self.assertEqual(done.returncode, 2)
        self.assertIn("unrecognized arguments", done.stderr)
        self.assertFalse(claude_called)
        self.assertEqual(fixtures, [])


if __name__ == "__main__":
    unittest.main()
