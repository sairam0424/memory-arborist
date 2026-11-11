"""Regression tests for the confirmed round-2 review findings (A1..A5)."""

import contextlib
import io
import json
import os
import re
import stat
import sys
import tempfile
import time
import unicodedata
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memtest_helpers as th  # noqa: E402

sys.path.insert(0, th.SCRIPTS)  # python3 -I does not put the script dir on sys.path
import memory_common as mc  # noqa: E402
import memory_frontmatter as mf  # noqa: E402
import memory_hygiene as mh  # noqa: E402
import memory_links as mk  # noqa: E402
import memory_lint as ml  # noqa: E402
import memory_scan as ms  # noqa: E402
import memory_snapshot as sn  # noqa: E402

T0 = 1_700_000_000


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = mh.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def lint(files, **kw):
    return ml.lint_dir(th.make_dir(files), **kw)


def codes(result):
    return [f.code for f in result.findings]


class Base(unittest.TestCase):
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

    def snap(self, now=T0):
        return sn.take_snapshot(self.mem, self.ops, now=now)

    def archived_texts(self):
        found = []
        for base, _dirs, names in os.walk(os.path.join(self.ops, "archive")):
            found += [th.read(os.path.join(base, n)) for n in names]
        return found


class RestoreRaceTests(Base):
    def test_a_file_that_appears_while_restoring_a_missing_note_is_archived_not_clobbered(
        self,
    ):
        snap_id = self.snap()["id"]
        os.rename(self.live, os.path.join(self.root, "parked.md"))
        real_stage = sn._stage

        def stage_then_live_write(*args, **kw):
            tmp = real_stage(*args, **kw)
            th.write(self.live, "FRESH LIVE SESSION WRITE\n")
            return tmp

        with mock.patch.object(sn, "_stage", stage_then_live_write):
            code, _ = sn.restore_file(
                self.mem, self.ops, snap_id, "feedback-a.md", apply=True, now=T0 + 5
            )
        self.assertEqual(code, 0)
        self.assertTrue(
            any("FRESH LIVE SESSION WRITE" in t for t in self.archived_texts())
        )
        self.assertNotIn("FRESH LIVE SESSION WRITE", th.read(self.live))

    def test_case_and_unicode_spellings_that_the_filesystem_accepts_are_refused(self):
        th.write(
            os.path.join(self.mem, "Case-Note.md"),
            "case note content line is long enough\n",
        )
        nfc = unicodedata.normalize("NFC", "café-note.md")
        th.write(
            os.path.join(self.mem, nfc), "unicode note content line is long enough\n"
        )
        snap_id = self.snap()["id"]
        for wrong in ("case-note.md", unicodedata.normalize("NFD", "café-note.md")):
            with self.subTest(wrong):
                code, msgs = sn.restore_file(
                    self.mem,
                    self.ops,
                    snap_id,
                    wrong,
                    apply=True,
                    force=True,
                    now=T0 + 5,
                )
                self.assertEqual(code, 1, msgs)
        self.assertTrue(os.path.exists(os.path.join(self.mem, "Case-Note.md")))
        self.assertTrue(os.path.exists(os.path.join(self.mem, nfc)))

    def test_the_same_directory_reached_through_a_symlinked_path_is_accepted(self):
        alias = os.path.join(tempfile.mkdtemp(), "alias")
        os.symlink(os.path.dirname(self.mem), alias)
        via_alias = os.path.join(alias, os.path.basename(self.mem))
        first = self.snap()
        self.assertEqual(
            sn.take_snapshot(via_alias, self.ops, now=T0 + 5)["id"], first["id"]
        )
        th.write(self.live, "changed\n")
        self.assertEqual(
            sn.restore_file(
                via_alias,
                self.ops,
                first["id"],
                "feedback-a.md",
                apply=True,
                force=True,
                now=T0 + 9,
            )[0],
            0,
        )

    def test_the_restore_staging_name_is_exclusive_and_does_not_follow_planted_symlinks(
        self,
    ):
        snap_id = self.snap()["id"]
        th.write(self.live, "live edit\n")
        victim = th.make_dir({"v.md": "victim\n"})
        os.symlink(
            os.path.join(victim, "v.md"),
            os.path.join(self.mem, ".restore-%d.part" % os.getpid()),
        )
        code, _ = sn.restore_file(
            self.mem,
            self.ops,
            snap_id,
            "feedback-a.md",
            apply=True,
            force=True,
            now=T0 + 5,
        )
        self.assertEqual(code, 0)
        self.assertEqual(th.read(os.path.join(victim, "v.md")), "victim\n")

    def test_the_symlink_check_is_repeated_right_before_the_swap(self):
        os.makedirs(os.path.join(self.mem, "sub"))
        th.write(
            os.path.join(self.mem, "sub", "n.md"),
            "nested note content line is long enough\n",
        )
        snap_id = self.snap()["id"]
        th.write(os.path.join(self.mem, "sub", "n.md"), "live edit differs from the snapshot\n")
        outside = th.make_dir({"n.md": "outside file\n"})
        real_stage = sn._stage

        def stage_then_swap_dir(*args, **kw):
            tmp = real_stage(*args, **kw)
            os.rename(
                os.path.join(self.mem, "sub"), os.path.join(self.root, "sub-moved")
            )
            os.symlink(outside, os.path.join(self.mem, "sub"))
            return tmp

        th.write(os.path.join(self.root, "marker"), "x")
        with mock.patch.object(sn, "_stage", stage_then_swap_dir):
            with self.assertRaises(sn.HygieneError):
                sn.restore_file(
                    self.mem,
                    self.ops,
                    snap_id,
                    "sub/n.md",
                    apply=True,
                    force=True,
                    now=T0 + 5,
                )
        self.assertEqual(th.read(os.path.join(outside, "n.md")), "outside file\n")


