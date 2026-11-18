"""The doctor must not say OK where the engine would refuse. Each test names a state that a real command refuses (the evidence
verifier, starting claude in a child, a snapshot) and asserts the doctor does not report it as fine. No test spends a token."""

import os
import stat
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ENGINE, "tests"))
sys.path.insert(0, ENGINE)
sys.path.insert(0, os.path.join(ENGINE, "recall"))
sys.path.insert(0, os.path.join(ENGINE, "dream"))
sys.path.insert(0, os.path.dirname(HERE))
import memtest_helpers as th  # noqa: E402
import memory_common as mc  # noqa: E402
import memory_dream_lanes as lanes  # noqa: E402
import memory_doctor as doc  # noqa: E402
import memory_evidence_verify as mev  # noqa: E402
import memory_recall_run as mrr  # noqa: E402
import memory_snapshot as sn  # noqa: E402
from test_doctor_checks import HELP_ALL, WithClaudeOnPath, probe_for  # noqa: E402

SESSION = "00000001-0000-4000-8000-000000000001"
DIGEST = (
    "DATA ONLY - UNTRUSTED TRANSCRIPT EXCERPTS\nsession: %s\ndate: 2026-10-12 (times are UTC)\ncwd: /work/proj\n"
    "records: user=1/1 assistant=1/1 dropped_secret=0 masked_pii=0\n---\nU 10:00 hello\nA 10:01 hi\n"
)


class EvidenceAgreesWithTheVerifier(unittest.TestCase):
    def setUp(self):
        self.ops = tempfile.mkdtemp(prefix="docev_")
        self.evidence = os.path.join(self.ops, "evidence")
        os.makedirs(self.evidence)
        os.chmod(
            self.evidence, 0o700
        )  # explicit: the result must not depend on the umask

    def digest(self, name=SESSION + ".md", mode=0o600, text=None):
        path = os.path.join(self.evidence, name)
        th.write(path, DIGEST % SESSION if text is None else text)
        os.chmod(path, mode)
        return path

    def test_a_digest_the_verifier_accepts_is_ok(self):
        self.digest()
        self.assertTrue(mev.verify_dir(self.evidence).ok)
        found = doc._evidence_state(self.ops)
        self.assertEqual((found.status, found.detail), (doc.OK, "1 evidence digests"))

    def test_a_digest_the_verifier_refuses_is_not_ok(self):
        cases = {
            "name is not a session id": dict(name="notes.md"),
            "readable by the group": dict(mode=0o640),
            "no header": dict(text="no header here\n"),
            "empty": dict(text=""),
        }
        for label, kwargs in cases.items():
            with self.subTest(label):
                path = self.digest(**kwargs)
                self.assertFalse(mev.verify_dir(self.evidence).ok)
                found = doc._evidence_state(self.ops)
                self.assertEqual(found.status, doc.WARN, found.detail)
                self.assertIn("1 evidence digests", found.detail)
                os.remove(path)

    def test_a_symlinked_evidence_folder_is_not_ok(self):
        self.digest()
        real = os.path.join(self.ops, "elsewhere")
        os.rename(self.evidence, real)
        os.symlink(real, self.evidence)
        self.assertFalse(mev.verify_dir(self.evidence).ok)
        self.assertEqual(doc._evidence_state(self.ops).status, doc.WARN)

    def test_an_evidence_folder_others_can_read_is_not_ok(self):
        self.digest()
        os.chmod(self.evidence, 0o755)
        self.assertFalse(mev.verify_dir(self.evidence).ok)
        self.assertEqual(doc._evidence_state(self.ops).status, doc.WARN)

    def test_an_empty_or_missing_folder_stays_informational(self):
        self.assertEqual(doc._evidence_state(self.ops).status, doc.INFO)
        os.rmdir(self.evidence)
        self.assertEqual(doc._evidence_state(self.ops).status, doc.INFO)

    def test_a_file_name_is_never_printed(self):
        self.digest(name="Acme-client-plan.md")
        found = doc._evidence_state(self.ops)
        self.assertNotIn("Acme", found.detail + found.fix)


