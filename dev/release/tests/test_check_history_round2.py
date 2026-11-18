"""Found by a second, fresh read-only reviewer of check_history.py: places a clone still carries that the scan skipped or compared too
literally (late header lines named like structural ones, look-alike Unicode, other encodings, an unborn HEAD, an empty directory) and
an --allow-identity that passed free text. Each case builds a real repository and expects a hit (exit 1) or the stated pass (exit 0)."""

import os
import subprocess
import sys
import tempfile
import unicodedata
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from test_check_history import GIT_ENV, OWNER_MAIL, PRIVATE_WORD, Repo, write_words  # noqa: E402
from test_check_history_review_fixes import check  # noqa: E402


def git_bytes(repo, *args, data=None, env=None):
    merged = dict(os.environ, **GIT_ENV)
    merged.update(env or {})
    return subprocess.run(
        ["git", "-C", repo.path, *args],
        input=data,
        capture_output=True,
        check=True,
        env=merged,
    ).stdout


def git(repo, *args, stdin=None, env=None):
    return (
        git_bytes(repo, *args, data=None if stdin is None else stdin.encode(), env=env)
        .decode()
        .strip()
    )


class RepoCase(unittest.TestCase):
    def setUp(self):
        self.repo = Repo()
        self.repo.commit({"a.txt": "clean\n"})
        self.repo.commit({"a.txt": "clean again\n"})
        self.words = write_words([PRIVATE_WORD])

    def verdict(self, words=None, *extra):
        return check(self.repo.path, words or self.words, *extra)

    def craft_commit(self, edit):
        raw = git_bytes(self.repo, "cat-file", "commit", "HEAD")
        made = (
            git_bytes(
                self.repo,
                "hash-object",
                "-t",
                "commit",
                "-w",
                "--literally",
                "--stdin",
                data=edit(raw),
            )
            .decode()
            .strip()
        )
        git_bytes(self.repo, "update-ref", "refs/heads/side", made)
        return made


class LateStructuralHeaderTests(RepoCase):
    def test_a_late_parent_line_or_its_continuation_is_scanned(self):
        word = PRIVATE_WORD.encode()
        for label, edit in (
            (
                "a second parent line after the committer",
                lambda raw: raw.replace(b"\n\n", b"\nparent " + word + b"\n\n", 1),
            ),
            (
                "a continuation line after the parent line",
                lambda raw: raw.replace(
                    b"\ncommitter", b"\n " + word + b"\ncommitter", 1
                ),
            ),
        ):
            with self.subTest(label):
                self.setUp()
                self.craft_commit(edit)
                self.assertEqual(self.verdict()[0], 1, label)

    def test_a_late_object_or_type_line_of_a_tag_is_scanned(self):
        tag = "object %s\ntype commit\ntag odd\ntagger A <a@example.net> 1700000000 +0000\n%s\n\nmsg\n"
        for label, late in (
            ("a late object line", "object " + PRIVATE_WORD),
            ("a late type line", "type " + PRIVATE_WORD),
        ):
            with self.subTest(label):
                self.setUp()
                body = tag % (git(self.repo, "rev-parse", "HEAD"), late)
                made = (
                    git_bytes(
                        self.repo,
                        "hash-object",
                        "-t",
                        "tag",
                        "-w",
                        "--literally",
                        "--stdin",
                        data=body.encode(),
                    )
                    .decode()
                    .strip()
                )
                git_bytes(self.repo, "update-ref", "refs/tags/odd", made)
                self.assertEqual(self.verdict()[0], 1, label)

    def test_an_ordinary_history_with_parents_is_still_clean(self):
        self.assertEqual(self.verdict()[0], 0)


