"""Found by a third verification pass: the --save preflight looked only at the folder named on the command line. A file that exists but
cannot be written, a symbolic link whose target lies in a folder that does not exist, and a link loop all passed the check and failed
at the very end, after the paid calls, with a traceback."""

import errno
import os
import stat
import sys
import tempfile
import threading
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

    def baseline_run_without_save(self, runner):
        argv = ["--questions", self.qfile, "--dir", self.mem, "--ops-root", self.root, "--no-log", "--workers", "1", "--arm", "baseline"]
        return run_main(argv, runner)

    def hold_a_reader_on(self, fifo):
        """A thread that opens the named pipe for reading (which waits for a writer), and a cleanup that lets it go."""
        reader = threading.Thread(target=lambda: open(fifo, "rb").read(), daemon=True)
        reader.start()

        def release():
            try:
                os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))  # succeeds only while the reader is waiting
            except OSError:
                pass
            reader.join(5)

        self.addCleanup(release)

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

    def test_a_write_that_fails_after_the_paid_calls_keeps_the_previous_baseline_and_names_the_partial_copy(self):
        save = os.path.join(self.scratch, "late.json")
        with open(save, "w", encoding="utf-8") as fh:
            fh.write('{"old": "baseline"}')
        with mock.patch.object(mrc.json, "dump", side_effect=OSError(errno.ENOSPC, "No space left on device")):
            code, out, err = self.baseline_run(save, MemoryReadingRunner())
        self.assertEqual(code, 1, out + err)
        self.assertNotIn("Traceback", out + err)
        self.assertIn("could not be saved", err)
        self.assertIn("No space left on device", err)
        self.assertIn("previous file at that path is untouched", err)
        self.assertIn(".baseline-", err)  # the partial copy is named: this tool never deletes
        self.assertIn("incomplete", err)  # the write itself failed: that copy is NOT a usable baseline
        with open(save, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), '{"old": "baseline"}')

    def test_a_folder_that_cannot_take_the_new_file_is_refused_even_when_the_old_file_is_writable(self):
        if os.geteuid() == 0:
            self.skipTest("root can write anywhere")
        folder = os.path.join(self.scratch, "readonly")
        os.mkdir(folder)
        save = os.path.join(folder, "b.json")
        with open(save, "w", encoding="utf-8") as fh:
            fh.write("{}")
        os.chmod(folder, stat.S_IRUSR | stat.S_IXUSR)  # the file may be written, the folder may not: the new file is made next to it
        self.addCleanup(os.chmod, folder, stat.S_IRWXU)
        self.refused(save)
        os.chmod(folder, stat.S_IRUSR | stat.S_IWUSR)  # writable but not searchable
        self.refused(os.path.join(folder, "new.json"))

    def test_a_file_name_too_long_for_its_partial_copy_is_refused_before_the_first_paid_call(self):
        self.refused(os.path.join(self.scratch, "n" * 240 + ".json"))

    def test_the_new_file_is_flushed_before_it_is_synced(self):
        save = os.path.join(self.scratch, "synced.json")
        sizes, real_fsync = [], os.fsync

        def sync(fd):
            sizes.append(os.fstat(fd).st_size)  # what is on disk when the sync is asked for
            return real_fsync(fd)

        with mock.patch.object(mrc.os, "fsync", sync):
            code, out, err = self.baseline_run(save, MemoryReadingRunner())
        self.assertEqual(code, 0, out + err)
        self.assertEqual(len(sizes), 1)
        self.assertGreater(sizes[0], 100, "the sync was asked for before the data left the write buffer")

    def test_a_failed_move_leaves_the_previous_baseline_too(self):
        save = os.path.join(self.scratch, "move.json")
        with open(save, "w", encoding="utf-8") as fh:
            fh.write('{"old": "baseline"}')
        with mock.patch.object(mrc.os, "replace", side_effect=OSError(errno.EXDEV, "Invalid cross-device link")):
            code, out, err = self.baseline_run(save, MemoryReadingRunner())
        self.assertEqual(code, 1, out + err)
        self.assertIn("could not be moved into place", err)
        self.assertIn("complete", err)  # the copy is the whole result of a paid run: the person is told to move it, not to remove it
        self.assertIn("move it", err)
        self.assertNotIn("safe to remove", err)
        kept = [n for n in os.listdir(self.scratch) if n.startswith(".baseline-")]
        self.assertEqual(len(kept), 1)
        self.assertIn(kept[0], err)
        with open(save, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), '{"old": "baseline"}')

    def test_the_new_file_is_private_is_made_next_to_its_place_and_leaves_no_partial_copy(self):
        save = os.path.join(self.scratch, "private.json")
        with open(save, "w", encoding="utf-8") as fh:
            fh.write("{}")
        os.chmod(save, 0o644)  # an older, readable file: the new one holds the person's questions and answers, so it is private
        moves, events = [], []
        real_replace, real_fsync = os.replace, os.fsync

        def spy(src, dst):
            moves.append((src, dst))
            events.append("replace")
            return real_replace(src, dst)

        def sync(fd):
            events.append("fsync")
            return real_fsync(fd)

        with mock.patch.object(mrc.os, "replace", spy), mock.patch.object(mrc.os, "fsync", sync):
            code, out, err = self.baseline_run(save, MemoryReadingRunner())
        self.assertEqual(code, 0, out + err)
        self.assertEqual(events, ["fsync", "replace"], "the new file must be on disk before it takes the old one's place")
        self.assertEqual(len(moves), 1)
        source, destination = moves[0]
        self.assertEqual(os.path.dirname(source), os.path.dirname(os.path.realpath(save)))  # same folder: the move is atomic
        self.assertTrue(os.path.basename(source).startswith(".baseline-"))
        self.assertEqual(destination, os.path.realpath(save))
        self.assertEqual(stat.S_IMODE(os.stat(save).st_mode), 0o600)
        self.assertEqual([n for n in os.listdir(self.scratch) if n.startswith(".baseline-")], [])

    def test_a_link_stays_a_link_and_the_file_it_leads_to_is_replaced(self):
        real = os.path.join(self.scratch, "target.json")
        with open(real, "w", encoding="utf-8") as fh:
            fh.write("{}")
        link = os.path.join(self.scratch, "link-to-target.json")
        os.symlink(real, link)
        code, out, err = self.baseline_run(link, MemoryReadingRunner())
        self.assertEqual(code, 0, out + err)
        self.assertTrue(os.path.islink(link))
        self.assertGreater(os.path.getsize(real), 2)

    def test_a_named_pipe_or_another_file_that_is_not_a_regular_file_is_refused(self):
        fifo = os.path.join(self.scratch, "fifo.json")
        os.mkfifo(fifo)
        self.hold_a_reader_on(fifo)  # so that a judge that does open it fails the test instead of waiting for ever
        self.refused(fifo)  # opening it for writing would wait for a reader for ever, after the paid calls
        link = os.path.join(self.scratch, "fifo-link.json")
        os.symlink(fifo, link)
        self.refused(link)

    def test_an_empty_save_path_is_refused_and_a_missing_one_is_noted_before_the_first_paid_call(self):
        self.refused("")
        code, out, err = self.baseline_run_without_save(MemoryReadingRunner())
        self.assertEqual(code, 0, out + err)
        self.assertLess(out.index("NOTE: no --save"), out.index("COST"), "the note must come before the paid calls are summed up")


if __name__ == "__main__":
    unittest.main()
