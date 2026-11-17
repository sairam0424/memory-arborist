"""rewrite_history.py: a new root and a replay of the later commits, with trees, identities, dates, messages and tags kept, and nothing older in the result."""

import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "rewrite_history.py")

BASE_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}
OWNER = {
    "GIT_AUTHOR_NAME": "Owner Name",
    "GIT_AUTHOR_EMAIL": "owner@example.org",
    "GIT_COMMITTER_NAME": "Owner Name",
    "GIT_COMMITTER_EMAIL": "owner@example.org",
}


def git(repo, *args, env=None, data=None):
    merged = dict(os.environ, **BASE_ENV)
    merged.update(env or {})
    return subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        check=True,
        env=merged,
        input=data,
    ).stdout


class Source:
    """A throwaway repository whose history is five commits, one of which holds a word that must not survive."""

    def __init__(self):
        self.path = tempfile.mkdtemp(prefix="rwsrc_")
        git(self.path, "init", "-q", "-b", "main")
        self.shas = []
        stamps = [1_700_000_000 + 86_400 * i for i in range(5)]
        files = [
            {"a.txt": "one\n", "old-secret.txt": "forbidden\n"},
            {"a.txt": "two\n"},
            {"a.txt": "three\n", "b/c.txt": "deep\n"},
            {"a.txt": "four\n"},
            {"a.txt": "five\n"},
        ]
        for i, (tree, stamp) in enumerate(zip(files, stamps)):
            if i == 2:
                os.remove(os.path.join(self.path, "old-secret.txt"))
            for name, text in tree.items():
                full = os.path.join(self.path, name)
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "w", encoding="utf-8") as fh:
                    fh.write(text)
            git(self.path, "add", "-A")
            when = {
                "GIT_AUTHOR_DATE": "%d +0530" % stamp,
                "GIT_COMMITTER_DATE": "%d +0530" % (stamp + 60),
            }
            message = (
                "commit %d\n\nbody line\n\nSigned-off-by: Owner Name <owner@example.org>\n"
                % i
            )
            git(self.path, "commit", "-q", "-m", message, env=dict(OWNER, **when))
            self.shas.append(git(self.path, "rev-parse", "HEAD").strip())
        tag_env = dict(OWNER, GIT_COMMITTER_DATE="1700500000 +0530")
        git(
            self.path,
            "tag",
            "-a",
            "v1",
            "-m",
            "release one\n\nnotes",
            self.shas[3],
            env=tag_env,
        )
        git(self.path, "tag", "-a", "v0", "-m", "too old", self.shas[0], env=tag_env)


def run(source, dest, *extra):
    done = subprocess.run(
        [
            sys.executable,
            "-B",
            "-I",
            TOOL,
            "--source",
            source.path if hasattr(source, "path") else source,
            "--dest",
            dest,
            *extra,
        ],
        capture_output=True,
        text=True,
    )
    return done.returncode, done.stdout, done.stderr


class RewriteTests(unittest.TestCase):
    def setUp(self):
        self.src = Source()
        self.dest = os.path.join(tempfile.mkdtemp(prefix="rwdst_"), "new")

    def rewrite(self, *extra, root=2):
        return run(self.src, self.dest, "--root", self.src.shas[root], *extra)

    def new_shas(self):
        return git(self.dest, "rev-list", "--reverse", "main").split()

    def test_the_result_starts_at_the_chosen_root_and_keeps_the_later_commits(self):
        code, out, err = self.rewrite()
        self.assertEqual(code, 0, out + err)
        shas = self.new_shas()
        self.assertEqual(len(shas), 3)
        self.assertEqual(
            git(self.dest, "rev-list", "--max-parents=0", "main").split(), [shas[0]]
        )
        self.assertIn("commits=3", out)

    def test_every_tree_is_the_same_as_the_original_at_that_point(self):
        self.rewrite()
        for old, new in zip(self.src.shas[2:], self.new_shas()):
            self.assertEqual(
                git(self.src.path, "rev-parse", old + "^{tree}"),
                git(self.dest, "rev-parse", new + "^{tree}"),
            )

    def test_authors_committers_dates_and_messages_are_kept_exactly(self):
        self.rewrite()
        fmt = "--format=%an|%ae|%ad|%cn|%ce|%cd|%B"
        for old, new in zip(self.src.shas[2:], self.new_shas()):
            self.assertEqual(
                git(self.src.path, "show", "-s", fmt, "--date=raw", old),
                git(self.dest, "show", "-s", fmt, "--date=raw", new),
            )

    def test_nothing_older_than_the_root_is_in_the_result(self):
        self.rewrite()
        forbidden_blob = git(
            self.src.path, "rev-parse", self.src.shas[0] + ":old-secret.txt"
        ).strip()
        missing = subprocess.run(
            ["git", "-C", self.dest, "cat-file", "-e", forbidden_blob],
            capture_output=True,
        )
        self.assertNotEqual(missing.returncode, 0)
        for old in self.src.shas[:2]:
            self.assertNotEqual(
                subprocess.run(
                    ["git", "-C", self.dest, "cat-file", "-e", old], capture_output=True
                ).returncode,
                0,
            )
        self.assertNotIn(
            "old-secret", git(self.dest, "log", "--all", "--name-only", "--format=")
        )

    def test_a_tag_after_the_root_is_recreated_as_an_annotated_tag_on_the_replayed_commit(
        self,
    ):
        code, out, err = self.rewrite("--tag", "v1")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(git(self.dest, "cat-file", "-t", "v1").strip(), "tag")
        self.assertEqual(
            git(self.dest, "rev-parse", "v1^{commit}").strip(), self.new_shas()[1]
        )
        self.assertEqual(
            git(
                self.src.path,
                "for-each-ref",
                "--format=%(taggername) %(taggeremail) %(taggerdate:raw) %(contents)",
                "refs/tags/v1",
            ),
            git(
                self.dest,
                "for-each-ref",
                "--format=%(taggername) %(taggeremail) %(taggerdate:raw) %(contents)",
                "refs/tags/v1",
            ),
        )

    def test_a_branch_can_be_pointed_at_a_replayed_tag(self):
        code, out, err = self.rewrite("--tag", "v1", "--branch", "stable=v1")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(
            git(self.dest, "rev-parse", "stable").strip(), self.new_shas()[1]
        )

    def test_only_the_requested_refs_exist_in_the_result(self):
        self.rewrite("--tag", "v1")
        refs = git(self.dest, "for-each-ref", "--format=%(refname)").split()
        self.assertEqual(sorted(refs), ["refs/heads/main", "refs/tags/v1"])

    def test_a_tag_on_a_dropped_commit_is_a_usage_error(self):
        code, _out, err = self.rewrite("--tag", "v0")
        self.assertEqual(code, 2)
        self.assertIn("v0", err)
        self.assertFalse(os.path.exists(self.dest))  # refused before anything was created

    def test_the_source_repository_is_left_as_it_was(self):
        before = git(self.src.path, "for-each-ref") + git(
            self.src.path, "count-objects", "-v"
        )
        self.rewrite()
        after = git(self.src.path, "for-each-ref") + git(
            self.src.path, "count-objects", "-v"
        )
        self.assertEqual(before, after)