class UnicodeFormTests(RepoCase):
    def test_look_alike_forms_of_the_word_are_found(self):
        forms = {
            "fullwidth": "".join(chr(0xFF41 + ord(c) - ord("a")) for c in PRIVATE_WORD),
            "math bold": "".join(
                chr(0x1D41A + ord(c) - ord("a")) for c in PRIVATE_WORD
            ),
            "circled": "".join(chr(0x24D0 + ord(c) - ord("a")) for c in PRIVATE_WORD),
            "blank letter between": PRIVATE_WORD[:6] + "ㅤ" + PRIVATE_WORD[6:],
            "zero width joiner between": PRIVATE_WORD[:6] + "‍" + PRIVATE_WORD[6:],
        }
        for label, text in forms.items():
            with self.subTest(label):
                self.setUp()
                self.repo.commit({"b.txt": text + "\n"})
                self.assertEqual(self.verdict()[0], 1, label)

    def test_a_word_with_accents_is_found_in_either_unicode_composition(self):
        word = "Müllerström"
        words = write_words([word])
        for form in ("NFC", "NFD"):
            with self.subTest(form):
                self.setUp()
                self.repo.commit({"b.txt": unicodedata.normalize(form, word) + "\n"})
                self.assertEqual(self.verdict(words)[0], 1, form)

    def test_a_word_outside_latin_1_is_found_in_a_utf_16_file(self):
        word = "секретпроект"  # no Latin-1 reading of the UTF-16 bytes spells it
        words = write_words([word])
        for label, codec in (("bom", "utf-16"), ("little endian", "utf-16-le"), ("big endian", "utf-16-be")):
            with self.subTest(label):
                self.setUp()
                blob = git_bytes(self.repo, "hash-object", "-w", "--stdin", data=word.encode(codec)).decode().strip()
                tree = git_bytes(self.repo, "mktree", data=("100644 blob %s\tb.bin\n" % blob).encode()).decode().strip()
                commit = git_bytes(self.repo, "commit-tree", tree, "-p", "HEAD", "-m", "n").decode().strip()
                git_bytes(self.repo, "update-ref", "refs/heads/side", commit)
                self.assertEqual(self.verdict(words)[0], 1, label)

    def test_a_decomposed_file_name_is_found_for_a_composed_entry(self):
        word = "Müllerström"
        blob = (
            git_bytes(self.repo, "hash-object", "-w", "--stdin", data=b"x\n")
            .decode()
            .strip()
        )
        name = unicodedata.normalize("NFD", word).encode("utf-8")
        tree = (
            git_bytes(
                self.repo,
                "mktree",
                "-z",
                data=b"100644 blob " + blob.encode() + b"\t" + name + b"\0",
            )
            .decode()
            .strip()
        )
        commit = (
            git_bytes(self.repo, "commit-tree", tree, "-p", "HEAD", "-m", "n")
            .decode()
            .strip()
        )
        git_bytes(self.repo, "update-ref", "refs/heads/side", commit)
        self.assertEqual(self.verdict(write_words([word]))[0], 1)

    def test_a_file_in_latin_1_or_utf_16_is_found(self):
        word = "Müllerström"
        words = write_words([word])
        for label, data in (
            ("latin-1", word.encode("latin-1")),
            ("utf-16", word.encode("utf-16")),
            ("utf-16-le", word.encode("utf-16-le")),
        ):
            with self.subTest(label):
                self.setUp()
                blob = (
                    git_bytes(self.repo, "hash-object", "-w", "--stdin", data=data)
                    .decode()
                    .strip()
                )
                tree = (
                    git_bytes(
                        self.repo,
                        "mktree",
                        data=("100644 blob %s\tb.bin\n" % blob).encode(),
                    )
                    .decode()
                    .strip()
                )
                commit = (
                    git_bytes(self.repo, "commit-tree", tree, "-p", "HEAD", "-m", "n")
                    .decode()
                    .strip()
                )
                git_bytes(self.repo, "update-ref", "refs/heads/side", commit)
                self.assertEqual(self.verdict(words)[0], 1, label)


class RefsAndTreesTests(RepoCase):
    def test_the_branch_an_unborn_head_points_at_is_scanned(self):
        git(self.repo, "symbolic-ref", "HEAD", "refs/heads/%s-draft" % PRIVATE_WORD)
        code, out, _err = self.verdict()
        self.assertEqual(code, 1, out)
        self.assertIn("ref", out)

    def test_the_name_of_an_empty_directory_is_scanned(self):
        empty = (
            git_bytes(self.repo, "hash-object", "-t", "tree", "-w", "--stdin", data=b"")
            .decode()
            .strip()
        )
        blob = (
            git_bytes(self.repo, "hash-object", "-w", "--stdin", data=b"x\n")
            .decode()
            .strip()
        )
        tree = (
            git_bytes(
                self.repo,
                "mktree",
                data=(
                    "100644 blob %s\ta.txt\n040000 tree %s\t%s\n"
                    % (blob, empty, PRIVATE_WORD)
                ).encode(),
            )
            .decode()
            .strip()
        )
        commit = (
            git_bytes(self.repo, "commit-tree", tree, "-p", "HEAD", "-m", "n")
            .decode()
            .strip()
        )
        git_bytes(self.repo, "update-ref", "refs/heads/side", commit)
        code, out, _err = self.verdict()
        self.assertEqual(code, 1, out)
        self.assertIn("path", out)


