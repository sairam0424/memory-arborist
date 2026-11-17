"""Found by the independent code review: a promote that failed with anything but a clean refusal left no marker, no log entry and no
way back on screen, and the loser of a concurrent promote was told the run could be promoted again."""

import errno
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import memtest_helpers as th  # noqa: E402
from test_memory_promote_edges import PromoteBase  # noqa: E402

sys.path.insert(0, th.SCRIPTS)
import memory_common as mc  # noqa: E402
import memory_dream as dr  # noqa: E402
import memory_snapshot as sn  # noqa: E402


def marker_of(run):
    return json.loads(th.read(os.path.join(run, dr.MARKER)))


class NonCleanFailureTests(PromoteBase):
    def promote_with(self, swap):
        run = self.two_note_run()
        digest = self.digest_of(run)
        real, calls = sn._swap_in, {"n": 0}

        def wrapper(memory_dir, tmp, live, archive_dir, clean):
            calls["n"] += 1
            return swap(real, calls["n"], memory_dir, tmp, live, archive_dir, clean)

        sn._swap_in = wrapper
        try:
            result = self.cli(
                "promote", os.path.basename(run), "--apply", "--expect", digest
            )
        finally:
            sn._swap_in = real
        return run, result

    def test_an_oserror_while_replacing_the_second_note_is_a_partial_promote_with_a_way_back(
        self,
    ):
        def swap(real, n, memory_dir, tmp, live, archive_dir, clean):
            if n == 2:
                raise OSError(errno.EIO, "Input/output error")
            return real(memory_dir, tmp, live, archive_dir, clean)

        run, (code, out, err) = self.promote_with(swap)
        self.assertEqual(code, 1, out + err)
        marker = marker_of(run)
        self.assertEqual(marker["status"], "partial")
        self.assertEqual(len(marker["files"]), 1)
        self.assertIn("PARTIAL replaced", out)
        self.assertIn(
            "memory_hygiene.py restore --snapshot " + marker["pre_snapshot"], out
        )
        self.assertNotIn("Traceback", out + err)

    def test_a_live_note_that_vanishes_mid_promote_is_a_partial_promote_not_a_raw_error(
        self,
    ):
        def swap(real, n, memory_dir, tmp, live, archive_dir, clean):
            result = real(memory_dir, tmp, live, archive_dir, clean)
            if n == 1:
                for name in os.listdir(memory_dir):
                    if (
                        name.endswith(".md")
                        and name != "MEMORY.md"
                        and os.path.join(memory_dir, name) != live
                    ):
                        os.remove(
                            os.path.join(memory_dir, name)
                        )  # a live session removed the other note
                        break
            return result

        run, (code, out, err) = self.promote_with(swap)
        self.assertEqual(code, 1, out + err)
        self.assertEqual(marker_of(run)["status"], "partial")
        self.assertIn("PARTIAL replaced", out)
        code, _out, err = self.cli("promote", os.path.basename(run))
        self.assertIn("already partly promoted", err)

    def test_a_marker_that_cannot_be_written_after_the_replacement_still_shows_the_way_back(
        self,
    ):
        run = self.two_note_run()
        digest = self.digest_of(run)
        real = dr._write_marker

        def broken(run_dir, data, name=dr.MARKER):
            raise OSError(errno.ENOSPC, "No space left on device")

        dr._write_marker = broken
        try:
            code, out, err = self.cli(
                "promote", os.path.basename(run), "--apply", "--expect", digest
            )
        finally:
            dr._write_marker = real
        self.assertEqual(code, 1, out + err)
        self.assertIn(
            "PROMOTED 2 notes; pre-promote snapshot", out
        )  # printed before the marker is written
        self.assertIn("WERE replaced", err)
        self.assertIn("do not promote this run again", err)


class ConcurrentLoserTests(PromoteBase):
    def test_the_loser_of_a_race_is_told_another_promote_finished_first(self):
        run = self.two_note_run()
        digest = self.digest_of(run)
        real = sn._swap_in

        def lose(memory_dir, tmp, live, archive_dir, clean):
            dr._write_marker(
                run, {"status": "promoted", "files": ["x.md"], "pre_snapshot": "S"}
            )  # the winner finished meanwhile
            raise mc.HygieneError("simulated failure before anything was replaced")

        sn._swap_in = lose
        try:
            code, out, err = self.cli(
                "promote", os.path.basename(run), "--apply", "--expect", digest
            )
        finally:
            sn._swap_in = real
        self.assertEqual(code, 1, out + err)
        self.assertIn("another promote of this run finished first", out)
        self.assertNotIn("the run can be promoted again", out)
        self.assertEqual(
            [n for n in os.listdir(run) if n.startswith("promote-failed")], []
        )


if __name__ == "__main__":
    unittest.main()
