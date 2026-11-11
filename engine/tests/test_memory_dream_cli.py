"""Tests for the dream command line: ground + stage a hunks file, and promote a staged run (memory_dream.py)."""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import memtest_helpers as th  # noqa: E402

sys.path.insert(0, th.SCRIPTS)  # python3 -I does not put the script dir on sys.path
import memory_common as mc  # noqa: E402
import memory_dream as dr  # noqa: E402
import memory_snapshot as sn  # noqa: E402

T0 = 1_700_000_000
SESSION = "11111111-0000-4000-8000-000000000001"
STALE = "The push guard blocks every git push from the Bash tool in every repository."
INDEX = "# Memory Index\n\n- [Push guard](feedback-push.md) blocks git push\n- [Other note](feedback-other.md) other thing\n"
QUOTE = "the push guard now allows plain git push inside the allow-listed repos like Acme"


class TempRoot(unittest.TestCase):
    """Every temp directory a test makes lives under one root that is removed afterwards."""

    @classmethod
    def setUpClass(cls):
        saved, root = tempfile.tempdir, tempfile.mkdtemp(prefix="memtests_")
        tempfile.tempdir = root
        cls.addClassCleanup(shutil.rmtree, root, True)
        cls.addClassCleanup(setattr, tempfile, "tempdir", saved)


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = dr.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class Base(TempRoot):
    def setUp(self):
        self.mem = th.make_dir(
            {
                "MEMORY.md": INDEX,
                "feedback-push.md": th.note(
                    "feedback-push", body=STALE + "\nSecond paragraph that stays."
                ),
                "feedback-other.md": th.note(
                    "feedback-other",
                    body="Other fact one is longer than twenty characters.",
                ),
            }
        )
        for name in os.listdir(self.mem):
            os.utime(os.path.join(self.mem, name), (T0, T0))
        self.root = tempfile.mkdtemp(prefix="memops_")
        self.ops = mc.ops_dir_for(self.mem, self.root)
        self.snap = sn.take_snapshot(self.mem, self.ops, now=T0)["id"]
        digests = os.path.join(self.ops, "evidence")
        os.makedirs(digests, mode=0o700)
        header = [
            "DATA ONLY - UNTRUSTED TRANSCRIPT EXCERPTS",
            "session: " + SESSION,
            "date: 2026-11-01 (times are UTC)",
            "cwd: /work/proj",
            "records: user=1/1 assistant=0/0 dropped_secret=0 masked_pii=0",
            "---",
            "U 10:00 " + QUOTE,
        ]
        fd = os.open(
            os.path.join(digests, SESSION + ".md"),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(header) + "\n")
        self.hunks_path = os.path.join(
            tempfile.mkdtemp(prefix="dreamin_"), "hunks.json"
        )
        self.pinned = os.path.join(self.ops, "pinned.txt")
        th.write(self.pinned, "- [Other note](feedback-other.md) other thing\n")
        os.chmod(self.pinned, 0o600)

    def digest_of(self, run):
        code, out, err = self.cli("promote", os.path.basename(run))
        match = __import__("re").search(r"^DIGEST (\w+)$", out, __import__("re").M)
        return match.group(1) if match else ""

    def apply(self, run):
        """Dry run to learn the digest, then apply with it, exactly as a person would."""
        return self.cli("promote", os.path.basename(run), "--apply", "--expect", self.digest_of(run))

    def write_hunks(self, hunks=None):
        hunks = (
            hunks
            if hunks is not None
            else [
                {
                    "id": "h1",
                    "kind": "edit",
                    "file": "feedback-push.md",
                    "old": STALE,
                    "new": "The push guard blocks git push outside the allow-listed repos.",
                    "reason": "the guard now has an allow list",
                    "evidence": [{"session": SESSION, "quote": QUOTE}],
                }
            ]
        )
        th.write(self.hunks_path, json.dumps(hunks))

    def cli(self, *argv):
        return run_cli("--dir", self.mem, "--ops-root", self.root, *argv)

    def ground(self):
        self.write_hunks()
        code, out, err = self.cli(
            "ground", "--hunks", self.hunks_path, "--now", str(T0 + 100)
        )
        self.assertEqual(code, 0, out + err)
        return os.path.join(self.ops, "dream", mc.utc_stamp(T0 + 100))