class SnapshotRegistryTests(Base):
    def test_noop_recomputes_the_tree_hash_instead_of_trusting_the_stored_one(self):
        first = self.snap()
        path = os.path.join(self.ops, "snapshots", first["id"], "manifest.json")
        manifest = json.loads(th.read(path))
        del manifest["files"]["MEMORY.md"]
        os.chmod(path, 0o600)
        th.write(path, json.dumps(manifest))
        self.assertEqual(self.snap(now=T0 + 5)["status"], "ok")

    def test_latest_follows_creation_order_when_the_clock_steps_back(self):
        first = self.snap(now=T0 + 1000)
        th.write(self.live, "second\n", "a")
        second = self.snap(now=T0)
        self.assertEqual(sn.list_snapshots(self.ops), [first["id"], second["id"]])
        self.assertEqual(sn.resolve_snapshot(self.ops, "latest"), second["id"])

    def test_malformed_manifest_entries_make_a_snapshot_broken_not_a_traceback(self):
        first = self.snap()
        path = os.path.join(self.ops, "snapshots", first["id"], "manifest.json")
        manifest = json.loads(th.read(path))
        manifest["files"]["feedback-a.md"] = None
        os.chmod(path, 0o600)
        th.write(path, json.dumps(manifest))
        self.assertEqual(sn.list_snapshots(self.ops), [])
        self.assertEqual(sn.broken_snapshots(self.ops), [first["id"]])

    def test_prune_never_proposes_removing_the_only_intact_snapshot(self):
        older = self.snap(now=T0)
        th.write(self.live, "second\n", "a")
        newer = self.snap(now=T0 + 10)
        os.chmod(
            os.path.join(self.ops, "snapshots", newer["id"], "memory", "feedback-a.md"),
            0o600,
        )
        th.write(
            os.path.join(self.ops, "snapshots", newer["id"], "memory", "feedback-a.md"),
            "damaged\n",
        )
        plan = "\n".join(sn.prune_plan(self.ops, keep=1))
        self.assertNotIn(older["id"], plan)
        self.assertIn(newer["id"], plan)

    def test_prune_comment_lines_cannot_forge_commands(self):
        os.makedirs(os.path.join(self.ops, "snapshots", "bad\nrm -rf /tmp/x"))
        physical = "\n".join(sn.prune_plan(self.ops, keep=5)).splitlines()
        self.assertTrue(physical and all(line.startswith("#") for line in physical), physical)

    def test_special_files_are_reported(self):
        os.mkfifo(os.path.join(self.mem, "pipe.md"))
        self.assertEqual(self.snap()["skipped"], ["pipe.md"])
        self.assertIn(
            ("WARN", "special-file"),
            [(f.severity, f.code) for f in ml.lint_dir(self.mem).findings],
        )
        self.assertIn("pipe.md", [p for p, _r in ms.scan_tree(self.mem).skipped])

    def test_compare_refuses_to_compare_a_directory_with_itself(self):
        with self.assertRaises(sn.HygieneError):
            sn.compare_dirs(self.mem, self.mem)


