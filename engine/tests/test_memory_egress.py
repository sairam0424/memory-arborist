"""The gate in front of every call that can send note text to the model provider (memory_egress.py)."""

import ast
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import memtest_helpers as th  # noqa: E402

sys.path.insert(0, th.SCRIPTS)
import memory_egress as eg  # noqa: E402
import memory_scan as ms  # noqa: E402

INDEX = "- [A](a.md) h\n"


class TempRoot(unittest.TestCase):
    """Every temp directory a test makes lives under one root that is removed afterwards."""

    @classmethod
    def setUpClass(cls):
        saved, root = tempfile.tempdir, tempfile.mkdtemp(prefix="memtests_")
        tempfile.tempdir = root
        cls.addClassCleanup(shutil.rmtree, root, True)
        cls.addClassCleanup(setattr, tempfile, "tempdir", saved)


def refusal(directory):
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            eg.require_clear(directory)
        except eg.SecretShapesRefused as err:
            return str(err)
    return None


class VerdictTests(TempRoot):
    def test_a_clean_directory_passes_with_an_empty_verdict(self):
        clean = th.make_dir({"MEMORY.md": INDEX, "a.md": th.note("a")})
        self.assertEqual(tuple(eg.require_clear(clean)), (0, (), 0))

    def test_every_block_pattern_refuses_with_counts_and_file_names_but_never_the_text(self):
        samples = th.pattern_samples()
        files = {"MEMORY.md": INDEX}
        files.update({"p%02d.md" % i: "note: " + text + "\n" for i, text in enumerate(samples.values())})
        total = sum(1 for text in samples.values() for hit in ms.scan_text("note: " + text + "\n") if hit.tier == "block")
        message = refusal(th.make_dir(files))
        self.assertIsNotNone(message)
        self.assertIn("%d block-tier secret shape" % total, message)
        self.assertIn("p00.md", message)
        self.assertIn("Nothing was sent", message)
        for text in samples.values():
            self.assertNotIn(text, message)

    def test_warn_tier_shapes_alone_do_not_refuse(self):
        text = "reach me at someone@example.com\n"
        hits = ms.scan_text(text)
        self.assertTrue(hits and all(hit.tier == "warn" for hit in hits), hits)
        self.assertIsNone(refusal(th.make_dir({"MEMORY.md": INDEX, "a.md": text})))

    def test_a_secret_shape_in_the_index_itself_refuses(self):
        message = refusal(th.make_dir({"MEMORY.md": "- key " + th.fake_github_token() + "\n"}))
        self.assertIn("MEMORY.md", message)

    def test_a_secret_shape_in_a_nested_or_non_markdown_file_refuses(self):
        for rel in ("sub/deep.md", "notes.txt"):
            with self.subTest(rel):
                root = th.make_dir({"MEMORY.md": INDEX, rel: "key " + th.fake_github_token() + "\n"})
                self.assertIn(os.path.basename(rel), refusal(root))

    def test_an_entry_that_could_not_be_scanned_refuses(self):
        root = th.make_dir({"MEMORY.md": INDEX, "real.txt": "fine\n"})
        os.symlink(os.path.join(root, "real.txt"), os.path.join(root, "link.md"))
        message = refusal(root)
        self.assertIn("1 entry (file or link) could not be scanned", message)

    def test_the_wording_is_right_for_one_and_for_several(self):
        root = th.make_dir({"MEMORY.md": INDEX, "real.txt": "fine\n"})
        for name in ("l1.md", "l2.md"):
            os.symlink(os.path.join(root, "real.txt"), os.path.join(root, name))
        self.assertIn("2 entries (files or links) could not be scanned", refusal(root))

    def test_the_override_sends_with_one_warning_line_and_returns_the_verdict(self):
        root = th.make_dir({"MEMORY.md": INDEX, "a.md": "key " + th.fake_github_token() + "\n"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            verdict = eg.require_clear(root, allow=True)
        self.assertEqual(verdict.block, 1)
        self.assertEqual(out.getvalue().count("WARN --allow-secret-shapes"), 1)
        self.assertNotIn(th.fake_github_token(), out.getvalue())

    def test_a_directory_that_cannot_be_listed_is_refused_even_with_the_override(self):
        gone = os.path.join(th.make_dir({}), "does-not-exist")
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(eg.SecretShapesRefused) as caught:
                eg.require_clear(gone, allow=True)
        self.assertIn("could not scan", str(caught.exception))

    def test_the_file_list_is_capped_at_ten(self):
        files = {"MEMORY.md": INDEX}
        files.update({"leak%02d.md" % i: "key " + th.fake_github_token() + "\n" for i in range(12)})
        message = refusal(th.make_dir(files))
        self.assertIn("in 12 files", message)
        self.assertIn("+2 more files", message)
        self.assertEqual(message.count(".md ("), 10)

    def test_a_secret_shaped_file_name_is_withheld_from_the_message(self):
        token = th.fake_github_token()
        message = refusal(th.make_dir({"MEMORY.md": INDEX, token + ".md": "key " + token + "\n"}))
        self.assertNotIn(token, message)
        self.assertIn("<name withheld>", message)

    def test_a_high_entropy_file_name_is_withheld_too(self):
        odd = "xK9fQ2mZ7vB3nL8pR4tY6wC1dH5jS0aE"
        message = refusal(th.make_dir({"MEMORY.md": INDEX, odd + ".md": "key " + th.fake_github_token() + "\n"}))
        self.assertNotIn(odd, message)
        self.assertIn("<name withheld>", message)

    def test_unscannable_entries_get_advice_that_fits_them(self):
        root = th.make_dir({"MEMORY.md": INDEX, "real.txt": "fine\n"})
        os.symlink(os.path.join(root, "real.txt"), os.path.join(root, "link.md"))
        message = refusal(root)
        self.assertIn("symlink", message)
        self.assertIn("permissions", message)

    def test_the_override_cannot_come_from_the_environment(self):
        with open(os.path.join(th.SCRIPTS, "memory_egress.py"), encoding="utf-8") as fh:
            source = fh.read()
        tree = ast.parse(source)
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        self.assertFalse({"environ", "getenv", "environb"} & names)
        self.assertEqual(eg.EXIT_SECRET_SHAPES, 4)
        self.assertEqual(eg.FLAG, "--allow-secret-shapes")


if __name__ == "__main__":
    unittest.main()
