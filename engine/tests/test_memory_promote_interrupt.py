"""Promote and interrupts. Ctrl-C, SIGTERM and SIGHUP (a closed terminal) used to end a promote wherever they landed: notes replaced and no
marker, or, between the two moves of one note, a note missing from the memory folder. Each test sends real signals, from inside one step of
a real `memory_dream.py promote --apply` process, and looks at what is on disk afterwards.

The rule: from the first note to the last record the three signals are held back; between notes the promote asks whether one arrived and, if so,
stops and records what it replaced; at the end what arrived is consumed, not delivered, because the work it asked to stop is done or recorded."""

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
import os, signal, sys
sys.path.insert(0, %r)
import memory_snapshot as sn
import memory_dream as dr
import memory_dream_stage as ds
SIGS = [getattr(signal, name) for name in sys.argv[1].split(",")]
WHERE = sys.argv[2]
real_swap, calls = sn._swap_in, {"n": 0}

def fire():
    for sig in SIGS:
        signal.raise_signal(sig)

def swap(memory_dir, tmp, live, archive_dir, clean):
    calls["n"] += 1
    if WHERE == "swap:%%d" %% calls["n"]:
        fire()  # arrives while this note is being swapped
    done = real_swap(memory_dir, tmp, live, archive_dir, clean)
    if WHERE == "readback" and calls["n"] == 2:
        fire()
        with open(live, "a") as fh:
            fh.write("tampered\\n")  # the read-back check of the last note must fail
    return done

sn._swap_in = swap
if WHERE == "log":
    real_log = ds.log_or_note

    def log(*args, **kwargs):
        fire()  # arrives while the record is being written
        return real_log(*args, **kwargs)

    ds.log_or_note = log
if os.environ.get("DEAD_STREAMS"):  # stdout and stderr both end in a pipe that nobody reads (a closed terminal)
    reader, writer = os.pipe()
    os.close(reader)
    os.dup2(writer, 1)
    os.dup2(writer, 2)
sys.exit(dr.main(sys.argv[3:]))
"""
    % th.SCRIPTS
)


class InterruptedPromoteTests(PromoteBase):
    def promote_with(self, signals, where, dead_streams=False, ignore_hangup=False):
        run = self.two_note_run()
        digest = self.digest_of(run)
        before = {
            n: th.read(os.path.join(self.mem, n))
            for n in os.listdir(self.mem)
            if n.endswith(".md")
        }
        env = dict(os.environ, DEAD_STREAMS="1") if dead_streams else dict(os.environ)
        proc = subprocess.run(
            [
                sys.executable,
                "-B",
                "-I",
                "-c",
                DRIVER,
                signals,
                where,
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
            env=env,
            preexec_fn=(lambda: signal.signal(signal.SIGHUP, signal.SIG_IGN))
            if ignore_hangup
            else None,
        )
        return run, before, proc

    def marker(self, run):
        return json.loads(th.read(os.path.join(run, dr.MARKER)))

    def live(self, name):
        return th.read(os.path.join(self.mem, name))

    def test_a_signal_during_the_first_note_lets_it_finish_then_records_a_partial_promote(
        self,
    ):
        for name in ("SIGINT", "SIGTERM", "SIGHUP"):
            with self.subTest(name):
                self.setUp()
                run, before, proc = self.promote_with(name, "swap:1")
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
                    self.live("feedback-other.md"), before["feedback-other.md"]
                )
                self.assertEqual(
                    self.live("feedback-push.md"), before["feedback-push.md"]
                )
                self.assertEqual(
                    sorted(n for n in os.listdir(self.mem) if n.endswith(".md")),
                    sorted(before),
                )

    def test_two_different_signals_at_once_still_record_the_partial_promote(self):
        for pair in ("SIGHUP,SIGTERM", "SIGINT,SIGTERM", "SIGINT,SIGHUP"):
            with self.subTest(pair):
                self.setUp()
                run, before, proc = self.promote_with(pair, "swap:1")
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)
                marker = self.marker(run)
                self.assertEqual(
                    (marker["status"], marker["files"]),
                    ("partial", ["feedback-other.md"]),
                )
                self.assertEqual(
                    self.live("feedback-push.md"), before["feedback-push.md"]
                )
                self.assertIn("PARTIAL replaced", proc.stdout)

    def test_a_signal_during_the_last_note_is_a_finished_promote(self):
        for signals in ("SIGTERM", "SIGHUP,SIGTERM"):
            with self.subTest(signals):
                self.setUp()
                run, before, proc = self.promote_with(signals, "swap:2")
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertIn("PROMOTED 2 notes", proc.stdout)
                self.assertEqual(self.marker(run)["status"], "promoted")
                for name in ("feedback-other.md", "feedback-push.md"):
                    self.assertNotEqual(self.live(name), before[name])

    def test_a_signal_while_the_record_is_written_does_not_turn_a_finished_promote_into_exit_130(
        self,
    ):
        run, before, proc = self.promote_with("SIGINT,SIGTERM", "log")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PROMOTED 2 notes", proc.stdout)
        self.assertNotIn("INTERRUPTED", proc.stderr)
        self.assertEqual(self.marker(run)["status"], "promoted")

    def test_a_failed_read_back_is_still_an_error_when_a_signal_is_pending(self):
        run, before, proc = self.promote_with("SIGINT", "readback")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("read-back check failed", proc.stderr)
        marker = self.marker(run)
        self.assertEqual(
            (marker["status"], sorted(marker["files"])),
            ("partial", ["feedback-other.md", "feedback-push.md"]),
        )

    def test_a_closed_terminal_does_not_cost_the_record(self):
        # stdout and stderr both fail (a dead terminal gives EIO, a closed pipe EPIPE); the marker is what counts
        run, before, proc = self.promote_with("SIGHUP", "swap:1", dead_streams=True)
        marker = self.marker(run)
        self.assertEqual(
            (marker["status"], marker["files"]), ("partial", ["feedback-other.md"])
        )
        self.assertEqual(self.live("feedback-push.md"), before["feedback-push.md"])
        self.setUp()
        run, before, proc = self.promote_with(
            "SIGCONT", "swap:1", dead_streams=True
        )  # no signal that matters: a finished promote
        self.assertEqual(self.marker(run)["status"], "promoted")
        self.assertNotIn("Traceback", proc.stderr)

    def test_a_hangup_that_the_parent_ignored_stays_ignored(self):
        run, before, proc = self.promote_with(
            "SIGHUP", "swap:1", ignore_hangup=True
        )  # as under nohup
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.marker(run)["status"], "promoted")
        for name in ("feedback-other.md", "feedback-push.md"):
            self.assertNotEqual(self.live(name), before[name])

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
