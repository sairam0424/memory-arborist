"""Found by a third verification pass: the --save preflight looked only at the folder named on the command line. A file that exists but
cannot be written, a symbolic link whose target lies in a folder that does not exist, and a link loop all passed the check and failed
at the very end, after the paid calls, with a traceback."""

import errno
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

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

import memory_recall as mrc  # noqa: E402


class SavePreflightTests(TempRoot):
    def setUp(self):
        self.mem = memory_with_phrases()
        self.root = tempfile.mkdtemp(prefix="recops_")
        sn.take_snapshot(self.mem, mc.ops_dir_for(self.mem, self.root), now=T0)
        self.qfile = write_questions(questions())
        self.scratch = tempfile.mkdtemp(prefix="recsave_")

    def baseline_run(self, save, runner):
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
        ]
        return run_main(argv + ["--arm", "baseline", "--save", save], runner)

    def refused(self, path):
        runner = MemoryReadingRunner()
        code, out, err = self.baseline_run(path, runner)
        self.assertEqual(code, 1, out + err)
        self.assertEqual(runner.calls, [], "a paid call was made before the refusal")
        self.assertIn("--save", err)
        self.assertNotIn("Traceback", out + err)

    def test_an_existing_file_that_cannot_be_written_is_refused_before_any_paid_call(
        self,
    ):
        if os.geteuid() == 0:
            self.skipTest("root can write anywhere")
        path = os.path.join(self.scratch, "locked.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{}")
        os.chmod(path, stat.S_IRUSR)
        self.addCleanup(os.chmod, path, stat.S_IRUSR | stat.S_IWUSR)
        self.refused(path)

    def test_a_dangling_link_into_a_folder_that_does_not_exist_is_refused(self):
        link = os.path.join(self.scratch, "link.json")
        os.symlink(os.path.join(self.scratch, "no-such-folder", "b.json"), link)
        self.refused(link)

    def test_a_link_that_leads_back_to_itself_is_refused(self):
        loop = os.path.join(self.scratch, "loop.json")
        os.symlink(loop, loop)
        self.refused(loop)

    def test_a_link_to_a_folder_is_refused(self):
        link = os.path.join(self.scratch, "dirlink.json")
        os.symlink(self.scratch, link)
        self.refused(link)

    def test_a_dangling_link_into_an_existing_folder_still_works(self):
        real = os.path.join(self.scratch, "real.json")
        link = os.path.join(self.scratch, "ok-dangling.json")
        os.symlink(real, link)
        code, out, err = self.baseline_run(link, MemoryReadingRunner())
        self.assertEqual(code, 0, out + err)
        self.assertGreater(os.path.getsize(real), 2)

    def test_a_link_to_an_existing_writable_file_still_works(self):
        real = os.path.join(self.scratch, "existing.json")
        with open(real, "w", encoding="utf-8") as fh:
            fh.write("{}")
        link = os.path.join(self.scratch, "ok-file.json")
        os.symlink(real, link)
        code, out, err = self.baseline_run(link, MemoryReadingRunner())
        self.assertEqual(code, 0, out + err)
        self.assertGreater(os.path.getsize(real), 2)

    def test_a_write_that_fails_after_the_paid_calls_is_a_message_that_says_what_was_lost(
        self,
    ):
        save = os.path.join(self.scratch, "late.json")
        real_open = os.open

        def failing(path, flags, mode=0o777, **kwargs):
            if path == save and flags & os.O_CREAT:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_open(path, flags, mode, **kwargs)

        with mock.patch.object(mrc.os, "open", failing):
            code, out, err = self.baseline_run(save, MemoryReadingRunner())
        self.assertEqual(code, 1, out + err)
        self.assertNotIn("Traceback", out + err)
        self.assertIn("could not be saved", err)
        self.assertIn("No space left on device", err)
        self.assertFalse(os.path.exists(save))


if __name__ == "__main__":
    unittest.main()
