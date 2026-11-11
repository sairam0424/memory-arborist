"""Tests for memory_snapshot: snapshot, verify, compare, restore, prune-plan."""

import errno
import fcntl
import glob
import json
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memtest_helpers as th  # noqa: E402

sys.path.insert(0, th.SCRIPTS)  # python3 -I does not put the script dir on sys.path
import memory_common as mc  # noqa: E402
import memory_snapshot as sn  # noqa: E402

T0 = 1_700_000_000
LONG = "this baseline line is long enough to count as content"
EDITED = "edited by a live session"


class Base(unittest.TestCase):
    def setUp(self):
        self.mem = th.make_dir(
            {
                "MEMORY.md": "- [A](feedback-a.md) hook\n",
                "feedback-a.md": th.note("feedback-a"),
            }
        )
        self.ops = os.path.join(tempfile.mkdtemp(prefix="memops_"), "slug")
        self.live = os.path.join(self.mem, "feedback-a.md")

    def snap(self, now=T0, **kw):
        return sn.take_snapshot(self.mem, self.ops, now=now, **kw)

    def edit_live(self, text=EDITED):
        with open(self.live, "a") as fh:
            fh.write(text + "\n")

    def copy_of(self, snap_id, rel="feedback-a.md"):
        return os.path.join(self.ops, "snapshots", snap_id, "memory", rel)

    def manifest(self, snap_id):
        return json.loads(
            th.read(os.path.join(self.ops, "snapshots", snap_id, "manifest.json"))
        )

    def tamper(self, path):
        os.chmod(path, 0o600)
        with open(path, "a") as fh:
            fh.write("tampered\n")


