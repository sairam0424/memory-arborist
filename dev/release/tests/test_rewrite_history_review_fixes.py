"""Found by the independent code review of rewrite_history.py: a replay that changed bytes without saying so (a name in another
encoding, a header it did not know, a tag's time zone), tags and objects nobody asked for in the 'clean' destination, a source
repository written to because of an inherited GIT_DIR, a half-built destination after a failure, and a verify step that could
not see most of that."""

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
import rewrite_history as rh  # noqa: E402
from test_rewrite_history import BASE_ENV, OWNER, TOOL, Source, git  # noqa: E402


def raw_git(repo, *args, data=None, env=None):
    """git with bytes in and out: the review's cases are about bytes that are not UTF-8."""
    return subprocess.run(
        ["git", "-C", repo, *args],
        input=data,
        capture_output=True,
        check=True,
        env=dict(os.environ, **BASE_ENV, **(env or {})),
    ).stdout


def rewrite(source, dest, *extra, env=None):
    done = subprocess.run(
        [sys.executable, "-B", "-I", TOOL, "--source", source, "--dest", dest, *extra],
        capture_output=True,
        text=True,
        env=dict(os.environ, **(env or {})),
    )
    return done.returncode, done.stdout, done.stderr


def fresh_dest():
    return os.path.join(tempfile.mkdtemp(prefix="rwdst_"), "new")


def minus(raw, prefix):
    return b"\n".join(line for line in raw.split(b"\n") if not line.startswith(prefix))


