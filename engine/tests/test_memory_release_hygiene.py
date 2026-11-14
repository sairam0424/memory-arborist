"""Release guard: no machine-specific path, and no word from the maintainer's private deny-list, may ship in any text file.

Home paths are always checked. The deny-list (personal, employer and project names, and names of undocumented Claude Code
internals) is NOT in this repository: it is read from the git-ignored file `.release-guard-words` at the repository root
(one word or phrase per line, `#` starts a comment), or from the path in MEMORY_ARBORIST_RELEASE_GUARD_WORDS. Without that
file, as in CI, only the generic rules run, and a note on stderr says so. If this test fails, remove the string (use neutral
fixtures such as Acme and /Users/example); never add an exception.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import memtest_helpers as th  # noqa: E402

sys.path.insert(0, th.SCRIPTS)  # python3 -I does not put the script dir on sys.path
import memory_scan as ms  # noqa: E402

REPO = os.path.dirname(th.SCRIPTS)
# Every regular file is scanned unless it looks binary (a NUL byte in its first 4 KB) or sits in a cache folder that
# .gitignore keeps out of the repository.
SKIP_DIRS = {".git", "__pycache__", ".ruff_cache", ".mypy_cache", "node_modules"}
MUST_BE_SCANNED = (
    "LICENSE",
    "NOTICE",
    ".gitignore",
    "README.md",
    "SECURITY.md",
    "hooks/hooks.json",
    "skills/tend/SKILL.md",
    ".claude-plugin/plugin.json",
    "engine/memory_scan.py",
    "engine/tests/test_memory_release_hygiene.py",
)

PRIVATE_WORDS_NAME = ".release-guard-words"
PRIVATE_WORDS_FILE = os.environ.get("MEMORY_ARBORIST_RELEASE_GUARD_WORDS") or os.path.join(REPO, PRIVATE_WORDS_NAME)


def read_private_words(path=None):
    """One word or phrase per line; blank lines and lines that start with # are ignored. A missing file means no words."""
    try:
        with open(path or PRIVATE_WORDS_FILE, encoding="utf-8") as fh:
            lines = [line.strip() for line in fh]
    except OSError:
        return []
    return [line for line in lines if line and not line.startswith("#")]


def forbidden_pattern(words):
    """A case-insensitive pattern for the words, or one that can never match when there are none."""
    if not words:
        return re.compile(r"(?!)")
    return re.compile("|".join(re.escape(w) for w in words), re.IGNORECASE)


WORDS = read_private_words()
FORBIDDEN = forbidden_pattern(WORDS)
# any absolute home path except the neutral placeholders used by fixtures
HOME_PATH = re.compile(
    r"/(?i:Users|home)/(?!(?:x|someone|example|you|name|user)(?![A-Za-z0-9_.-]))[A-Za-z0-9_.-]+"
)


def is_binary(path):
    with open(path, "rb") as fh:
        return b"\0" in fh.read(4096)


def setUpModule():
    if not WORDS:
        print("\nrelease guard: no private deny-list at %s, so only the generic rules ran" % PRIVATE_WORDS_FILE, file=sys.stderr)


def all_files():
    for base, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in sorted(files):
            path = os.path.join(base, name)
            if os.path.abspath(path) == os.path.abspath(PRIVATE_WORDS_FILE):
                continue  # the list names the very words it forbids
            if os.path.isfile(path) and not os.path.islink(path):
                yield path


def text_files():
    return (path for path in all_files() if not is_binary(path))


