"""check_history.py: a word on the private list must be found in any revision of a repository, however it is written down, and never printed."""

import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL_DIR = os.path.dirname(HERE)
sys.path.insert(0, TOOL_DIR)
import check_history as ch  # noqa: E402

PRIVATE_WORD = "zorbelquonk"  # a made-up word standing in for a private entry
OWNER_MAIL = "private.person@example.org"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Dev One",
    "GIT_AUTHOR_EMAIL": "dev.one@example.net",
    "GIT_COMMITTER_NAME": "Dev One",
    "GIT_COMMITTER_EMAIL": "dev.one@example.net",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


class Repo:
    def __init__(self):
        self.path = tempfile.mkdtemp(prefix="chhist_")
        self.git("init", "-q", "-b", "main")

    def git(self, *args, env=None):
        merged = dict(os.environ, **GIT_ENV)
        merged.update(env or {})
        return subprocess.run(
            ["git", "-C", self.path, *args],
            capture_output=True,
            text=True,
            check=True,
            env=merged,
        ).stdout

    def commit(self, files, message="c", env=None):
        for name, text in files.items():
            full = os.path.join(self.path, name)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as fh:
                fh.write(text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message, env=env)
        return self.git("rev-parse", "--short", "HEAD").strip()


def write_words(lines):
    path = os.path.join(tempfile.mkdtemp(prefix="chwords_"), "words.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def run(repo, words, *extra):
    out = subprocess.run(
        [
            sys.executable,
            "-B",
            "-I",
            os.path.join(TOOL_DIR, "check_history.py"),
            "--repo",
            repo.path,
            "--words",
            words,
            *extra,
        ],
        capture_output=True,
        text=True,
    )
    return out.returncode, out.stdout, out.stderr


class FindingTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repo()
        self.words = write_words(
            ["# comment", "", PRIVATE_WORD, "another-private-thing"]
        )

    def test_a_clean_history_exits_zero(self):
        self.repo.commit({"a.txt": "nothing to see\n"})
        code, out, _err = run(self.repo, self.words)
        self.assertEqual(code, 0, out)
        self.assertIn("CLEAN", out)

    def test_a_word_in_an_old_revision_is_found_even_after_it_was_removed(self):
        self.repo.commit({"a.txt": "hello\n" + PRIVATE_WORD + "\n"})
        self.repo.commit({"a.txt": "hello\n"})
        code, out, _err = run(self.repo, self.words)
        self.assertEqual(code, 1, out)
        self.assertIn("ENTRY 1:", out)  # the first entry after comments and blank lines
        self.assertIn("content", out)

    def test_a_word_written_as_string_fragments_is_found(self):
        self.repo.commit({"t.py": 'WORDS = ["zor" "bel", "quo" "nk"]\n'})
        code, out, _err = run(self.repo, self.words)
        self.assertEqual(code, 1, out)

    def test_case_spacing_and_punctuation_do_not_hide_a_word(self):
        self.repo.commit({"a.txt": "Zorbel-Quonk\nzorbel quonk\n"})
        self.assertEqual(run(self.repo, self.words)[0], 1)

    def test_a_word_in_a_file_name_a_message_or_a_tag_is_found(self):
        for label, action in (
            ("path", lambda: self.repo.commit({PRIVATE_WORD + ".txt": "x\n"})),
            (
                "message",
                lambda: self.repo.commit(
                    {"a.txt": "x\n"}, message="mentions " + PRIVATE_WORD
                ),
            ),
            (
                "tag",
                lambda: (
                    self.repo.commit({"a.txt": "x\n"}),
                    self.repo.git("tag", "-a", "v1", "-m", "release " + PRIVATE_WORD),
                ),
            ),
        ):
            with self.subTest(label):
                self.setUp()
                action()
                code, out, _err = run(self.repo, self.words)
                self.assertEqual(code, 1, out)
                self.assertIn(label, out)

    def test_the_word_is_never_printed(self):
        self.repo.commit(
            {PRIVATE_WORD + ".txt": PRIVATE_WORD + "\n"},
            message="about " + PRIVATE_WORD,
        )
        code, out, err = run(self.repo, self.words)
        self.assertEqual(code, 1)
        for text in (out, err):
            self.assertNotIn(PRIVATE_WORD, text.casefold())
            self.assertNotIn("zorbel", text.casefold())

    def test_the_commit_that_holds_it_is_named_by_short_sha(self):
        self.repo.commit({"a.txt": "clean\n"})
        bad = self.repo.commit({"b.txt": PRIVATE_WORD + "\n"})
        _code, out, _err = run(self.repo, self.words)
        self.assertIn(bad, out)


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repo()
        self.words = write_words([PRIVATE_WORD, OWNER_MAIL])
        self.owner = {"GIT_AUTHOR_EMAIL": OWNER_MAIL, "GIT_COMMITTER_EMAIL": OWNER_MAIL}

    def test_an_identity_in_commit_metadata_is_a_hit_unless_it_is_allowed(self):
        self.repo.commit({"a.txt": "x\n"}, env=self.owner)
        code, out, _err = run(self.repo, self.words)
        self.assertEqual(code, 1, out)
        self.assertIn("metadata", out)

    def test_an_allowed_identity_passes_in_metadata_and_signoff_but_not_in_a_file(self):
        self.repo.commit(
            {"a.txt": "x\n"},
            message="fix\n\nSigned-off-by: Dev <%s>" % OWNER_MAIL,
            env=self.owner,
        )
        self.assertEqual(run(self.repo, self.words, "--allow-identity", "2")[0], 0)
        self.repo.commit({"b.txt": OWNER_MAIL + "\n"}, env=self.owner)
        code, out, _err = run(self.repo, self.words, "--allow-identity", "2")
        self.assertEqual(code, 1, out)
        self.assertIn("content", out)

    def test_an_allowed_identity_in_the_message_body_is_still_a_hit(self):
        self.repo.commit(
            {"a.txt": "x\n"}, message="contact " + OWNER_MAIL, env=self.owner
        )
        self.assertEqual(run(self.repo, self.words, "--allow-identity", "2")[0], 1)


class UsageTests(unittest.TestCase):
    def test_a_missing_or_empty_word_list_is_a_usage_error_not_a_clean_pass(self):
        repo = Repo()
        repo.commit({"a.txt": "x\n"})
        self.assertEqual(
            run(repo, os.path.join(tempfile.gettempdir(), "no-such-words-file"))[0], 2
        )
        self.assertEqual(run(repo, write_words(["# only a comment", ""]))[0], 2)

    def test_not_a_repository_is_a_usage_error(self):
        class NotARepo:
            path = tempfile.mkdtemp(prefix="chnorepo_")

        self.assertEqual(run(NotARepo, write_words([PRIVATE_WORD]))[0], 2)

    def test_entries_too_short_to_check_reliably_are_reported_and_skipped(self):
        repo = Repo()
        repo.commit({"a.txt": "abc abc\n"})
        code, out, _err = run(repo, write_words(["abc", PRIVATE_WORD]))
        self.assertEqual(code, 0, out)
        self.assertIn("skipped_short=1", out)

    def test_an_allow_identity_number_outside_the_list_is_a_usage_error(self):
        repo = Repo()
        repo.commit({"a.txt": "x\n"})
        self.assertEqual(
            run(repo, write_words([PRIVATE_WORD]), "--allow-identity", "9")[0], 2
        )


class NormaliseTests(unittest.TestCase):
    def test_normalise_keeps_letters_and_digits_only_and_ignores_case(self):
        self.assertEqual(ch.normalise('"Zor" "bel-Q_u onk" 9'), "zorbelquonk9")

    def test_read_entries_skips_comments_blank_lines_and_trailing_comments(self):
        path = write_words(["# c", "", "  alpha beta  # tail", "gamma"])
        self.assertEqual(ch.read_entries(path), ["alpha beta", "gamma"])


if __name__ == "__main__":
    unittest.main()
