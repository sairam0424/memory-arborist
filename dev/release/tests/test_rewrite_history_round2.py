"""Found by a second, fresh read-only reviewer of rewrite_history.py: a --ref that git read as something else (a tag of the same name, an
option, a file), a destination inside the source, a run killed by a signal that left a full copy of the source behind, reflogs that
record the operator's name, and OS errors that ended in a traceback."""

import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import rewrite_history as rh  # noqa: E402
from test_rewrite_history import BASE_ENV, OWNER, TOOL, Source, git  # noqa: E402
from test_rewrite_history_review_fixes import fresh_dest, rewrite  # noqa: E402


class RefResolutionTests(unittest.TestCase):
    def setUp(self):
        self.src = Source()

    def test_a_tag_with_the_name_of_the_branch_does_not_change_the_history_that_is_replayed(
        self,
    ):
        git(self.src.path, "tag", "main", self.src.shas[2], env=OWNER)
        dest = fresh_dest()
        code, out, err = rewrite(self.src.path, dest, "--root", self.src.shas[1])
        self.assertEqual(code, 0, out + err)
        self.assertIn("commits=4", out)
        self.assertEqual(git(dest, "rev-list", "--count", "main").strip(), "4")

    def test_a_ref_that_is_an_option_or_a_range_is_refused(self):
        for ref in ("--all", "--branches", "main...main", "HEAD~1"):
            with self.subTest(ref):
                dest = fresh_dest()
                code, _out, err = rewrite(
                    self.src.path, dest, "--ref=" + ref, "--root", self.src.shas[1]
                )
                self.assertEqual(code, 2, err)
                self.assertFalse(os.path.exists(dest))

    def test_a_file_named_like_the_branch_in_the_work_tree_does_not_break_the_default(
        self,
    ):
        with open(os.path.join(self.src.path, "main"), "w", encoding="utf-8") as fh:
            fh.write("x\n")
        dest = fresh_dest()
        code, out, err = rewrite(self.src.path, dest, "--root", self.src.shas[2])
        self.assertEqual(code, 0, out + err)
        self.assertIn("commits=3", out)

    def test_a_tag_is_not_a_branch(self):
        dest = fresh_dest()
        code, _out, err = rewrite(
            self.src.path, dest, "--ref", "v1", "--root", self.src.shas[1]
        )
        self.assertEqual(code, 2, err)
        self.assertIn("branch", err)


class DestinationInsideTheSourceTests(unittest.TestCase):
    def test_a_destination_inside_the_work_tree_or_the_git_folder_is_refused_and_nothing_is_written(
        self,
    ):
        src = Source()
        before = git(src.path, "status", "--porcelain") + git(
            src.path, "count-objects", "-v"
        )
        for inside in ("public/site", ".git/pub", "a/b/c"):
            with self.subTest(inside):
                code, _out, err = rewrite(
                    src.path, os.path.join(src.path, inside), "--root", src.shas[2]
                )
                self.assertEqual(code, 2, err)
                self.assertIn("inside the source", err)
        self.assertEqual(
            git(src.path, "status", "--porcelain")
            + git(src.path, "count-objects", "-v"),
            before,
        )
        self.assertFalse(os.path.exists(os.path.join(src.path, "public")))
        self.assertFalse(os.path.exists(os.path.join(src.path, ".git", "pub")))

    def test_a_destination_inside_a_bare_source_is_refused(self):
        src = Source()
        bare = os.path.join(tempfile.mkdtemp(prefix="rwbare_"), "b.git")
        subprocess.run(
            ["git", "clone", "-q", "--bare", src.path, bare],
            check=True,
            capture_output=True,
            env=dict(os.environ, **BASE_ENV),
        )
        code, _out, err = rewrite(
            bare, os.path.join(bare, "out"), "--root", src.shas[2]
        )
        self.assertEqual(code, 2, err)
        self.assertFalse(os.path.exists(os.path.join(bare, "out")))

    def test_a_destination_that_only_shares_a_name_prefix_with_the_source_is_fine(self):
        src = Source()
        code, out, err = rewrite(src.path, src.path + "-copy", "--root", src.shas[2])
        self.addCleanup(shutil.rmtree, src.path + "-copy", True)
        self.assertEqual(code, 0, out + err)


