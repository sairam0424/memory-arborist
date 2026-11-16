"""Doctor findings from the first tester: the doctor must check what the paid steps will check, say plainly why a probe failed,
keep one unreadable file from hiding the rest, and keep custom paths and cloud ids out of output that gets pasted into issues."""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ENGINE, "tests"))
sys.path.insert(0, HERE)
import memtest_helpers as th  # noqa: E402
from test_doctor_checks import (  # noqa: E402
    WithClaudeOnPath,
    fake_lane_output,
    probe_for,
    statuses,
    valid_question,
)

sys.path.insert(0, ENGINE)
sys.path.insert(0, os.path.dirname(HERE))
import memory_common as mc  # noqa: E402
import memory_doctor as doc  # noqa: E402

IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


def by_name(checks):
    return {check.name: check for check in checks}


class OpsFixture(unittest.TestCase):
    def setUp(self):
        self.mem = th.make_dir(
            {
                "MEMORY.md": "# Index\n- [A](a.md) a\n- [B](b.md) b\n",
                "a.md": th.note("a"),
            }
        )
        self.root = tempfile.mkdtemp(prefix="docops_")
        self.ops = mc.ops_dir_for(self.mem, self.root)
        os.makedirs(self.ops, mode=0o700)

    def first_run(self):
        return by_name(doc.check_first_run(self.ops, self.mem))


class PinnedTests(OpsFixture):
    def test_a_byte_order_mark_from_an_editor_does_not_hide_the_first_line(self):
        with open(os.path.join(self.ops, "pinned.txt"), "wb") as fh:
            fh.write(b"\xef\xbb\xbf- [A](a.md) a\n- [B](b.md) b\n")
        check = self.first_run()["pinned"]
        self.assertEqual(check.status, doc.OK)
        self.assertIn("2 of them match", check.detail)

    def test_the_fix_line_does_not_send_the_user_to_an_empty_location(self):
        th.write(
            os.path.join(self.ops, "pinned.txt"), "- [A](a.md) written differently\n"
        )
        check = self.first_run()["pinned"]
        self.assertEqual(check.status, doc.WARN)
        self.assertNotIn("empty pinned.txt location", check.fix)
        self.assertIn("never replaces an existing file", check.fix)


class QuestionFileTests(OpsFixture):
    def write(self, text):
        os.makedirs(os.path.join(self.ops, "recall"), mode=0o700, exist_ok=True)
        th.write(os.path.join(self.ops, "recall", "questions.jsonl"), text)

    def lines(self, n):
        return "".join(json.dumps(valid_question(i)) + "\n" for i in range(n))

    def test_a_file_of_the_right_length_that_the_judge_cannot_load_is_not_ok(self):
        for name, text in (
            ("not json", self.lines(24) + "this is not json\n"),
            ("missing keys", self.lines(24) + '{"id": "q99"}\n'),
            ("duplicate id", self.lines(24) + json.dumps(valid_question(3)) + "\n"),
            (
                "bad regex",
                self.lines(24)
                + json.dumps(dict(valid_question(99), must=["("]))
                + "\n",
            ),
        ):
            with self.subTest(name):
                self.write(text)
                check = self.first_run()["questions"]
                self.assertEqual(check.status, doc.WARN)
                self.assertIn("does not load", check.detail)

    def test_a_loadable_set_of_the_right_size_is_ok(self):
        self.write(self.lines(25))
        check = self.first_run()["questions"]
        self.assertEqual((check.status, check.detail), (doc.OK, "25 recall questions"))

    def test_a_file_that_is_not_utf8_or_is_deeply_nested_is_reported_not_a_crash(self):
        os.makedirs(os.path.join(self.ops, "recall"), mode=0o700)
        with open(os.path.join(self.ops, "recall", "questions.jsonl"), "wb") as fh:
            fh.write(b"\xff\xfe\xfa not utf-8\n")
        self.assertIn(self.first_run()["questions"].status, (doc.WARN, doc.FAIL))
        self.write("[" * 300_000 + "\n")
        self.assertIn(self.first_run()["questions"].status, (doc.WARN, doc.FAIL))


