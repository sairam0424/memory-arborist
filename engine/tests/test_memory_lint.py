"""Tests for memory_common helpers, memory_links and memory_lint."""

import os
import stat
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memtest_helpers as th  # noqa: E402

sys.path.insert(0, th.SCRIPTS)  # python3 -I does not put the script dir on sys.path
import memory_common as mc  # noqa: E402
import memory_links as mk  # noqa: E402
import memory_lint as ml  # noqa: E402

UUID_A = "11111111-1111-4111-8111-111111111111"
UUID_B = "22222222-2222-4222-8222-222222222222"


def lint(files, **kw):
    return ml.lint_dir(th.make_dir(files), **kw)


def pairs(result):
    return [(f.severity, f.code) for f in result.findings]


def codes(result, severity=None):
    return [
        f.code for f in result.findings if severity is None or f.severity == severity
    ]


def index_with_chars(count, line_len=140):
    full, rest = divmod(
        count + 1, line_len + 1
    )  # each full line costs line_len chars plus one newline
    lines = ["x" * line_len] * full + (["y" * (rest - 1)] if rest > 1 else [])
    text = "\n".join(lines)
    return text + "z" * (count - mc.utf16_len(text))


class CommonTests(unittest.TestCase):
    def test_modules_import_under_isolated_mode(self):
        code = (
            "import sys; sys.path.insert(0, %r); import memory_common, memory_scan, memory_links, memory_lint"
            % th.SCRIPTS
        )
        proc = subprocess.run(
            [sys.executable, "-I", "-c", code], capture_output=True, text=True
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_utf16_len_counts_astral_characters_as_two_units(self):
        self.assertEqual(
            (mc.utf16_len("abc"), mc.utf16_len("\U0001f600"), mc.utf16_len("é")),
            (3, 2, 1),
        )

    def test_slug_is_the_project_directory_name(self):
        self.assertEqual(
            mc.slug_for("/x/projects/-Users-me-proj/memory"), "-Users-me-proj"
        )

    def test_walk_reports_symlinks_and_never_follows_them(self):
        root = th.make_dir({"a.md": "x", "sub/b.md": "y"})
        os.symlink(os.path.join(root, "sub"), os.path.join(root, "dirlink"))
        os.symlink(os.path.join(root, "a.md"), os.path.join(root, "filelink.md"))
        walk = mc.walk_files(root)
        self.assertEqual(walk.files, ["a.md", "sub/b.md"])
        self.assertEqual(walk.symlinks, ["dirlink", "filelink.md"])

    def test_walk_of_a_missing_directory_raises(self):
        with self.assertRaises(FileNotFoundError):
            mc.walk_files("/nonexistent/walk/dir")

    def test_unique_path_never_returns_an_existing_path(self):
        root = th.make_dir({"x.md": "1", "x.md.1": "2"})
        self.assertEqual(
            mc.unique_path(os.path.join(root, "x.md")), os.path.join(root, "x.md.2")
        )
        self.assertEqual(
            mc.unique_path(os.path.join(root, "new.md")), os.path.join(root, "new.md")
        )

    def test_log_sanitises_control_characters_and_caps_length(self):
        ops = os.path.join(tempfile.mkdtemp(), "ops")
        mc.append_log(
            ops,
            "lint",
            "bad\n## [2020-01-01] injected | x\x1b[31m" + "A" * 1000,
            ["b\nline"],
            now=0,
        )
        text = th.read(os.path.join(ops, "log.md"))
        self.assertEqual(
            len([line for line in text.split("\n") if line.startswith("## [")]), 1
        )
        self.assertNotIn("\x1b", text)
        self.assertLess(len(text), 800)

    def test_makedirs_private_tightens_an_existing_loose_directory(self):
        root = tempfile.mkdtemp()
        os.chmod(root, 0o755)
        mc.makedirs_private(root)
        self.assertEqual(stat.S_IMODE(os.stat(root).st_mode), 0o700)

    def test_is_within_resolves_symlinks(self):
        base = th.make_dir({"a/x.md": "1"})
        outside = th.make_dir({"y.md": "2"})
        os.symlink(outside, os.path.join(base, "lnk"))
        self.assertTrue(mc.is_within(base, os.path.join(base, "a", "x.md")))
        self.assertFalse(mc.is_within(base, os.path.join(base, "lnk", "y.md")))


class IndexStatsTests(unittest.TestCase):
    def test_bytes_and_lines_follow_wc_and_chars_are_utf16_units(self):
        text = "- [A](a.md) hook é\n- [B](b.md) \U0001f600\n"
        stats = ml.index_stats(text)
        self.assertEqual(
            (stats["bytes"], stats["lines"], stats["chars"]),
            (len(text.encode()), 2, mc.utf16_len(text.strip())),
        )

    def test_frontmatter_and_block_comments_are_not_counted(self):
        text = "---\nname: x\n---\n<!-- hidden -->\n- [A](a.md) hook\n"
        self.assertEqual(
            ml.index_stats(text)["chars"], mc.utf16_len("- [A](a.md) hook")
        )

    def test_an_unclosed_comment_is_left_alone_and_scanning_stays_linear(self):
        text = "<!-- " * 20000 + "\n- [A](a.md) hook\n"
        self.assertGreater(ml.index_stats(text)["chars"], 50000)

    def test_line_length_buckets_are_exclusive_upper_bounds(self):
        text = (
            "\n".join(
                ["x" * 150, "x" * 151, "x" * 200, "x" * 201, "x" * 300, "x" * 301]
            )
            + "\n"
        )
        stats = ml.index_stats(text)
        self.assertEqual(
            (stats["over150"], stats["over200"], stats["over300"]), (5, 3, 1)
        )

    def test_a_clean_run_of_the_helper_builds_exact_character_counts(self):
        for count in (19999, 20000, 22499, 22500):
            self.assertEqual(ml.index_stats(index_with_chars(count))["chars"], count)


class IndexFindingTests(unittest.TestCase):
    def index_codes(self, text):
        return pairs(lint({"MEMORY.md": text, "a.md": th.note("a")}))

    def test_character_thresholds_at_the_exact_boundaries(self):
        self.assertNotIn(
            "index-chars", [c for _s, c in self.index_codes(index_with_chars(19999))]
        )
        self.assertIn(
            ("WARN", "index-chars"), self.index_codes(index_with_chars(20000))
        )
        self.assertIn(
            ("WARN", "index-chars"), self.index_codes(index_with_chars(22499))
        )
        self.assertIn(
            ("ERROR", "index-chars"), self.index_codes(index_with_chars(22500))
        )

    def test_line_count_thresholds_at_the_exact_boundaries(self):
        def n_lines(n):
            return "\n".join(["- [x](a.md) h"] * n) + "\n"

        self.assertNotIn("index-lines", [c for _s, c in self.index_codes(n_lines(159))])
        self.assertIn(("WARN", "index-lines"), self.index_codes(n_lines(160)))
        self.assertIn(("WARN", "index-lines"), self.index_codes(n_lines(189)))
        self.assertIn(("ERROR", "index-lines"), self.index_codes(n_lines(190)))

    def test_long_line_findings_are_graded_and_numbered_from_the_original_file(self):
        text = (
            "<!-- c -->\n\n"
            + "\n".join(["x" * 150, "x" * 151, "x" * 201, "x" * 301])
            + "\n"
        )
        res = lint({"MEMORY.md": text, "a.md": th.note("a")})
        found = {
            f.code: f.line for f in res.findings if f.code.startswith("index-line")
        }
        self.assertEqual(
            found, {"index-line-150": 4, "index-line-200": 5, "index-line-300": 6}
        )
        self.assertEqual(
            [f.line for f in res.findings if f.code == "index-line-300"], [6]
        )

    def test_strict_mode_catches_index_lines_over_150(self):
        res = lint({"MEMORY.md": "x" * 160 + "\n", "a.md": th.note("a")})
        self.assertEqual((ml.exit_code(res), ml.exit_code(res, strict=True)), (0, 1))


class FrontmatterTests(unittest.TestCase):
    def test_closing_line_limit_is_inclusive_at_30(self):
        def with_close_at(n):
            return (
                "---\nname: a\ndescription: d\nmetadata:\n  type: feedback\n"
                + "pad: 1\n" * (n - 6)
                + "---\nbody\n"
            )

        self.assertNotIn(
            "frontmatter-unclosed",
            codes(lint({"MEMORY.md": "", "a.md": with_close_at(30)})),
        )
        self.assertIn(
            "frontmatter-unclosed",
            codes(lint({"MEMORY.md": "", "a.md": with_close_at(31)})),
        )

    def test_missing_unclosed_and_incomplete_frontmatter(self):
        res = lint(
            {
                "MEMORY.md": "",
                "a.md": "no frontmatter\n",
                "b.md": "---\nname: b\n" + "pad: 1\n" * 40,
                "c.md": "---\nname: dup\nmetadata:\n  type: feedback\n---\nb\n",
            }
        )
        self.assertEqual(
            (
                codes(res).count("no-frontmatter"),
                codes(res).count("frontmatter-unclosed"),
            ),
            (1, 1),
        )
        self.assertIn("missing-description", codes(res, "WARN"))

    def test_duplicate_names_and_bad_types(self):
        res = lint(
            {
                "MEMORY.md": "",
                "a.md": th.note("dup"),
                "b.md": th.note("dup", typ="weird"),
                "c.md": th.note("dup"),
            }
        )
        self.assertIn("duplicate-name", codes(res, "ERROR"))
        self.assertIn("bad-type", codes(res, "WARN"))

    def test_flow_style_metadata_and_grandchild_keys(self):
        flow = "---\nname: f\ndescription: d\nmetadata: {type: project, node_type: memory}\n---\nx\n"
        deep = "---\nname: g\ndescription: d\nmetadata:\n  other: 1\n    type: project\n---\nx\n"
        res = lint({"MEMORY.md": "", "f.md": flow, "g.md": deep})
        missing = [f.path for f in res.findings if f.code == "missing-type"]
        self.assertEqual(missing, ["g.md"])

    def test_note_content_is_never_echoed_in_messages(self):
        weird = "T" * 200
        res = lint({"MEMORY.md": "", "a.md": th.note("a", typ=weird)})
        self.assertNotIn(weird, " ".join(f.message for f in res.findings))
        self.assertIn("redacted", " ".join(f.message for f in res.findings))


class YamlHazardTests(unittest.TestCase):
    def test_plain_scalar_hazards(self):
        h = ml.description_hazards
        self.assertEqual(h("fine plain text"), [])
        self.assertIn("yaml-colon", h("has: a colon"))
        self.assertIn("yaml-comment", h("text # not a comment"))
        self.assertIn("yaml-comment", h("tab\t# comment"))
        self.assertIn("yaml-comment", h("# only a comment"))
        self.assertIn("yaml-indicator", h("[starts with bracket"))
        self.assertIn("yaml-indicator", h("-"))
        for value in ("yes", "null", "~", "2026-11-08", "42"):
            self.assertIn("yaml-nonstring", h(value), value)

    def test_quoted_scalar_hazards(self):
        h = ml.description_hazards
        self.assertEqual(h('"quoted: safe # too"'), [])
        self.assertEqual(h("'it''s fine'"), [])
        self.assertIn("yaml-unterminated", h('"open quote'))
        self.assertIn("yaml-unterminated", h('"escaped final quote\\"'))
        self.assertIn("yaml-trailing-text", h('"closed" and then more'))

    def test_a_continuation_line_marks_a_multiline_plain_scalar(self):
        text = "---\nname: m\ndescription: starts here\n  and continues here\nmetadata:\n  type: feedback\n---\nx\n"
        self.assertIn("yaml-multiline", codes(lint({"MEMORY.md": "", "m.md": text})))


class LinkTests(unittest.TestCase):
    def test_extract_links_ignores_code_and_normalises_dot_slash(self):
        text = "[[a]] `[[x]]` ``[[y]]`` ```[[z]]```\n```\n[[fenced]]\n```\n~~~\n[[tilde]]\n~~~\n[[after]] [t](./b.md) [u](sub/c.md) [w](https://x/y.md)\n"
        self.assertEqual(
            [t for t, _n in mk.extract_links(text)], ["a", "after", "b.md"]
        )

    def test_an_inline_triple_backtick_span_does_not_open_a_fence(self):
        links = mk.extract_links("```inline``` then [[kept]]\n[[also-kept]]\n")
        self.assertEqual([t for t, _n in links], ["kept", "also-kept"])

    def test_resolution_by_name_stem_and_unprefixed_alias(self):
        res = lint(
            {
                "MEMORY.md": "- [A](feedback-alpha.md) h\n- [B](./project-beta.md) h\n",
                "feedback-alpha.md": th.note(
                    "feedback-alpha",
                    body="[[project-beta]] [[beta]] [t](project-beta.md)",
                ),
                "project-beta.md": th.note("project-beta", typ="project"),
            }
        )
        self.assertEqual(
            [c for c in codes(res) if c in ("dangling-link", "ambiguous-link")], []
        )
        self.assertEqual(res.stats["loose"], 1)
        self.assertIn("alias-resolved-link", codes(res, "INFO"))
        self.assertEqual(res.stats["not_in_index"], 0)

    def test_dot_slash_index_links_count_as_in_index_and_broken_ones_dangle(self):
        res = lint(
            {"MEMORY.md": "- [A](./a.md) h\n- [B](./gone.md) h\n", "a.md": th.note("a")}
        )
        self.assertEqual((res.stats["dangling"], res.stats["not_in_index"]), (1, 0))

    def test_typed_targets_never_resolve_through_the_unprefixed_alias(self):
        res = lint(
            {
                "MEMORY.md": "",
                "feedback-x.md": th.note("feedback-x", body="[[project-x]]"),
                "project-x-extra.md": th.note("project-x-extra", typ="project"),
            }
        )
        self.assertIn("dangling-link", codes(res, "ERROR"))

    def test_dangling_and_ambiguous_links_are_errors(self):
        res = lint(
            {
                "MEMORY.md": "- [A](feedback-same.md) h\n- [B](project-same.md) h\n",
                "feedback-same.md": th.note(
                    "feedback-same", body="[[missing-x]] [[same]]"
                ),
                "project-same.md": th.note("project-same", typ="project"),
            }
        )
        self.assertTrue({"dangling-link", "ambiguous-link"} <= set(codes(res, "ERROR")))
        self.assertEqual(ml.exit_code(res), 1)

    def test_orphans_and_not_in_index(self):
        res = lint(
            {
                "MEMORY.md": "- [A](a.md) h\n",
                "a.md": th.note("a", body="[[b]] [[a]]"),
                "b.md": th.note("b"),
                "c.md": th.note("c"),
            }
        )
        self.assertEqual((res.stats["orphans"], res.stats["not_in_index"]), (1, 2))

    def test_a_note_linking_only_to_itself_is_still_an_orphan(self):
        res = lint({"MEMORY.md": "", "a.md": th.note("a", body="[[a]]")})
        self.assertEqual(res.stats["orphans"], 1)


class FileLevelTests(unittest.TestCase):
    def test_clean_directory_has_no_errors(self):
        res = lint(
            {
                "MEMORY.md": "- [A](feedback-a.md) hook\n",
                "feedback-a.md": th.note("feedback-a"),
            }
        )
        self.assertEqual((codes(res, "ERROR"), ml.exit_code(res)), ([], 0))

    def test_big_files_and_long_lines(self):
        res = lint(
            {
                "MEMORY.md": "",
                "a.md": th.note("a", body="x" * (101 * 1024)),
                "b.md": th.note("b", body="y" * 5000),
            }
        )
        self.assertIn("big-file", codes(res, "WARN"))
        self.assertEqual(
            sorted({f.path for f in res.findings if f.code == "long-line"}),
            ["a.md", "b.md"],
        )

    def test_dead_origin_session_pointers_are_counted_in_the_right_direction(self):
        project = tempfile.mkdtemp()
        open(os.path.join(project, UUID_A + ".jsonl"), "w").close()
        res = lint(
            {
                "MEMORY.md": "",
                "a.md": th.note("a", extra="  originSessionId: %s\n" % UUID_A),
                "b.md": th.note("b", extra="  originSessionId: %s\n" % UUID_B),
            },
            project_dir=project,
        )
        self.assertEqual(res.stats["dead_origin"], 1)

    def test_a_shrinking_file_count_trips_the_wire_and_a_lock_like_file_name_means_nothing(self):
        files = {"MEMORY.md": "", "a.md": th.note("a"), ".some.lock": "123"}
        res = lint(files, last_snapshot_count=5)
        self.assertEqual(codes(res).count("memory-shrink-tripwire"), 1)
        quiet = lint(files, last_snapshot_count=2)
        self.assertEqual(quiet.stats["tripwire"], 0)

    def test_bad_filenames(self):
        self.assertIsNotNone(ml.bad_filename("memory.md"))
        self.assertIsNotNone(ml.bad_filename("a\x01b.md"))
        self.assertIsNotNone(ml.bad_filename("draft.tmp.md"))
        self.assertIsNone(ml.bad_filename("feedback-ok.md"))

    def test_symlinks_and_unreadable_files_are_findings_not_silence(self):
        root = th.make_dir(
            {"MEMORY.md": "", "a.md": th.note("a"), "locked.md": th.note("locked")}
        )
        os.symlink(os.path.join(root, "a.md"), os.path.join(root, "link.md"))
        os.chmod(os.path.join(root, "locked.md"), 0)
        try:
            res = ml.lint_dir(root)
        finally:
            os.chmod(os.path.join(root, "locked.md"), stat.S_IRUSR | stat.S_IWUSR)
        self.assertIn(("WARN", "symlink"), pairs(res))
        self.assertIn(("ERROR", "unreadable-file"), pairs(res))

    def test_no_finding_is_made_about_how_many_notes_recall_can_see(self):
        files = {"MEMORY.md": ""}
        files.update({"n%03d.md" % i: th.note("n%03d" % i) for i in range(301)})
        res = lint(files)
        self.assertNotIn("recall-pool", codes(res))
        self.assertEqual(ml.exit_code(res, strict=True), 0)

    def test_strict_promotes_warnings_to_exit_one(self):
        res = lint({"MEMORY.md": "", "a.md": th.note("a", body="x" * (101 * 1024))})
        self.assertEqual((ml.exit_code(res), ml.exit_code(res, strict=True)), (0, 1))

    def test_missing_directory_raises(self):
        with self.assertRaises(FileNotFoundError):
            ml.lint_dir("/nonexistent/lint/dir")

    def test_render_prints_numbers_but_never_note_bodies(self):
        marker = "BODY-MARKER-" + "9" * 8
        out = "\n".join(
            ml.render(
                lint(
                    {
                        "MEMORY.md": "- [A](a.md) hook\n",
                        "a.md": th.note("a", body=marker),
                    }
                )
            )
        )
        self.assertRegex(out, r"INDEX bytes=\d+ chars=\d+ lines=\d+ headroom=\d+")
        self.assertNotIn(marker, out)

    def test_render_caps_each_code_unless_full(self):
        files = {
            "MEMORY.md": "",
            **{"b%02d.md" % i: "no frontmatter\n" for i in range(15)},
        }
        res = lint(files)
        self.assertIn("more no-frontmatter", "\n".join(ml.render(res)))
        self.assertNotIn("more no-frontmatter", "\n".join(ml.render(res, full=True)))


if __name__ == "__main__":
    unittest.main()