class ReleaseHygieneTests(unittest.TestCase):
    def offenders(self, pattern):
        found = []
        for path in text_files():
            with open(path, encoding="utf-8", errors="replace") as fh:
                for number, line in enumerate(fh, 1):
                    if pattern.search(line):
                        found.append("%s:%d" % (os.path.relpath(path, REPO), number))
        return found

    def test_the_scan_really_walks_the_repository(self):
        scanned = {os.path.relpath(p, REPO) for p in text_files()}
        self.assertGreater(len(scanned), 60)
        for must in MUST_BE_SCANNED:
            self.assertIn(must, scanned)

    def test_no_file_or_folder_name_carries_a_forbidden_word(self):
        names = [os.path.relpath(p, REPO) for p in all_files()]
        self.assertEqual([n for n in names if FORBIDDEN.search(n) or HOME_PATH.search("/" + n)], [])

    def test_gitignore_keeps_cache_folders_that_embed_home_paths_out(self):
        with open(os.path.join(REPO, ".gitignore"), encoding="utf-8") as fh:
            ignored = {line.strip() for line in fh}
        self.assertTrue({"__pycache__/", "*.pyc", ".ruff_cache/"} <= ignored)

    def test_no_personal_employer_or_project_name_appears_anywhere(self):
        self.assertEqual(self.offenders(FORBIDDEN), [])

    def test_no_real_home_directory_path_appears_anywhere(self):
        self.assertEqual(self.offenders(HOME_PATH), [])

    def test_no_source_file_holds_a_secret_shaped_literal_the_repo_scans_itself(self):
        """Fake credentials must be assembled at runtime, so that secret scanners (this repo's own and others') find nothing."""
        loud = {"Quoted Key Value", "Key Context", "Env Assignment"}  # warn-tier shapes a secret scanner treats as a finding
        found = []
        for path in text_files():
            rel = os.path.relpath(path, REPO)
            if not rel.endswith((".py", ".md", ".json", ".yml", ".yaml", ".toml", ".txt", ".sh")):
                continue
            for hit in ms.scan_file(path, rel):
                if hit.tier == "block" or hit.pattern in loud:
                    found.append("%s:%d:%s" % (rel, hit.line, hit.pattern))
        self.assertEqual(found, [])

    def test_the_repos_own_fake_credential_body_never_appears_as_one_piece(self):
        fake_body = "aB3dE5gH7jK9" + "mN1pQ3sT"  # the synthetic body the tests use; a literal copy is flagged by secret scanners
        offenders = []
        for path in text_files():
            with open(path, encoding="utf-8", errors="replace") as fh:
                if fake_body in fh.read():
                    offenders.append(os.path.relpath(path, REPO))
        self.assertEqual(offenders, [])

    def test_the_patterns_themselves_catch_what_they_should(self):
        sample = forbidden_pattern(["Exam" + "pleCorp", "native " + "thing"])
        self.assertTrue(sample.search("see exam" + "plecorp here"))
        self.assertTrue(sample.search("a NATIVE " + "THING"))
        self.assertTrue(HOME_PATH.search("/Users/" + "alice" + "/work"))
        self.assertTrue(HOME_PATH.search("/Us" + "ers/x." + "smith/work"))
        self.assertTrue(HOME_PATH.search("/users/" + "alice"))
        self.assertTrue(HOME_PATH.search("/ho" + "me/user-" + "jdoe/src"))
        self.assertFalse(HOME_PATH.search("/Users/example/project"))
        self.assertFalse(HOME_PATH.search("/Users/x"))
        self.assertFalse(sample.search("Acme and widgets"))
        self.assertFalse(forbidden_pattern([]).search("anything at all"))
        self.assertFalse(FORBIDDEN.search("Acme and widgets"))

    def test_the_private_list_is_parsed_and_comments_are_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "words")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("# a comment\n\n  Exam" + "pleCorp  \n#another\nsome phrase\n")
            self.assertEqual(read_private_words(path), ["Exam" + "pleCorp", "some phrase"])
            self.assertEqual(read_private_words(os.path.join(folder, "missing")), [])

    def test_the_private_list_is_never_tracked_and_is_ignored_by_git(self):
        with open(os.path.join(REPO, ".gitignore"), encoding="utf-8") as fh:
            self.assertIn(PRIVATE_WORDS_NAME, {line.strip() for line in fh})
        if shutil.which("git") and os.path.isdir(os.path.join(REPO, ".git")):
            done = subprocess.run(["git", "ls-files", PRIVATE_WORDS_NAME], cwd=REPO, capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(done.stdout.strip(), "")

    def test_the_scan_skips_the_private_list_itself(self):
        with tempfile.TemporaryDirectory() as folder:
            private = os.path.join(folder, PRIVATE_WORDS_NAME)
            for name, text in ((PRIVATE_WORDS_NAME, "x\n"), ("other.txt", "y\n")):
                with open(os.path.join(folder, name), "w", encoding="utf-8") as fh:
                    fh.write(text)
            with mock.patch.object(sys.modules[__name__], "REPO", folder), mock.patch.object(sys.modules[__name__], "PRIVATE_WORDS_FILE", private):
                self.assertEqual([os.path.basename(p) for p in all_files()], ["other.txt"])


if __name__ == "__main__":
    unittest.main()