class VerificationTests(unittest.TestCase):
    """The tool's own check must be able to fail: it is the last thing between a replay bug and a published history."""

    def setUp(self):
        sys.path.insert(0, os.path.dirname(HERE))
        import rewrite_history as rh

        self.rh = rh
        self.src = Source()
        self.dest = os.path.join(tempfile.mkdtemp(prefix="rwdst_"), "new")
        code, out, err = run(self.src, self.dest, "--root", self.src.shas[2])
        self.assertEqual(code, 0, out + err)
        self.commits = [rh.read_commit(self.src.path, sha) for sha in self.src.shas[2:]]
        new = git(self.dest, "rev-list", "--reverse", "main").split()
        self.mapping = dict(zip(self.src.shas[2:], new))

    def test_a_correct_mapping_verifies(self):
        self.rh.verify(self.dest, self.commits, self.mapping)

    def test_a_commit_replayed_as_another_one_is_caught(self):
        wrong = dict(self.mapping)
        first, second = self.src.shas[2], self.src.shas[3]
        wrong[first], wrong[second] = self.mapping[second], self.mapping[first]
        with self.assertRaises(RuntimeError):
            self.rh.verify(self.dest, self.commits, wrong)

    def test_a_history_of_the_wrong_length_is_caught(self):
        with self.assertRaises(RuntimeError):
            self.rh.verify(self.dest, self.commits[:-1], self.mapping)


class BranchSpecTests(unittest.TestCase):
    def setUp(self):
        self.src = Source()
        self.dest = os.path.join(tempfile.mkdtemp(prefix="rwdst_"), "new")

    def test_a_branch_spec_without_a_known_tag_is_refused(self):
        for spec in ("stable=nope", "=v1", "stable"):
            with self.subTest(spec):
                self.dest = os.path.join(tempfile.mkdtemp(prefix="rwdst_"), "new")
                code, _out, err = run(self.src, self.dest, "--root", self.src.shas[2], "--tag", "v1", "--branch", spec)
                self.assertEqual(code, 2, err)
                self.assertFalse(os.path.exists(self.dest))


class RefusalTests(unittest.TestCase):
    def setUp(self):
        self.src = Source()
        self.dest = os.path.join(tempfile.mkdtemp(prefix="rwdst_"), "new")

    def test_a_destination_that_already_exists_with_content_is_refused(self):
        os.makedirs(self.dest)
        with open(os.path.join(self.dest, "keep.txt"), "w") as fh:
            fh.write("x")
        code, _out, err = run(self.src, self.dest, "--root", self.src.shas[2])
        self.assertEqual(code, 2)
        self.assertIn("not empty", err)

    def test_a_root_that_is_not_in_the_history_is_refused(self):
        code, _out, _err = run(self.src, self.dest, "--root", "0" * 40)
        self.assertEqual(code, 2)

    def test_a_merge_commit_is_refused_because_a_linear_replay_would_flatten_it(self):
        git(self.src.path, "checkout", "-q", "-b", "side", self.src.shas[3])
        with open(os.path.join(self.src.path, "s.txt"), "w") as fh:
            fh.write("side\n")
        git(self.src.path, "add", "-A")
        git(self.src.path, "commit", "-q", "-m", "side", env=OWNER)
        git(self.src.path, "checkout", "-q", "main")
        git(self.src.path, "merge", "-q", "--no-ff", "-m", "merge", "side", env=OWNER)
        code, _out, err = run(self.src, self.dest, "--root", self.src.shas[2])
        self.assertEqual(code, 2)
        self.assertIn("merge", err)

    def test_a_source_that_is_not_a_repository_is_refused(self):
        code, _out, _err = run(
            tempfile.mkdtemp(prefix="rwnone_"), self.dest, "--root", "abc"
        )
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