class AllowIdentityTests(RepoCase):
    def setUp(self):
        super().setUp()
        self.words = write_words([PRIVATE_WORD, OWNER_MAIL])
        self.owner = {"GIT_AUTHOR_EMAIL": OWNER_MAIL, "GIT_COMMITTER_EMAIL": OWNER_MAIL}

    def test_a_real_trailer_passes_and_a_line_that_only_starts_like_one_does_not(self):
        self.repo.commit(
            {"b.txt": "x\n"},
            message="fix\n\nSigned-off-by: Dev <%s>" % OWNER_MAIL,
            env=self.owner,
        )
        self.assertEqual(self.verdict(None, "--allow-identity", "2")[0], 0)
        self.repo.commit(
            {"c.txt": "x\n"},
            message="fix\n\nSigned-off-by: our unannounced project is codenamed %s, do not leak\n\nmore text"
            % PRIVATE_WORD,
            env=self.owner,
        )
        self.assertEqual(
            self.verdict(write_words([PRIVATE_WORD]), "--allow-identity", "1")[0], 1
        )

    def test_a_free_text_signed_off_line_in_the_trailer_block_is_still_a_hit(self):
        self.repo.commit({"b.txt": "x\n"}, message="fix\n\nSigned-off-by: codenamed %s do not leak" % PRIVATE_WORD, env=self.owner)
        self.assertEqual(self.verdict(write_words([PRIVATE_WORD]), "--allow-identity", "1")[0], 1)

    def test_a_signed_off_line_in_the_middle_of_the_body_is_a_hit(self):
        self.repo.commit(
            {"b.txt": "x\n"},
            message="intro\n\nSigned-off-by: Dev <%s>\n\nand then more body text"
            % OWNER_MAIL,
            env=self.owner,
        )
        self.assertEqual(self.verdict(None, "--allow-identity", "2")[0], 1)

    def test_a_co_author_trailer_and_a_mergetag_tagger_line_pass(self):
        self.repo.commit(
            {"b.txt": "x\n"},
            message="fix\n\nCo-authored-by: Dev <%s>" % OWNER_MAIL,
            env=self.owner,
        )
        self.assertEqual(self.verdict(None, "--allow-identity", "2")[0], 0)
        self.craft_commit(
            lambda raw: raw.replace(
                b"\n\n",
                b"\nmergetag object 0000000000000000000000000000000000000001\n type commit\n tag v1\n tagger Dev <"
                + OWNER_MAIL.encode()
                + b"> 1700000000 +0000\n\n",
                1,
            )
        )
        self.assertEqual(self.verdict(None, "--allow-identity", "2")[0], 0)


class RepoRootAndListTests(RepoCase):
    def test_a_folder_inside_a_repository_is_not_a_repository(self):
        inner = os.path.join(self.repo.path, "exported")
        os.makedirs(inner)
        with open(os.path.join(inner, "x.txt"), "w", encoding="utf-8") as fh:
            fh.write(PRIVATE_WORD + "\n")
        code, out, err = check(inner, self.words)
        self.assertEqual(code, 2, out)
        self.assertIn("root", err)

    def test_the_git_folder_and_a_bare_repository_are_accepted(self):
        self.assertEqual(check(os.path.join(self.repo.path, ".git"), self.words)[0], 0)
        bare = os.path.join(tempfile.mkdtemp(prefix="chbare_"), "b.git")
        subprocess.run(["git", "clone", "-q", "--bare", self.repo.path, bare], check=True, capture_output=True, env=dict(os.environ, **GIT_ENV))
        self.assertEqual(check(bare, self.words)[0], 0)

    def words_file(self, data):
        path = os.path.join(tempfile.mkdtemp(prefix="chwords_"), "w.txt")
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def test_entries_are_numbered_by_newline_only_and_a_line_with_nothing_visible_is_blank(self):
        self.repo.commit({"b.txt": "charlieword\n"})
        for label, data in (
            ("form feed inside a line", b"alphaword\x0cbravoword\ncharlieword\n"),
            ("a zero-width space line", b"alphaword\n\xe2\x80\x8b\ncharlieword\n"),
            ("a line separator inside a line", "alphaword\u2028bravoword\ncharlieword\n".encode()),
        ):
            with self.subTest(label):
                code, out, _err = check(self.repo.path, self.words_file(data))
                self.assertEqual(code, 1, out)
                self.assertIn("entries=2", out)
                self.assertIn("ENTRY 2:", out)

    def test_a_clean_result_with_entries_too_short_to_check_does_not_read_as_a_full_pass(self):
        code, out, _err = check(self.repo.path, write_words([PRIVATE_WORD, "abc", "xy"]))
        self.assertEqual(code, 0, out)
        self.assertIn("skipped_short=2", out)
        last = out.strip().splitlines()[-1]
        self.assertTrue(last.startswith("CLEAN-PARTIAL"), last)
        self.assertNotEqual(last, "CLEAN")

    def test_any_unexpected_failure_is_exit_two_with_a_message_not_a_traceback(self):
        import io
        from contextlib import redirect_stderr
        from unittest import mock

        import check_history as ch

        err = io.StringIO()
        with mock.patch.object(ch, "scan", side_effect=MemoryError()), redirect_stderr(err):
            code = ch.main(["--repo", self.repo.path, "--words", self.words])
        self.assertEqual(code, 2)
        self.assertIn("ERROR", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())


if __name__ == "__main__":
    unittest.main()