class ByteExactTests(unittest.TestCase):
    def setUp(self):
        self.src = Source()

    def add_crafted_commit(self, edit):
        """Replace the tip with a copy whose object bytes `edit` changed (hash-object --literally keeps whatever is written)."""
        raw = raw_git(self.src.path, "cat-file", "commit", "HEAD")
        made = (
            raw_git(
                self.src.path,
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
        raw_git(self.src.path, "update-ref", "refs/heads/main", made)
        return made

    def test_an_author_name_in_another_encoding_survives_unchanged(self):
        crafted = self.add_crafted_commit(
            lambda raw: raw.replace(b"author Owner Name", b"author Jos\xe9 Dev", 1)
        )
        dest = fresh_dest()
        code, out, err = rewrite(self.src.path, dest, "--root", self.src.shas[2])
        self.assertEqual(code, 0, out + err)
        new_tip = raw_git(dest, "rev-parse", "main").decode().strip()
        self.assertEqual(
            minus(raw_git(dest, "cat-file", "commit", new_tip), b"parent "),
            minus(raw_git(self.src.path, "cat-file", "commit", crafted), b"parent "),
        )

    def test_a_header_the_tool_does_not_know_is_refused_and_nothing_is_written(self):
        cases = {
            "an extra header": lambda raw: raw.replace(
                b"\n\n", b"\nx-change-id abcdef\n\n", 1
            ),
            "an encoding header": lambda raw: raw.replace(
                b"\n\n", b"\nencoding ISO-8859-1\n\n", 1
            ),
            "a signature block": lambda raw: raw.replace(
                b"\n\n",
                b"\ngpgsig-sha256 -----BEGIN PGP SIGNATURE-----\n x\n -----END PGP SIGNATURE-----\n\n",
                1,
            ),
        }
        for label, edit in cases.items():
            with self.subTest(label):
                self.src = Source()
                self.add_crafted_commit(edit)
                dest = fresh_dest()
                code, _out, err = rewrite(
                    self.src.path, dest, "--root", self.src.shas[0]
                )
                self.assertEqual(code, 2, err)
                self.assertIn("header", err)
                self.assertFalse(os.path.exists(dest))

    def test_a_tag_keeps_its_time_zone_and_a_message_that_starts_with_a_hash(self):
        target = self.src.shas[3]
        raw = (
            "object %s\ntype commit\ntag t0\ntagger Owner Name <owner@example.org> 1700500000 -0000\n\n# heading\n\nbody\n"
            % target
        ).encode()
        tag_id = (
            raw_git(
                self.src.path,
                "hash-object",
                "-t",
                "tag",
                "-w",
                "--literally",
                "--stdin",
                data=raw,
            )
            .decode()
            .strip()
        )
        raw_git(self.src.path, "update-ref", "refs/tags/t0", tag_id)
        dest = fresh_dest()
        code, out, err = rewrite(
            self.src.path, dest, "--root", self.src.shas[2], "--tag", "t0"
        )
        self.assertEqual(code, 0, out + err)
        again = raw_git(dest, "cat-file", "tag", "t0")
        self.assertEqual(minus(again, b"object "), minus(raw, b"object "))
        self.assertIn(b"-0000", again)

    def test_a_signed_tag_is_refused(self):
        raw = (
            "object %s\ntype commit\ntag signed1\ntagger Owner Name <owner@example.org> 1700500000 +0000\n\nmsg\n-----BEGIN PGP SIGNATURE-----\nabc\n-----END PGP SIGNATURE-----\n"
            % self.src.shas[3]
        ).encode()
        tag_id = (
            raw_git(
                self.src.path,
                "hash-object",
                "-t",
                "tag",
                "-w",
                "--literally",
                "--stdin",
                data=raw,
            )
            .decode()
            .strip()
        )
        raw_git(self.src.path, "update-ref", "refs/tags/signed1", tag_id)
        dest = fresh_dest()
        code, _out, err = rewrite(
            self.src.path, dest, "--root", self.src.shas[2], "--tag", "signed1"
        )
        self.assertEqual(code, 2, err)
        self.assertIn("signed", err)
        self.assertFalse(os.path.exists(dest))


class NothingUnrequestedTests(unittest.TestCase):
    def setUp(self):
        self.src = Source()

    def test_tags_nobody_asked_for_are_not_in_the_result(self):
        # root = the original first commit: the replay is then identical, so git fetch would follow the source tags v0 and v1
        dest = fresh_dest()
        code, out, err = rewrite(self.src.path, dest, "--root", self.src.shas[0])
        self.assertEqual(code, 0, out + err)
        self.assertEqual(
            git(dest, "for-each-ref", "--format=%(refname)").split(),
            ["refs/heads/main"],
        )

    def test_a_tag_on_a_blob_or_a_tree_that_the_new_history_holds_is_not_carried_over(self):
        blob = git(self.src.path, "rev-parse", self.src.shas[3] + ":a.txt").strip()
        tree = git(self.src.path, "rev-parse", self.src.shas[3] + "^{tree}").strip()
        git(self.src.path, "tag", "-a", "blobtag", "-m", "OLD-TAG-MESSAGE", blob, env=OWNER)
        git(self.src.path, "tag", "-a", "treetag", "-m", "OLD-TAG-MESSAGE", tree, env=OWNER)
        dest = fresh_dest()
        code, out, err = rewrite(self.src.path, dest, "--root", self.src.shas[2])
        self.assertEqual(code, 0, out + err)
        self.assertEqual(
            git(dest, "for-each-ref", "--format=%(refname)").split(),
            ["refs/heads/main"],
        )

    def test_the_result_holds_no_object_that_a_ref_does_not_reach_and_its_work_tree_is_clean(
        self,
    ):
        dest = fresh_dest()
        self.assertEqual(
            rewrite(self.src.path, dest, "--root", self.src.shas[2], "--tag", "v1")[0],
            0,
        )
        reached = {
            line.split()[0]
            for line in git(dest, "rev-list", "--objects", "--all").splitlines()
            if line
        }
        present = {
            line.split()[0]
            for line in git(
                dest, "cat-file", "--batch-all-objects", "--batch-check"
            ).splitlines()
            if line
        }
        self.assertEqual(present, reached)
        self.assertEqual(git(dest, "status", "--porcelain"), "")
        self.assertEqual(git(dest, "symbolic-ref", "HEAD").strip(), "refs/heads/main")


class EnvironmentTests(unittest.TestCase):
    def test_an_inherited_git_dir_does_not_make_the_tool_write_into_the_source(self):
        src, decoy = Source(), Source()
        before = git(decoy.path, "for-each-ref") + git(
            decoy.path, "count-objects", "-v"
        )
        dest = fresh_dest()
        code, out, err = rewrite(
            src.path,
            dest,
            "--root",
            src.shas[2],
            env={"GIT_DIR": os.path.join(decoy.path, ".git")},
        )
        self.assertEqual(code, 0, out + err)
        self.assertEqual(
            before,
            git(decoy.path, "for-each-ref") + git(decoy.path, "count-objects", "-v"),
        )
        self.assertEqual(git(dest, "rev-list", "--count", "main").strip(), "3")

    def test_both_tools_remove_the_same_variables(self):
        self.assertEqual(rh.GIT_SCRUB, ch.GIT_SCRUB)

    def test_the_source_is_read_with_replace_refs_and_grafts_off(self):
        env = rh.git_env()
        self.assertEqual(
            (env["GIT_NO_REPLACE_OBJECTS"], env["GIT_GRAFT_FILE"]), ("1", os.devnull)
        )

    def test_a_replace_ref_in_the_source_does_not_change_what_is_replayed(self):
        src = Source()
        stand_in = git(
            src.path,
            "commit-tree",
            src.shas[3] + "^{tree}",
            "-p",
            src.shas[3],
            "-m",
            "stand-in",
            env=OWNER,
        ).strip()
        git(src.path, "replace", src.shas[4], stand_in)
        dest = fresh_dest()
        code, out, err = rewrite(src.path, dest, "--root", src.shas[2])
        self.assertEqual(code, 0, out + err)
        self.assertEqual(
            git(dest, "rev-parse", "main^{tree}").strip(),
            git(
                src.path,
                "rev-parse",
                src.shas[4] + "^{tree}",
                env={"GIT_NO_REPLACE_OBJECTS": "1"},
            ).strip(),
        )


class DestinationTests(unittest.TestCase):
    def setUp(self):
        self.src = Source()

    def test_an_existing_empty_destination_folder_is_used(self):
        dest = fresh_dest()
        os.makedirs(dest)
        code, out, err = rewrite(self.src.path, dest, "--root", self.src.shas[2])
        self.assertEqual(code, 0, out + err)
        self.assertEqual(git(dest, "rev-list", "--count", "main").strip(), "3")

    def test_bad_names_and_duplicates_are_refused_before_anything_is_created(self):
        cases = (
            ("--tag", "v1", "--tag", "v1"),
            ("--tag", "v1", "--branch", "main=v1"),
            ("--tag", "v1", "--branch", "bad..name=v1"),
            ("--tag", "v1", "--branch", "a=v1", "--branch", "a=v1"),
            ("--tag", "bad..tag"),
        )
        for extra in cases:
            with self.subTest(extra):
                dest = fresh_dest()
                code, _out, err = rewrite(
                    self.src.path, dest, "--root", self.src.shas[2], *extra
                )
                self.assertEqual(code, 2, err)
                self.assertFalse(os.path.exists(dest))
                self.assertEqual(
                    [
                        n
                        for n in os.listdir(os.path.dirname(dest))
                        if n.startswith(".rewrite-")
                    ],
                    [],
                )

    def test_a_failed_verification_leaves_no_destination_and_no_staging_folder_and_a_rerun_works(
        self,
    ):
        dest = fresh_dest()
        argv = ["--source", self.src.path, "--dest", dest, "--root", self.src.shas[2]]
        with mock.patch.object(rh, "verify", side_effect=RuntimeError("simulated")):
            self.assertEqual(rh.main(argv), 1)
        self.assertFalse(os.path.exists(dest))
        self.assertEqual(
            [n for n in os.listdir(os.path.dirname(dest)) if n.startswith(".rewrite-")],
            [],
        )
        self.assertEqual(rh.main(argv), 0)

    def test_a_failure_in_the_middle_leaves_nothing_behind_either(self):
        dest = fresh_dest()
        with mock.patch.object(
            rh, "create_tags", side_effect=rh.UsageError("simulated")
        ):
            code = rh.main(
                [
                    "--source",
                    self.src.path,
                    "--dest",
                    dest,
                    "--root",
                    self.src.shas[2],
                    "--tag",
                    "v1",
                ]
            )
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(dest))
        self.assertEqual(
            [n for n in os.listdir(os.path.dirname(dest)) if n.startswith(".rewrite-")],
            [],
        )


class RefTests(unittest.TestCase):
    def test_a_branch_other_than_main_can_be_the_one_rewritten(self):
        src = Source()
        git(src.path, "branch", "develop", src.shas[3])
        dest = fresh_dest()
        code, out, err = rewrite(
            src.path, dest, "--root", src.shas[2], "--ref", "develop"
        )
        self.assertEqual(code, 0, out + err)
        self.assertIn("commits=2", out)
        self.assertEqual(git(dest, "rev-list", "--count", "main").strip(), "2")

    def test_a_source_that_uses_sha256_object_ids(self):
        path = tempfile.mkdtemp(prefix="rwsha256_")
        if subprocess.run(
            ["git", "init", "-q", "-b", "main", "--object-format=sha256", path],
            capture_output=True,
            env=dict(os.environ, **BASE_ENV),
        ).returncode:
            self.skipTest("this git cannot create a sha256 repository")
        shas = []
        for i in range(3):
            with open(os.path.join(path, "a.txt"), "w", encoding="utf-8") as fh:
                fh.write("v%d\n" % i)
            git(path, "add", "-A")
            git(path, "commit", "-q", "-m", "c%d" % i, env=OWNER)
            shas.append(git(path, "rev-parse", "HEAD").strip())
        dest = fresh_dest()
        code, out, err = rewrite(path, dest, "--root", shas[1])
        self.assertEqual(code, 0, out + err)
        self.assertEqual(
            git(dest, "rev-parse", "--show-object-format").strip(), "sha256"
        )


class TagRefusalTests(unittest.TestCase):
    def setUp(self):
        self.src = Source()

    def refused(self, name):
        dest = fresh_dest()
        code, _out, err = rewrite(self.src.path, dest, "--root", self.src.shas[2], "--tag", name)
        self.assertEqual(code, 2, err)
        self.assertFalse(os.path.exists(dest))
        return err

    def test_a_lightweight_tag_is_refused(self):
        git(self.src.path, "tag", "light", self.src.shas[3])
        self.assertIn("annotated", self.refused("light"))

    def test_a_tag_on_a_tree_is_refused(self):
        git(self.src.path, "tag", "-a", "treetag", "-m", "x", self.src.shas[3] + "^{tree}", env=OWNER)
        self.assertIn("does not point at a commit", self.refused("treetag"))


class VerifyContentsTests(unittest.TestCase):
    """verify_contents() is the last look at the finished repository: it must be able to fail on each thing it promises."""

    def setUp(self):
        self.src = Source()
        self.dest = fresh_dest()
        code, out, err = rewrite(self.src.path, self.dest, "--root", self.src.shas[2], "--tag", "v1")
        self.assertEqual(code, 0, out + err)
        new = git(self.dest, "rev-list", "--reverse", "main").split()
        self.mapping = dict(zip(self.src.shas[2:], new))

    def check(self):
        rh.verify_contents(self.dest, self.src.path, {"v1": 1}, [], self.mapping)

    def test_the_finished_repository_passes(self):
        self.check()

    def test_an_extra_ref_is_caught(self):
        git(self.dest, "branch", "extra")
        with self.assertRaises(RuntimeError):
            self.check()

    def test_a_tag_that_differs_from_its_source_is_caught(self):
        tag = git(self.dest, "cat-file", "tag", "v1").replace("release one", "release two")
        edited = git(self.dest, "hash-object", "-t", "tag", "-w", "--stdin", data=tag).strip()
        git(self.dest, "update-ref", "refs/tags/v1", edited)
        with self.assertRaises(RuntimeError):
            self.check()

    def test_a_tag_whose_message_differs_from_the_source_alone_is_caught(self):
        # a second source with the same commits but another message on v1; the destination's own objects all stay reachable
        other = Source()
        git(
            other.path,
            "tag",
            "-f",
            "-a",
            "v1",
            "-m",
            "a different message",
            other.shas[3],
            env=dict(OWNER, GIT_COMMITTER_DATE="1700500000 +0530"),
        )
        with self.assertRaises(RuntimeError) as caught:
            rh.verify_contents(self.dest, other.path, {"v1": 1}, [], self.mapping)
        self.assertIn("tag v1 differs", str(caught.exception))

    def test_a_tag_that_points_at_another_commit_than_the_mapping_says_is_caught(self):
        wrong = dict(self.mapping)
        wrong[self.src.shas[3]] = self.mapping[self.src.shas[2]]
        with self.assertRaises(RuntimeError) as caught:
            rh.verify_contents(self.dest, self.src.path, {"v1": 1}, [], wrong)
        self.assertIn("tag v1 differs", str(caught.exception))

    def test_an_object_that_no_ref_reaches_is_caught(self):
        git(self.dest, "hash-object", "-w", "--stdin", data="unreferenced\n")
        with self.assertRaises(RuntimeError):
            self.check()


class VerifyCanFailTests(unittest.TestCase):
    """verify() must see a difference in the author, the committer or the message alone, not only in the tree or the parent.

    Two sibling commits share a parent and a tree, so the replay mapping {tip: sibling} passes every check except the one that
    compares the commit objects themselves."""

    def setUp(self):
        self.path = tempfile.mkdtemp(prefix="rwverify_")
        git(self.path, "init", "-q", "-b", "main")
        with open(os.path.join(self.path, "a.txt"), "w", encoding="utf-8") as fh:
            fh.write("x\n")
        git(self.path, "add", "-A")
        git(self.path, "commit", "-q", "-m", "root", env=OWNER)
        self.root = git(self.path, "rev-parse", "HEAD").strip()

    def sibling(self, name, message="same message", **env):
        git(self.path, "checkout", "-q", "-b", name, self.root)
        git(self.path, "commit", "-q", "--allow-empty", "-m", message, env=dict(OWNER, **env))
        return git(self.path, "rev-parse", "HEAD").strip()

    def assert_a_sibling_is_caught_as_the_replay(self, **difference):
        original, impostor = self.sibling("one"), self.sibling("two", **difference)
        commits = [rh.read_commit(self.path, c) for c in (self.root, original)]
        mapping = {self.root: self.root, original: impostor}
        with self.assertRaises(RuntimeError) as caught:
            rh.verify(self.path, commits, mapping)
        self.assertIn("differs", str(caught.exception))

    def test_an_identical_sibling_would_pass_so_the_cases_below_prove_the_comparison(self):
        original = self.sibling("one")
        commits = [rh.read_commit(self.path, c) for c in (self.root, original)]
        # the same commit mapped to itself passes; the chain check needs `main` to hold both commits
        git(self.path, "checkout", "-q", "-B", "main", original)
        rh.verify(self.path, commits, {self.root: self.root, original: original})

    def test_a_replay_that_hangs_on_the_wrong_parent_is_caught(self):
        """Two commits identical but for their parent: only the chain check can tell the replay from the original."""
        tree = git(self.path, "rev-parse", "HEAD^{tree}").strip()
        fixed = dict(OWNER, GIT_AUTHOR_DATE="1700000000 +0000", GIT_COMMITTER_DATE="1700000000 +0000")
        other_root = git(self.path, "commit-tree", tree, "-m", "other root", env=fixed).strip()
        original = git(self.path, "commit-tree", tree, "-p", self.root, "-m", "child", env=fixed).strip()
        impostor = git(self.path, "commit-tree", tree, "-p", other_root, "-m", "child", env=fixed).strip()
        git(self.path, "update-ref", "refs/heads/main", impostor)  # main: one root, two commits, as verify expects
        commits = [rh.read_commit(self.path, self.root), rh.read_commit(self.path, original)]
        with self.assertRaises(RuntimeError) as caught:
            rh.verify(self.path, commits, {self.root: self.root, original: impostor})
        self.assertIn("wrong parent", str(caught.exception))

    def test_a_different_author_alone_is_caught(self):
        self.assert_a_sibling_is_caught_as_the_replay(GIT_AUTHOR_NAME="Someone Else")

    def test_a_different_committer_alone_is_caught(self):
        self.assert_a_sibling_is_caught_as_the_replay(GIT_COMMITTER_EMAIL="other@example.org")

    def test_a_different_message_alone_is_caught(self):
        self.assert_a_sibling_is_caught_as_the_replay(message="another message")


if __name__ == "__main__":
    unittest.main()