class OneUnreadableFileTests(OpsFixture):
    def test_a_check_that_raises_is_one_failure_and_the_others_still_report(self):
        th.write(os.path.join(self.ops, "pinned.txt"), "- [A](a.md) a\n")
        boom = PermissionError(13, "Permission denied")
        with mock.patch.object(doc, "_evidence_state", side_effect=boom):
            found = self.first_run()
        self.assertEqual(found["evidence"].status, doc.FAIL)
        self.assertIn("Permission denied", found["evidence"].detail)
        for name in ("pinned", "questions", "snapshots"):
            self.assertNotEqual(found[name].status, doc.FAIL, name)
            self.assertIn(name, found)

    @unittest.skipIf(IS_ROOT, "root ignores file permissions")
    def test_a_real_unreadable_evidence_folder_leaves_the_other_checks_alone(self):
        evidence = os.path.join(self.ops, "evidence")
        os.makedirs(evidence, mode=0o700)
        os.chmod(evidence, 0)
        self.addCleanup(os.chmod, evidence, 0o700)
        found = self.first_run()
        self.assertEqual(found["evidence"].status, doc.FAIL)
        self.assertIn("pinned", found)
        self.assertIn("snapshots", found)

    def test_a_memory_folder_that_cannot_be_listed_does_not_hide_the_data_root_check(
        self,
    ):
        with mock.patch.object(
            doc, "_count_notes", side_effect=PermissionError(13, "Permission denied")
        ):
            found = by_name(doc.check_dirs(self.mem, self.root, self.ops))
        self.assertEqual(found["memory"].status, doc.FAIL)
        self.assertIn("data-root", found)


class DataRootTests(OpsFixture):
    def check(self, ops_root):
        return doc._data_root_check(
            self.mem, ops_root, mc.ops_dir_for(self.mem, ops_root)
        )

    def test_a_file_where_the_folder_would_go_is_a_failure(self):
        blocker = os.path.join(tempfile.mkdtemp(prefix="docblock_"), "afile")
        th.write(blocker, "x")
        for root in (blocker, os.path.join(blocker, "data")):
            with self.subTest(root):
                check = self.check(root)
                self.assertEqual(check.status, doc.FAIL)
                self.assertIn("a file is in the way", check.detail)

    @unittest.skipIf(IS_ROOT, "root ignores file permissions")
    def test_a_read_only_parent_is_a_failure(self):
        parent = tempfile.mkdtemp(prefix="docro_")
        os.chmod(parent, 0o500)
        self.addCleanup(os.chmod, parent, 0o700)
        check = self.check(os.path.join(parent, "data"))
        self.assertEqual(check.status, doc.FAIL)
        self.assertIn("not writable", check.detail)

    def test_less_than_the_snapshot_minimum_free_is_a_failure_and_enough_is_ok(self):
        with mock.patch.object(mc, "free_bytes", return_value=1024**3):
            low = self.check(self.root)
        self.assertEqual(low.status, doc.FAIL)
        self.assertIn("only 1.0 GB free", low.detail)
        self.assertIn("2 GB", low.detail)
        with mock.patch.object(mc, "free_bytes", return_value=50 * 1024**3):
            self.assertEqual(self.check(self.root).status, doc.OK)


class ProbeFailureWordsTests(WithClaudeOnPath):
    def test_a_version_probe_that_timed_out_says_so_and_does_not_invent_an_exit_code(
        self,
    ):
        def probe(argv, env):
            return 124, "", "timed out"

        detail = doc.check_claude(probe, self.environ)[0].detail
        self.assertIn(
            "did not answer within %d seconds" % doc.PROBE_TIMEOUT_SECONDS, detail
        )
        self.assertNotIn("124", detail)

    def test_a_probe_that_could_not_start_says_so(self):
        def probe(argv, env):
            return 127, "", "Permission denied"

        detail = doc.check_claude(probe, self.environ)[0].detail
        self.assertIn("could not be started (Permission denied)", detail)
        self.assertNotIn("127", detail)

    def test_an_ordinary_nonzero_exit_still_reports_the_exit_code(self):
        detail = doc.check_claude(probe_for(version="", version_code=3), self.environ)[
            0
        ].detail
        self.assertIn("gave no version (exit 3)", detail)

    def test_a_help_probe_that_timed_out_is_a_warning_in_words(self):
        def probe(argv, env):
            return (
                (0, "2.1.325 (Claude Code)", "")
                if argv[1] == "--version"
                else (124, "", "timed out")
            )

        checks = doc.check_claude(probe, self.environ)
        flags = by_name(checks)["flags"]
        self.assertEqual(flags.status, doc.WARN)
        self.assertIn("did not answer within", flags.detail)
        self.assertNotIn("124", flags.detail)


class CustomPathTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="dochome_")
        patcher = mock.patch.dict(os.environ, {"HOME": self.home}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("CLAUDE_CONFIG_DIR", "MEMORY_ARBORIST_CLAUDE_HOME"):
            os.environ.pop(name, None)

    def test_the_standard_places_are_shown_and_every_other_path_is_a_placeholder(self):
        standard = os.path.join(
            self.home, ".claude", "projects", "-Users-alice-acme", "memory"
        )
        self.assertEqual(
            doc.memory_label(standard), "~/.claude/projects/<project>/memory"
        )
        self.assertEqual(doc.memory_label("/srv/client-acme/notes"), doc.CUSTOM_PATH)
        self.assertEqual(
            doc.memory_label(
                os.path.join(self.home, ".claude", "projects", "x", "other")
            ),
            doc.CUSTOM_PATH,
        )
        self.assertEqual(
            doc.data_root_label(os.path.join(self.home, ".claude", "memory-arborist")),
            "~/.claude/memory-arborist",
        )
        self.assertEqual(doc.data_root_label("/srv/client-acme/data"), doc.CUSTOM_PATH)

    def test_a_custom_directory_never_reaches_the_output(self):
        mem = th.make_dir({"MEMORY.md": "# Index\n", "a.md": th.note("a")})
        root = tempfile.mkdtemp(prefix="docacme_")
        text = " ".join(
            c.detail + " " + c.fix
            for c in doc.check_dirs(mem, root, mc.ops_dir_for(mem, root))
        )
        for path in (mem, root):
            self.assertNotIn(path, text)
        self.assertIn(doc.CUSTOM_PATH, text)

    def test_a_missing_directory_hint_says_to_run_it_from_the_project(self):
        check = doc.check_dirs(
            "/srv/client-acme/none", "/srv/client-acme/data", "/srv/client-acme/data/x"
        )[0]
        self.assertEqual(check.status, doc.FAIL)
        self.assertNotIn("acme", check.detail)
        self.assertIn("run this from the project", check.fix)
        self.assertIn("--dir", check.fix)


class ScrubTests(unittest.TestCase):
    def test_a_role_arn_and_an_account_id_in_a_childs_text_are_replaced(self):
        text = "AccessDenied: arn:aws:sts::123456789012:assumed-role/Dev/alice is not allowed in account 123456789012"
        out = doc.scrub(text)
        for word in ("123456789012", "alice", "assumed-role"):
            self.assertNotIn(word, out)
        self.assertIn("<arn>", out)

    def test_the_live_failure_line_has_no_account_id(self):
        def runner(argv, cwd, env, stdin_text):
            return 1, "", "denied for account 210987654321"

        check = doc.check_live(runner, "m")[0]
        self.assertNotIn("210987654321", check.detail)


class LiveCostTests(unittest.TestCase):
    def test_a_call_that_cost_more_than_its_cap_adds_a_warning(self):
        checks = doc.check_live(
            lambda a, c, e, s: (0, fake_lane_output("{}", cost=0.2), ""), "m"
        )
        self.assertEqual(statuses(checks), [doc.OK, doc.WARN])
        self.assertIn("more than the 0.05 USD cap", checks[1].detail)

    def test_a_call_within_its_cap_adds_nothing(self):
        checks = doc.check_live(
            lambda a, c, e, s: (0, fake_lane_output("{}", cost=0.001), ""), "m"
        )
        self.assertEqual(statuses(checks), [doc.OK])


if __name__ == "__main__":
    unittest.main()