class InterruptionTests(unittest.TestCase):
    """A run that is killed must leave neither the staging folder nor the scratch clone, which holds every dropped commit."""

    def start_slow_run(self):
        self.src = Source()
        parent = tempfile.mkdtemp(prefix="rwsig_")
        self.dest = os.path.join(parent, "new")
        self.tmp = tempfile.mkdtemp(prefix="rwtmp_")
        shim_dir = tempfile.mkdtemp(prefix="rwshim_")
        shim = os.path.join(shim_dir, "git")
        with open(shim, "w", encoding="utf-8") as fh:
            fh.write(
                '#!/bin/sh\ncase " $* " in *" reset "*) exec sleep 20;; esac\nexec %s "$@"\n'
                % shutil.which("git")
            )
        os.chmod(shim, stat.S_IRWXU)
        env = dict(
            os.environ,
            PATH=shim_dir + os.pathsep + os.environ["PATH"],
            TMPDIR=self.tmp,
            **BASE_ENV,
        )
        proc = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-I",
                TOOL,
                "--source",
                self.src.path,
                "--dest",
                self.dest,
                "--root",
                self.src.shas[2],
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        for _ in range(600):
            if any(name.startswith(".rewrite-") for name in os.listdir(parent)) and any(
                os.path.isdir(os.path.join(self.tmp, n)) for n in os.listdir(self.tmp)
            ):
                time.sleep(1.0)  # past the clone and the replay, into the slow reset
                return proc, parent
            time.sleep(0.1)
        proc.kill()
        self.fail("the run never created its staging folder")

    def leftovers(self, parent):
        return [n for n in os.listdir(parent) if n.startswith(".rewrite-")] + [
            n for n in os.listdir(self.tmp) if n.startswith("rewrite_")
        ]

    def test_a_terminate_or_hangup_signal_cleans_up(self):
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            with self.subTest(sig.name):
                proc, parent = self.start_slow_run()
                proc.send_signal(sig)
                _out, err = proc.communicate(timeout=60)
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(self.leftovers(parent), [], err)
                self.assertFalse(os.path.exists(self.dest))
                self.assertNotIn("Traceback", err)


class QuietResultTests(unittest.TestCase):
    def setUp(self):
        self.src = Source()
        self.dest = fresh_dest()
        code, out, err = rewrite(
            self.src.path, self.dest, "--root", self.src.shas[2], "--tag", "v1"
        )
        self.assertEqual(code, 0, out + err)

    def test_the_destination_holds_no_reflog_and_no_leftover_of_the_build(self):
        gitdir = os.path.join(self.dest, ".git")
        self.assertFalse(os.path.exists(os.path.join(gitdir, "logs")))
        self.assertFalse(os.path.exists(os.path.join(gitdir, "ORIG_HEAD")))
        self.assertFalse(os.path.exists(os.path.join(gitdir, "refs", "rewritten")))

    def test_the_destination_folder_has_the_mode_a_new_folder_would_have(self):
        mask = os.umask(0)
        os.umask(mask)
        self.assertEqual(stat.S_IMODE(os.stat(self.dest).st_mode), 0o777 & ~mask)


