"""Found by a third verification pass: the partial-promote record could itself fail (a full disk is the likeliest cause of both), and then the
restore command was never shown; a marker left empty read as 'already promoted'; the recorded reason of a partial promote said that
nothing was replaced; the marker was written after the lock was released, so a promote that lost a race could not see it; a closed
output pipe on the PROMOTED line cost the marker; and a live note missing during preparation ended in a raw errno."""

import argparse
import builtins
import contextlib
import errno
import io
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import memtest_helpers as th  # noqa: E402
from test_memory_promote_edges import PromoteBase  # noqa: E402

sys.path.insert(0, th.SCRIPTS)
import memory_common as mc  # noqa: E402
import memory_dream as dr  # noqa: E402
import memory_registry as reg  # noqa: E402
import memory_snapshot as sn  # noqa: E402


class PartialRecordTests(PromoteBase):
    def promote(self, swap_failure, *patches):
        run = self.two_note_run()
        digest = self.digest_of(run)
        real, calls = sn._swap_in, {"n": 0}

        def wrapper(memory_dir, tmp, live, archive_dir, clean):
            calls["n"] += 1
            if calls["n"] == 2:
                raise swap_failure
            return real(memory_dir, tmp, live, archive_dir, clean)

        sn._swap_in = wrapper
        try:
            with contextlib_exit_stack(patches):
                result = self.cli(
                    "promote", os.path.basename(run), "--apply", "--expect", digest
                )
        finally:
            sn._swap_in = real
        return run, result

    def test_a_marker_that_cannot_be_written_still_shows_the_partial_line_and_the_way_back(
        self,
    ):
        broken = mock.patch.object(
            dr,
            "_write_marker",
            side_effect=OSError(errno.ENOSPC, "No space left on device"),
        )
        _run, (code, out, err) = self.promote(
            OSError(errno.EIO, "Input/output error"), broken
        )
        self.assertEqual(code, 1, out + err)
        self.assertIn("PARTIAL replaced", out)
        self.assertIn("memory_hygiene.py restore --snapshot", out)
        self.assertIn("WERE replaced", err)
        self.assertIn("do not promote this run again", err)
        self.assertNotIn("Traceback", out + err)

    def test_a_log_that_cannot_be_written_does_not_hide_the_partial_line_or_the_marker(
        self,
    ):
        broken = mock.patch.object(
            mc,
            "append_log",
            side_effect=OSError(errno.ENOSPC, "No space left on device"),
        )
        run, (code, out, err) = self.promote(
            OSError(errno.EIO, "Input/output error"), broken
        )
        self.assertEqual(code, 1, out + err)
        self.assertIn("PARTIAL replaced", out)
        self.assertEqual(
            json.loads(th.read(os.path.join(run, dr.MARKER)))["status"], "partial"
        )
        self.assertNotIn("Traceback", out + err)

    def test_a_log_that_cannot_be_written_after_a_finished_promote_is_a_note_not_a_failure(
        self,
    ):
        run = self.two_note_run()
        digest = self.digest_of(run)
        with mock.patch.object(
            mc,
            "append_log",
            side_effect=OSError(errno.ENOSPC, "No space left on device"),
        ):
            code, out, err = self.cli(
                "promote", os.path.basename(run), "--apply", "--expect", digest
            )
        self.assertEqual(code, 0, out + err)
        self.assertIn("PROMOTED 2 notes", out)
        self.assertIn("log entry could not be written", out + err)
        self.assertEqual(
            json.loads(th.read(os.path.join(run, dr.MARKER)))["status"], "promoted"
        )
        self.assertNotIn("Traceback", out + err)

    def test_the_recorded_reason_does_not_claim_that_nothing_was_replaced(self):
        failure = mc.HygieneError(
            "the path changed during the restore (a link); nothing was replaced; staged copy: /x/.restore-1.part"
        )
        run, (_code, out, err) = self.promote(failure)
        recorded = json.loads(th.read(os.path.join(run, dr.MARKER)))["error"]
        for text in (recorded, err):
            self.assertNotIn("nothing was replaced", text)
        self.assertIn("this note was not replaced", recorded)

    def test_a_marker_that_cannot_be_read_is_not_taken_for_a_finished_promote(self):
        run = self.two_note_run()
        th.write(os.path.join(run, dr.MARKER), "")  # what a failed write leaves
        code, _out, err = self.cli("promote", os.path.basename(run))
        self.assertEqual(code, 1)
        self.assertIn("cannot be read", err)
        self.assertIn("memory_hygiene.py", err)


