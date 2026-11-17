"""Found by the independent code review of check_history.py: places a word can sit in a repository that the first version never
looked at (objects a replace ref or a graft hides, tags of blobs and trees, nested tags, extra commit headers, side branches), a
check that ended in CLEAN although it could not be complete (missing objects, a shallow clone, no commits), and an environment
that pointed it at another repository."""

import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL_DIR = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, TOOL_DIR)
import check_history as ch  # noqa: E402
from test_check_history import (  # noqa: E402
    GIT_ENV,
    OWNER_MAIL,
    PRIVATE_WORD,
    Repo,
    write_words,
)

TOOL = os.path.join(TOOL_DIR, "check_history.py")


def git(repo, *args, stdin=None, env=None):
    merged = dict(os.environ, **GIT_ENV)
    merged.update(env or {})
    return subprocess.run(
        ["git", "-C", repo.path, *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=True,
        env=merged,
    ).stdout.strip()


def check(repo_path, words, *extra, env=None):
    done = subprocess.run(
        [
            sys.executable,
            "-B",
            "-I",
            TOOL,
            "--repo",
            repo_path,
            "--words",
            words,
            *extra,
        ],
        capture_output=True,
        text=True,
        env=dict(os.environ, **(env or {})),
    )
    return done.returncode, done.stdout, done.stderr


def blob_with_word(repo):
    return git(repo, "hash-object", "-w", "--stdin", stdin=PRIVATE_WORD + "\n")


class HiddenObjectTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repo()
        self.words = write_words([PRIVATE_WORD])

    def test_a_replace_ref_does_not_hide_the_original_commit(self):
        self.repo.commit({"a.txt": "clean\n"})
        first = git(self.repo, "rev-parse", "HEAD")
        bad = self.repo.commit({"a.txt": PRIVATE_WORD + "\n"})
        self.repo.commit({"a.txt": "clean again\n"})
        stand_in = git(
            self.repo, "commit-tree", first + "^{tree}", "-p", first, "-m", "stand-in"
        )
        git(self.repo, "replace", bad, stand_in)
        code, out, _err = check(self.repo.path, self.words)
        self.assertEqual(code, 1, out)
        self.assertIn("content", out)

    def test_a_graft_file_does_not_hide_the_ancestry(self):
        self.repo.commit({"a.txt": PRIVATE_WORD + "\n"})
        self.repo.commit({"a.txt": "clean\n"})
        tip = git(self.repo, "rev-parse", "HEAD")
        with open(
            os.path.join(self.repo.path, ".git", "info", "grafts"),
            "w",
            encoding="utf-8",
        ) as fh:
            fh.write(tip + "\n")  # a graft line with no parent makes the tip a root
        code, out, _err = check(self.repo.path, self.words)
        self.assertEqual(code, 1, out)

    def test_an_environment_that_points_git_elsewhere_does_not_redirect_the_check(self):
        self.repo.commit({"a.txt": PRIVATE_WORD + "\n"})
        decoy = Repo()
        decoy.commit({"a.txt": "clean\n"})
        code, out, _err = check(
            self.repo.path,
            self.words,
            env={"GIT_DIR": os.path.join(decoy.path, ".git")},
        )
        self.assertEqual(code, 1, out)


class ReachedOnlyThroughATagTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repo()
        self.repo.commit({"a.txt": "clean\n"})
        self.words = write_words([PRIVATE_WORD])

    def assert_found(self, expect=None):
        code, out, _err = check(self.repo.path, self.words)
        self.assertEqual(code, 1, out)
        if expect:
            self.assertIn(expect, out)

    def test_an_annotated_tag_on_a_blob(self):
        git(self.repo, "tag", "-a", "blobtag", "-m", "plain", blob_with_word(self.repo))
        self.assert_found("content")

    def test_a_lightweight_tag_on_a_blob(self):
        git(self.repo, "update-ref", "refs/tags/lw", blob_with_word(self.repo))
        self.assert_found("content")

    def test_a_tag_on_a_tree_whose_file_name_holds_the_word(self):
        blob = git(self.repo, "hash-object", "-w", "--stdin", stdin="plain\n")
        tree = git(
            self.repo, "mktree", stdin="100644 blob %s\t%s.txt\n" % (blob, PRIVATE_WORD)
        )
        git(self.repo, "tag", "-a", "treetag", "-m", "plain", tree)
        self.assert_found("path")

    def test_the_message_of_a_tag_that_another_tag_points_at(self):
        git(self.repo, "tag", "-a", "inner", "-m", "inner " + PRIVATE_WORD)
        git(self.repo, "tag", "-a", "outer", "-m", "clean", "inner")
        git(self.repo, "tag", "-d", "inner")
        self.assert_found("tag")

    def test_the_name_inside_a_tag_object_that_a_ref_now_hides(self):
        git(self.repo, "tag", "-a", PRIVATE_WORD + "-rc", "-m", "clean")
        git(self.repo, "tag", "v1", PRIVATE_WORD + "-rc")
        git(self.repo, "tag", "-d", PRIVATE_WORD + "-rc")
        self.assert_found("tag")

    def test_an_extra_header_in_a_commit_object(self):
        raw = git(self.repo, "cat-file", "commit", "HEAD")
        head, _sep, message = raw.partition("\n\n")
        edited = head + "\nx-private-note " + PRIVATE_WORD + "\n\n" + message + "\n"
        made = git(
            self.repo,
            "hash-object",
            "-t",
            "commit",
            "-w",
            "--literally",
            "--stdin",
            stdin=edited,
        )
        git(self.repo, "update-ref", "refs/heads/side", made)
        self.assert_found("metadata")

    def test_a_word_split_over_the_lines_of_one_multi_line_header(self):
        raw = git(self.repo, "cat-file", "commit", "HEAD")
        head, _sep, message = raw.partition("\n\n")
        edited = head + "\nx-private-note " + PRIVATE_WORD[:6] + "\n " + PRIVATE_WORD[6:] + "\n\n" + message + "\n"
        made = git(self.repo, "hash-object", "-t", "commit", "-w", "--literally", "--stdin", stdin=edited)
        git(self.repo, "update-ref", "refs/heads/side", made)
        self.assert_found("metadata")

    def test_a_word_that_is_only_on_a_side_branch(self):
        git(self.repo, "checkout", "-q", "-b", "side")
        self.repo.commit({"b.txt": PRIVATE_WORD + "\n"})
        git(self.repo, "checkout", "-q", "main")
        self.assert_found("content")

    def test_a_ref_name_that_holds_the_word(self):
        git(self.repo, "branch", PRIVATE_WORD + "-branch")
        self.assert_found("ref")

    def test_the_name_of_a_submodule_entry(self):
        git(
            self.repo,
            "update-index",
            "--add",
            "--cacheinfo",
            "160000,%s,%s-sub" % ("1" * 40, PRIVATE_WORD),
        )
        git(self.repo, "commit", "-q", "-m", "link")
        self.assert_found("path")

    def test_a_control_character_in_a_message_does_not_hide_the_rest_of_it(self):
        self.repo.commit({"b.txt": "x\n"}, message="start\x1f%s\x1eend" % PRIVATE_WORD)
        self.assert_found("message")


class IdentityAndBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repo()
        self.repo.commit({"a.txt": "clean\n"})
        self.owner = {"GIT_COMMITTER_EMAIL": OWNER_MAIL, "GIT_AUTHOR_EMAIL": OWNER_MAIL}

    def test_an_identity_only_in_a_tagger_line_is_a_hit_unless_allowed(self):
        words = write_words([PRIVATE_WORD, OWNER_MAIL])
        git(self.repo, "tag", "-a", "v1", "-m", "release", env=self.owner)
        code, out, _err = check(self.repo.path, words)
        self.assertEqual((code, "metadata" in out), (1, True), out)
        self.assertEqual(check(self.repo.path, words, "--allow-identity", "2")[0], 0)

    def test_a_four_character_entry_is_checked_and_a_three_character_one_is_skipped(
        self,
    ):
        self.repo.commit({"b.txt": "abcd xyz\n"})
        self.assertEqual(check(self.repo.path, write_words(["abcd"]))[0], 1)
        code, out, _err = check(self.repo.path, write_words(["xyz", PRIVATE_WORD]))
        self.assertEqual(code, 0, out)
        self.assertIn("skipped_short=1", out)

    def test_the_word_is_never_printed_for_these_forms(self):
        git(self.repo, "tag", "-a", PRIVATE_WORD + "-rc", "-m", "x " + PRIVATE_WORD)
        _code, out, err = check(self.repo.path, write_words([PRIVATE_WORD]))
        for text in (out, err):
            self.assertNotIn("zorbel", text.casefold())


class IncompleteCheckTests(unittest.TestCase):
    """A result that cannot be complete is an error (2), never CLEAN."""

    def test_an_object_that_a_ref_reaches_but_the_repository_lacks(self):
        repo = Repo()
        repo.commit({"a.txt": PRIVATE_WORD + "\n"})
        repo.commit({"a.txt": "clean\n"})
        blob = git(repo, "rev-parse", "HEAD~1:a.txt")
        os.remove(os.path.join(repo.path, ".git", "objects", blob[:2], blob[2:]))
        code, out, _err = check(repo.path, write_words([PRIVATE_WORD]))
        self.assertEqual(code, 2, out)
        self.assertNotIn("CLEAN", out)

    def test_an_object_that_cat_file_reports_as_missing_is_an_error(self):
        reply = ("%s missing\n" % ("a" * 40)).encode()
        with mock.patch.object(ch, "git", return_value=reply), self.assertRaises(ch.UsageError) as caught:
            ch.load_objects("anywhere", ["a" * 40])
        self.assertIn("missing", str(caught.exception))

    def test_a_shallow_clone(self):
        repo = Repo()
        repo.commit({"a.txt": PRIVATE_WORD + "\n"})
        repo.commit({"a.txt": "clean\n"})
        clone = os.path.join(tempfile.mkdtemp(prefix="chshallow_"), "c")
        subprocess.run(
            ["git", "clone", "-q", "--depth", "1", "file://" + repo.path, clone],
            check=True,
            capture_output=True,
            env=dict(os.environ, **GIT_ENV),
        )
        code, out, err = check(clone, write_words([PRIVATE_WORD]))
        self.assertEqual(code, 2, out)
        self.assertIn("shallow", err)

    def test_a_partial_clone(self):
        repo = Repo()
        repo.commit({"a.txt": "x\n"})
        git(repo, "config", "remote.origin.promisor", "true")
        code, _out, err = check(repo.path, write_words([PRIVATE_WORD]))
        self.assertEqual(code, 2)
        self.assertIn("partial", err)

    def test_a_repository_without_commits(self):
        repo = Repo()
        code, out, err = check(repo.path, write_words([PRIVATE_WORD]))
        self.assertEqual(code, 2, out)
        self.assertIn("no commits", err)

    def test_a_list_in_which_every_entry_is_too_short(self):
        repo = Repo()
        repo.commit({"a.txt": "abc xy\n"})
        code, out, err = check(repo.path, write_words(["abc", "xy"]))
        self.assertEqual(code, 2, out)
        self.assertIn("shorter", err)


class WordListTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repo()
        self.repo.commit({"a.txt": PRIVATE_WORD + "\n"})

    def words_file(self, data):
        path = os.path.join(tempfile.mkdtemp(prefix="chwords_"), "w.txt")
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def test_a_byte_order_mark_does_not_add_a_phantom_entry(self):
        path = self.words_file(
            b"\xef\xbb\xbf# header\n" + PRIVATE_WORD.encode() + b"\n"
        )
        code, out, _err = check(self.repo.path, path)
        self.assertEqual(code, 1, out)
        self.assertIn("ENTRY 1:", out)
        self.assertNotIn("ENTRY 2:", out)

    def test_a_list_that_is_not_utf8_is_a_usage_error_not_a_traceback(self):
        path = self.words_file(b"vexilthrandor\ncaf\xe9quonk\n")
        code, _out, err = check(self.repo.path, path)
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", err)


class EnvironmentTests(unittest.TestCase):
    def test_the_scrubbed_names_include_the_variables_that_redirect_git(self):
        for name in (
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_COMMON_DIR",
            "GIT_NAMESPACE",
        ):
            self.assertIn(name, ch.GIT_SCRUB)

    def test_the_environment_given_to_git_has_replace_refs_and_grafts_switched_off(
        self,
    ):
        env = ch.git_env()
        self.assertEqual(env["GIT_NO_REPLACE_OBJECTS"], "1")
        self.assertEqual(env["GIT_GRAFT_FILE"], os.devnull)


if __name__ == "__main__":
    unittest.main()