class CliTests(Base):
    def cli(self, *argv):
        return run_cli("--dir", self.mem, "--ops-root", self.root, *argv)

    def test_a_failing_log_write_never_masks_the_scan_exit_code(self):
        th.write(self.live, "leak " + th.fake_github_token() + "\n", "a")
        with mock.patch.object(mc, "append_log", side_effect=OSError("disk full")):
            code, _out, err = self.cli("scan")
        self.assertEqual(code, 2)
        self.assertIn("could not write the log", err)

    def test_empty_paths_are_rejected_not_replaced_by_the_live_defaults(self):
        self.assertEqual(run_cli("--dir", "", "lint")[0], 1)
        self.assertEqual(run_cli("--dir", self.mem, "--ops-root", "", "snapshot")[0], 1)

    def test_an_explicit_dir_equal_to_the_default_is_treated_as_the_default(self):
        with (
            mock.patch.object(mc, "DEFAULT_MEMORY_DIR", self.mem),
            mock.patch.object(mc, "DEFAULT_OPS_ROOT", self.root),
        ):
            self.assertEqual(run_cli("--dir", self.mem, "snapshot")[0], 0)

    def test_ops_inside_the_memory_dir_is_refused_by_every_stateful_command(self):
        inside = os.path.join(self.mem, "ops")
        for argv in (
            ["snapshot"],
            ["log", "decision", "x"],
            ["snapshots"],
            ["prune-plan"],
        ):
            with self.subTest(argv):
                self.assertEqual(
                    run_cli("--dir", self.mem, "--ops-root", inside, *argv)[0], 1
                )
        self.assertFalse(os.path.exists(os.path.join(inside, "log.md")))

    def test_file_names_are_escaped_before_printing(self):
        th.write(os.path.join(self.mem, "a\x1b[31mred.md"), "no frontmatter\n")
        out = (
            self.cli("lint", "--no-log", "--full")[1] + self.cli("scan", "--no-log")[1]
        )
        self.assertNotIn("\x1b", out)

    def test_comparing_two_directories_does_not_log_but_a_snapshot_baseline_does(self):
        self.snap()
        staging = th.make_dir({"feedback-a.md": th.note("feedback-a")})
        self.cli("compare", self.mem, staging)
        self.assertFalse(
            os.path.exists(os.path.join(self.ops, "log.md"))
            and "compare |" in th.read(os.path.join(self.ops, "log.md"))
        )
        self.cli("compare", "latest", staging)
        self.assertIn("compare |", th.read(os.path.join(self.ops, "log.md")))

    def test_a_snapshot_id_wins_over_a_directory_with_the_same_name(self):
        self.snap()
        cwd, here = tempfile.mkdtemp(), os.getcwd()
        os.makedirs(os.path.join(cwd, "latest"))
        th.write(
            os.path.join(cwd, "latest", "other.md"),
            "completely different directory content here\n",
        )
        os.chdir(cwd)
        try:
            code, out, _ = self.cli("compare", "latest", self.mem, "--no-log")
        finally:
            os.chdir(here)
        self.assertEqual(code, 0, out)

    def test_compare_refuses_the_live_directory_as_baseline(self):
        self.assertEqual(self.cli("compare", self.mem, self.mem, "--no-log")[0], 1)

    def test_tilde_is_expanded(self):
        self.assertEqual(mc.expand("~/x"), os.path.join(os.path.expanduser("~"), "x"))

    def test_a_snapshot_whose_notes_hold_secret_shapes_says_so(self):
        th.write(self.live, "leak " + th.fake_github_token() + "\n", "a")
        code, out, _ = self.cli("snapshot")
        self.assertEqual(code, 0)
        self.assertIn("block-tier secret shapes", out)


class CommonTests(unittest.TestCase):
    def test_log_refuses_a_symlinked_log_and_strips_invisible_control_characters(self):
        ops = os.path.join(tempfile.mkdtemp(), "ops")
        mc.append_log(ops, "lint", "first", now=0)
        outside = th.make_dir({"target.md": "target\n"})
        os.chmod(os.path.join(outside, "target.md"), 0o644)
        os.rename(os.path.join(ops, "log.md"), os.path.join(outside, "moved.md"))
        os.symlink(os.path.join(outside, "target.md"), os.path.join(ops, "log.md"))
        with self.assertRaises(OSError):
            mc.append_log(ops, "lint", "second", now=0)
        self.assertEqual(
            stat.S_IMODE(os.stat(os.path.join(outside, "target.md")).st_mode), 0o644
        )
        os.unlink(os.path.join(ops, "log.md")) if False else None
        clean = mc.clean_line("a\u202eb\u200bc\x85d", 50)
        self.assertEqual(clean, "a b c d")

    def test_safe_snippet_redacts_short_mixed_class_runs(self):
        self.assertIn("redacted", ms.safe_snippet("Zq8Xv2Lm9Pk4Rt7Y"))
        self.assertEqual(ms.safe_snippet("feedback-alpha"), "feedback-alpha")