class RaceAndPipeTests(PromoteBase):
    def test_the_marker_is_written_while_the_lock_is_still_held(self):
        run = self.two_note_run()
        digest = self.digest_of(run)
        held, real = [], dr._write_marker

        def watching(run_dir, data, name=dr.MARKER):
            try:
                with reg.ops_lock(self.ops):  # takes the lock only if nobody holds it
                    held.append(False)
            except mc.HygieneError:
                held.append(True)
            return real(run_dir, data, name)

        with mock.patch.object(dr, "_write_marker", watching):
            code, out, err = self.cli("promote", os.path.basename(run), "--apply", "--expect", digest)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(held, [True], "the marker was written after the lock was released: a promote that lost the race cannot see it")

    def test_the_partial_marker_is_written_while_the_lock_is_still_held_too(self):
        run = self.two_note_run()
        digest = self.digest_of(run)
        held, real = [], dr._write_marker

        def watching(run_dir, data, name=dr.MARKER):
            try:
                with reg.ops_lock(self.ops):
                    held.append(False)
            except mc.HygieneError:
                held.append(True)
            return real(run_dir, data, name)

        with mock.patch.object(dr, "_write_marker", watching):
            code, out, err = self.with_swap(
                2, lambda: self.cli("promote", os.path.basename(run), "--apply", "--expect", digest)
            )
        self.assertEqual(code, 1, out + err)
        self.assertEqual(held, [True], "the partial marker was written after the lock was released")

    def test_a_loser_built_from_a_stale_plan_records_nothing_and_says_the_winner_finished(self):
        # a guard, not a reproduction of the race itself: it passed before the marker moved inside the lock, because the race needs two
        # processes; it pins what the loser must say and leave behind once the winner's marker exists
        run = self.two_note_run()
        digest = self.digest_of(run)
        snap, _accepted, touched, _digest = dr._promotion_plan(self.mem, self.ops, run)
        code, out, err = self.cli("promote", os.path.basename(run), "--apply", "--expect", digest)
        self.assertEqual(code, 0, out + err)
        log_before = th.read(os.path.join(self.ops, "log.md"))
        args = argparse.Namespace(run=os.path.basename(run), now=1_700_000_500)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), self.assertRaises(mc.HygieneError):
            dr._apply_promotion(args, self.mem, self.ops, run, snap, touched)
        self.assertIn("another promote of this run finished first", buffer.getvalue())
        self.assertEqual([n for n in os.listdir(run) if n.startswith("promote-failed")], [])
        self.assertEqual(th.read(os.path.join(self.ops, "log.md")), log_before)

    def test_a_live_note_that_vanishes_while_the_copies_are_prepared_is_a_message_not_a_raw_errno(self):
        run = self.two_note_run()
        rel = sorted(os.listdir(os.path.join(run, "memory")))[0]
        os.remove(os.path.join(self.mem, rel))  # another promote has the note out of the way for a moment
        with self.assertRaises(mc.HygieneError) as ctx:
            dr._prepare_install(self.mem, self.ops, run, [rel], {})
        self.assertIn(rel, str(ctx.exception))

    def test_a_closed_output_pipe_on_the_promoted_line_does_not_cost_the_marker(self):
        run = self.two_note_run()
        digest = self.digest_of(run)
        real_print = builtins.print

        def flaky(*args, **kwargs):
            if args and str(args[0]).startswith("PROMOTED") and kwargs.get("file") is None:
                raise BrokenPipeError(errno.EPIPE, "Broken pipe")
            return real_print(*args, **kwargs)

        with mock.patch("builtins.print", flaky):
            code, out, err = self.cli("promote", os.path.basename(run), "--apply", "--expect", digest)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(json.loads(th.read(os.path.join(run, dr.MARKER)))["status"], "promoted")
        self.assertIn("PROMOTED 2 notes", err)  # the line goes to the error stream when the output stream is gone
        self.assertNotIn("Traceback", out + err)


class RealStreamTests(PromoteBase):
    def test_a_real_closed_pipe_still_shows_the_way_back_on_the_error_stream_and_writes_the_marker(self):
        # no mock: the documented form (python3 -B -I) with stdout on a pipe nobody reads. The output is block-buffered there, so the
        # failure only shows at a flush, and the line has to be flushed inside the fallback for the fallback to fire
        run = self.two_note_run()
        digest = self.digest_of(run)
        reader, writer = os.pipe()
        os.close(reader)
        proc = subprocess.run(
            [sys.executable, "-B", "-I", os.path.join(th.SCRIPTS, "memory_dream.py"), "--dir", self.mem, "--ops-root", self.root,
             "promote", os.path.basename(run), "--apply", "--expect", digest],
            stdout=writer, stderr=subprocess.PIPE, text=True, timeout=120,
        )
        os.close(writer)
        self.assertEqual(json.loads(th.read(os.path.join(run, dr.MARKER)))["status"], "promoted")
        self.assertIn("PROMOTED 2 notes", proc.stderr)

    def test_a_promote_refused_because_the_lock_is_held_never_prints_replacing(self):
        run = self.two_note_run()
        digest = self.digest_of(run)
        with reg.ops_lock(self.ops):
            code, out, err = self.cli("promote", os.path.basename(run), "--apply", "--expect", digest)
        self.assertEqual(code, 1, out + err)
        self.assertIn("holds the lock", err)
        self.assertNotIn("replacing", out)
        self.assertFalse(os.path.exists(os.path.join(run, dr.MARKER)))


class contextlib_exit_stack:  # noqa: N801 - a tiny with-statement over several mock patches
    def __init__(self, patches):
        self.patches = patches

    def __enter__(self):
        for patch in self.patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in reversed(self.patches):
            patch.stop()
        return False


if __name__ == "__main__":
    unittest.main()
