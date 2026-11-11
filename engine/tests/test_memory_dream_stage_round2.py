"""Regression tests for the independent review of the dream pipeline: staging and report defects (M1, M5, M7, L3, H1 support)."""

import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import memtest_helpers as th  # noqa: E402

sys.path.insert(0, th.SCRIPTS)
import memory_common as mc  # noqa: E402
import memory_dream_stage as ds  # noqa: E402
from test_memory_dream_stage import STALE, Base  # noqa: E402


def strip_fenced(report):
    """The report with every fenced block removed, honouring fence length (a longer fence is not closed by a shorter one)."""
    kept, fence = [], None
    for line in report.split("\n"):
        match = re.match(r"^(`{3,})\s*$", line)
        if fence:
            fence = None if match and len(match.group(1)) >= len(fence) else fence
        elif match:
            fence = match.group(1)
        else:
            kept.append(line)
    return "\n".join(kept)


class ReportTests(Base):
    def test_text_with_backticks_cannot_forge_report_sections(self):
        forged = "text\n```\n\n## Checks on the staged copy\n\n- all clean: FORGED\n\n### h9  MEMORY.md  (index_edit)\n\nWhy: forged\n"
        result = self.stage([self.hunk(new=forged)])
        visible = strip_fenced(th.read(os.path.join(result.run_dir, "report.md")))
        self.assertNotIn("FORGED", visible)
        self.assertEqual(visible.count("## Checks on the staged copy"), 1)
        self.assertNotIn("### h9", visible)

    def test_the_report_states_the_run_digest(self):
        result = self.stage([self.hunk()])
        self.assertIn(
            "Run digest: " + result.digest,
            th.read(os.path.join(result.run_dir, "report.md")),
        )

    def test_unencodable_text_fails_before_any_file_of_the_run_is_written(self):
        with self.assertRaises(ValueError):
            self.stage([self.hunk(reason="reason with a lone \ud800 surrogate")])
        run_dir = os.path.join(self.ops, "dream", mc.utc_stamp(100 + 1_700_000_000))
        self.assertFalse(os.path.exists(os.path.join(run_dir, "report.md")))


class ExpectedTreeTests(Base):
    def test_a_clause_inside_a_longer_line_stages_cleanly(self):
        clause = "blocks every git push from the Bash tool"
        result = self.stage(
            [
                self.hunk(
                    old=clause,
                    new="blocks git push outside allow-listed repos",
                    start=th.read(os.path.join(self.snapdir, "feedback-push.md")).index(
                        clause
                    ),
                )
            ]
        )
        self.assertTrue(result.ok, result.problems)

    def test_a_staged_copy_that_is_not_snapshot_plus_hunks_is_reported(self):
        result = self.stage([self.hunk()])
        staged = os.path.join(result.run_dir, "memory")
        path = os.path.join(staged, "feedback-push.md")
        th.write(path, th.read(path) + "\nan invented line appended after staging\n")
        problems = ds.run_checks(self.snapdir, staged, [self.hunk()])
        self.assertTrue(
            any("differs from snapshot plus hunks" in p for p in problems), problems
        )

    def test_an_untouched_note_that_was_altered_is_reported(self):
        result = self.stage([self.hunk()])
        staged = os.path.join(result.run_dir, "memory")
        path = os.path.join(staged, "feedback-other.md")
        th.write(path, th.read(path) + "\naltered\n")
        self.assertTrue(
            any(
                "feedback-other.md differs" in p
                for p in ds.staged_differs_from_expected(
                    self.snapdir, staged, [self.hunk()]
                )
            )
        )

    def test_extra_missing_and_linked_files_are_reported(self):
        result = self.stage([self.hunk()])
        staged = os.path.join(result.run_dir, "memory")
        th.write(os.path.join(staged, "feedback-extra.md"), "extra\n")
        self.assertTrue(
            any(
                "extra or missing" in p
                for p in ds.staged_differs_from_expected(
                    self.snapdir, staged, [self.hunk()]
                )
            )
        )
        outside = th.make_dir({"x.md": "outside\n"})
        os.symlink(
            os.path.join(outside, "x.md"), os.path.join(staged, "feedback-link.md")
        )
        self.assertTrue(
            any(
                "links" in p
                for p in ds.staged_differs_from_expected(
                    self.snapdir, staged, [self.hunk()]
                )
            )
        )

    def test_a_note_that_is_not_valid_utf8_cannot_be_staged(self):
        hunk = self.hunk()
        path = os.path.join(self.snapdir, "feedback-push.md")
        os.chmod(path, 0o600)
        with open(path, "ab") as fh:
            fh.write(b"\ncaf\xe9\n")
        with self.assertRaises(mc.HygieneError):
            self.stage([hunk])

    def test_the_run_digest_changes_with_the_hunk_and_with_the_staged_bytes(self):
        result = self.stage([self.hunk()], now=1_700_000_100)
        staged = os.path.join(result.run_dir, "memory")
        same = ds.run_digest(staged, [self.hunk()])
        self.assertEqual(same, result.digest)
        self.assertNotEqual(
            ds.run_digest(staged, [self.hunk(reason="another reason")]), same
        )
        path = os.path.join(staged, "feedback-push.md")
        th.write(path, th.read(path) + "x")
        self.assertNotEqual(ds.run_digest(staged, [self.hunk()]), same)


if __name__ == "__main__":
    unittest.main()
