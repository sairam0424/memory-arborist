"""Found by a third verification pass: the partial-promote record could itself fail (a full disk is the likeliest cause of both), and then the
restore command was never shown; a marker left empty read as 'already promoted'; and the recorded reason of a partial promote said that
nothing was replaced."""

import errno
import json
import os
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