class OsErrorTests(unittest.TestCase):
    def setUp(self):
        self.src = Source()

    def test_a_destination_below_a_file_is_a_message_and_exit_two(self):
        blocker = os.path.join(tempfile.mkdtemp(prefix="rwblock_"), "file")
        with open(blocker, "w", encoding="utf-8") as fh:
            fh.write("x")
        code, _out, err = rewrite(
            self.src.path, os.path.join(blocker, "x"), "--root", self.src.shas[2]
        )
        self.assertEqual(code, 2, err)
        self.assertNotIn("Traceback", err)

    def test_an_unwritable_parent_is_a_message_and_exit_two(self):
        if os.geteuid() == 0:
            self.skipTest("root can write anywhere")
        parent = tempfile.mkdtemp(prefix="rwro_")
        os.chmod(parent, stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(os.chmod, parent, stat.S_IRWXU)
        code, _out, err = rewrite(
            self.src.path, os.path.join(parent, "dest"), "--root", self.src.shas[2]
        )
        self.assertEqual(code, 2, err)
        self.assertNotIn("Traceback", err)

    def test_a_late_failure_removes_the_parent_folders_it_created(self):
        base = tempfile.mkdtemp(prefix="rwdeep_")
        dest = os.path.join(base, "deep", "er", "dst")
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
        self.assertEqual(os.listdir(base), [])

    def test_a_closed_output_pipe_does_not_turn_a_finished_run_into_a_failure(self):
        dest = fresh_dest()
        with mock.patch("builtins.print", side_effect=BrokenPipeError()):
            code = rh.main(
                ["--source", self.src.path, "--dest", dest, "--root", self.src.shas[2]]
            )
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isdir(os.path.join(dest, ".git")))


