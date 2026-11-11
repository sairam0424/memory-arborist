"""Tests for staging dream hunks outside the memory directory (memory_dream_stage.py). No model is involved."""

import ast
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import memtest_helpers as th  # noqa: E402

sys.path.insert(0, th.SCRIPTS)  # python3 -I does not put the script dir on sys.path
import memory_common as mc  # noqa: E402
import memory_dream_stage as ds  # noqa: E402
import memory_lint as ml  # noqa: E402
import memory_snapshot as sn  # noqa: E402

# One note per YAML hazard code memory_frontmatter.py can emit: (code, th.note keyword arguments).
YAML_HAZARD_NOTES = (
    ("yaml-colon", {"desc": "uses a: colon"}),
    ("yaml-comment", {"desc": "see issue #12"}),
    ("yaml-indicator", {"desc": "`git push` is blocked"}),
    ("yaml-bad-escape", {"desc": '"bad \\q escape"'}),
    ("yaml-trailing-text", {"desc": '"quoted" and then more'}),
    ("yaml-unterminated", {"desc": '"never closed'}),
    ("yaml-nonstring", {"desc": "true"}),
    ("yaml-multiline", {"desc": "first line\n  continues here"}),
    ("yaml-tab-indent", {"extra": "\tstray: 1\n"}),
)
T0 = 1_700_000_000
STALE = "The push guard blocks every git push from the Bash tool in every repository."
INDEX = "# Memory Index\n\n- [Push guard](feedback-push.md) blocks git push\n- [Other note](feedback-other.md) other thing\n"


class TempRoot(unittest.TestCase):
    """Every temp directory a test makes lives under one root that is removed afterwards."""

    @classmethod
    def setUpClass(cls):
        saved, root = tempfile.tempdir, tempfile.mkdtemp(prefix="memtests_")
        tempfile.tempdir = root
        cls.addClassCleanup(shutil.rmtree, root, True)
        cls.addClassCleanup(setattr, tempfile, "tempdir", saved)


def sha_tree(directory):
    return {
        rel: mc.sha256_file(os.path.join(directory, rel))
        for rel in mc.list_files(directory)
    }


class Base(TempRoot):
    def setUp(self):
        self.mem = th.make_dir(
            {
                "MEMORY.md": INDEX,
                "feedback-push.md": th.note(
                    "feedback-push",
                    body=STALE
                    + "\nSecond paragraph that stays.\nThird paragraph also stays here.",
                ),
                "feedback-other.md": th.note(
                    "feedback-other", body="Other fact one is longer than twenty characters.\nOther fact two is also longer than twenty."
                ),
            }
        )
        for name in os.listdir(self.mem):
            os.utime(os.path.join(self.mem, name), (T0, T0))
        self.root = tempfile.mkdtemp(prefix="memops_")
        self.ops = mc.ops_dir_for(self.mem, self.root)
        self.snap = sn.take_snapshot(self.mem, self.ops, now=T0)["id"]
        self.snapdir = os.path.join(self.ops, "snapshots", self.snap, "memory")

    def hunk(self, **over):
        text = th.read(os.path.join(self.snapdir, "feedback-push.md"))
        base = {
            "id": "h1",
            "kind": "edit",
            "file": "feedback-push.md",
            "old": STALE,
            "start": text.index(STALE),
            "new": "The push guard blocks git push outside the allow-listed repos.",
            "reason": "guard changed",
            "evidence": [
                {
                    "session": "11111111-0000-4000-8000-000000000001",
                    "quote": "allows plain git push inside the allow-listed repos",
                }
            ],
        }
        base.update(over)
        return base

    def stage(self, accepted, rejected=(), deferred=(), now=T0 + 100):
        return ds.stage_run(
            self.ops, self.snap, list(accepted), list(rejected), list(deferred), now=now
        )


