"""Regression tests for the safety-net defects the hardening verifiers confirmed (round 3)."""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memtest_helpers as th  # noqa: E402

sys.path.insert(0, th.SCRIPTS)  # python3 -I does not put the script dir on sys.path
import memory_common as mc  # noqa: E402
import memory_compare as mcmp  # noqa: E402
import memory_snapshot as sn  # noqa: E402

T0 = 1_700_000_000


class TempRoot(unittest.TestCase):
    """Every temp directory a test makes lives under one root that is removed afterwards."""

    @classmethod
    def setUpClass(cls):
        saved, root = tempfile.tempdir, tempfile.mkdtemp(prefix="memtests_")
        tempfile.tempdir = root
        cls.addClassCleanup(shutil.rmtree, root, True)
        cls.addClassCleanup(setattr, tempfile, "tempdir", saved)


class Base(TempRoot):
    def setUp(self):
        self.mem = th.make_dir(
            {
                "MEMORY.md": "- [A](feedback-a.md) hook\n",
                "feedback-a.md": th.note("feedback-a"),
            }
        )
        self.root = tempfile.mkdtemp(prefix="memops_")
        self.ops = mc.ops_dir_for(self.mem, self.root)
        self.live = os.path.join(self.mem, "feedback-a.md")
        self.snap_id = sn.take_snapshot(self.mem, self.ops, now=T0)["id"]
        self.manifest_path = os.path.join(
            self.ops, "snapshots", self.snap_id, "manifest.json"
        )

    def edit_manifest(self, change):
        data = json.loads(th.read(self.manifest_path))
        change(data)
        os.chmod(self.manifest_path, 0o600)
        th.write(self.manifest_path, json.dumps(data))


class ManifestShapeTests(Base):
    def test_a_manifest_whose_source_is_not_a_string_is_a_refusal_not_a_traceback(self):
        for bad in (None, 5, ["x"], {"a": 1}):
            with self.subTest(source=bad):
                self.edit_manifest(lambda data: data.update(source=bad))
                with self.assertRaises(sn.HygieneError):
                    sn.take_snapshot(self.mem, self.ops, now=T0 + 5)
                code, messages = sn.restore_file(
                    self.mem,
                    self.ops,
                    self.snap_id,
                    "feedback-a.md",
                    apply=True,
                    force=True,
                    now=T0 + 9,
                )
                self.assertEqual(code, 1, messages)

    def test_a_sha256_with_a_trailing_newline_makes_the_manifest_malformed(self):
        self.edit_manifest(
            lambda data: data["files"]["feedback-a.md"].update(
                sha256=data["files"]["feedback-a.md"]["sha256"] + "\n"
            )
        )
        self.assertEqual(sn.list_snapshots(self.ops), [])
        self.assertEqual(sn.broken_snapshots(self.ops), [self.snap_id])

    def test_a_boolean_size_or_mtime_makes_the_manifest_malformed(self):
        for field in ("size", "mtime_ns"):
            with self.subTest(field):
                self.edit_manifest(
                    lambda data: data["files"]["feedback-a.md"].update({field: True})
                )
                self.assertEqual(sn.list_snapshots(self.ops), [])


class DirectoryAtLivePathTests(Base):
    def make_directory_there(self):
        os.remove(self.live)
        os.mkdir(self.live)
        th.write(os.path.join(self.live, "inner.md"), "inner file that must survive\n")

    def test_a_directory_where_the_note_belongs_is_refused_and_left_alone(self):
        self.make_directory_there()
        for apply_, force in ((False, False), (True, False), (True, True)):
            with self.subTest(apply=apply_, force=force):
                code, messages = sn.restore_file(
                    self.mem,
                    self.ops,
                    self.snap_id,
                    "feedback-a.md",
                    apply=apply_,
                    force=force,
                    now=T0 + 9,
                )
                self.assertEqual(code, 1, messages)
                self.assertIn("directory", " ".join(messages))
        self.assertTrue(os.path.isdir(self.live))
        self.assertEqual(
            th.read(os.path.join(self.live, "inner.md")),
            "inner file that must survive\n",
        )
        self.assertFalse(os.path.exists(os.path.join(self.ops, "archive")))


class CompareModifiedLineTests(TempRoot):
    LONG = " 2026-11-08T15:02:07.926Z and some more text"

    def compare(self, baseline_line):
        base = th.make_dir(
            {"a.md": "intro line that is long enough to count\n" + baseline_line + "\n"}
        )
        staged = th.make_dir({"a.md": "intro line that is long enough to count\n"})
        return mcmp.compare_dirs(base, staged)

    def test_a_front_matter_modified_stamp_may_change_freely(self):
        self.assertEqual(self.compare("modified:" + self.LONG).lost, [])

    def test_only_a_leading_modified_prefix_is_exempt(self):
        self.assertEqual(self.compare("last modified:" + self.LONG).lost, [("a.md", 2)])
        self.assertEqual(self.compare("not modified:" + self.LONG).lost, [("a.md", 2)])


if __name__ == "__main__":
    unittest.main()