class InheritedGitEnvironmentTests(unittest.TestCase):
    """GIT_TEMPLATE_DIR and GIT_CONFIG_* are not in the GIT_DIR family, yet they copy files and run hooks inside the 'clean' destination."""

    def make_template(self):
        base = tempfile.mkdtemp(prefix="rwtpl_")
        self.marker = os.path.join(base, "marker")
        os.makedirs(os.path.join(base, "tpl", "hooks"))
        with open(os.path.join(base, "tpl", "PAYLOAD.txt"), "w", encoding="utf-8") as fh:
            fh.write("payload\n")
        hook = os.path.join(base, "tpl", "hooks", "reference-transaction")
        with open(hook, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\necho ran >> %s\n" % self.marker)
        os.chmod(hook, stat.S_IRWXU)
        return base

    def test_a_template_directory_is_not_copied_into_the_destination_and_no_hook_runs(self):
        src, base = Source(), self.make_template()
        dest = fresh_dest()
        code, out, err = rewrite(src.path, dest, "--root", src.shas[2], env={"GIT_TEMPLATE_DIR": os.path.join(base, "tpl")})
        self.assertEqual(code, 0, out + err)
        self.assertFalse(os.path.exists(os.path.join(dest, ".git", "PAYLOAD.txt")))
        self.assertFalse(os.path.exists(self.marker))

    def test_config_given_through_the_environment_does_not_run_hooks_or_write_reflogs(self):
        src, base = Source(), self.make_template()
        dest = fresh_dest()
        env = {
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": os.path.join(base, "tpl", "hooks"),
            "GIT_CONFIG_KEY_1": "core.logAllRefUpdates",
            "GIT_CONFIG_VALUE_1": "always",
            "GIT_CONFIG_PARAMETERS": "'core.hooksPath'='%s'" % os.path.join(base, "tpl", "hooks"),
        }
        code, out, err = rewrite(src.path, dest, "--root", src.shas[2], env=env)
        self.assertEqual(code, 0, out + err)
        self.assertFalse(os.path.exists(self.marker))
        self.assertFalse(os.path.exists(os.path.join(dest, ".git", "logs")))

    def test_the_variables_are_in_the_removed_list(self):
        for name in ("GIT_TEMPLATE_DIR", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"):
            self.assertIn(name, rh.GIT_SCRUB)


class MergeOutsideTheKeptRangeTests(unittest.TestCase):
    def build(self):
        path = tempfile.mkdtemp(prefix="rwmerge_")
        git(path, "init", "-q", "-b", "main")

        def commit(name, text):
            with open(os.path.join(path, name), "w", encoding="utf-8") as fh:
                fh.write(text)
            git(path, "add", "-A")
            git(path, "commit", "-q", "-m", "c " + name, env=OWNER)
            return git(path, "rev-parse", "HEAD").strip()

        first = commit("a.txt", "1\n")
        git(path, "checkout", "-q", "-b", "side")
        commit("s.txt", "side\n")
        git(path, "checkout", "-q", "main")
        git(path, "merge", "-q", "--no-ff", "-m", "merge side", "side", env=OWNER)
        merge = git(path, "rev-parse", "HEAD").strip()
        after = [commit("b.txt", "2\n"), commit("c.txt", "3\n")]
        return path, first, merge, after

    def test_a_merge_before_the_new_root_is_dropped_with_everything_else_before_it(self):
        path, _first, _merge, after = self.build()
        dest = fresh_dest()
        code, out, err = rewrite(path, dest, "--root", after[0])
        self.assertEqual(code, 0, out + err)
        self.assertIn("commits=2", out)
        self.assertEqual(git(dest, "rev-list", "--count", "main").strip(), "2")

    def test_a_merge_as_the_new_root_is_accepted_and_a_merge_after_it_is_not(self):
        path, _first, merge, _after = self.build()
        dest = fresh_dest()
        code, out, err = rewrite(path, dest, "--root", merge)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(git(dest, "rev-list", "--count", "main").strip(), "3")

    def test_a_merge_inside_the_kept_range_is_still_refused(self):
        path, first, _merge, _after = self.build()
        code, _out, err = rewrite(path, fresh_dest(), "--root", first)
        self.assertEqual(code, 2, err)
        self.assertIn("merge", err)


class NamesAndPrefixesTests(unittest.TestCase):
    def setUp(self):
        self.src = Source()

    def test_a_root_prefix_shorter_than_seven_characters_is_refused(self):
        code, _out, err = rewrite(self.src.path, fresh_dest(), "--root", self.src.shas[2][:5])
        self.assertEqual(code, 2, err)
        self.assertIn("at least 7", err)

    def test_a_tag_or_branch_name_must_match_exactly_and_main_in_any_case_is_main(self):
        for extra in (("--tag", "V1"), ("--tag", "v1", "--branch", "Main=v1")):
            with self.subTest(extra):
                dest = fresh_dest()
                code, _out, err = rewrite(self.src.path, dest, "--root", self.src.shas[2], *extra)
                self.assertEqual(code, 2, err)
                self.assertFalse(os.path.exists(dest))

    def test_a_destination_that_is_a_symbolic_link_is_refused_before_any_work(self):
        target = tempfile.mkdtemp(prefix="rwlink_")
        link = os.path.join(tempfile.mkdtemp(prefix="rwlinkp_"), "dest")
        os.symlink(target, link)
        code, _out, err = rewrite(self.src.path, link, "--root", self.src.shas[2])
        self.assertEqual(code, 2, err)
        self.assertIn("symbolic link", err)
        self.assertEqual(os.listdir(target), [])

    def test_a_sha256_source_accepts_a_root_prefix_longer_than_forty_characters(self):
        path = tempfile.mkdtemp(prefix="rwsha256_")
        if subprocess.run(["git", "init", "-q", "-b", "main", "--object-format=sha256", path], capture_output=True, env=dict(os.environ, **BASE_ENV)).returncode:
            self.skipTest("this git cannot create a sha256 repository")
        shas = []
        for i in range(3):
            with open(os.path.join(path, "a.txt"), "w", encoding="utf-8") as fh:
                fh.write("v%d\n" % i)
            git(path, "add", "-A")
            git(path, "commit", "-q", "-m", "c%d" % i, env=OWNER)
            shas.append(git(path, "rev-parse", "HEAD").strip())
        code, out, err = rewrite(path, fresh_dest(), "--root", shas[1][:50])
        self.assertEqual(code, 0, out + err)


if __name__ == "__main__":
    unittest.main()
