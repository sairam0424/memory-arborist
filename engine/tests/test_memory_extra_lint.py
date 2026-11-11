"""Mutation-hardening tests for memory_links, memory_frontmatter and memory_lint (lane H3)."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memtest_helpers as th  # noqa: E402

sys.path.insert(0, th.SCRIPTS)  # python3 -I does not put the script dir on sys.path
import memory_common as mc  # noqa: E402
import memory_frontmatter as mf  # noqa: E402
import memory_links as mk  # noqa: E402
import memory_lint as ml  # noqa: E402

F = mc.Finding
NOT_ROOT = unittest.skipIf(os.geteuid() == 0, "root ignores permission bits")
ALLOWED_ESCAPES = "0abt\tnvfre \"/\\N_LPxuU\n"

def lint(files, **kw):
    return ml.lint_dir(th.make_dir(files), **kw)

def rows(res, *codes):
    return sorted((f.severity, f.code, f.path, f.line) for f in res.findings if f.code in codes)

def full(res, *codes):
    return [tuple(f) for f in res.findings if f.code in codes]

def fm_text(*lines, close=True):
    return "---\n" + "\n".join(lines) + ("\n---\nbody\n" if close else "\n")

def parse(*lines, **kw):
    return mf.parse_frontmatter(fm_text(*lines, **kw))

def note_rows(text):
    return [(f.severity, f.code, f.line) for f in mf.check_note("n.md", mf.parse_frontmatter(text))]

def padded(size):
    full_lines, rest = divmod(size, 100)
    return ("x" * 99 + "\n") * full_lines + "y" * rest

def index_with_chars(count, line_len=140):
    full_lines, rest = divmod(count + 1, line_len + 1)
    lines = ["x" * line_len] * full_lines + (["y" * (rest - 1)] if rest > 1 else [])
    text = "\n".join(lines)
    return text + "z" * (count - mc.utf16_len(text))

class LinkExtractionGaps(unittest.TestCase):
    def targets(self, text):
        return [t for t, _n in mk.extract_links(text)]
    def test_fence_state_machine_with_line_numbers(self):
        cases = {
            "other marker never closes": ("```\n[[a]]\n~~~\n[[b]]\n```\n[[c]]", [("c", 6)]),
            "tilde fence": ("~~~\n[[a]]\n```\n[[b]]\n~~~\n[[c]]", [("c", 6)]), "indented fence": ("   ```\n[[a]]\n   ```\n[[b]]", [("b", 4)]),
            "closer with trailing text": ("```\n[[a]]\n``` text\n[[b]]\n```\n[[c]]", [("c", 6)]),
            "equal length closes": ("```\n[[a]]\n```\n[[b]]", [("b", 4)]), "longer closes": ("```\n[[a]]\n`````\n[[b]]", [("b", 4)]),
            "shorter does not close": ("````\n[[a]]\n```\n[[b]]\n````\n[[c]]", [("c", 6)]),
            "info string": ("```python\n[[a]]\n```\n[[b]]", [("b", 4)]), "blank line inside": ("```\n\n[[a]]\n```\n[[b]]", [("b", 5)]),
            "unclosed hides the rest": ("[[a]]\n```\n[[b]]\n[[c]]", [("a", 1)]), "backtick one-liner is a span": ("```x``` [[a]]\n[[b]]", [("a", 1), ("b", 2)]),
            "tilde one-liner is not a fence": ("~~~x~~~ [[a]]\n[[b]]", [("a", 1), ("b", 2)]),
            "plain numbering": ("[[a]]\n\n[t](b.md)\n[ref]: c.md\n[[w]] [m](m.md)", [("a", 1), ("b.md", 3), ("c.md", 4), ("w", 5), ("m.md", 5)]),
        }
        for label, (text, expected) in cases.items():
            with self.subTest(label):
                self.assertEqual(mk.extract_links(text), expected)
    def test_inline_code_spans_are_lazy_and_do_not_cross_lines(self):
        cases = {
            "single": ("`[[x]]` [[a]]", ["a"]), "double": ("``[[x]]`` [[a]]", ["a"]), "triple": ("```[[x]]``` [[a]]", ["a"]),
            "double holding a backtick": ("``a`b`` [[a]]", ["a"]), "lazy triple": ("t ```a``` [[keep]] ```b```", ["keep"]),
            "lazy double": ("t ``a`` [[keep]] ``b``", ["keep"]), "lazy single": ("t `a` [[keep]] `b`", ["keep"]),
            "unclosed single": ("`open [[a]]", ["a"]), "single never crosses a line": ("`[[a]]\n[[b]]`", ["a", "b"]),
        }
        for label, (text, expected) in cases.items():
            with self.subTest(label):
                self.assertEqual(self.targets(text), expected)
    def test_link_forms_and_their_normalisation(self):
        cases = [
            ("[[a|label]]", ["a"]), ("[[a\\|label]]", ["a"]), ("[[a#sec]]", ["a"]), ("[[a#sec|l]]", ["a"]),
            ("[[ a b ]]", ["a b"]), ("[[]] [[ ]] [[|x]] [[#s]]", []), ("[[a]] [[b]]", ["a", "b"]),
            ("[t](a.md#sec)", ["a.md"]), ('[t](a.md "ti tle")', ["a.md"]), ("[t](./a.md)", ["a.md"]),
            ("[t](././a.md)", ["a.md"]), ("[t](d%20e.md)", ["d e.md"]), ("[t](sub%2Fc.md)", []),
            ("[t](c%3Anotes.md)", []), ("[t](c:notes.md)", []), ("[t](sub/c.md)", []), ("[t](a.txt)", []),
            ("[t](https://x/y.md)", []), ("[ref]: f.md", ["f.md"]), ("   [ref]: f.md", ["f.md"]),
            ("    [ref]: f.md", []), ("[ref]: f.md 'title'", ["f.md"]), ("[ref]: ./f.md", ["f.md"]),
            ("[ref]: sub/f.md", []), ("[ref]: f.mdx", []), ("(a.md)", []), ("see (a.md) now", []), ("[x]]", []),
        ]
        for text, expected in cases:
            with self.subTest(text):
                self.assertEqual(self.targets(text), expected)
    def test_length_limits_of_each_syntax(self):
        a200, a201 = "a" * 200, "a" * 201
        cases = [
            ("[[" + a200 + "]]", [a200]), ("[[" + a201 + "]]", []), ("[t](" + a200 + ".md)", [a200 + ".md"]), ("[t](" + a201 + ".md)", []),
            ("[t](a.md#" + "s" * 100 + ")", ["a.md"]), ("[t](a.md#" + "s" * 101 + ")", []), ('[t](a.md "' + "q" * 200 + '")', ["a.md"]), ('[t](a.md "' + "q" * 201 + '")', []),
            ("[" + "l" * 100 + "]: f.md", ["f.md"]), ("[" + "l" * 101 + "]: f.md", []), ("[r]: " + a200 + ".md", [a200 + ".md"]), ("[r]: " + a201 + ".md", []),
        ]
        for text, expected in cases:
            with self.subTest(text[:30] + str(len(text))):
                self.assertEqual(self.targets(text), expected)

class ResolveGaps(unittest.TestCase):
    NAMES = {
        "feedback-alpha.md": "feedback-alpha", "project-beta.md": "Beta Name",
        "user-x.md": "", "feedback-project-x.md": "",
    }
    def res(self, target, names=None, folded=True):
        exact, loose, fold = mk.alias_tiers(names or self.NAMES)
        r = mk.resolve(target, exact, loose, fold if folded else None)
        return r.status, sorted(r.files), r.via
    def test_alias_tiers_contents(self):
        names = {"feedback-a.md": "Alpha Name", "sub/project-b.md": "", "user-c.md": "feedback-a", "plain.md": "", "reference-doc.md": ""}
        exact, loose, folded = mk.alias_tiers(names)
        self.assertEqual(exact, {
            "feedback-a.md": {"feedback-a.md"}, "feedback-a": {"feedback-a.md", "user-c.md"}, "Alpha Name": {"feedback-a.md"},
            "project-b.md": {"sub/project-b.md"}, "project-b": {"sub/project-b.md"},
            "user-c.md": {"user-c.md"}, "user-c": {"user-c.md"}, "plain.md": {"plain.md"}, "plain": {"plain.md"},
            "reference-doc.md": {"reference-doc.md"}, "reference-doc": {"reference-doc.md"},
        })
        self.assertEqual(loose, {"a": {"feedback-a.md"}, "b": {"sub/project-b.md"}, "c": {"user-c.md"}, "plain": {"plain.md"}, "doc": {"reference-doc.md"}})
        self.assertEqual(folded["alpha name"], {"feedback-a.md"})
        self.assertEqual(folded["feedback-a"], {"feedback-a.md", "user-c.md"})
        self.assertEqual(set(folded), {k.casefold() for k in exact})
    def test_resolution_tiers(self):
        cases = {
            "MEMORY": ("index", [], "index"), "MEMORY.md": ("index", [], "index"),
            "feedback-alpha": ("ok", ["feedback-alpha.md"], "exact"), "feedback-alpha.md": ("ok", ["feedback-alpha.md"], "exact"),
            "Beta Name": ("ok", ["project-beta.md"], "exact"), "Beta Name.md": ("ok", ["project-beta.md"], "exact"),
            "alpha": ("ok", ["feedback-alpha.md"], "loose"), "alpha.md": ("ok", ["feedback-alpha.md"], "loose"), "x": ("ok", ["user-x.md"], "loose"),
            "project-x": ("dangling", [], None), "project-x.md": ("dangling", [], None),
            "FEEDBACK-ALPHA": ("ok", ["feedback-alpha.md"], "case"), "Feedback-Alpha.md": ("ok", ["feedback-alpha.md"], "case"),
            "beta name": ("ok", ["project-beta.md"], "case"), "BETA NAME.md": ("ok", ["project-beta.md"], "case"), "ALPHA": ("dangling", [], None), "nothing": ("dangling", [], None),
        }
        for target, expected in cases.items():
            with self.subTest(target):
                self.assertEqual(self.res(target), expected)
        self.assertEqual(self.res("FEEDBACK-ALPHA", folded=False), ("dangling", [], None))
    def test_ambiguity_and_precedence(self):
        same = {"feedback-same.md": "", "project-same.md": ""}
        self.assertEqual(self.res("same", same), ("ambiguous", ["feedback-same.md", "project-same.md"], "loose"))
        self.assertEqual(self.res("feedback-same", same), ("ok", ["feedback-same.md"], "exact"))
        fold = {"a.md": "Foo", "b.md": "foo"}
        self.assertEqual(self.res("FOO", fold), ("ambiguous", ["a.md", "b.md"], "case"))
        self.assertEqual(self.res("Foo", fold), ("ok", ["a.md"], "exact"))
        self.assertEqual(self.res("a.md", {"a.md": "", "other.md": "a"}), ("ok", ["a.md"], "exact"))
        self.assertEqual(self.res("a", {"a.md": "", "other.md": "a"}), ("ambiguous", ["a.md", "other.md"], "exact"))
        self.assertEqual(self.res("a", {"feedback-a.md": "", "a.md": ""}), ("ok", ["a.md"], "exact"))
        self.assertEqual(self.res("FOO", {"a.md": "Foo", "b.md": "FOO"}), ("ok", ["b.md"], "exact"))
        self.assertEqual(self.res("A.md", {"a.md": "", "other.md": "a"}), ("ok", ["a.md"], "case"))
    def test_check_links_findings_and_statistics(self):
        names = {"project-b.md": "project-b", "dup2.md": "dupe", "feedback-a.md": "feedback-a", "dup1.md": "dupe", "c.md": "c"}
        texts = {
            "feedback-a.md": "[[b]] [[PROJECT-B]] [[ghost]]\n[[ghost]] [[two words]] [[dupe]] [[feedback-a]]", "c.md": "[[project-b]]", "dup1.md": "", "dup2.md": "", "project-b.md": "",
        }
        report = mk.check_links(texts, "- [A](feedback-a.md) h\n- [M](MEMORY.md)\n- [Z](nowhere.md)\n", names)
        self.assertEqual([tuple(f) for f in report.findings], [
            ("ERROR", "dangling-link", "MEMORY.md", 3, "nowhere.md resolves to no note"),
            ("INFO", "alias-resolved-link", "feedback-a.md", 1, "b matched only through a looser spelling"),
            ("WARN", "case-mismatch-link", "feedback-a.md", 1, "PROJECT-B matched only through a looser spelling"),
            ("ERROR", "dangling-link", "feedback-a.md", 1, "ghost resolves to no note"), ("ERROR", "dangling-link", "feedback-a.md", 2, "ghost resolves to no note"),
            ("INFO", "non-note-link", "feedback-a.md", 2, "two words is not shaped like a note name"), ("ERROR", "ambiguous-link", "feedback-a.md", 2, "dupe matches 2 notes"),
            ("INFO", "orphan", "c.md", 0, "no note or index line links here"), ("INFO", "orphan", "dup1.md", 0, "no note or index line links here"),
            ("INFO", "orphan", "dup2.md", 0, "no note or index line links here"),
        ])
        self.assertEqual(report.stats, {
            "dangling": 3, "ambiguous": 1, "loose": 1, "case": 1, "non_note": 1, "dangling_targets": 2, "orphans": 3, "not_in_index": 4,
        })
    def test_slug_rule_separates_dangling_from_non_note_links(self):
        text = "[[.hidden]] [[-x]] [[x_y.z-w]] [[9lives]] [[a b]] [[.md]] [[q]]"
        report = mk.check_links({"s.md": text}, "", {"s.md": ""})
        got = [(f.code, f.message.split(" ")[0]) for f in report.findings if f.code != "orphan"]
        self.assertEqual(got, [
            ("non-note-link", ".hidden"), ("non-note-link", "-x"), ("dangling-link", "x_y.z-w"),
            ("dangling-link", "9lives"), ("non-note-link", "a"), ("non-note-link", ".md"), ("dangling-link", "q"),
        ])
        self.assertEqual((report.stats["dangling"], report.stats["non_note"], report.stats["dangling_targets"]), (3, 4, 3))
    def test_link_targets_in_messages_go_through_safe_snippet(self):
        secrets = [th.fake_github_token(), th.pattern_samples()["AWS Access Key"], "a" * 41]
        for target in secrets:
            with self.subTest(len(target)):
                report = mk.check_links({"s.md": "[[%s]]" % target}, "", {"s.md": ""})
                messages = [f.message for f in report.findings if f.code == "dangling-link"]
                self.assertEqual(messages, ["<redacted %d chars> resolves to no note" % len(target)])
        edge = mk.check_links({"s.md": "[[%s]]" % ("a" * 40)}, "", {"s.md": ""})
        self.assertEqual([f.message for f in edge.findings if f.code == "dangling-link"], ["a" * 40 + " resolves to no note"])

class LinkLintGaps(unittest.TestCase):
    def test_a_symlink_that_is_not_a_note_name_does_not_create_an_alias(self):
        root = th.make_dir({"MEMORY.md": "", "s.md": th.note("s", body="[[sub]]")})
        os.mkdir(os.path.join(root, "real"))
        os.symlink(os.path.join(root, "real"), os.path.join(root, "sub"))
        self.assertIn(("ERROR", "dangling-link", "s.md", 8), rows(ml.lint_dir(root), "dangling-link"))
    def test_a_symlinked_index_is_not_listed_as_an_orphan_note(self):
        root = th.make_dir({"real.txt": "- [A](a.md) h\n", "a.md": th.note("a")})
        os.symlink(os.path.join(root, "real.txt"), os.path.join(root, "MEMORY.md"))
        res = ml.lint_dir(root)
        self.assertEqual(rows(res, "orphan"), [("INFO", "orphan", "a.md", 0)])
        self.assertEqual((res.stats["not_in_index"], res.stats["symlinks"]), (1, 1))
        self.assertEqual(rows(res, "index-is-symlink", "no-index"), [("WARN", "index-is-symlink", "MEMORY.md", 0), ("WARN", "no-index", "MEMORY.md", 0)])
class FrontmatterParseGaps(unittest.TestCase):
    def test_parse_result_shape_and_line_numbers(self):
        p = mf.parse_frontmatter("---\nname: a\ndescription:  d e \nmetadata:\n  type: feedback\n  other: x\nlate: 1\n---\nafter: no\n")
        self.assertEqual((p["has"], p["closed"], p["close_line"]), (True, True, 8))
        self.assertEqual(p["fields"], {"name": "a", "description": "d e", "metadata": "", "late": "1"})
        self.assertEqual(p["meta"], {"type": "feedback", "other": "x"})
        self.assertEqual(p["line_of"], {"name": 2, "description": 3, "metadata": 4, "type": 5, "other": 6, "late": 7})
        self.assertEqual((p["multiline"], p["tab_lines"]), ((), ()))
    def test_text_without_frontmatter_gives_independent_empty_results(self):
        for text in ("", "x\n---\n", " ---\nname: a\n---\n", "--\n", "---x\nname: a\n---\n"):
            with self.subTest(text):
                p = mf.parse_frontmatter(text)
                self.assertEqual((p["has"], p["closed"], p["close_line"], p["fields"], p["meta"], p["line_of"]), (False, False, None, {}, {}, {}))
        first = mf.parse_frontmatter("x")
        for key in ("fields", "meta", "line_of"):
            first[key]["leak"] = 1
        again = mf.parse_frontmatter("x")
        self.assertEqual((again["fields"], again["meta"], again["line_of"]), ({}, {}, {}))
        self.assertTrue(mf.parse_frontmatter("---  \nname: a\n---\t\n")["closed"])
    def test_closing_line_limit_is_30_and_unclosed_notes_keep_no_close_line(self):
        for n, closed in ((3, True), (30, True), (31, False), (50, False)):
            with self.subTest(n):
                p = mf.parse_frontmatter("---\nname: a\n" + "pad: 1\n" * (n - 3) + "---\nbody\n")
                self.assertEqual((p["closed"], p["close_line"], p["fields"]["name"]), (closed, n, "a"))
        self.assertEqual(mf.parse_frontmatter("---\nname: a")["fields"], {"name": "a"})
        p = parse("name: a", close=False)
        self.assertEqual((p["has"], p["closed"], p["close_line"], p["fields"]), (True, False, None, {"name": "a"}))
        self.assertEqual(mf.parse_frontmatter("---\nname: a\n---\ntype: x\n")["fields"], {"name": "a"})
    def test_crlf_line_endings_are_understood_in_the_opening_and_closing_lines(self):
        crlf = "---\r\nname: a\r\ndescription: d\r\nmetadata:\r\n  type: feedback\r\n---\r\nbody\r\n"
        p = mf.parse_frontmatter(crlf)
        self.assertEqual((p["closed"], p["close_line"], p["fields"]["name"], p["meta"]), (True, 6, "a", {"type": "feedback"}))
        for text in ("---\r\nname: a\n---\n", "---\nname: a\n---\r\n"):
            q = mf.parse_frontmatter(text)
            self.assertEqual((q["has"], q["closed"], q["close_line"]), (True, True, 3))
        res = lint({"MEMORY.md": "", "crlf.md": crlf})
        self.assertEqual([f.code for f in res.findings if f.code not in ("orphan",)], [])
    def test_continuation_lines_tabs_and_blank_indents(self):
        p = parse("name: a", "description: d", "  more", "\tmore2", "   ", "metadata:", "  type: x")
        self.assertEqual((p["multiline"], p["tab_lines"]), (("description", "description"), (5,)))
        for lines, expected in ((("name: a", "  b", "description: d"), ("name",)), (("name: a", "description: d", "other: o", "  cont"), ()),
                                (("name: a", "description:", "  cont"), ()), (("description: d", "metadata:", "  type: x", "    deeper"), ())):
            self.assertEqual(parse(*lines)["multiline"], expected, lines)
        self.assertEqual(parse("name: a", "\tt: 1", "x: 1", "\ty: 2")["tab_lines"], (3, 5))
    def test_metadata_block_flow_and_comment_forms(self):
        cases = [
            (["metadata:", "  type: project"], {"type": "project"}), (["metadata: # note", "  type: project"], {"type": "project"}),
            (["metadata:   ", "  type: project"], {"type": "project"}), (["metadata:#c", "  type: project"], {"type": "project"}),
            (["metadata: {type: project, node_type: memory}"], {"type": "project", "node_type": "memory"}),
            (["metadata: {type: project} # c"], {"type": "project"}), (["metadata: {type: project}\t# c"], {"type": "project"}),
            (["metadata: {type: project}#c"], {}), (["metadata: {a, type: p, url: http://x}"], {"type": "p", "url": "http://x"}),
            (["metadata: {type: project}  # c"], {"type": "project"}), (["metadata: a#b", "  type: q"], {}), (["metadata: {}"], {}),
            (["metadata: {type: p", "  type: q"], {}), (["metadata: scalar", "  type: q"], {}), (["metadata: scalar # c", "  type: q"], {}),
            (["metadata:", "  a:   1  ", "    b: 2", " c: 3", "  d: 4"], {"a": "1", "d": "4"}), (["metadata:", "    a: 1", "  b: 2"], {"a": "1"}),
            (["metadata:", "  a: 1", "top: 2", "  b: 3"], {"a": "1"}), (["metadata:", "  a: 1", "metadata:", "    b: 2", "  c: 3"], {"a": "1", "b": "2"}),
        ]
        for lines, expected in cases:
            with self.subTest(lines):
                self.assertEqual(parse(*lines)["meta"], expected)
        self.assertEqual(set(parse("type-x: 1", "9x: 2", "_y: 3")["fields"]), {"type-x", "_y"})
    def test_note_type_and_name_fallbacks(self):
        cases = [
            (("type: project", "metadata:", "  type: feedback"), "feedback"), (("type: project",), "project"), (('type: "project"',), "project"), (("type: 'user'",), "user"),
            (("type: project", "metadata:", "  type: "), "project"), (("name: a",), ""),
        ]
        for lines, expected in cases:
            with self.subTest(lines):
                self.assertEqual(mf.note_type(parse(*lines)), expected)
        for line, expected in (('name: "quoted"', "quoted"), ("name: 'q'", "q"), ("name: plain", "plain"), ("type: x", "")):
            self.assertEqual(mf.note_name(parse(line)), expected)

class DescriptionHazardGaps(unittest.TestCase):
    def test_plain_scalar_hazard_table(self):
        cases = [
            ("", []), ("   ", []), ("plain text", []), ("http://x.com/a", []), ("C# is fun", []), ("a:b", []),
            ("-x", []), ("?x", []), ("--- x", []), ("]abc", []), ("}abc", []), (",abc", []), ("$abc", []),
            ("a: b", ["yaml-colon"]), ("a:\tb", ["yaml-colon"]), ("ends with:", ["yaml-colon"]), ("a #b", ["yaml-comment"]), ("a\t#b", ["yaml-comment"]), ("#only", ["yaml-comment"]),
            ("- a: b", ["yaml-colon", "yaml-indicator"]), ("# a: b", ["yaml-colon", "yaml-comment"]),
            ("-", ["yaml-indicator"]), ("- x", ["yaml-indicator"]), ("? x", ["yaml-indicator"]),
        ]
        for value, expected in cases:
            with self.subTest(value):
                self.assertEqual(mf.description_hazards(value), expected)
        for char in "[{*&!|>%@`":
            self.assertEqual(mf.description_hazards(char + "abc"), ["yaml-indicator"], char)
    def test_nonstring_variants(self):
        yes = ("yes", "No", "TRUE", "false", "Null", "on", "OFF", "~", "42", "-5", "+7", "3.14", "-0.5", "  YES  ",
               "2026-11-08", "2026-11-08 10:30", "2026-11-08T10:30:00Z", "2026-11-08T10:30:00.5Z")
        no = ("y", "n", "yes please", "1.", "1e5", ".5", "2026-1-8", "2026-11-08x", "maybe", "nullable", "truex", "12ab")
        for value in yes + no:
            self.assertEqual(mf.description_hazards(value), ["yaml-nonstring"] if value in yes else [], value)
    def test_quoted_scalar_hazard_table(self):
        cases = [
            ('"ok"', []), ("'ok'", []), ('"a" # c', []), ('"a"#c', []), ('"a" b', ["yaml-trailing-text"]),
            ('"a"b', ["yaml-trailing-text"]), ("'it''s'", []), ("'it''s' x", ["yaml-trailing-text"]),
            ("'a' 'b'", ["yaml-trailing-text"]), ('"esc \\" q"', []), ('"open', ["yaml-unterminated"]),
            ("'open", ["yaml-unterminated"]), ("'", ["yaml-unterminated"]), ('"', ["yaml-unterminated"]), ("'it''s", ["yaml-unterminated"]), ('"final quote\\"', ["yaml-unterminated"]),
            ('"bad \\q"', ["yaml-bad-escape"]), ('"\\qab"', ["yaml-bad-escape"]), ('"ab\\q"', ["yaml-bad-escape"]),
            ("'a \\q b'", []), ('"a\\q" tail', ["yaml-trailing-text", "yaml-bad-escape"]),
        ]
        for value, expected in cases:
            with self.subTest(value):
                self.assertEqual(mf.description_hazards(value), expected)
    def test_every_yaml_escape_is_allowed_and_other_letters_are_not(self):
        for char in ALLOWED_ESCAPES + "cdqswz19":
            self.assertEqual(mf.description_hazards('"x\\' + char + 'x"'), [] if char in ALLOWED_ESCAPES else ["yaml-bad-escape"], repr(char))

class CheckNoteGaps(unittest.TestCase):
    def test_exact_rows_for_missing_pieces_and_unclosed_notes(self):
        self.assertEqual(mf.check_note("n.md", mf.parse_frontmatter("plain\n")), [F("ERROR", "no-frontmatter", "n.md", 1, "file does not start with ---")])
        self.assertEqual(note_rows("---\n---\n"), [("ERROR", "missing-name", 1), ("WARN", "missing-description", 1), ("WARN", "missing-type", 1)])
        unclosed = mf.check_note("n.md", mf.parse_frontmatter(fm_text("name: a", "description: d", "metadata:", "  type: user", close=False)))
        self.assertEqual(unclosed, [F("WARN", "frontmatter-unclosed", "n.md", 1, "no closing --- within the first 30 lines (this tool's own limit)")])
        self.assertEqual(note_rows(fm_text("name:", "description:", "\tt: 1", close=False)), [
            ("WARN", "frontmatter-unclosed", 1), ("ERROR", "missing-name", 1), ("WARN", "missing-description", 1), ("WARN", "missing-type", 1), ("WARN", "yaml-tab-indent", 4),
        ])
    def test_description_missing_when_blank_or_on_the_next_line(self):
        for desc in ("description:", "description:    "):
            self.assertEqual(note_rows(fm_text("name: a", desc, "  on the next line", "metadata:", "  type: user")), [("WARN", "missing-description", 1)])
    def test_type_rows_and_their_line_numbers(self):
        for kind in mk.NOTE_TYPES:
            self.assertEqual(note_rows(fm_text("name: a", "description: d", "metadata:", "  type: " + kind)), [])
        self.assertEqual(note_rows(fm_text("name: a", "description: d", 'type: "feedback"')), [])
        nested = mf.check_note("n.md", mf.parse_frontmatter(fm_text("name: a", "description: d", "metadata:", "  type: weird")))
        self.assertEqual(nested, [F("WARN", "bad-type", "n.md", 5, "type weird is not one of user/feedback/project/reference")])
        self.assertEqual(note_rows(fm_text("name: a", "description: d", "type: weird")), [("WARN", "bad-type", 4)])
    def test_tab_hazard_and_multiline_rows(self):
        tabs = fm_text("name: a", "description: d", "metadata:", "\ttype: user", "\tx: 1")
        self.assertEqual(note_rows(tabs), [("WARN", "yaml-tab-indent", 5)])
        hazards = mf.check_note("n.md", mf.parse_frontmatter(fm_text("name: yes", "description: a: b", "metadata:", "  type: user")))
        self.assertEqual([tuple(f) for f in hazards], [
            ("WARN", "yaml-colon", "n.md", 3, "description line would misparse as YAML"), ("WARN", "yaml-nonstring", "n.md", 2, "name line would misparse as YAML"),
        ])
        multi = mf.check_note("n.md", mf.parse_frontmatter(fm_text("name: a", "  b", "description: d", "  c", "  e", "metadata:", "  type: user")))
        self.assertEqual([tuple(f) for f in multi], [
            ("WARN", "yaml-multiline", "n.md", 2, "name continues on the next line"), ("WARN", "yaml-multiline", "n.md", 4, "description continues on the next line"),
        ])
    def test_duplicate_names_are_owned_by_the_first_sorted_note(self):
        parsed = {
            "c.md": parse("name: x", "description: d"), "a.md": parse('name: "x"', "description: d"),
            "b.md": parse("description: d", "name: x"), "n1.md": parse("description: d"), "n2.md": parse("description: d"), "u.md": parse("name: X"),
        }
        self.assertEqual([tuple(f) for f in mf.check_duplicate_names(parsed)], [
            ("ERROR", "duplicate-name", "b.md", 3, "same name as a.md"), ("ERROR", "duplicate-name", "c.md", 2, "same name as a.md"),
        ])

class IndexStatsGaps(unittest.TestCase):
    def test_exact_statistics_with_frontmatter_comments_and_blank_edges(self):
        text = "---\nname: x\n---\n<!-- hidden\nstill hidden -->\n- [A](a.md) one\n\n- [B](b.md) \U0001f600 two\n\n"
        self.assertEqual(ml.index_stats(text), {
            "bytes": len(text.encode("utf-8")), "chars": 35, "lines": 9, "eff_lines": 3, "headroom": 24965, "max_len": 18,
            "numbered_lengths": [(6, 15), (7, 0), (8, 18)], "over150": 0, "over200": 0, "over300": 0,
        })
        empty = ml.index_stats("")
        self.assertEqual((empty["bytes"], empty["chars"], empty["lines"], empty["eff_lines"], empty["headroom"], empty["max_len"], empty["numbered_lengths"]), (0, 0, 0, 0, 25000, 0, []))
    def test_blank_edges_and_whitespace_only_lines(self):
        self.assertEqual(ml.index_stats("\n\n  \nabc\n\n  \n")["numbered_lengths"], [(4, 3)])
        inner = ml.index_stats("a\n  \nb")
        self.assertEqual((inner["numbered_lengths"], inner["chars"], inner["eff_lines"]), ([(1, 1), (2, 2), (3, 1)], 6, 3))
    def test_comment_stripping_edges(self):
        cases = {
            "inline": ("x<!-- c -->y\n", [(1, 2)]), "two comments": ("<!-- a -->x<!-- b -->y", [(1, 2)]),
            "unclosed": ("a <!-- b", [(1, 8)]), "four dashes close": ("<!---->x", [(1, 1)]), "dashes of the opener do not close it": ("<!--->aaa\nbbb-->\nvisible\n", [(3, 7)]),
            "multi-line keeps numbering": ("<!-- a\nb\nc -->\nz", [(4, 1)]),
        }
        for label, (text, expected) in cases.items():
            with self.subTest(label):
                self.assertEqual(ml.index_stats(text)["numbered_lengths"], expected)
    def test_frontmatter_blanking_rules(self):
        self.assertEqual(ml.index_stats("---\na: b\n---\n- [A](a.md) hook\n")["numbered_lengths"], [(4, 16)])
        self.assertEqual(ml.index_stats("---\nk: v\n")["numbered_lengths"], [(1, 3), (2, 4)])
        self.assertEqual(ml.index_stats("---")["numbered_lengths"], [(1, 3)])
        self.assertEqual(ml.index_stats("---\n---\nz")["numbered_lengths"], [(3, 1)])
        self.assertEqual(ml.index_stats("  ---\nk: v\n---\nz")["numbered_lengths"], [(1, 5), (2, 4), (3, 3), (4, 1)])
        self.assertEqual(ml.index_stats("--- \nk: v\n--- \nz")["numbered_lengths"], [(4, 1)])

class IndexFindingGaps(unittest.TestCase):
    def test_every_graded_line_is_reported_separately_with_original_numbers(self):
        lengths = (150, 151, 200, 201, 300, 301)
        text = "---\nk: v\n---\n<!-- c\nc -->\n" + "\n".join("x" * n for n in lengths) + "\n"
        res = lint({"MEMORY.md": text, "a.md": th.note("a")})
        self.assertEqual(full(res, "index-line-150", "index-line-200", "index-line-300"), [
            ("WARN", "index-line-150", "MEMORY.md", 7, "151 chars"), ("WARN", "index-line-150", "MEMORY.md", 8, "200 chars"),
            ("WARN", "index-line-200", "MEMORY.md", 9, "201 chars, candidate for trimming"), ("WARN", "index-line-200", "MEMORY.md", 10, "300 chars, candidate for trimming"),
            ("ERROR", "index-line-300", "MEMORY.md", 11, "301 chars"),
        ])
        stats = res.stats
        self.assertEqual((stats["over150"], stats["over200"], stats["over300"], stats["max_len"]), (5, 3, 1, 301))
    def test_index_size_is_judged_in_characters_not_bytes(self):
        res = lint({"MEMORY.md": ("\u00e9" * 140 + "\n") * 100, "a.md": th.note("a")})
        self.assertEqual((full(res, "index-chars"), res.stats["bytes"] > 22500, res.stats["chars"]), ([], True, 14099))
    def test_index_line_length_is_measured_in_utf16_units(self):
        res = lint({"MEMORY.md": "\U0001f600" * 76 + "\n", "a.md": th.note("a")})
        self.assertEqual(full(res, "index-line-150"), [("WARN", "index-line-150", "MEMORY.md", 1, "152 chars")])
    def test_character_thresholds_report_exact_severity_and_message(self):
        expected = {
            19999: [], 20000: [("WARN", "index-chars", "MEMORY.md", 0, "20000 is at or above 20000")], 22499: [("WARN", "index-chars", "MEMORY.md", 0, "22499 is at or above 20000")],
            22500: [("ERROR", "index-chars", "MEMORY.md", 0, "22500 is at or above 22500 (the hard cap truncates the tail silently)")],
        }
        for count, findings in expected.items():
            with self.subTest(count):
                self.assertEqual(full(lint({"MEMORY.md": index_with_chars(count), "a.md": th.note("a")}), "index-chars"), findings)
    def test_line_thresholds_use_the_effective_line_count(self):
        n_lines = lambda n: "\n".join(["- [x](a.md) h"] * n) + "\n"  # noqa: E731
        expected = {
            159: [], 160: [("WARN", "index-lines", "MEMORY.md", 0, "160 is at or above 160")], 189: [("WARN", "index-lines", "MEMORY.md", 0, "189 is at or above 160")],
            190: [("ERROR", "index-lines", "MEMORY.md", 0, "190 is at or above 190 (the hard cap truncates the tail silently)")],
        }
        for count, findings in expected.items():
            with self.subTest(count):
                self.assertEqual(full(lint({"MEMORY.md": n_lines(count), "a.md": th.note("a")}), "index-lines"), findings)
        padded_index = "---\n" + "k: v\n" * 200 + "---\n" + "\n" * 50 + n_lines(5) + "\n" * 50
        res = lint({"MEMORY.md": padded_index, "a.md": th.note("a")})
        self.assertEqual((full(res, "index-lines"), res.stats["eff_lines"], res.stats["lines"]), ([], 5, 307))
    def test_missing_and_unreadable_index(self):
        res = lint({"a.md": th.note("a")})
        self.assertEqual(full(res, "no-index"), [("WARN", "no-index", "MEMORY.md", 0, "MEMORY.md is missing")])
        self.assertEqual((res.stats["chars"], res.stats["eff_lines"], res.stats["headroom"]), (0, 0, 25000))
    @NOT_ROOT
    def test_unreadable_index_and_note_are_errors_not_a_missing_index(self):
        root = th.make_dir({"MEMORY.md": "- [A](a.md) h\n", "a.md": th.note("a"), "b.md": th.note("b")})
        for name in ("MEMORY.md", "b.md"):
            os.chmod(os.path.join(root, name), 0)
        try:
            res = ml.lint_dir(root)
        finally:
            for name in ("MEMORY.md", "b.md"):
                os.chmod(os.path.join(root, name), 0o600)
        self.assertEqual(full(res, "unreadable-file"), [("ERROR", "unreadable-file", "b.md", 0, "PermissionError"), ("ERROR", "unreadable-file", "MEMORY.md", 0, "PermissionError")])
        self.assertEqual(full(res, "no-index"), [])

class FileRuleGaps(unittest.TestCase):
    def test_bad_filename_reasons(self):
        self.assertIn("case-insensitive", ml.bad_filename("memory.md"))
        for name in ("a\x01b.md", "a\x7f.md", "a\x85.md", "a\u200b.md", "a\u202e.md", "a\ufeff.md"):
            self.assertIn("control", ml.bad_filename(name), repr(name))
        for name in ("x.tmp.md", "x.tmp", "x~", "x.swp", ".#lock.md", ".restore-abc"):
            self.assertIn("temporary", ml.bad_filename(name), name)
        for name in ("MEMORY.md", "feedback-ok.md", ".hidden", "tmp.md", "a.swpx", "notes~.md", "memory.md.bak", "my-memory.md", ".restoreabc"):
            self.assertIsNone(ml.bad_filename(name), name)
    def test_bad_names_and_non_note_files_are_reported_by_lint(self):
        files = {"MEMORY.md": "", "n.txt": "x", ".DS_Store": "x", "sub/.hid": "x", "sub/img.png": b"x", "a.swp": "x", "sub/b.tmp": "x",
                 "c~": "x", ".#d.md": "x", "e.tmp.md": "x", ".restore-1": "x", "ok.md": th.note("ok")}
        got = rows(lint(files), "bad-filename", "non-note-file")
        self.assertEqual(got, sorted(
            [("ERROR", "bad-filename", p, 0) for p in ("a.swp", "sub/b.tmp", "c~", ".#d.md", "e.tmp.md", ".restore-1")]
            + [("WARN", "non-note-file", p, 0) for p in ("n.txt", "sub/img.png")]
        ))
    @NOT_ROOT
    def test_symlinks_special_files_and_unreadable_directories_are_findings(self):
        root = th.make_dir({"MEMORY.md": "", "a.md": th.note("a"), "locked/x.md": "x"})
        os.mkdir(os.path.join(root, "real"))
        for link in ("dirlink1", "dirlink2"):
            os.symlink(os.path.join(root, "real"), os.path.join(root, link))
        os.mkfifo(os.path.join(root, "pipe.fifo"))
        os.chmod(os.path.join(root, "locked"), 0)
        try:
            res = ml.lint_dir(root)
        finally:
            os.chmod(os.path.join(root, "locked"), 0o700)
        self.assertEqual(rows(res, "symlink", "special-file", "unreadable"), [
            ("ERROR", "unreadable", "locked", 0), ("WARN", "special-file", "pipe.fifo", 0), ("WARN", "symlink", "dirlink1", 0), ("WARN", "symlink", "dirlink2", 0),
        ])
        self.assertTrue(all(f.message for f in res.findings if f.code in ("symlink", "special-file", "unreadable")))
        self.assertEqual(res.stats["symlinks"], 2)
    def test_big_file_threshold_is_100_kib_measured_in_bytes(self):
        multibyte = ("\u00e9" * 45 + "\n") * 1200  # 109200 bytes but only 55200 characters
        res = lint({"MEMORY.md": "", "ok.md": padded(102400), "big.md": padded(102401), "utf.md": multibyte})
        self.assertEqual(full(res, "big-file"), [("WARN", "big-file", "big.md", 0, "100 KB"), ("WARN", "big-file", "utf.md", 0, "106 KB")])
        self.assertEqual(res.stats["big"], 2)
    def test_long_line_threshold_is_4096_bytes_and_numbered(self):
        res = lint({
            "MEMORY.md": "", "l1.md": "ok\n" + "x" * 4096, "l2.md": "ok\n" + "x" * 4097, "l3.md": "a\nb\n" + "\u00e9" * 2049 + "\nc", "l4.md": "short\n",
        })
        self.assertEqual(full(res, "long-line"), [
            ("WARN", "long-line", "l2.md", 2, "4097 bytes in one line"), ("WARN", "long-line", "l3.md", 3, "4098 bytes in one line"),
        ])
        self.assertEqual(res.stats["long_line"], 2)
    def test_files_over_4kb_statistic(self):
        res = lint({"MEMORY.md": "", "s1.md": padded(4096), "s2.md": padded(4097), "s3.md": padded(5000), "s4.md": "tiny"})
        self.assertEqual((res.stats.get("over4096", 0), res.stats["notes"]), (2, 4))

class OriginTripwireRecallGaps(unittest.TestCase):
    def origin_note(self, name, origin, top=False):
        if top:
            return "---\nname: %s\ndescription: d\noriginSessionId: %s\nmetadata:\n  type: user\n---\nx\n" % (name, origin)
        return th.note(name, extra="  originSessionId: %s\n" % origin)
    def test_origin_ids_must_be_plain_ids_whose_transcript_exists(self):
        parent = tempfile.mkdtemp()
        project = os.path.join(parent, "proj")
        os.mkdir(project)
        for stem in ("abcdefgh", "abcdefg", "a" * 64, "a" * 65, "outside"):
            th.write(os.path.join(project if stem != "outside" else parent, stem + ".jsonl"), "")
        files = {
            "MEMORY.md": "", "alive8.md": self.origin_note("alive8", "abcdefgh"), "dead7.md": self.origin_note("dead7", "abcdefg"),
            "alive64.md": self.origin_note("alive64", "a" * 64), "dead65.md": self.origin_note("dead65", "a" * 65),
            "trav.md": self.origin_note("trav", "../outside"), "topdead.md": self.origin_note("topdead", "'zzzzzzzz'", top=True),
            "topalive.md": self.origin_note("topalive", "'abcdefgh'", top=True), "none.md": th.note("none"),
            "quoted.md": self.origin_note("quoted", '"abcdefgh"'), "nofile.md": self.origin_note("nofile", "nofile-1234"),
        }
        res = lint(files, project_dir=project)
        self.assertEqual(res.stats["dead_origin"], 5)
        self.assertEqual(full(res, "dead-origin"), [("INFO", "dead-origin", "", 0, "5 notes cite a transcript that no longer exists (unverifiable by transcript)")])
    def test_no_project_dir_or_no_dead_pointer_means_no_finding(self):
        files = {"MEMORY.md": "", "a.md": self.origin_note("a", "abcdefgh")}
        self.assertEqual(lint(files).stats["dead_origin"], 0)
        project = tempfile.mkdtemp()
        th.write(os.path.join(project, "abcdefgh.jsonl"), "")
        res = lint(files, project_dir=project)
        self.assertEqual((res.stats["dead_origin"], full(res, "dead-origin")), (0, []))
    def test_the_shrink_tripwire_fires_only_when_the_markdown_count_drops_and_no_file_name_is_special(self):
        base = {"MEMORY.md": "", "a.md": th.note("a"), "b.md": th.note("b"), "c.txt": "x"}
        self.assertEqual(full(lint(base, last_snapshot_count=3), "memory-shrink-tripwire"), [])
        self.assertEqual(lint(base).stats["tripwire"], 0)
        shrink = lint(base, last_snapshot_count=4)
        self.assertEqual(full(shrink, "memory-shrink-tripwire"), [("WARN", "memory-shrink-tripwire", "", 0, "3 markdown files now vs 4 in the last snapshot")])
        locked = lint({**base, ".some.lock": "1"}, last_snapshot_count=3)
        self.assertEqual([f for f in locked.findings if f.code == "memory-shrink-tripwire"], [])
        self.assertEqual((shrink.stats["tripwire"], locked.stats["tripwire"]), (1, 0))
    def test_no_recall_pool_finding_exists_at_any_size(self):
        for count in (180, 181, 400):
            files = {"MEMORY.md": ""}
            files.update({"n%03d.md" % i: th.note("n%03d" % i) for i in range(count)})
            self.assertEqual(full(lint(files), "recall-pool"), [], count)

class StatsWiringGaps(unittest.TestCase):
    def test_lint_dir_statistics_are_wired_to_the_right_findings(self):
        root = th.make_dir({
            "MEMORY.md": "- [A](a.md) h\n", "a.md": th.note("a", desc="a: b # c"),
            "b.md": "---\nname: b\ndescription: d\nmetadata:\n\ttype: user\n---\nx\n", "c.md": "no fm\n", "d.md": "no fm\n",
            "e.md": th.note("e", extra="  originSessionId: nofile-1234\n"), ".some.lock": "1", "notes.txt": "x",
        })
        os.mkdir(os.path.join(root, "sub"))
        for link in ("dirlink1", "dirlink2"):
            os.symlink(os.path.join(root, "sub"), os.path.join(root, link))
        res = ml.lint_dir(root, project_dir=tempfile.mkdtemp())
        s = res.stats
        self.assertEqual((s["notes"], s["yamlfail"], s["nofm"], s["symlinks"], s["tripwire"], s["dead_origin"]), (5, 2, 2, 2, 0, 1))
        self.assertEqual((s["lines"], s["eff_lines"], s["chars"], s["bytes"]), (1, 1, 13, 14))
        self.assertEqual((s["orphans"], s["not_in_index"], s["dangling"], s["loose"], s["ambiguous"]), (4, 4, 0, 0, 0))
        self.assertEqual(rows(res, "no-frontmatter"), [("ERROR", "no-frontmatter", "c.md", 1), ("ERROR", "no-frontmatter", "d.md", 1)])
        self.assertEqual(sorted(f.code for f in res.findings if f.code.startswith("yaml-")), ["yaml-colon", "yaml-comment", "yaml-tab-indent"])
    def test_symlinked_notes_are_not_counted_as_notes_and_leading_blanks_break_frontmatter(self):
        root = th.make_dir({"MEMORY.md": "", "a.md": th.note("a"), "ws.md": "\n" + th.note("ws")})
        os.symlink(os.path.join(root, "a.md"), os.path.join(root, "a-link.md"))
        res = ml.lint_dir(root)
        self.assertEqual(res.stats["notes"], 2)
        self.assertEqual(rows(res, "no-frontmatter"), [("ERROR", "no-frontmatter", "ws.md", 1)])
        self.assertEqual([f.path for f in res.findings if f.code == "orphan"], ["a-link.md", "a.md", "ws.md"])

class RenderAndExitGaps(unittest.TestCase):
    MANDATORY = dict(bytes=1, chars=2, lines=3, headroom=4, over150=5, over200=6, over300=7, max_len=8)
    def test_header_lines_map_every_statistic(self):
        stats = dict(self.MANDATORY, notes=11, dangling=12, dangling_targets=13, loose=14, ambiguous=15, orphans=16, not_in_index=17,
                     big=18, long_line=19, over4096=20, yamlfail=21, nofm=22, dead_origin=23, symlinks=24, tripwire=25)
        out = ml.render(ml.LintResult((), stats))
        self.assertEqual(out, [
            "INDEX bytes=1 chars=2 lines=3 headroom=4 over150=5 over200=6 over300=7 max=8",
            "NOTES n=11 DANGLING=12 targets=13 LOOSE=14 AMBIGUOUS=15 ORPHANS=16 NOT_IN_INDEX=17 BIG=18 LONG_LINE=19 "
            "FILES_OVER_4KB=20 YAMLFAIL=21 NO_FRONTMATTER=22 DEAD_ORIGIN=23 SYMLINKS=24 TRIPWIRE=25", "LINT errors=0 warn=0 info=0",
        ])
        self.assertIn("n=0 DANGLING=0 targets=0 LOOSE=0", ml.render(ml.LintResult((), self.MANDATORY))[1])
    def test_groups_are_ordered_by_severity_then_code_then_location(self):
        findings = (
            F("INFO", "b-code", "z.md", 1, "m"), F("ERROR", "z-code", "b.md", 2, "m1"), F("ERROR", "a-code", "a.md", 3, "m2"),
            F("WARN", "c-code", "", 0, "m3"), F("ERROR", "a-code", "a.md", 1, "m4\x1b"), F("INFO", "a-code", "a.md", 1, "m5"), F("ERROR", "z-code", "a.md", 5, "m6"),
        )
        out = ml.render(ml.LintResult(findings, self.MANDATORY))
        self.assertEqual(out[2:], [
            "LINT errors=4 warn=1 info=2", "ERROR a-code a.md:1 m4?", "ERROR a-code a.md:3 m2", "ERROR z-code a.md:5 m6", "ERROR z-code b.md:2 m1",
            "WARN c-code -:0 m3", "INFO a-code a.md:1 m5", "INFO b-code z.md:1 m",
        ])
    def test_each_code_is_capped_separately_unless_full(self):
        many = tuple(F("WARN", "x-code", "p%02d.md" % i, 1, "m") for i in reversed(range(12)))
        exact = tuple(F("WARN", "y-code", "q%02d.md" % i, 1, "m") for i in range(10))
        result = ml.LintResult(many + exact, self.MANDATORY)
        out = ml.render(result)[3:]
        self.assertEqual([l for l in out if l.startswith("WARN x-code")], ["WARN x-code p%02d.md:1 m" % i for i in range(10)])
        self.assertEqual(len([l for l in out if l.startswith("WARN y-code")]), 10)
        self.assertEqual([l for l in out if "more" in l], ["  ... +2 more x-code (use --full)"])
        self.assertEqual(ml.PER_CODE_SHOWN, 10)
        everything = ml.render(result, full=True)[3:]
        self.assertEqual((len(everything), any("more" in l for l in everything)), (22, False))
        self.assertEqual([l for l in ml.render(result, per_code=3) if "more" in l], ["  ... +9 more x-code (use --full)", "  ... +7 more y-code (use --full)"])
    def test_exit_code_for_every_severity_mix(self):
        mixes = {
            (): (0, 0), ("INFO",): (0, 0), ("WARN",): (0, 1), ("ERROR",): (1, 1), ("ERROR", "WARN"): (1, 1),
            ("INFO", "WARN"): (0, 1), ("INFO", "ERROR"): (1, 1), ("INFO", "WARN", "ERROR"): (1, 1),
        }
        for severities, (plain, strict) in mixes.items():
            res = ml.LintResult(tuple(F(s, "c", "p", 0, "m") for s in severities), {})
            self.assertEqual((ml.exit_code(res), ml.exit_code(res, strict=True)), (plain, strict), severities)

if __name__ == "__main__":
    unittest.main()