class ClaudeOnARelativePath(unittest.TestCase):
    """The children run in an empty temporary folder, so only an absolute PATH entry can find claude for them."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="docrel_")
        self.bin = os.path.join(self.base, "bin")
        os.makedirs(self.bin)
        claude = os.path.join(self.bin, "claude")
        th.write(claude, "#!/bin/sh\n")
        os.chmod(claude, stat.S_IRWXU)
        self.saved = os.getcwd()
        os.chdir(self.bin)  # here "." and the empty entry both find claude
        self.addCleanup(os.chdir, self.saved)

    def check(self, path):
        return doc.check_claude(
            probe_for(), {"PATH": path, "HOME": tempfile.gettempdir()}
        )

    def test_the_premise_a_child_in_another_folder_cannot_start_it(self):
        child_cwd = tempfile.mkdtemp(prefix="docchild_")
        with self.assertRaises(mrr.AskError):
            lanes.subprocess_runner(
                ["claude", "--version"],
                child_cwd,
                {"PATH": "." + os.pathsep + "../bin"},
                "",
            )

    def test_a_relative_or_empty_entry_alone_is_a_failure(self):
        for path in (
            ".",
            os.pathsep + "/no/such/dir",
            "../bin",
            "/no/such/dir" + os.pathsep,
        ):
            with self.subTest(path):
                first = self.check(path)[0]
                self.assertEqual(first.status, doc.FAIL)
                self.assertIn("relative or empty", first.detail)

    def test_an_absolute_entry_after_a_relative_one_is_ok(self):
        self.assertEqual(self.check("../bin" + os.pathsep + self.bin)[0].status, doc.OK)

    def test_no_claude_anywhere_keeps_the_old_message(self):
        for path in (tempfile.mkdtemp(prefix="docempty_"), ""):
            with self.subTest(path):
                first = self.check(path)[0]
                self.assertEqual(first.status, doc.FAIL)
                self.assertIn("not found on the PATH the children get", first.detail)


class FlagAndVersionParsing(WithClaudeOnPath):
    def flags_line(self, text):
        return [
            c
            for c in doc.check_claude(probe_for(help_text=text), self.environ)
            if c.name == "flags"
        ][0]

    def test_a_flag_that_is_only_the_start_of_another_flag_is_not_listed(self):
        for flag in doc.REQUIRED_FLAGS:
            if flag == "--append-system-prompt-file":
                continue
            with self.subTest(flag):
                text = HELP_ALL.replace(
                    "\n  " + flag + "\n", "\n  " + flag + "-allowed\n"
                )
                self.assertNotEqual(text, HELP_ALL)
                found = self.flags_line(text)
                self.assertEqual(found.status, doc.WARN)
                self.assertIn(flag, found.detail)

    def test_the_shorthand_and_the_real_flag_forms_still_count(self):
        self.assertEqual(self.flags_line(HELP_ALL).status, doc.OK)
        self.assertEqual(
            self.flags_line(
                HELP_ALL + "  --output-format=stream-json, --tools, --add-dir\n"
            ).status,
            doc.OK,
        )

    def test_only_a_version_that_starts_a_line_counts(self):
        self.assertEqual(doc.parse_version("2.1.325 (Claude Code)"), (2, 1, 325))
        self.assertEqual(
            doc.parse_version(
                "warning: node 20.11.0 is deprecated\n2.1.100 (Claude Code)"
            ),
            (2, 1, 100),
        )
        self.assertIsNone(
            doc.parse_version(
                "/home/user/.nvm/versions/node/v18.19.0/bin/claude: no version"
            )
        )

    def test_a_node_version_in_a_warning_does_not_hide_an_old_claude(self):
        checks = doc.check_claude(
            probe_for(
                version="warning: node 20.11.0 is deprecated\n2.1.100 (Claude Code)"
            ),
            self.environ,
        )
        self.assertEqual(checks[0].status, doc.FAIL)
        self.assertIn("2.1.100", checks[0].detail)


class WatchListCoversTheWebIdentityRoute(unittest.TestCase):
    NAMES = (
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_ROLE_ARN",
        "AWS_ROLE_SESSION_NAME",
        "AWS_CA_BUNDLE",
    )

    def test_each_name_is_reported_when_set_but_not_passed(self):
        for name in self.NAMES:
            with self.subTest(name):
                checks = doc.check_login_env({"PATH": "/bin", name: "x"})
                self.assertTrue(
                    any(c.status == doc.WARN and name in c.detail for c in checks)
                )

    def test_none_of_them_is_a_variable_the_children_get_by_default(self):
        self.assertEqual(set(self.NAMES) & set(mrr.ALLOWED_ENV), set())


class SnapshotRefusalsAreFailures(unittest.TestCase):
    def setUp(self):
        self.mem = th.make_dir(
            {"MEMORY.md": "# Index\n- [A](a.md) a\n", "a.md": th.note("a")}
        )
        self.root = tempfile.mkdtemp(prefix="docops_")
        self.ops = mc.ops_dir_for(self.mem, self.root)

    def refusal(self):
        try:
            sn.take_snapshot(self.mem, self.ops, now=1_700_000_000)
        except (sn.HygieneError, OSError) as err:
            return err
        return None

    def checks(self):
        return doc.check_dirs(self.mem, self.root, self.ops)

    def assert_both_refuse(self):
        self.assertIsNotNone(self.refusal())
        self.assertIn(doc.FAIL, [c.status for c in self.checks()])

    def test_a_folder_that_cannot_be_read(self):
        sub = os.path.join(self.mem, "sub")
        os.makedirs(sub)
        th.write(os.path.join(sub, "x.md"), "x\n")
        os.chmod(sub, 0)
        self.addCleanup(os.chmod, sub, 0o700)
        if os.access(sub, os.R_OK):
            self.skipTest("this user can read a mode 0 folder (root)")
        self.assert_both_refuse()

    def test_a_note_that_cannot_be_read(self):
        note = os.path.join(self.mem, "a.md")
        os.chmod(note, 0)
        self.addCleanup(os.chmod, note, 0o600)
        if os.access(note, os.R_OK):
            self.skipTest("this user can read a mode 0 file (root)")
        self.assert_both_refuse()

    def test_a_data_folder_that_is_a_file(self):
        th.write(self.ops, "in the way\n")
        self.assert_both_refuse()

    def test_a_data_folder_that_is_a_broken_link(self):
        os.symlink(os.path.join(self.root, "nowhere"), self.ops)
        self.assert_both_refuse()

    def test_a_data_root_that_is_a_broken_link(self):
        os.rmdir(self.root)
        os.symlink(
            os.path.join(
                os.path.dirname(self.root), "nowhere-" + os.path.basename(self.root)
            ),
            self.root,
        )
        self.addCleanup(os.remove, self.root)
        self.assert_both_refuse()

    def test_a_readable_memory_folder_and_a_fresh_data_root_stay_ok(self):
        self.assertEqual(
            [c.status for c in doc.check_dirs(self.mem, self.root, self.ops)],
            [doc.OK, doc.OK],
        )
        self.assertIsNone(self.refusal())


if __name__ == "__main__":
    unittest.main()
