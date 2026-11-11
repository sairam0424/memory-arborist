"""Hardening tests for memory_snapshot, memory_registry and memory_compare (lane H1)."""

import errno
import fcntl
import glob
import hashlib
import json
import os
import shlex
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memtest_helpers as th  # noqa: E402

sys.path.insert(0, th.SCRIPTS)  # python3 -I does not put the script dir on sys.path
import memory_common as mc  # noqa: E402
import memory_registry as reg  # noqa: E402
import memory_scan as ms  # noqa: E402
import memory_snapshot as sn  # noqa: E402

T0 = 1_700_000_000
STAMP0 = "20231114T221320Z"
EDITED = "edited by a live session"
LONG = "this baseline line is long enough to count as content"
GIB2 = 2 * 1024**3
NOTE = "nested note content line is long enough\n"
NOT_ROOT = unittest.skipIf(os.geteuid() == 0, "root ignores file modes")


def mode_of(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class Base(unittest.TestCase):
    def setUp(self):
        self.mem = th.make_dir({"MEMORY.md": "- [A](feedback-a.md) hook\n", "feedback-a.md": th.note("feedback-a")})
        self.ops = os.path.join(tempfile.mkdtemp(prefix="memops_"), "slug")
        self.live = os.path.join(self.mem, "feedback-a.md")
        self.parked = os.path.join(tempfile.mkdtemp(prefix="memparked_"), "parked")

    def snap(self, now=T0, **kw):
        return sn.take_snapshot(self.mem, self.ops, now=now, **kw)

    def edit_live(self, text=EDITED):
        with open(self.live, "a") as fh:
            fh.write(text + "\n")

    def snap_dir(self, snap_id):
        return os.path.join(self.ops, "snapshots", snap_id)

    def copy_of(self, snap_id, rel="feedback-a.md"):
        return os.path.join(self.snap_dir(snap_id), "memory", rel)

    def manifest(self, snap_id):
        return json.loads(th.read(os.path.join(self.snap_dir(snap_id), "manifest.json")))

    def write_manifest(self, snap_id, data):
        path = os.path.join(self.snap_dir(snap_id), "manifest.json")
        os.chmod(path, 0o600)
        th.write(path, data if isinstance(data, str) else json.dumps(data))

    def edit_manifest(self, snap_id, **changes):
        data = dict(self.manifest(snap_id), **changes)
        self.write_manifest(snap_id, {k: v for k, v in data.items() if v is not None})

    def without_modes(self, snap_id):
        files = {k: {f: v for f, v in e.items() if f != "mode"} for k, e in self.manifest(snap_id)["files"].items()}
        self.edit_manifest(snap_id, files=files)

    def clone(self, snap_id, new_id, **changes):
        shutil.copytree(self.snap_dir(snap_id), self.snap_dir(new_id))
        self.edit_manifest(new_id, id=new_id, **changes)

    def tamper(self, path):
        os.chmod(path, 0o600)
        with open(path, "a") as fh:
            fh.write("tampered\n")

    def chdir(self, path):
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(path)

    def with_sub_note(self, now):
        os.makedirs(os.path.join(self.mem, "sub"), exist_ok=True)
        th.write(os.path.join(self.mem, "sub", "n.md"), NOTE)
        return self.snap(now=now)["id"]


class SnapshotTests(Base):
    def test_manifest_and_result_have_exactly_the_documented_content(self):
        os.chmod(self.live, 0o640)
        stamp_ns = 1_600_000_000_123_456_789
        os.utime(self.live, ns=(stamp_ns, stamp_ns))
        os.symlink(self.live, os.path.join(self.mem, "zz-link.md"))
        os.mkfifo(os.path.join(self.mem, "aa-pipe.md"))
        res = self.snap()
        self.assertEqual(res, {"status": "ok", "id": STAMP0, "files": 2, "ok_count": 2, "scan": (0, 0),
                               "skipped": ["aa-pipe.md", "zz-link.md"], "previous_problems": []})
        data = self.manifest(STAMP0)
        sha = {rel: hashlib.sha256(th.read(os.path.join(self.mem, rel)).encode()).hexdigest() for rel in data["files"]}
        keys = {"version", "id", "seq", "created_utc", "source", "files", "tree_sha256", "scan", "skipped_symlinks", "skipped_special"}
        self.assertEqual(set(data), keys)
        self.assertEqual([data[k] for k in ("version", "id", "seq", "created_utc", "source")], [1, STAMP0, 1, STAMP0, os.path.realpath(self.mem)])
        self.assertEqual((data["skipped_symlinks"], data["skipped_special"]), (["zz-link.md"], ["aa-pipe.md"]))
        self.assertEqual(data["scan"], {"block": 0, "warn": 0})
        entry = data["files"]["feedback-a.md"]
        self.assertEqual(set(entry), {"sha256", "size", "mtime_ns", "mode"})
        self.assertEqual((entry["sha256"], entry["size"], entry["mtime_ns"], entry["mode"]),
                         (sha["feedback-a.md"], os.path.getsize(self.live), stamp_ns, 0o640))
        joined = "\n".join("%s\0%s" % (rel, sha[rel]) for rel in sorted(sha))
        self.assertEqual(data["tree_sha256"], hashlib.sha256(joined.encode()).hexdigest())
        self.assertEqual(th.read(os.path.join(self.snap_dir(STAMP0), "manifest.json")), json.dumps(data, indent=1, sort_keys=True))

    def test_source_is_resolved_and_sequence_counts_up_even_from_a_legacy_manifest(self):
        alias = os.path.join(tempfile.mkdtemp(prefix="memalias_"), "alias")
        os.symlink(self.mem, alias)
        first = sn.take_snapshot(alias, self.ops, now=T0)["id"]
        self.assertEqual(self.manifest(first)["source"], os.path.realpath(self.mem))
        self.edit_manifest(first, seq=None)
        seqs = []
        for i in range(2):
            self.edit_live("edit %d" % i)
            seqs.append(self.manifest(self.snap(now=T0 + 10 * (i + 1))["id"])["seq"])
        self.assertEqual(seqs, [1, 2])

    def test_scan_counts_describe_the_verified_copy(self):
        self.edit_live("leak " + th.fake_github_token())
        self.edit_live("contact alice" + "@" + "example.org")
        block, warn = ms.summarize(ms.scan_tree(self.mem).hits)
        self.assertGreaterEqual((block, warn), (1, 1))
        self.assertEqual(self.manifest(self.snap()["id"])["scan"], {"block": block, "warn": warn})
        real_copy = sn._copy_tree
        th.write(self.live, th.note("feedback-a"))

        def copy_then_live_edit(*args):
            count = real_copy(*args)
            self.edit_live("leak " + th.fake_github_token())
            return count

        with mock.patch.object(sn, "_copy_tree", copy_then_live_edit):
            res = self.snap(now=T0 + 5)
        self.assertEqual((res["scan"], self.manifest(res["id"])["scan"]["block"]), ((0, 0), 0))

    def test_every_directory_and_file_written_is_private(self):
        os.makedirs(os.path.join(self.mem, "sub", "deeper"))
        th.write(os.path.join(self.mem, "sub", "deeper", "n.md"), NOTE)
        snap_id = self.snap()["id"]
        root = self.snap_dir(snap_id)
        for path in (self.ops, os.path.join(self.ops, "snapshots"), root, os.path.join(root, "memory"),
                     os.path.join(root, "memory", "sub"), os.path.join(root, "memory", "sub", "deeper")):
            self.assertEqual(mode_of(path), 0o700, path)
        for path in (os.path.join(root, "manifest.json"), self.copy_of(snap_id, "sub/deeper/n.md")):
            self.assertEqual(mode_of(path), 0o600, path)
        self.assertEqual(glob.glob(os.path.join(root, "*.part")), [])

    def test_ids_within_one_second_run_from_bare_stamp_to_dash_99_then_refuse(self):
        made = []
        for i in range(100):
            self.edit_live("edit %d" % i)
            made.append(self.snap(now=T0)["id"])
        self.assertEqual(made[:3], [STAMP0, STAMP0 + "-01", STAMP0 + "-02"])
        self.assertEqual((made[9], made[10], made[99]), (STAMP0 + "-09", STAMP0 + "-10", STAMP0 + "-99"))
        self.assertEqual(sn.list_snapshots(self.ops), made)
        self.edit_live("one too many")
        with self.assertRaisesRegex(sn.HygieneError, "too many snapshots"):
            self.snap(now=T0)
        self.assertEqual(len(sn.list_snapshots(self.ops)) + len(sn.broken_snapshots(self.ops)), 100)
        self.assertEqual(self.snap(now=T0 + 1)["id"], "20231114T221321Z")

    def test_only_content_and_names_decide_a_noop(self):
        first = self.snap()
        os.utime(self.live, ns=(1_500_000_000_000_000_000,) * 2)
        again = self.snap(now=T0 + 5)
        self.assertEqual(again, {"status": "noop", "id": first["id"], "files": 2, "ok_count": 2, "scan": (0, 0),
                                 "skipped": [], "previous_problems": []})
        th.write(self.live, th.read(self.live).replace("body text", "body TEXT"))  # same size
        self.assertEqual(self.snap(now=T0 + 10)["status"], "ok")
        os.rename(self.live, os.path.join(self.mem, "feedback-b.md"))  # same content, same sort position
        res = self.snap(now=T0 + 20)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(sorted(self.manifest(res["id"])["files"]), ["MEMORY.md", "feedback-b.md"])

    def test_the_latest_snapshot_is_the_reference_not_the_oldest_and_a_damaged_one_is_reported(self):
        original = th.read(self.live)
        first = self.snap()
        self.edit_live()
        second = self.snap(now=T0 + 10)
        again = self.snap(now=T0 + 20)
        self.assertEqual((again["status"], again["id"]), ("noop", second["id"]))
        th.write(self.live, original)
        third = self.snap(now=T0 + 30)
        self.assertEqual(third["status"], "ok")
        self.assertNotIn(third["id"], (first["id"], second["id"]))
        self.tamper(self.copy_of(third["id"]))
        fourth = self.snap(now=T0 + 40)
        self.assertEqual((fourth["status"], fourth["previous_problems"]), ("ok", ["sha256 mismatch: feedback-a.md"]))

    def test_the_tree_hash_is_recomputed_not_trusted_from_the_manifest(self):
        first = self.snap()["id"]
        self.edit_manifest(first, tree_sha256="0" * 64)
        second = self.snap(now=T0 + 5)["id"]
        self.assertNotEqual(second, first)
        data = self.manifest(second)
        os.rename(self.copy_of(second, "MEMORY.md"), self.parked)  # a self-consistent snapshot of one file
        self.edit_manifest(second, files={k: v for k, v in data["files"].items() if k != "MEMORY.md"})
        self.assertTrue(sn.verify_snapshot(self.ops, second)[0])
        self.assertEqual(self.snap(now=T0 + 10)["status"], "ok")

    def test_a_manifest_without_a_source_is_refused_even_when_cwd_is_the_memory_dir(self):
        first = self.snap()["id"]
        self.chdir(self.mem)
        for source in ("", None):
            self.edit_manifest(first, source=source)
            with self.assertRaisesRegex(sn.HygieneError, "different memory dir"):
                self.snap(now=T0 + 5)

    def test_free_space_is_measured_on_the_ops_dir_with_a_two_gib_floor(self):
        free = mock.Mock(return_value=GIB2 - 1)
        with self.assertRaisesRegex(sn.HygieneError, r"refusing: only %d bytes free, need %d" % (GIB2 - 1, GIB2)):
            self.snap(free_fn=free)
        free.assert_called_once_with(self.ops)
        self.assertEqual((sn.list_snapshots(self.ops), sn.broken_snapshots(self.ops)), ([], []))
        self.assertEqual(self.snap(free_fn=lambda _p: GIB2)["status"], "ok")

    def test_a_copy_error_of_any_os_kind_becomes_a_handled_refusal(self):
        denied = PermissionError(errno.EACCES, "Permission denied")
        with mock.patch.object(sn.shutil, "copyfile", side_effect=denied):
            with self.assertRaisesRegex(sn.HygieneError, r"copy failed \(PermissionError\); no manifest written"):
                self.snap()
        self.assertEqual(sn.list_snapshots(self.ops), [])


class VerifyAndRegistryTests(Base):
    def test_each_problem_kind_is_reported_with_its_own_label_and_name(self):
        snap_id = self.snap()["id"]
        self.assertEqual(sn.verify_snapshot(self.ops, snap_id), (True, 2, []))
        os.rename(self.copy_of(snap_id), self.parked)
        self.assertEqual(sn.verify_snapshot(self.ops, snap_id), (False, 2, ["missing: feedback-a.md"]))
        os.mkdir(self.copy_of(snap_id))  # a directory where the file belongs
        self.assertEqual(sn.verify_snapshot(self.ops, snap_id), (False, 2, ["missing: feedback-a.md"]))
        os.rmdir(self.copy_of(snap_id))
        shutil.copyfile(self.parked, self.copy_of(snap_id))
        self.tamper(self.copy_of(snap_id))
        th.write(self.copy_of(snap_id, "stray.md"), "x")
        self.assertEqual(sn.verify_snapshot(self.ops, snap_id)[1:],
                         (2, ["sha256 mismatch: feedback-a.md", "unexpected file: stray.md"]))
        os.rename(os.path.join(self.snap_dir(snap_id), "memory"), self.parked + "2")
        self.assertEqual(sn.verify_snapshot(self.ops, snap_id)[2], ["missing: MEMORY.md", "missing: feedback-a.md"])

    def test_the_lock_is_exclusive_reusable_tightened_and_released_after_errors(self):
        os.makedirs(self.ops)
        lock = os.path.join(self.ops, ".lock")
        th.write(lock, "")
        os.chmod(lock, 0o666)
        with reg.ops_lock(self.ops):
            self.assertEqual(mode_of(lock), 0o600)
            with self.assertRaisesRegex(sn.HygieneError, "holds the lock"):
                with reg.ops_lock(self.ops):
                    pass
        with self.assertRaises(KeyError):
            with reg.ops_lock(self.ops):
                raise KeyError("boom")

    def test_ordering_uses_seq_first_then_numeric_suffix_and_ignores_non_int_seq(self):
        base = self.snap()["id"]
        for new_id, seq in (("20200101T000000Z-9", None), ("20200101T000000Z-10", None), ("20200101T000000Z", None),
                            ("20200101T000000Z-x", None), ("20190101T000000Z", "9")):
            self.clone(base, new_id, seq=seq)
        expected = ["20190101T000000Z", "20200101T000000Z", "20200101T000000Z-x", "20200101T000000Z-9", "20200101T000000Z-10", base]
        self.assertEqual(sn.list_snapshots(self.ops), expected)
        self.assertEqual(sn.resolve_snapshot(self.ops, "latest"), base)
        for name in ("c-dir", "a-dir", "b-dir"):
            os.makedirs(self.snap_dir(name))
        th.write(os.path.join(self.ops, "snapshots", "notes.txt"), "a stray file is not a snapshot")
        real_listdir = os.listdir
        with mock.patch.object(reg.os, "listdir", lambda p: sorted(real_listdir(p), reverse=True)):  # an unordered filesystem
            self.assertEqual(sn.broken_snapshots(self.ops), ["a-dir", "b-dir", "c-dir"])
            self.assertEqual(sn.list_snapshots(self.ops), expected)

    def test_resolve_snapshot_rules(self):
        self.assertEqual((sn.list_snapshots(self.ops), sn.broken_snapshots(self.ops)), ([], []))
        with self.assertRaisesRegex(sn.HygieneError, "no snapshots yet"):
            sn.resolve_snapshot(self.ops, None)
        first = self.snap()["id"]
        self.edit_live()
        second = self.snap(now=T0 + 10)["id"]
        self.assertEqual([sn.resolve_snapshot(self.ops, k) for k in (None, "latest", first, second)], [second, second, first, second])
        with self.assertRaisesRegex(sn.HygieneError, "no such snapshot: nope"):
            sn.resolve_snapshot(self.ops, "nope")

    def test_malformed_entries_make_a_snapshot_broken_with_a_handled_error(self):
        sid = self.snap()["id"]
        good = self.manifest(sid)["files"]["feedback-a.md"]
        sha = good["sha256"]
        broken = [dict(good, **{f: v}) for f, v in (
            ("sha256", 12345), ("sha256", sha.upper()), ("sha256", sha + "0"), ("sha256", sha[:63]), ("sha256", sha[:32]),
            ("mtime_ns", "5"), ("mtime_ns", 1.5), ("size", "7"), ("size", 1.5))]
        broken += [[]] + [{k: v for k, v in good.items() if k != field} for field in good if field != "mode"]
        for number, entry in enumerate(broken):
            with self.subTest(number):
                self.edit_manifest(sid, files={"feedback-a.md": entry})
                self.assertEqual((sn.list_snapshots(self.ops), sn.broken_snapshots(self.ops)), ([], [sid]))
                with self.assertRaisesRegex(sn.HygieneError, sid):
                    sn.load_manifest(self.ops, sid)

    def test_entries_without_a_mode_are_valid_and_odd_manifest_shapes_are_broken(self):
        sid = self.snap()["id"]
        self.without_modes(sid)
        self.assertEqual(sn.list_snapshots(self.ops), [sid])
        for label, text in (("list", "[]"), ("null", "null"), ("string", '"text"'), ("no files", "{}"),
                            ("files list", '{"files": []}'), ("files null", '{"files": null}')):
            with self.subTest(label):
                self.write_manifest(sid, text)
                self.assertEqual((sn.list_snapshots(self.ops), sn.broken_snapshots(self.ops)), ([], [sid]))
        with open(os.path.join(self.snap_dir(sid), "manifest.json"), "wb") as fh:
            fh.write(b"\xff\xfe\x00 not utf-8")
        self.assertEqual((sn.list_snapshots(self.ops), sn.broken_snapshots(self.ops)), ([], [sid]))


class RestoreBase(Base):
    def setUp(self):
        super().setUp()
        self.snap_id = self.snap()["id"]
        self.original = th.read(self.live)
        self.edit_live()
        self.edited = th.read(self.live)

    def restore(self, name="feedback-a.md", now=T0 + 5, **kw):
        return sn.restore_file(self.mem, self.ops, self.snap_id, name, now=now, **kw)

    def no_archive(self):
        self.assertFalse(os.path.exists(os.path.join(self.ops, "archive")))

    def raising_replace(self, matches):
        real = os.replace

        def fake(src, dst, **k):
            if matches(src):
                raise OSError(errno.EXDEV, "boom")
            return real(src, dst, **k)

        return mock.patch.object(sn.os, "replace", fake)


class RestoreTests(RestoreBase):
    def refused(self, name, reason):
        code, msgs = self.restore(name, apply=True, force=True)
        self.assertEqual((code, msgs), (1, ["refused: %s (%s)" % (reason, name)]))
        self.assertEqual(th.read(self.live), self.edited)
        self.no_archive()

    def test_each_unsafe_name_shape_is_refused_for_its_own_reason(self):
        for name in ("", ".", os.path.abspath(self.live), "../%s/feedback-a.md" % os.path.basename(self.mem)):
            with self.subTest(name):
                self.refused(name, "unsafe file name")

    def test_a_symlink_to_a_directory_inside_the_memory_dir_is_not_followed(self):
        os.mkdir(os.path.join(self.mem, "real"))
        th.write(os.path.join(self.mem, "real", "n.md"), NOTE)
        os.symlink(os.path.join(self.mem, "real"), os.path.join(self.mem, "lnk"))
        self.snap_id = self.snap(now=T0 + 3)["id"]
        self.refused("lnk/n.md", "path crosses a symlink")
        self.assertEqual(th.read(os.path.join(self.mem, "real", "n.md")), NOTE)

    def test_redundant_path_spellings_are_normalised_before_the_manifest_lookup(self):
        self.snap_id = self.with_sub_note(T0 + 3)
        th.write(self.live, "changed\n")
        self.assertEqual(self.restore("./feedback-a.md", apply=True, force=True)[0], 0)
        self.assertEqual(th.read(self.live), self.edited)
        th.write(os.path.join(self.mem, "sub", "n.md"), "changed\n")
        code, msgs = self.restore("sub//n.md", apply=True, force=True)
        self.assertEqual((code, msgs[-1]), (0, "restored sub/n.md from snapshot %s" % self.snap_id))

    def test_a_snapshot_from_another_directory_or_without_a_source_is_refused_with_the_reason(self):
        other = th.make_dir({"feedback-a.md": "x\n"})
        code, msgs = sn.restore_file(other, self.ops, self.snap_id, "feedback-a.md", apply=True, force=True, now=T0)
        self.assertEqual(code, 1)
        self.assertRegex(msgs[0], r"^refused: snapshot %s was taken from a different directory \(" % self.snap_id)
        self.edit_manifest(self.snap_id, source="")
        self.chdir(self.mem)
        self.assertIn("taken from a different directory", self.restore(apply=True, force=True)[1][0])
        self.assertEqual(th.read(self.live), self.edited)

    def test_a_note_stored_under_an_equivalent_spelling_is_never_treated_as_absent(self):
        for stored, asked in (("Caf\u00e9.md", "Cafe\u0301.md"), ("Stra\u00dfe.md", "strasse.md"), ("Note.md", "note.md")):
            with self.subTest(stored):
                th.write(os.path.join(self.mem, stored), "a stored note line that is long enough\n")
                self.snap_id = self.snap(now=T0 + 3 + len(stored))["id"]
                code, msgs = self.restore(asked, apply=True, force=True)
                self.assertEqual(code, 1)
                self.assertIn("the snapshot stores this note as %s, not %s" % (stored, asked), msgs[0])
                self.assertEqual(th.read(os.path.join(self.mem, stored)), "a stored note line that is long enough\n")
        self.no_archive()

    def test_refusal_logic_is_exact_about_twins_and_sources(self):
        manifest = {"source": os.path.realpath(self.mem), "files": {"a.md": {}, "A.md": {}, "only.md": {}}}
        self.assertIsNone(sn._refusal(self.mem, manifest, "id", "a.md"))
        self.assertIsNone(sn._refusal(self.mem, manifest, "id", "new.md"))
        self.assertEqual(sn._refusal(self.mem, manifest, "id", "ONLY.md"), "the snapshot stores this note as only.md, not ONLY.md")
        self.assertIn("different directory", sn._refusal(self.mem, dict(manifest, source=""), "id", "a.md"))

    def test_dry_run_messages_distinguish_differing_missing_and_identical_live_files(self):
        want = "would restore feedback-a.md from snapshot %s (live file %%s)" % self.snap_id
        self.assertEqual(self.restore(), (0, [want % "differs"]))
        os.rename(self.live, self.parked)
        self.assertEqual(self.restore(), (0, [want % "is missing"]))
        self.assertFalse(os.path.exists(self.live))
        th.write(self.live, self.original)
        os.utime(self.live, ns=(1_500_000_000_000_000_000,) * 2)
        self.assertEqual(self.restore(apply=True, force=True), (0, ["already identical: feedback-a.md"]))
        self.assertEqual(os.stat(self.live).st_mtime_ns, 1_500_000_000_000_000_000)
        self.no_archive()

    def test_apply_with_force_reports_both_steps_and_archives_privately_under_the_run_stamp(self):
        code, msgs = self.restore(apply=True, force=True)
        archived = os.path.join(self.ops, "archive", mc.utc_stamp(T0 + 5), "feedback-a.md")
        self.assertEqual((code, msgs), (0, ["archived live copy -> " + archived, "restored feedback-a.md from snapshot " + self.snap_id]))
        self.assertEqual(th.read(archived), self.edited)
        self.assertEqual([mode_of(p) for p in (os.path.join(self.ops, "archive"), os.path.dirname(archived))], [0o700, 0o700])
        self.assertEqual(glob.glob(os.path.join(self.mem, ".restore-*")), [])

    def test_archiving_a_nested_note_creates_private_parent_directories(self):
        self.snap_id = self.with_sub_note(T0 + 3)
        th.write(os.path.join(self.mem, "sub", "n.md"), "changed\n")
        self.restore("sub/n.md", apply=True, force=True)
        self.assertEqual(mode_of(os.path.join(self.ops, "archive", mc.utc_stamp(T0 + 5), "sub")), 0o700)

    def test_without_a_stored_mode_the_live_mode_is_kept_or_a_private_default_used_and_no_archive_step_is_reported(self):
        self.without_modes(self.snap_id)
        os.chmod(self.live, 0o640)
        self.restore(apply=True, force=True)
        self.assertEqual(mode_of(self.live), 0o640)
        os.rename(self.live, self.parked)
        self.assertEqual(self.restore(apply=True), (0, ["restored feedback-a.md from snapshot " + self.snap_id]))
        self.assertEqual(mode_of(self.live), 0o600)

    def test_a_bad_staged_copy_or_a_missing_snapshot_copy_leaves_the_live_file_alone(self):
        pattern = r"the staged copy failed its sha256 check\); the live file was not touched; leftover temp file: \S*\.restore-"
        with mock.patch.object(sn.shutil, "copyfile", side_effect=lambda src, dst, **_kw: th.write(dst, "garbage")):
            with self.assertRaisesRegex(sn.HygieneError, pattern):
                self.restore(apply=True, force=True)
        self.assertEqual(th.read(self.live), self.edited)
        os.rename(self.copy_of(self.snap_id), self.parked)
        self.assertEqual(self.restore(apply=True, force=True), (1, ["refused: snapshot copy of feedback-a.md failed its sha256 check"]))

    def test_read_back_verification_catches_a_note_changed_right_after_the_swap(self):
        real_swap = sn._swap_in

        def swap_then_concurrent_write(*args):
            result = real_swap(*args)
            self.edit_live("a live session wrote in the gap")
            return result

        with mock.patch.object(sn, "_swap_in", swap_then_concurrent_write):
            with self.assertRaisesRegex(sn.HygieneError, "failed read-back verification: feedback-a.md"):
                self.restore(apply=True, force=True)

    def test_failed_moves_are_handled_errors_and_a_failed_swap_puts_the_original_back(self):
        is_staged = lambda src: os.path.basename(src).startswith(".restore-")  # noqa: E731
        with self.raising_replace(is_staged):
            with self.assertRaisesRegex(sn.HygieneError, r"could not place the restored file \(boom\); the original was put back"):
                self.restore(apply=True, force=True)
        self.assertEqual(th.read(self.live), self.edited)
        os.rename(self.live, self.parked)
        with self.raising_replace(is_staged):
            with self.assertRaisesRegex(sn.HygieneError, r"could not place the restored file \(boom\)"):
                self.restore(apply=True)
        self.assertFalse(os.path.exists(self.live))
        os.rename(self.parked, self.live)
        with self.raising_replace(lambda src: src == self.live):
            with self.assertRaisesRegex(sn.HygieneError, "could not archive feedback-a.md: boom"):
                self.restore(apply=True, force=True)
        self.assertEqual(th.read(self.live), self.edited)


class RestoreAbsentTests(RestoreBase):
    def setUp(self):
        super().setUp()
        self.extra = os.path.join(self.mem, "new-note.md")
        th.write(self.extra, "brand new\n")

    def test_each_stage_has_its_own_message(self):
        archived = os.path.join(self.ops, "archive", mc.utc_stamp(T0 + 5), "new-note.md")
        self.assertEqual(self.restore("new-note.md"), (0, ["would move new-note.md (absent from the snapshot) into the archive"]))
        refusal = "refused: new-note.md is absent from the snapshot; pass --force to archive it"
        self.assertEqual(self.restore("new-note.md", apply=True), (1, [refusal]))
        self.assertTrue(os.path.exists(self.extra))
        self.assertEqual(self.restore("new-note.md", apply=True, force=True), (0, ["archived new-note.md -> " + archived]))
        self.assertEqual(th.read(archived), "brand new\n")
        os.mkdir(os.path.join(self.mem, "dir.md"))
        for name in ("ghost.md", "dir.md"):
            for flags in ({}, {"apply": True, "force": True}):
                self.assertEqual(self.restore(name, **flags), (1, ["%s is in neither the snapshot nor the live directory" % name]))
        self.assertTrue(os.path.isdir(os.path.join(self.mem, "dir.md")))

    def test_only_a_real_archive_move_takes_the_lock(self):
        with open(os.path.join(self.ops, ".lock"), "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.restore("new-note.md")[0], 0)
            with self.assertRaisesRegex(sn.HygieneError, "holds the lock"):
                self.restore("new-note.md", apply=True, force=True)
        self.assertTrue(os.path.exists(self.extra))


class PruneGapTests(Base):
    def make(self, n):
        ids = []
        for i in range(n):
            self.edit_live("edit %d" % i)
            ids.append(self.snap(now=T0 + 10 * i)["id"])
        return ids

    def rm(self, snap_id):
        return "rm -r " + shlex.quote(os.path.join(self.ops, "snapshots", snap_id))

    def test_default_keep_and_the_newest_intact_snapshots_are_retained(self):
        ids = self.make(7)
        self.assertEqual(sn.prune_plan(self.ops), [self.rm(ids[0]), self.rm(ids[1])])
        self.assertEqual(len(sn.prune_plan(self.ops, keep=1)), 6)
        self.tamper(self.copy_of(ids[6]))  # a damaged snapshot never counts towards the kept ones
        self.assertEqual(sn.prune_plan(self.ops, keep=2), [self.rm(i) for i in ids[:4]] + [self.rm(ids[6])])

    @NOT_ROOT
    def test_an_unreadable_snapshot_counts_as_damaged_instead_of_crashing(self):
        ids = self.make(2)
        os.chmod(self.copy_of(ids[1]), 0)
        try:
            self.assertEqual(sn.prune_plan(self.ops, keep=1), [self.rm(ids[1])])
        finally:
            os.chmod(self.copy_of(ids[1]), 0o600)

    def test_paths_are_shell_quoted_and_odd_ones_become_comments_never_commands(self):
        self.ops = os.path.join(tempfile.mkdtemp(prefix="memops_"), "my ops; echo 'x'", "slug")
        ids = self.make(2)
        line = sn.prune_plan(self.ops, keep=1)[0]
        self.assertEqual(shlex.split(line), ["rm", "-r", os.path.join(os.path.abspath(self.ops), "snapshots", ids[0])])
        self.ops = os.path.join(tempfile.mkdtemp(prefix="memops_"), "sl\nug")
        self.make(2)
        plan = sn.prune_plan(self.ops, keep=1)
        self.assertEqual((len(plan), len(plan[0].splitlines())), (1, 1))
        self.assertTrue(plan[0].startswith("# skipped, unusual name: "))


class CompareGapTests(unittest.TestCase):
    def compare(self, base, stage, files=None):
        return sn.compare_dirs(th.make_dir(base), th.make_dir(stage), files)

    def test_both_sides_must_be_directories_and_must_differ(self):
        good, plain = th.make_dir({"a.md": LONG + "\n"}), th.make_dir({"f.txt": "x"})
        file_path, missing = os.path.join(plain, "f.txt"), os.path.join(plain, "nope")
        for base, stage in ((file_path, good), (good, file_path), (good, missing)):
            with self.assertRaisesRegex(sn.HygieneError, "not a directory: "):
                sn.compare_dirs(base, stage)
        alias = os.path.join(tempfile.mkdtemp(prefix="memalias_"), "alias")
        os.symlink(good, alias)
        with self.assertRaisesRegex(sn.HygieneError, "same directory"):
            sn.compare_dirs(good, alias)

    def test_scope_is_reported_empty_scope_checks_nothing_and_unknown_names_are_listed(self):
        base = {"a.md": LONG + "\n" + LONG + " two\n", "b.md": LONG + " three\n", "notes.txt": LONG + " not markdown\n"}
        stage = {"x.md": "nothing\n"}
        full = self.compare(base, stage)
        self.assertEqual((full._fields, full.baseline_files, full.lines_checked), (("lost", "baseline_files", "lines_checked"), 2, 3))
        self.assertEqual(self.compare(base, stage, files=["a.md"])[1:], (1, 2))
        self.assertEqual(self.compare(base, stage, files=[]), ([], 0, 0))
        with self.assertRaisesRegex(sn.HygieneError, r"not in the baseline: notes\.txt, c\.md$"):
            self.compare(base, base, files=["a.md", "notes.txt", "c.md"])

    def test_only_markdown_staging_files_hold_moved_lines_and_a_shared_line_is_charged_to_the_last_file(self):
        self.assertEqual(self.compare({"a.md": LONG + "\n"}, {"a.md": "x\n", "notes.txt": LONG + "\n"}).lost, [("a.md", 1)])
        self.assertEqual(self.compare({"a.md": LONG + "\n"}, {"a.md": "x\n", "sub/deep.md": LONG + "\n"}).lost, [])
        base, stage = {"a.md": LONG + "\n", "b.md": LONG + "\n"}, {"x.md": LONG + "\n"}  # one copy for two files
        self.assertEqual(self.compare(base, stage).lost, [("b.md", 1)])
        self.assertEqual(self.compare(base, stage, files=["b.md", "a.md"]).lost, [("a.md", 1)])

    def test_only_newline_separates_lines_and_undecodable_bytes_do_not_crash(self):
        text = "short\x0cshort short\n" + LONG + "\n"
        self.assertEqual(self.compare({"a.md": text}, {"a.md": "x\n"}).lost, [("a.md", 2)])
        joined = "first part of a long line that qualifies second part of it\n"
        self.assertEqual(self.compare({"a.md": joined}, {"a.md": joined.replace(" ", "\n")}).lost, [("a.md", 1)])
        raw = b"\xff " + LONG.encode() + b"\n"
        self.assertEqual(self.compare({"a.md": raw}, {"a.md": raw}).lost, [])


if __name__ == "__main__":
    unittest.main()