class ApplyTests(Base):
    def test_only_the_hunk_changes_and_the_snapshot_is_untouched(self):
        before = sha_tree(self.snapdir)
        result = self.stage([self.hunk()])
        staged = os.path.join(result.run_dir, "memory")
        after = sha_tree(staged)
        self.assertEqual(sha_tree(self.snapdir), before)
        self.assertEqual(
            {k for k in after if after[k] != before[k]}, {"feedback-push.md"}
        )
        text = th.read(os.path.join(staged, "feedback-push.md"))
        self.assertIn("outside the allow-listed repos.", text)
        self.assertIn("Second paragraph that stays.", text)
        self.assertNotIn(STALE, text)

    def test_two_hunks_in_one_note_both_land(self):
        text = th.read(os.path.join(self.snapdir, "feedback-push.md"))
        second_old = "Third paragraph also stays here."
        second = self.hunk(
            id="h2",
            old=second_old,
            new="Third paragraph, revised.",
            start=text.index(second_old),
        )
        result = self.stage([self.hunk(), second])
        staged = th.read(os.path.join(result.run_dir, "memory", "feedback-push.md"))
        self.assertIn("allow-listed repos.", staged)
        self.assertIn("Third paragraph, revised.", staged)

    def test_a_hunk_whose_old_text_is_not_at_its_position_is_a_failure(self):
        with self.assertRaises(mc.HygieneError):
            self.stage([self.hunk(start=3)])

    def test_the_run_directory_is_private_and_holds_the_expected_files(self):
        result = self.stage(
            [self.hunk()],
            rejected=[("h9", "no evidence")],
            deferred=[self.hunk(id="h8")],
        )
        names = set(os.listdir(result.run_dir))
        self.assertEqual(
            names,
            {
                "memory",
                "report.md",
                "hunks.accepted.json",
                "hunks.rejected.json",
                "hunks.deferred.json",
            },
        )
        self.assertEqual(stat.S_IMODE(os.stat(result.run_dir).st_mode), 0o700)
        for name in names - {"memory"}:
            self.assertEqual(
                stat.S_IMODE(os.stat(os.path.join(result.run_dir, name)).st_mode),
                0o600,
                name,
            )
        self.assertEqual(
            json.loads(th.read(os.path.join(result.run_dir, "hunks.rejected.json"))),
            [["h9", "no evidence"]],
        )

    def test_a_run_directory_is_write_once(self):
        self.stage([self.hunk()])
        with self.assertRaises(mc.HygieneError):
            self.stage([self.hunk()])

    def test_the_run_lives_outside_the_memory_directory(self):
        result = self.stage([self.hunk()])
        self.assertFalse(mc.is_within(self.mem, result.run_dir))


class CheckTests(Base):
    def test_a_clean_run_has_no_problems(self):
        result = self.stage([self.hunk()])
        self.assertTrue(result.ok, result.problems)
        self.assertEqual(result.problems, [])

    def test_a_hunk_that_adds_a_dangling_link_is_a_lint_problem(self):
        result = self.stage(
            [
                self.hunk(
                    new="The guard now follows [[ghost-note-that-does-not-exist]] for details."
                )
            ]
        )
        self.assertFalse(result.ok)
        self.assertTrue(any("lint" in p for p in result.problems), result.problems)

    def description_hunk(self, value):
        old = "description: a fine description"
        text = th.read(os.path.join(self.snapdir, "feedback-push.md"))
        return self.hunk(
            id="h2", old=old, start=text.index(old), new="description: " + value, reason="clearer description"
        )

    def test_a_description_hunk_that_adds_a_colon_refuses_the_whole_run(self):
        result = self.stage([self.hunk(), self.description_hunk("a fine description: now with a colon")])
        self.assertFalse(result.ok)
        self.assertEqual(result.problems, ["new lint errors: 1 (first: yaml-colon in feedback-push.md)"])

    def test_a_plain_or_quoted_description_rewrite_stages_cleanly(self):
        for offset, value in enumerate(("the push guard allows plain pushes", '"a clearer description: quoted # safely"')):
            with self.subTest(value):
                result = self.stage([self.description_hunk(value)], now=T0 + 200 + offset)
                self.assertTrue(result.ok, result.problems)

    def test_a_staged_secret_shape_is_a_scan_problem(self):
        result = self.stage([self.hunk()])
        staged = os.path.join(result.run_dir, "memory", "feedback-other.md")
        th.write(
            staged, th.read(staged) + "\nkey " + th.fake_github_token() + "\n", "w"
        )
        self.assertTrue(
            ds.new_block_hits(self.snapdir, os.path.join(result.run_dir, "memory"))
        )

    def test_the_report_names_each_hunk_its_reason_and_its_evidence(self):
        result = self.stage(
            [self.hunk()],
            rejected=[("h9", "evidence is older than the note")],
            deferred=[self.hunk(id="h8")],
        )
        report = th.read(os.path.join(result.run_dir, "report.md"))
        for expected in (
            "h1",
            "guard changed",
            "allows plain git push inside the allow-listed repos",
            "h9",
            "evidence is older than the note",
            "h8",
            self.snap,
        ):
            self.assertIn(expected, report)


