"""A promote that is killed (SIGKILL, a power cut, the machine going down) cannot record anything: until now the next promote said only that a
note "changed since the snapshot", without saying that an earlier promote had died halfway, which notes it had replaced, or the way back. A
promote now writes an "attempt started" marker, with the pre-promote snapshot and the notes it is going to replace, before it replaces the first
note, and the marker is complete and on disk (flushed, fsynced, moved into place, folder fsynced) before the first note moves. A later promote that
finds such an attempt with no outcome says what happened and gives the restore commands; one that finds that none of its notes is in place closes it
and goes on. The tests that need a dead promote kill a real `memory_dream.py promote --apply` process with SIGKILL, before or after the first swap;
the others call the code that decides, or break the write, directly."""

import contextlib
import io
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import memtest_helpers as th  # noqa: E402
from test_memory_promote_edges import PromoteBase  # noqa: E402

sys.path.insert(0, th.SCRIPTS)
import memory_common as mc  # noqa: E402
import memory_dream as dr  # noqa: E402
import memory_hygiene as mh  # noqa: E402
import memory_dream_attempt as da  # noqa: E402
import memory_registry as reg  # noqa: E402
import memory_snapshot as sn  # noqa: E402

DRIVER = (
    """
import os, signal, sys
sys.path.insert(0, %r)
import memory_snapshot as sn
import memory_dream as dr
real, calls, WHEN = sn._swap_in, {"n": 0}, sys.argv[1]

def swap(*args, **kwargs):
    calls["n"] += 1
    if WHEN == "before-first" and calls["n"] == 1:
        os.kill(os.getpid(), signal.SIGKILL)  # killed before any note was replaced
    done = real(*args, **kwargs)
    if WHEN == "after-first" and calls["n"] == 1:
        os.kill(os.getpid(), signal.SIGKILL)  # killed with one note replaced and the other not
    return done

sn._swap_in = swap
sys.exit(dr.main(sys.argv[2:]))
"""
    % th.SCRIPTS
)