class SnapshotTests(Base):
    def test_copies_files_with_manifest_modes_and_mtimes(self):
        os.utime(self.live, ns=(1_600_000_000_000_000_000,) * 2)
        res = self.snap()
        manifest = self.manifest(res["id"])
        copy = self.copy_of(res["id"])
        self.assertEqual(sorted(manifest["files"]), ["MEMORY.md", "feedback-a.md"])
        self.assertEqual(
            manifest["files"]["feedback-a.md"]["sha256"], mc.sha256_file(copy)
        )
        self.assertEqual(manifest["source"], os.path.realpath(self.mem))
        self.assertIn("mode", manifest["files"]["feedback-a.md"])
        self.assertEqual(os.stat(copy).st_mtime_ns, 1_600_000_000_000_000_000)
        self.assertEqual(stat.S_IMODE(os.stat(copy).st_mode), 0o600)
        self.assertEqual(
            stat.S_IMODE(os.stat(os.path.dirname(os.path.dirname(copy))).st_mode), 0o700
        )

    def test_identical_tree_is_a_noop_and_changed_content_is_not(self):
        first = self.snap()
        second = self.snap(now=T0 + 60)
        self.assertEqual((second["status"], second["id"]), ("noop", first["id"]))
        self.edit_live()
        self.assertEqual(self.snap(now=T0 + 120)["status"], "ok")
        self.assertEqual(len(sn.list_snapshots(self.ops)), 2)

    def test_noop_does_not_trust_a_damaged_previous_snapshot(self):
        first = self.snap()
        self.tamper(self.copy_of(first["id"]))
        again = self.snap(now=T0 + 60)
        self.assertEqual(again["status"], "ok")
        self.assertNotEqual(again["id"], first["id"])
        self.assertTrue(again["previous_problems"])

    def test_same_second_ids_stay_unique_and_in_creation_order(self):
        made = []
        for i in range(12):
            self.edit_live("edit %d" % i)
            made.append(self.snap(now=T0)["id"])
        self.assertEqual(len(set(made)), 12)
        self.assertEqual(sn.list_snapshots(self.ops), made)
        self.assertEqual(sn.resolve_snapshot(self.ops, "latest"), made[-1])

    def test_low_disk_refuses_a_new_snapshot_but_not_a_noop(self):
        with self.assertRaises(sn.HygieneError):
            self.snap(free_fn=lambda _p: 1024)
        self.assertEqual(sn.list_snapshots(self.ops), [])
        self.snap()
        self.assertEqual(
            self.snap(now=T0 + 5, free_fn=lambda _p: 1024)["status"], "noop"
        )

    def test_missing_memory_dir_is_an_error(self):
        with self.assertRaises(sn.HygieneError):
            sn.take_snapshot("/nonexistent/memdir", self.ops)

    def test_the_immutable_flag_is_not_copied(self):
        if not hasattr(os, "chflags"):
            self.skipTest(
                "os.chflags is macOS/BSD only; the uchg gotcha does not exist elsewhere"
            )
        os.chflags(self.live, stat.UF_IMMUTABLE)
        try:
            res = self.snap()
        finally:
            os.chflags(self.live, 0)
        self.assertEqual(
            os.stat(self.copy_of(res["id"])).st_flags & stat.UF_IMMUTABLE, 0
        )

    def test_a_held_lock_refuses_a_concurrent_run(self):
        os.makedirs(self.ops, exist_ok=True)
        with open(os.path.join(self.ops, ".lock"), "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(sn.HygieneError):
                self.snap()

    def test_lock_file_is_private(self):
        self.snap()
        self.assertEqual(
            stat.S_IMODE(os.stat(os.path.join(self.ops, ".lock")).st_mode), 0o600
        )

    def test_a_copy_that_fails_verification_leaves_no_manifest(self):
        def bad_copy(src, dst, **_kw):
            with open(dst, "wb") as fh:
                fh.write(b"corrupt")

        with mock.patch.object(sn.shutil, "copyfile", bad_copy):
            with self.assertRaises(sn.HygieneError):
                self.snap()
        self.assertEqual(sn.list_snapshots(self.ops), [])
        self.assertEqual(len(sn.broken_snapshots(self.ops)), 1)

    def test_symlinks_are_reported_not_silently_dropped(self):
        os.symlink(self.live, os.path.join(self.mem, "alias.md"))
        res = self.snap()
        self.assertEqual(res["skipped"], ["alias.md"])
        self.assertEqual(self.manifest(res["id"])["skipped_symlinks"], ["alias.md"])

    def test_unreadable_file_and_directory_are_errors_not_traceback_or_silence(self):
        os.chmod(self.live, 0)
        try:
            with self.assertRaises(sn.HygieneError):
                self.snap()
        finally:
            os.chmod(self.live, 0o600)
        locked = os.path.join(self.mem, "locked")
        os.mkdir(locked)
        os.chmod(locked, 0)
        try:
            with self.assertRaises(sn.HygieneError):
                self.snap()
        finally:
            os.chmod(locked, 0o700)

    def test_ops_dir_inside_the_memory_dir_is_refused(self):
        with self.assertRaises(sn.HygieneError):
            sn.take_snapshot(self.mem, os.path.join(self.mem, "ops"), now=T0)

    def test_an_ops_dir_that_belongs_to_another_memory_dir_is_refused(self):
        self.snap()
        other = th.make_dir({"MEMORY.md": "x\n"})
        with self.assertRaises(sn.HygieneError):
            sn.take_snapshot(other, self.ops, now=T0 + 5)

    def test_zero_length_manifest_is_ignored_and_reported(self):
        first = self.snap()
        os.chmod(
            os.path.join(self.ops, "snapshots", first["id"], "manifest.json"), 0o600
        )
        open(
            os.path.join(self.ops, "snapshots", first["id"], "manifest.json"), "w"
        ).close()
        self.assertEqual(sn.list_snapshots(self.ops), [])
        self.assertEqual(sn.broken_snapshots(self.ops), [first["id"]])
        self.assertEqual(self.snap(now=T0 + 5)["status"], "ok")

    def test_scan_counts_are_recorded_in_the_manifest(self):
        self.edit_live("leak " + th.fake_github_token())
        self.assertEqual(self.manifest(self.snap()["id"])["scan"]["block"], 1)


class VerifyTests(Base):
    def test_clean_snapshot_verifies(self):
        res = self.snap()
        self.assertEqual(sn.verify_snapshot(self.ops, res["id"]), (True, 2, []))

    def test_tampered_missing_and_extra_files_are_reported(self):
        res = self.snap()
        self.tamper(self.copy_of(res["id"]))
        with open(self.copy_of(res["id"], "stray.md"), "w") as fh:
            fh.write("x")
        os.chmod(self.copy_of(res["id"], "MEMORY.md"), 0o600)
        ok, _n, problems = sn.verify_snapshot(self.ops, res["id"])
        self.assertFalse(ok)
        self.assertTrue(any("mismatch" in p and "feedback-a.md" in p for p in problems))
        self.assertTrue(any("unexpected" in p for p in problems))

    def test_corrupt_manifest_raises_a_handled_error(self):
        res = self.snap()
        path = os.path.join(self.ops, "snapshots", res["id"], "manifest.json")
        os.chmod(path, 0o600)
        with open(path, "w") as fh:
            fh.write("{not json")
        with self.assertRaises(sn.HygieneError):
            sn.verify_snapshot(self.ops, res["id"])


class CompareTests(unittest.TestCase):
    def compare(self, base, stage, files=None):
        return sn.compare_dirs(th.make_dir(base), th.make_dir(stage), files)

    def test_removed_long_line_is_lost_and_counts_are_reported(self):
        res = self.compare(
            {"a.md": LONG + "\nkeep this line that is also quite long\n"},
            {"a.md": "keep this line that is also quite long\n"},
        )
        self.assertEqual(
            (res.lost, res.baseline_files, res.lines_checked), ([("a.md", 1)], 1, 2)
        )

    def test_short_lines_and_the_modified_key_are_ignored(self):
        res = self.compare(
            {
                "a.md": "---\nshort\nmodified: 2026-11-08T12:00:00Z\n   modified: indented key\n"
                + LONG
                + "\n"
            },
            {"a.md": LONG + "\n"},
        )
        self.assertEqual(res.lost, [])

    def test_boundary_of_twenty_characters(self):
        nineteen, twenty = "x" * 19, "y" * 20
        res = self.compare(
            {"a.md": nineteen + "\n" + twenty + "\n"}, {"a.md": "nothing\n"}
        )
        self.assertEqual(res.lost, [("a.md", 2)])

    def test_duplicates_count_as_a_multiset(self):
        self.assertEqual(
            self.compare(
                {"a.md": LONG + "\n" + LONG + "\n"}, {"a.md": LONG + "\n"}
            ).lost,
            [("a.md", 2)],
        )

    def test_a_line_moved_to_another_file_is_not_lost(self):
        self.assertEqual(
            self.compare(
                {"a.md": LONG + "\n"}, {"a.md": "x\n", "archive.md": LONG + "\n"}
            ).lost,
            [],
        )

    def test_crlf_and_trailing_whitespace_do_not_count_as_changes(self):
        self.assertEqual(
            self.compare({"a.md": LONG + "\n"}, {"a.md": LONG + "  \r\n"}).lost, []
        )

    def test_scope_limits_which_baseline_files_are_checked(self):
        res = self.compare(
            {"a.md": LONG + "\n", "b.md": LONG + " but different\n"},
            {"a.md": "x\n", "b.md": "x\n"},
            files=["a.md"],
        )
        self.assertEqual(res.lost, [("a.md", 1)])

    def test_an_unknown_file_in_the_scope_is_an_error(self):
        with self.assertRaises(sn.HygieneError):
            self.compare(
                {"a.md": LONG + "\n"}, {"a.md": LONG + "\n"}, files=["typo.md"]
            )

    def test_an_empty_baseline_checks_nothing_and_says_so(self):
        res = self.compare({"notes.txt": "not markdown"}, {"a.md": LONG + "\n"})
        self.assertEqual((res.lost, res.baseline_files, res.lines_checked), ([], 0, 0))


class RestoreTests(Base):
    def setUp(self):
        super().setUp()
        self.snap_id = self.snap()["id"]
        self.original = th.read(self.live)
        self.edit_live()

    def restore(self, name="feedback-a.md", now=T0 + 5, **kw):
        return sn.restore_file(self.mem, self.ops, self.snap_id, name, now=now, **kw)

    def archived(self, name="feedback-a.md"):
        return sorted(glob.glob(os.path.join(self.ops, "archive", "*", name + "*")))

    def test_dry_run_changes_nothing(self):
        code, msgs = self.restore()
        self.assertEqual(code, 0)
        self.assertIn(EDITED, th.read(self.live))
        self.assertTrue(any("would restore" in m for m in msgs))

    def test_a_differing_live_file_needs_force(self):
        self.assertEqual(self.restore(apply=True)[0], 1)
        self.assertIn(EDITED, th.read(self.live))

    def test_apply_with_force_archives_live_then_restores_bytes_and_mtime(self):
        self.assertEqual(self.restore(apply=True, force=True)[0], 0)
        self.assertEqual(th.read(self.live), self.original)
        self.assertEqual(len(self.archived()), 1)
        self.assertIn(EDITED, th.read(self.archived()[0]))
        self.assertEqual(
            os.stat(self.live).st_mtime_ns,
            self.manifest(self.snap_id)["files"]["feedback-a.md"]["mtime_ns"],
        )

    def test_two_forced_restores_in_the_same_second_keep_both_archived_copies(self):
        self.restore(apply=True, force=True)
        self.edit_live("second live edit")
        self.restore(apply=True, force=True)
        texts = [th.read(p) for p in self.archived()]
        self.assertEqual(len(texts), 2)
        self.assertTrue(
            any(EDITED in t for t in texts)
            and any("second live edit" in t for t in texts)
        )

    def test_a_staging_failure_leaves_the_live_file_in_place_and_nothing_archived(self):
        def full_disk(src, dst, **_kw):
            raise OSError(errno.ENOSPC, "No space left on device")

        with mock.patch.object(sn.shutil, "copyfile", full_disk):
            with self.assertRaises((sn.HygieneError, OSError)):
                self.restore(apply=True, force=True)
        self.assertIn(EDITED, th.read(self.live))
        self.assertEqual(self.archived(), [])

    def test_a_staged_copy_that_fails_its_hash_check_is_never_swapped_in(self):
        def bad_copy(src, dst, **_kw):
            with open(dst, "wb") as fh:
                fh.write(b"garbage")

        with mock.patch.object(sn.shutil, "copyfile", bad_copy):
            with self.assertRaises(sn.HygieneError):
                self.restore(apply=True, force=True)
        self.assertIn(EDITED, th.read(self.live))
        self.assertEqual(self.archived(), [])

    def test_tampered_snapshot_file_is_refused_before_touching_live(self):
        self.tamper(self.copy_of(self.snap_id))
        self.assertEqual(self.restore(apply=True, force=True)[0], 1)
        self.assertIn(EDITED, th.read(self.live))

    def test_file_absent_from_snapshot_follows_dry_run_then_apply_then_force(self):
        extra = os.path.join(self.mem, "new-note.md")
        th.write(extra, "brand new\n", "w")
        self.assertEqual(self.restore("new-note.md")[0], 0)
        self.assertTrue(os.path.exists(extra))
        self.assertEqual(self.restore("new-note.md", apply=True)[0], 1)
        self.assertTrue(os.path.exists(extra))
        self.assertEqual(self.restore("new-note.md", apply=True, force=True)[0], 0)
        self.assertFalse(os.path.exists(extra))
        self.assertEqual(len(self.archived("new-note.md")), 1)

    def test_a_missing_parent_directory_is_recreated(self):
        nested_snap = self.snap_nested()
        nested = os.path.join(self.mem, "sub")
        os.rename(os.path.join(nested, "n.md"), os.path.join(self.mem, "moved.md"))
        os.rmdir(nested)
        code, _ = sn.restore_file(
            self.mem, self.ops, nested_snap, "sub/n.md", apply=True, now=T0 + 9
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            th.read(os.path.join(self.mem, "sub", "n.md")),
            "nested note line number one is here\n",
        )

    def snap_nested(self):
        os.makedirs(os.path.join(self.mem, "sub"))
        th.write(os.path.join(self.mem, "sub", "n.md"), "nested note line number one is here\n", "w")
        return self.snap(now=T0 + 7)["id"]

    def test_unsafe_names_are_refused_and_outside_files_are_never_touched(self):
        outside = os.path.join(os.path.dirname(self.mem), "evil.md")
        th.write(outside, "outside", "w")
        for name in ("../evil.md", "sub/../../evil.md", os.path.abspath(outside), ""):
            with self.subTest(name):
                self.assertEqual(self.restore(name, apply=True, force=True)[0], 1)
        self.assertEqual(th.read(outside), "outside")

    def test_names_that_merely_start_with_two_dots_are_legitimate(self):
        th.write(os.path.join(self.mem, "..dots.md"), "dots note line number one here\n", "w")
        snap_id = self.snap(now=T0 + 11)["id"]
        with open(os.path.join(self.mem, "..dots.md"), "a") as fh:
            fh.write("changed\n")
        code, _ = sn.restore_file(
            self.mem,
            self.ops,
            snap_id,
            "..dots.md",
            apply=True,
            force=True,
            now=T0 + 12,
        )
        self.assertEqual(code, 0)

    def test_a_symlinked_directory_cannot_be_used_to_reach_outside_files(self):
        outside_dir = th.make_dir({"o.md": "outside file\n"})
        os.symlink(outside_dir, os.path.join(self.mem, "lnk"))
        self.assertEqual(self.restore("lnk/o.md", apply=True, force=True)[0], 1)
        self.assertEqual(th.read(os.path.join(outside_dir, "o.md")), "outside file\n")

    def test_a_snapshot_taken_from_another_directory_is_refused(self):
        other = th.make_dir({"feedback-a.md": "x\n"})
        code, _ = sn.restore_file(
            other,
            self.ops,
            self.snap_id,
            "feedback-a.md",
            apply=True,
            force=True,
            now=T0 + 5,
        )
        self.assertEqual(code, 1)

    def test_the_manifest_mode_is_used_when_the_live_file_is_gone(self):
        os.chmod(self.live, 0o640)
        snap_id = self.snap(now=T0 + 20)["id"]
        os.rename(self.live, os.path.join(self.ops, "parked.md"))
        self.assertEqual(
            sn.restore_file(
                self.mem, self.ops, snap_id, "feedback-a.md", apply=True, now=T0 + 21
            )[0],
            0,
        )
        self.assertEqual(stat.S_IMODE(os.stat(self.live).st_mode), 0o640)

    def test_restore_takes_the_ops_lock(self):
        with open(os.path.join(self.ops, ".lock"), "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(sn.HygieneError):
                self.restore(apply=True, force=True)


class PruneTests(Base):
    def make_snapshots(self, n):
        for i in range(n):
            self.edit_live("edit %d" % i)
            self.snap(now=T0 + i * 100)

    def test_plan_lists_old_snapshots_with_absolute_paths_and_removes_nothing(self):
        self.make_snapshots(7)
        ids = sn.list_snapshots(self.ops)
        plan = sn.prune_plan(os.path.relpath(self.ops), keep=5)
        self.assertEqual(len(plan), 2)
        self.assertTrue(all(line.startswith("rm -r /") for line in plan))
        self.assertIn(ids[0], plan[0])
        self.assertEqual(len(sn.list_snapshots(self.ops)), 7)

    def test_keep_must_be_at_least_one(self):
        for keep in (0, -1):
            with self.assertRaises(sn.HygieneError):
                sn.prune_plan(self.ops, keep=keep)

    def test_incomplete_snapshot_directories_are_listed_as_comments(self):
        os.makedirs(os.path.join(self.ops, "snapshots", "20200101T000000Z", "memory"))
        plan = sn.prune_plan(self.ops, keep=5)
        self.assertTrue(any(line.startswith("# incomplete") for line in plan))


if __name__ == "__main__":
    unittest.main()