class LintTruthTests(unittest.TestCase):
    def test_links_in_index_comments_and_frontmatter_are_not_index_entries(self):
        res = lint(
            {
                "MEMORY.md": "<!-- [x](a.md) -->\n- [B](b.md) h\n",
                "a.md": th.note("a"),
                "b.md": th.note("b"),
            }
        )
        self.assertEqual(res.stats["not_in_index"], 1)

    def test_comment_text_does_not_count_toward_line_length_or_characters(self):
        text = "<!-- " + "c" * 400 + " --> - [A](a.md) hook\n"
        res = lint({"MEMORY.md": text, "a.md": th.note("a")})
        self.assertNotIn("index-line-300", codes(res))
        self.assertLess(res.stats["chars"], 60)

    def test_a_symlinked_index_is_a_warning_not_a_silent_empty_index(self):
        root = th.make_dir({"real-index.txt": "- [A](a.md) h\n", "a.md": th.note("a")})
        os.symlink(
            os.path.join(root, "real-index.txt"), os.path.join(root, "MEMORY.md")
        )
        self.assertIn(
            ("WARN", "index-is-symlink"),
            [(f.severity, f.code) for f in ml.lint_dir(root).findings],
        )
        message = next(
            f.message for f in ml.lint_dir(root).findings if f.code == "index-is-symlink"
        )
        self.assertIn("does not follow", message)

    def test_tab_indented_metadata_is_flagged(self):
        text = "---\nname: t\ndescription: d\nmetadata:\n\ttype: feedback\n---\nx\n"
        self.assertIn("yaml-tab-indent", codes(lint({"MEMORY.md": "", "t.md": text})))

    def test_a_metadata_line_with_a_trailing_comment_keeps_its_nested_keys(self):
        text = "---\nname: t\ndescription: d\nmetadata: # a comment\n  type: feedback\n---\nx\n"
        self.assertNotIn("missing-type", codes(lint({"MEMORY.md": "", "t.md": text})))

    def test_frontmatter_comment_stripping_is_linear(self):
        text = (
            "---\nname: t\ndescription: a"
            + " " * 200000
            + "\nmetadata:\n  type: feedback\n---\nx\n"
        )
        started = time.monotonic()
        lint({"MEMORY.md": "", "t.md": text})
        self.assertLess(time.monotonic() - started, 3)

    def test_yaml_nonstring_is_an_exact_match_and_new_hazards_exist(self):
        h = mf.description_hazards
        self.assertEqual(h("2026-11-08 plan for the week"), [])
        self.assertEqual(h("y"), [])
        self.assertIn("yaml-nonstring", h("2026-11-08"))
        self.assertIn("yaml-colon", h("a:\tb"))
        self.assertIn("yaml-bad-escape", h('"bad \\q escape"'))

    def test_placeholder_link_text_is_informational_not_an_error(self):
        res = lint(
            {
                "MEMORY.md": "",
                "a.md": th.note(
                    "a",
                    body="[[card:…]] [[deep research findings]] [[real-missing-note]]",
                ),
            }
        )
        self.assertEqual(
            sorted(
                f.code
                for f in res.findings
                if f.code in ("non-note-link", "dangling-link")
            ),
            ["dangling-link", "non-note-link", "non-note-link"],
        )
        self.assertEqual(res.stats["dangling"], 1)

    def test_links_to_symlinked_or_unreadable_notes_do_not_dangle(self):
        root = th.make_dir(
            {
                "MEMORY.md": "",
                "src.md": th.note("src", body="[[linked-note]] [[locked-note]]"),
                "target.txt": "x",
                "locked-note.md": th.note("locked-note"),
            }
        )
        os.symlink(
            os.path.join(root, "target.txt"), os.path.join(root, "linked-note.md")
        )
        os.chmod(os.path.join(root, "locked-note.md"), 0)
        try:
            res = ml.lint_dir(root)
        finally:
            os.chmod(os.path.join(root, "locked-note.md"), 0o600)
        self.assertNotIn("dangling-link", codes(res))


class LinkSyntaxTests(unittest.TestCase):
    def targets(self, text):
        return [t for t, _n in mk.extract_links(text)]

    def test_longer_fences_close_only_with_a_matching_or_longer_fence(self):
        text = "````\n[[ghost]]\n```\n[[still-code]]\n````\n[[kept]]\n"
        self.assertEqual(self.targets(text), ["kept"])

    def test_more_valid_link_syntaxes_are_seen(self):
        text = '[t](a.md "title") [[b\\|label]] [c](d%20e.md)\n[ref]: f.md\n'
        self.assertEqual(sorted(self.targets(text)), ["a.md", "b", "d e.md", "f.md"])

    def test_links_to_the_index_are_valid_and_case_variants_warn_instead_of_dangling(
        self,
    ):
        res = lint(
            {
                "MEMORY.md": "- [A](feedback-a.md) h\n",
                "feedback-a.md": th.note(
                    "feedback-a", body="[[MEMORY]] [x](MEMORY.md) [[Feedback-A]]"
                ),
            }
        )
        self.assertNotIn("dangling-link", codes(res))
        self.assertIn(
            ("WARN", "case-mismatch-link"), [(f.severity, f.code) for f in res.findings]
        )


if __name__ == "__main__":
    unittest.main()