class CrashedPromoteTests(PromoteBase):
    def crash(self, when, run=None):
        run = run or self.two_note_run()
        digest = self.digest_of(run)
        self.before = {
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
                when,
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
        self.assertEqual(proc.returncode, -signal.SIGKILL, proc.stdout + proc.stderr)
        return run, digest

    def files(self, run, prefix):
        return sorted(n for n in os.listdir(run) if n.startswith(prefix))

    def test_the_killed_promote_leaves_a_started_marker_and_no_outcome(self):
        run, _digest = self.crash("after-first")
        self.assertEqual(len(self.files(run, "promote-started")), 1)
        self.assertFalse(os.path.exists(os.path.join(run, dr.MARKER)))
        started = json.loads(
            th.read(os.path.join(run, self.files(run, "promote-started")[0]))
        )
        self.assertEqual(started["files"], ["feedback-other.md", "feedback-push.md"])
        self.assertTrue(started["pre_snapshot"])

    def test_the_next_promote_says_what_happened_which_notes_were_replaced_and_how_to_undo_it(
        self,
    ):
        run, digest = self.crash("after-first")
        for argv in (
            ["promote", os.path.basename(run)],
            ["promote", os.path.basename(run), "--apply", "--expect", digest],
        ):
            with self.subTest(argv[-1]):
                code, out, err = self.cli(*argv)
                self.assertEqual(code, 1, out + err)
                self.assertIn("did not finish", err)
                in_place = re.search(r"left them: (.+?) \(old copies", err)
                self.assertIsNotNone(in_place, err)
                self.assertEqual(in_place.group(1), "feedback-other.md")  # the replaced one, not the one it was only going to replace
                started = json.loads(
                    th.read(os.path.join(run, self.files(run, "promote-started")[0]))
                )
                self.assertIn(
                    "restore --snapshot %s --file <note> --apply --force"
                    % started["pre_snapshot"],
                    err,
                )
                self.assertNotIn("replacing", out)
                self.assertEqual(
                    th.read(os.path.join(self.mem, "feedback-push.md")),
                    self.before["feedback-push.md"],
                )

    def test_the_documented_way_back_restores_the_replaced_note(self):
        run, _digest = self.crash("after-first")
        code, _out, err = self.cli("promote", os.path.basename(run))
        started = json.loads(
            th.read(os.path.join(run, self.files(run, "promote-started")[0]))
        )
        self.assertNotEqual(
            th.read(os.path.join(self.mem, "feedback-other.md")),
            self.before["feedback-other.md"],
        )
        argv = [
            "--dir", self.mem, "--ops-root", self.root, "restore",
            "--snapshot", started["pre_snapshot"], "--file", "feedback-other.md", "--apply", "--force",
        ]
        with contextlib.redirect_stdout(io.StringIO()):
            rc = mh.main(argv)
        self.assertEqual(rc, 0)
        self.assertEqual(
            th.read(os.path.join(self.mem, "feedback-other.md")),
            self.before["feedback-other.md"],
        )
        code, out, err = self.cli("promote", os.path.basename(run))
        self.assertEqual(
            code, 0, out + err
        )  # after the undo the same run can be promoted again
        self.assertIn("DIGEST", out)

    def test_a_promote_that_died_before_replacing_anything_is_closed_and_the_run_goes_through(
        self,
    ):
        run, digest = self.crash("before-first")
        self.assertEqual(len(self.files(run, "promote-started")), 1)
        code, out, err = self.cli(
            "promote", os.path.basename(run), "--apply", "--expect", digest
        )
        self.assertEqual(code, 0, out + err)
        self.assertIn("PROMOTED 2 notes", out)
        self.assertEqual(
            json.loads(th.read(os.path.join(run, dr.MARKER)))["status"], "promoted"
        )
        self.assertEqual(
            len(self.files(run, "promote-started")), 2
        )  # the dead attempt and the new one
        failed = self.files(run, "promote-failed")
        self.assertEqual(len(failed), 1)
        self.assertIn(
            "did not finish", json.loads(th.read(os.path.join(run, failed[0])))["error"]
        )

    def test_a_promote_that_is_running_right_now_is_not_taken_for_a_dead_one(self):
        run, _digest = self.crash("after-first")
        with reg.ops_lock(self.ops):  # another run holds the lock
            code, out, err = self.cli("promote", os.path.basename(run))
        self.assertEqual(code, 1, out + err)
        self.assertIn("holds the lock", err)
        self.assertNotIn("did not finish", err)

    def test_a_normal_promote_and_a_clean_failure_leave_balanced_markers(self):
        run = self.two_note_run()
        self.with_swap(
            1,
            lambda: self.cli(
                "promote",
                os.path.basename(run),
                "--apply",
                "--expect",
                self.digest_of(run),
            ),
        )
        self.assertEqual(
            (
                len(self.files(run, "promote-started")),
                len(self.files(run, "promote-failed")),
            ),
            (1, 1),
        )  # failed before replacing
        code, out, err = self.apply(
            run
        )  # the cause is fixed: the same run goes through, with nothing left dangling
        self.assertEqual(code, 0, out + err)
        self.assertEqual(
            (
                len(self.files(run, "promote-started")),
                len(self.files(run, "promote-failed")),
            ),
            (2, 1),
        )
        self.assertEqual(da.dangling_attempts(run), 0)
        code, out, err = self.cli("promote", os.path.basename(run))
        self.assertIn("already promoted", err)

    def test_a_marker_that_cannot_be_written_stops_the_promote_before_it_replaces_anything(
        self,
    ):
        run = self.two_note_run()
        digest = self.digest_of(run)
        before = {
            n: th.read(os.path.join(self.mem, n))
            for n in os.listdir(self.mem)
            if n.endswith(".md")
        }
        if os.geteuid() == 0:
            self.skipTest("root can write anywhere")
        os.makedirs(
            os.path.join(run, "install"), mode=0o700
        )  # the copies can still be made; only a new file in the run folder cannot
        os.chmod(run, 0o500)
        try:
            code, out, err = self.cli(
                "promote", os.path.basename(run), "--apply", "--expect", digest
            )
        finally:
            os.chmod(run, 0o700)
        self.assertEqual(code, 1, out + err)
        self.assertIn("nothing was replaced", err)
        self.assertNotIn("replacing", out)
        self.assertEqual(
            {n: th.read(os.path.join(self.mem, n)) for n in before}, before
        )

    def test_the_message_names_the_newest_attempt_when_an_earlier_one_failed_cleanly(self):
        run = self.two_note_run()
        digest = self.digest_of(run)
        self.with_swap(1, lambda: self.cli("promote", os.path.basename(run), "--apply", "--expect", digest))  # attempt 1: fails before replacing anything
        th.write(os.path.join(self.mem, "project-new.md"), th.note("project-new", body="A note added between the two attempts."))  # so the snapshots differ
        self.crash("after-first", run)  # attempt 2: killed with one note replaced
        names = self.files(run, "promote-started")
        self.assertEqual(len(names), 2)
        older, newest = (json.loads(th.read(os.path.join(run, n)))["pre_snapshot"] for n in names)
        self.assertNotEqual(older, newest)
        code, out, err = self.cli("promote", os.path.basename(run))
        self.assertEqual(code, 1, out + err)
        self.assertIn("--snapshot %s --file" % newest, err)
        self.assertNotIn(older, err)

    def test_a_started_marker_that_cannot_be_read_stops_the_run(self):
        run, _digest = self.crash("before-first")
        th.write(os.path.join(run, self.files(run, "promote-started")[0]), "{not json")
        code, out, err = self.cli("promote", os.path.basename(run))
        self.assertEqual(code, 1, out + err)
        self.assertIn("cannot be read", err)
        self.assertIn("memory_hygiene.py snapshots", err)

    def test_the_check_is_made_again_under_the_lock_before_an_attempt_begins(self):
        run, _digest = self.crash("after-first")  # a promote died after the plan of another one was made: begin_attempt must not paper over it
        manifest = sn.load_manifest(self.ops, self.snap)
        with reg.ops_lock(self.ops):
            with self.assertRaises(mc.HygieneError) as caught:
                da.begin_attempt(run, self.mem, manifest, ["feedback-other.md", "feedback-push.md"], "20231114T221320Z")
        self.assertIn("did not finish", str(caught.exception))
        self.assertEqual(len(self.files(run, "promote-started")), 1)  # and it wrote nothing

    def test_a_marker_write_that_fails_halfway_leaves_no_torn_marker_and_the_run_still_goes_through(self):
        run = self.two_note_run()
        digest = self.digest_of(run)
        real = da.begin_attempt

        def failing(fd):
            raise OSError(5, "Input/output error")

        def begin_with_a_failing_disk(*args, **kwargs):
            with mock.patch.object(os, "fsync", failing):  # only the marker's own writes fail
                return real(*args, **kwargs)

        with mock.patch.object(da, "begin_attempt", begin_with_a_failing_disk):
            code, out, err = self.cli("promote", os.path.basename(run), "--apply", "--expect", digest)
        self.assertEqual(code, 1, out + err)
        self.assertIn("nothing was replaced", err)
        self.assertEqual(da._markers(run, da.STARTED_MARKER), [])  # no created-but-empty marker (a leftover .part file is not one)
        code, out, err = self.cli("promote", os.path.basename(run))
        self.assertEqual(code, 0, out + err)  # not refused: the run is healthy
        code, out, err = self.cli("promote", os.path.basename(run), "--apply", "--expect", digest)
        self.assertEqual(code, 0, out + err)

    def test_a_marker_is_complete_or_absent_never_torn(self):
        folder = tempfile.mkdtemp(prefix="marker_")
        target = os.path.join(folder, "m.json")
        with mock.patch.object(os, "fsync", side_effect=OSError(5, "Input/output error")):
            with self.assertRaises(OSError):
                da.write_marker_file(target, {"status": "started"})
        self.assertFalse(os.path.lexists(target))
        da.write_marker_file(target, {"status": "started"})
        self.assertEqual(json.loads(th.read(target)), {"status": "started"})
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)
        with self.assertRaises(FileExistsError):  # a name that is taken is never overwritten
            da.write_marker_file(target, {"status": "other"})
        self.assertEqual(json.loads(th.read(target)), {"status": "started"})

    def test_the_started_marker_is_on_disk_before_the_first_note_moves(self):
        run = self.two_note_run()
        digest = self.digest_of(run)
        events, real_fsync, real_swap = [], os.fsync, sn._swap_in

        def watching_fsync(fd):
            info = os.fstat(fd)
            events.append(("dir" if stat.S_ISDIR(info.st_mode) else "file", info.st_ino))
            return real_fsync(fd)

        def watching_swap(*args, **kwargs):
            events.append(("swap", 0))
            return real_swap(*args, **kwargs)

        sn._swap_in = watching_swap
        try:
            with mock.patch.object(os, "fsync", watching_fsync):
                code, out, err = self.cli("promote", os.path.basename(run), "--apply", "--expect", digest)
        finally:
            sn._swap_in = real_swap
        self.assertEqual(code, 0, out + err)
        marker, folder = os.stat(os.path.join(run, "promote-started.json")).st_ino, os.stat(run).st_ino
        before = events[: [kind for kind, _ in events].index("swap")]
        self.assertIn(("file", marker), before)  # the marker's own file, flushed to disk
        self.assertIn(("dir", folder), before[before.index(("file", marker)):])  # then the folder that holds it, and only then does a note move

    def test_marker_files_of_one_kind_are_read_in_the_order_they_were_written(self):
        folder = tempfile.mkdtemp(prefix="order_")
        for name in ("promote-started.json", "promote-started.json.1", "promote-started.json.2", "promote-started.json.10", "promote-started.json.11", "promote-started.json.bak", "other.json"):
            th.write(os.path.join(folder, name), "{}")
        self.assertEqual(
            da._markers(folder, da.STARTED_MARKER),
            ["promote-started.json", "promote-started.json.1", "promote-started.json.2", "promote-started.json.10", "promote-started.json.11"],
        )

    def test_a_marker_left_by_the_first_release_does_not_hide_a_killed_promote(self):
        run = self.two_note_run()
        legacy = {"status": "failed-before-replacing", "files": [], "error": "x", "archive": "a", "pre_snapshot": "p"}
        th.write(os.path.join(run, da.FAILED_MARKER), json.dumps(legacy))  # what release 0.1.1 wrote: an outcome with no started marker
        self.crash("after-first", run)
        code, out, err = self.cli("promote", os.path.basename(run))
        self.assertEqual(code, 1, out + err)
        self.assertIn("did not finish", err)

    def test_a_run_with_only_a_marker_from_the_first_release_is_not_a_dead_promote(self):
        run = self.two_note_run()
        legacy = {"status": "failed-before-replacing", "files": [], "error": "x", "archive": "a", "pre_snapshot": "p"}
        th.write(os.path.join(run, da.FAILED_MARKER), json.dumps(legacy))
        self.assertEqual(da.dangling_attempts(run), 0)
        code, out, err = self.cli("promote", os.path.basename(run))
        self.assertEqual(code, 0, out + err)

    def test_a_note_edited_after_a_dead_promote_that_replaced_nothing_is_not_reported_as_replaced(self):
        run, _digest = self.crash("before-first")
        with open(os.path.join(self.mem, "feedback-push.md"), "a", encoding="utf-8") as fh:
            fh.write("\nA line a person added afterwarda.\n")
        code, out, err = self.cli("promote", os.path.basename(run))
        self.assertEqual(code, 1, out + err)
        self.assertIn("changed since the snapshot", err)  # the ordinary refusal, whose advice keeps the edit
        self.assertNotIn("--force", err)
        self.assertNotIn("did not finish", err)

    def test_after_the_undo_the_apply_goes_through_and_does_not_claim_the_dead_promote_replaced_nothing(self):
        run, digest = self.crash("after-first")
        code, _out, _err = self.cli("promote", os.path.basename(run))
        started = json.loads(th.read(os.path.join(run, self.files(run, "promote-started")[0])))
        argv = [
            "--dir", self.mem, "--ops-root", self.root, "restore",
            "--snapshot", started["pre_snapshot"], "--file", "feedback-other.md", "--apply", "--force",
        ]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mh.main(argv), 0)
        code, out, err = self.cli("promote", os.path.basename(run), "--apply", "--expect", digest)
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("had replaced nothing", out)
        self.assertIn("restored", out)
        failed = json.loads(th.read(os.path.join(run, self.files(run, "promote-failed")[0])))
        self.assertNotIn("had replaced nothing", failed["error"])
        self.assertIn("restored", failed["error"])

    def test_the_attempt_is_recorded_while_the_lock_is_held(self):
        run = self.two_note_run()
        digest = self.digest_of(run)
        held, real = [], da.begin_attempt

        def watching(*args, **kwargs):
            try:
                with reg.ops_lock(self.ops):
                    held.append(False)  # the lock was free: nobody was holding it
            except mc.HygieneError:
                held.append(True)
            return real(*args, **kwargs)

        with mock.patch.object(da, "begin_attempt", watching):
            code, out, err = self.cli("promote", os.path.basename(run), "--apply", "--expect", digest)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(held, [True])

    def test_the_marker_names_match_the_ones_the_promote_itself_uses(self):
        self.assertEqual(
            (da.PROMOTED_MARKER, da.FAILED_MARKER), (dr.MARKER, dr.FAILED_MARKER)
        )


if __name__ == "__main__":
    unittest.main()
