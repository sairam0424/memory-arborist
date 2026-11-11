"""Tests added after the mutation check of the dream modules: each one fails when the guard it names is removed."""

import os
import shutil
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import memtest_helpers as th  # noqa: E402

sys.path.insert(0, th.SCRIPTS)
import memory_common as mc  # noqa: E402
import memory_dream as dr  # noqa: E402
import memory_dream_stage as ds  # noqa: E402
import memory_snapshot as sn  # noqa: E402
import test_memory_dream_cli_round2 as cli2  # noqa: E402
import test_memory_dream_stage as stage_tests  # noqa: E402


class StageCheckTests(stage_tests.Base):
    def test_a_staged_run_with_new_secret_shaped_text_is_not_ok(self):
        leaked = "The push guard blocks git push. key " + th.fake_github_token()
        result = self.stage([self.hunk(new=leaked)])
        self.assertFalse(result.ok)
        self.assertTrue(
            any("secret-shaped" in problem for problem in result.problems),
            result.problems,
        )

    def test_a_snapshot_note_that_is_not_valid_utf8_is_refused_by_the_stage(self):
        hunk = self.hunk()
        live = os.path.join(self.mem, "feedback-push.md")
        with open(live, "ab") as fh:
            fh.write(b"\ncaf\xe9\n")
        later = sn.take_snapshot(self.mem, self.ops, now=stage_tests.T0 + 50)["id"]
        with self.assertRaisesRegex(mc.HygieneError, "not valid UTF-8"):
            ds.stage_run(self.ops, later, [hunk], [], [], now=stage_tests.T0 + 100)


class RunDirectoryTrustTests(cli2.Setup):
    """promote must not trust anything inside the run directory except what it re-derives."""

    def test_a_snapshot_id_that_points_into_the_run_directory_is_refused(self):
        run = self.staged_run()
        fake = os.path.join(run, "fakesnap")
        shutil.copytree(os.path.join(self.ops, "snapshots", self.snap), fake)
        report = os.path.join(run, "report.md")
        th.write(
            report,
            th.read(report).replace(
                "Snapshot: " + self.snap,
                "Snapshot: ../dream/%s/fakesnap" % os.path.basename(run),
            ),
        )
        self.assertIn("does not exist", self.refused(run))

    def test_a_link_anywhere_in_the_run_directory_is_refused(self):
        run = self.staged_run()
        outside = th.make_dir({"x.json": "[]"})
        target = os.path.join(run, "hunks.rejected.json")
        os.chmod(target, 0o600)
        os.remove(target)
        os.symlink(os.path.join(outside, "x.json"), target)
        self.assertIn("run directory holds links", self.refused(run))


class LateChangeTests(cli2.Setup):
    two_note_run = cli2.InstallTests.two_note_run

    def test_a_note_turned_into_a_link_after_the_checks_stops_everything_first(self):
        run = self.two_note_run()
        digest = self.digest_of(run)
        other_before = th.read(os.path.join(self.mem, "feedback-other.md"))
        push = os.path.join(self.mem, "feedback-push.md")
        copy = th.make_dir({"copy.md": th.read(push)})
        real = ds.run_digest

        def swap_after_the_checks(staged_dir, accepted):
            result = real(staged_dir, accepted)
            os.remove(push)
            os.symlink(os.path.join(copy, "copy.md"), push)
            return result

        ds.run_digest = swap_after_the_checks
        try:
            code, out, err = self.cli(
                "promote", os.path.basename(run), "--apply", "--expect", digest
            )
        finally:
            ds.run_digest = real
        self.assertEqual(code, 1, out + err)
        self.assertIn("cannot be replaced", out + err)
        self.assertEqual(
            th.read(os.path.join(self.mem, "feedback-other.md")), other_before
        )
        self.assertEqual([n for n in os.listdir(self.mem) if n.startswith(".")], [])
        self.assertFalse(os.path.lexists(os.path.join(run, dr.MARKER)))


if __name__ == "__main__":
    unittest.main()
