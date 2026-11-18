"""Promote and interrupts. Ctrl-C, SIGTERM and SIGHUP (a closed terminal) used to end a promote wherever they landed: notes replaced and no
marker, or, between the two moves of one note, a note missing from the memory folder. Each test sends a real signal, from inside the swap of
one note, to a real `memory_dream.py promote --apply` process."""

import json
import os
import signal
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import memtest_helpers as th  # noqa: E402
from test_memory_promote_edges import PromoteBase  # noqa: E402

sys.path.insert(0, th.SCRIPTS)
import memory_dream as dr  # noqa: E402

DRIVER = (
    """
import signal, sys
sys.path.insert(0, %r)
import memory_snapshot as sn
import memory_dream as dr
real, calls = sn._swap_in, {"n": 0}
SIG, AT = getattr(signal, sys.argv[1]), int(sys.argv[2])

def swap(*args, **kwargs):
    calls["n"] += 1
    if calls["n"] == AT:
        signal.raise_signal(SIG)  # arrives while this note is being swapped
    return real(*args, **kwargs)

sn._swap_in = swap
sys.exit(dr.main(sys.argv[3:]))
"""
    % th.SCRIPTS
)


class InterruptedPromoteTests(PromoteBase):
    def promote_with_signal(self, sig_name, at):
        run = self.two_note_run()
        digest = self.digest_of(run)
        before = {
            n: th.read(os.path.join(self.mem, n))
            for n in os.listdir(self.mem)
            if n.endswith(".md")
        }
        proc = subprocess.run(
            [
                sys.executable,
                "-B",
                "-I",
                "-c",
                DRIVER,
                sig_name,
                str(at),
                "--dir",
                self.mem,
                "--ops-root",
                self.root,
                "promote",
                os.path.basename(run),
                "--apply",
                "--expect",
                digest,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        return run, before, proc

    def marker(self, run):
        return json.loads(th.read(os.path.join(run, dr.MARKER)))

    def test_a_signal_during_the_first_note_lets_it_finish_then_records_a_partial_promote(
        self,
    ):
        for name in ("SIGINT", "SIGTERM", "SIGHUP"):
            with self.subTest(name):
                self.setUp()
                run, before, proc = self.promote_with_signal(name, 1)
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)
                self.assertIn("interrupted", proc.stderr)
                self.assertIn("PARTIAL replaced feedback-other.md", proc.stdout)
                self.assertIn("memory_hygiene.py restore --snapshot", proc.stdout)
                marker = self.marker(run)
                self.assertEqual(
                    (marker["status"], marker["files"]),
                    ("partial", ["feedback-other.md"]),
                )
                self.assertNotEqual(
                    th.read(os.path.join(self.mem, "feedback-other.md")),
                    before["feedback-other.md"],
                )
                self.assertEqual(
                    th.read(os.path.join(self.mem, "feedback-push.md")),
                    before["feedback-push.md"],
                )
                self.assertEqual(
                    sorted(n for n in os.listdir(self.mem) if n.endswith(".md")),
                    sorted(before),
                )

    def test_a_signal_during_the_last_note_is_a_finished_promote(self):
        run, before, proc = self.promote_with_signal("SIGTERM", 2)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PROMOTED 2 notes", proc.stdout)
        self.assertEqual(self.marker(run)["status"], "promoted")
        for name in ("feedback-other.md", "feedback-push.md"):
            self.assertNotEqual(th.read(os.path.join(self.mem, name)), before[name])

    def test_an_interrupt_outside_the_recorded_part_is_a_message_and_exit_130(self):
        run = self.two_note_run()
        real = dr._promotion_plan

        def interrupted(*args, **kwargs):
            raise KeyboardInterrupt

        dr._promotion_plan = interrupted
        try:
            try:
                code, out, err = self.cli("promote", os.path.basename(run))
            except KeyboardInterrupt:
                self.fail("a KeyboardInterrupt reached the caller of main()")
        finally:
            dr._promotion_plan = real
        self.assertEqual(code, 130, out + err)
        self.assertIn("INTERRUPTED", err)
        self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