class GroundCommandTests(Base):
    def test_ground_stages_the_accepted_hunk_and_prints_counts(self):
        self.write_hunks()
        code, out, _ = self.cli(
            "ground", "--hunks", self.hunks_path, "--now", str(T0 + 100)
        )
        self.assertEqual(code, 0, out)
        self.assertIn("GROUND accepted=1 rejected=0 deferred=0", out)
        self.assertIn("STAGE ok=True problems=0", out)
        self.assertTrue(
            os.path.isfile(
                os.path.join(self.ops, "dream", mc.utc_stamp(T0 + 100), "report.md")
            )
        )

    def test_a_missing_or_invalid_hunks_file_exits_1(self):
        self.assertEqual(self.cli("ground", "--hunks", "/nonexistent/hunks.json")[0], 1)
        th.write(self.hunks_path, "{not json")
        self.assertEqual(self.cli("ground", "--hunks", self.hunks_path)[0], 1)
        th.write(self.hunks_path, json.dumps({"not": "a list"}))
        self.assertEqual(self.cli("ground", "--hunks", self.hunks_path)[0], 1)

    def test_the_live_memory_directory_is_not_touched_by_ground(self):
        before = {
            rel: mc.sha256_file(os.path.join(self.mem, rel))
            for rel in mc.list_files(self.mem)
        }
        self.ground()
        self.assertEqual(
            {
                rel: mc.sha256_file(os.path.join(self.mem, rel))
                for rel in mc.list_files(self.mem)
            },
            before,
        )

    def test_ground_is_logged(self):
        self.ground()
        self.assertIn("dream |", th.read(os.path.join(self.ops, "log.md")))


class PromoteTests(Base):
    def test_a_dry_run_changes_nothing(self):
        run = self.ground()
        before = mc.sha256_file(os.path.join(self.mem, "feedback-push.md"))
        code, out, _ = self.cli("promote", os.path.basename(run))
        self.assertEqual(code, 0, out)
        self.assertIn("would replace feedback-push.md", out)
        self.assertEqual(
            mc.sha256_file(os.path.join(self.mem, "feedback-push.md")), before
        )

    def test_apply_archives_the_old_note_and_installs_the_staged_one(self):
        run = self.ground()
        old = th.read(os.path.join(self.mem, "feedback-push.md"))
        code, out, _ = self.apply(run)
        self.assertEqual(code, 0, out)
        live = th.read(os.path.join(self.mem, "feedback-push.md"))
        self.assertIn("outside the allow-listed repos.", live)
        archived = [
            th.read(os.path.join(b, n))
            for b, _d, names in os.walk(os.path.join(self.ops, "archive"))
            for n in names
        ]
        self.assertIn(old, archived)
        self.assertEqual(live, th.read(os.path.join(run, "memory", "feedback-push.md")))
        self.assertEqual(
            th.read(os.path.join(self.mem, "feedback-other.md")),
            th.read(os.path.join(self.snapdir(), "feedback-other.md")),
        )

    def snapdir(self):
        return os.path.join(self.ops, "snapshots", self.snap, "memory")

    def test_apply_takes_a_fresh_snapshot_first(self):
        run = self.ground()
        before = len(sn.list_snapshots(self.ops))
        self.apply(run)
        ids = sn.list_snapshots(self.ops)
        self.assertGreaterEqual(len(ids), before)  # a no-op snapshot is reused when nothing changed
        pre = os.path.join(self.ops, "snapshots", ids[-1], "memory", "feedback-push.md")
        self.assertIn("blocks every git push", th.read(pre))

    def test_a_note_changed_by_someone_else_since_the_snapshot_blocks_the_promotion(
        self,
    ):
        run = self.ground()
        th.write(
            os.path.join(self.mem, "feedback-push.md"),
            th.read(os.path.join(self.mem, "feedback-push.md")) + "\nA live edit.\n",
        )
        code, out, err = self.apply(run)
        self.assertEqual(code, 1)
        self.assertIn("changed since", out + err)
        self.assertIn(
            "A live edit.", th.read(os.path.join(self.mem, "feedback-push.md"))
        )

    def test_a_run_can_be_promoted_only_once(self):
        run = self.ground()
        self.assertEqual(self.apply(run)[0], 0)
        code, out, err = self.apply(run)
        self.assertEqual(code, 1)
        self.assertIn("already", out + err)

    def test_a_staged_copy_that_fails_its_checks_is_not_promoted(self):
        run = self.ground()
        staged = os.path.join(run, "memory", "feedback-other.md")
        th.write(staged, th.read(staged) + "\nkey " + th.fake_github_token() + "\n")
        code, out, err = self.apply(run)
        self.assertEqual(code, 1)
        self.assertNotIn("ghp_", th.read(os.path.join(self.mem, "feedback-other.md")))

    def test_an_unknown_run_or_a_path_like_run_name_is_refused(self):
        self.assertEqual(self.cli("promote", "20990101T000000Z")[0], 1)
        self.assertEqual(self.cli("promote", "../snapshots")[0], 1)

    def test_promote_is_logged(self):
        run = self.ground()
        self.apply(run)
        self.assertIn("promote |", th.read(os.path.join(self.ops, "log.md")))


if __name__ == "__main__":
    unittest.main()