class SafetyTests(TempRoot):
    def test_the_module_never_deletes_moves_or_spawns(self):
        with open(ds.__file__, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        names = {
            n.func.attr
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        self.assertEqual(
            names
            & {
                "remove",
                "unlink",
                "rmtree",
                "replace",
                "rename",
                "system",
                "popen",
                "run",
            },
            set(),
        )


class LintGateTests(TempRoot):
    """Staging refuses an edit that breaks a note's frontmatter or the index, even where lint only warns about it."""

    INDEX_TEXT = "- [A](a.md) h\n"

    def trees(self, staged_files):
        snapshot = th.make_dir({"MEMORY.md": self.INDEX_TEXT, "a.md": th.note("a")})
        return snapshot, th.make_dir(staged_files)

    def test_a_staged_edit_that_drops_the_description_is_refused(self):
        broken = "---\nname: a\nmetadata:\n  type: feedback\n---\n\nbody\n"
        snapshot, staged = self.trees({"MEMORY.md": self.INDEX_TEXT, "a.md": broken})
        self.assertIn(("missing-description", "a.md"), ds.new_lint_errors(snapshot, staged))

    def test_a_staged_edit_that_loses_the_closing_fence_is_refused(self):
        broken = "---\nname: a\ndescription: d\nmetadata:\n  type: feedback\nbody\n"
        snapshot, staged = self.trees({"MEMORY.md": self.INDEX_TEXT, "a.md": broken})
        self.assertIn(("frontmatter-unclosed", "a.md"), ds.new_lint_errors(snapshot, staged))

    def test_a_staged_index_that_is_a_symlink_is_refused(self):
        snapshot, staged = self.trees({"real.txt": self.INDEX_TEXT, "a.md": th.note("a")})
        os.symlink(os.path.join(staged, "real.txt"), os.path.join(staged, "MEMORY.md"))
        self.assertIn(("index-is-symlink", "MEMORY.md"), ds.new_lint_errors(snapshot, staged))

    def test_an_ordinary_warning_does_not_block_staging(self):
        wordy = th.note("a", typ="not-a-note-type")  # a bad-type WARN: no YAML hazard, and no hunk can cause it
        snapshot, staged = self.trees({"MEMORY.md": self.INDEX_TEXT, "a.md": wordy})
        self.assertEqual(ds.new_lint_errors(snapshot, staged), [])

    def test_each_yaml_hazard_a_staged_edit_adds_is_refused(self):
        for code, fields in YAML_HAZARD_NOTES:
            with self.subTest(code):
                snapshot, staged = self.trees({"MEMORY.md": self.INDEX_TEXT, "a.md": th.note("a", **fields)})
                self.assertEqual(ds.new_lint_errors(snapshot, staged), [(code, "a.md")])

    def test_a_description_emptied_to_empty_quotes_is_refused(self):
        for empty in ('""', "''", '" "'):
            with self.subTest(empty):
                snapshot, staged = self.trees({"MEMORY.md": self.INDEX_TEXT, "a.md": th.note("a", desc=empty)})
                self.assertEqual(ds.new_lint_errors(snapshot, staged), [("missing-description", "a.md")])

    def test_a_hazard_on_the_name_line_is_refused(self):
        snapshot, staged = self.trees({"MEMORY.md": self.INDEX_TEXT, "a.md": th.note("a: b")})
        self.assertEqual(ds.new_lint_errors(snapshot, staged), [("yaml-colon", "a.md")])

    def test_ordinary_descriptions_and_quoted_fixes_do_not_block_staging(self):
        for desc in (
            "Push guard blocks git push outside the allow-listed repos",
            "Don't force-push; the URL http://example.com/x and 10:30 are fine, 50% too",
            "no force push, no exceptions; true to the on/off switch, null results fine",
            '"a clearer description: quoted # safely"',
            "'it''s quoted: and safe'",
        ):
            with self.subTest(desc):
                snapshot, staged = self.trees({"MEMORY.md": self.INDEX_TEXT, "a.md": th.note("a", desc=desc)})
                self.assertEqual(ds.new_lint_errors(snapshot, staged), [])

    def test_a_hazard_the_snapshot_already_had_is_not_new(self):
        snapshot = th.make_dir({"MEMORY.md": self.INDEX_TEXT, "a.md": th.note("a", desc="old: colon")})
        for desc in ("reworded: still a colon", "no colon at all"):
            with self.subTest(desc):
                staged = th.make_dir({"MEMORY.md": self.INDEX_TEXT, "a.md": th.note("a", desc=desc)})
                self.assertEqual(ds.new_lint_errors(snapshot, staged), [])

    def test_a_second_hazard_of_the_same_code_in_the_same_note_is_new(self):
        snapshot = th.make_dir({"MEMORY.md": self.INDEX_TEXT, "a.md": th.note("a", desc="old: colon")})
        staged = th.make_dir({"MEMORY.md": self.INDEX_TEXT, "a.md": th.note("a: b", desc="old: colon")})
        self.assertEqual(ds.new_lint_errors(snapshot, staged), [("yaml-colon", "a.md")])

    def test_swapping_one_hazard_for_another_code_is_new(self):
        snapshot = th.make_dir({"MEMORY.md": self.INDEX_TEXT, "a.md": th.note("a", desc="old: colon")})
        staged = th.make_dir({"MEMORY.md": self.INDEX_TEXT, "a.md": th.note("a", desc="old # comment")})
        self.assertEqual(ds.new_lint_errors(snapshot, staged), [("yaml-comment", "a.md")])

    def test_every_yaml_code_the_frontmatter_check_can_emit_blocks_staging(self):
        with open(os.path.join(th.SCRIPTS, "memory_frontmatter.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        emitted = {
            n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.startswith("yaml-")
        }
        self.assertEqual(emitted, {code for code, _fields in YAML_HAZARD_NOTES})
        self.assertLessEqual(emitted, ml.STAGING_BLOCKING_CODES)

    def test_a_second_problem_of_the_same_kind_in_the_same_file_is_new(self):
        one_dangling = self.INDEX_TEXT + "- [B](missing-b.md) gone\n"
        two_dangling = one_dangling + "- [C](missing-c.md) gone too\n"
        snapshot = th.make_dir({"MEMORY.md": one_dangling, "a.md": th.note("a")})
        staged = th.make_dir({"MEMORY.md": two_dangling, "a.md": th.note("a")})
        self.assertIn(("dangling-link", "MEMORY.md"), ds.new_lint_errors(snapshot, staged))
        self.assertEqual(ds.new_lint_errors(snapshot, snapshot), [])

    def test_a_problem_that_was_already_in_the_snapshot_is_not_new(self):
        broken = "---\nname: a\nmetadata:\n  type: feedback\n---\n\nbody\n"
        snapshot = th.make_dir({"MEMORY.md": self.INDEX_TEXT, "a.md": broken})
        staged = th.make_dir({"MEMORY.md": self.INDEX_TEXT, "a.md": broken})
        self.assertEqual(ds.new_lint_errors(snapshot, staged), [])


if __name__ == "__main__":
    unittest.main()
