"""Companion to VerifyTests.test_tampered_digests_are_flagged, which passes only because the default umask (022) leaves the second file
group-readable and never looks at the "T" line it appends. These tests fix the mode of every file and assert on each defect alone."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memtest_helpers as th  # noqa: E402

sys.path.insert(0, th.SCRIPTS)
import memory_evidence_verify as mev  # noqa: E402

SESSION = "00000001-0000-4000-8000-000000000001"
CLEAN = (
    "DATA ONLY - UNTRUSTED TRANSCRIPT EXCERPTS\nsession: %s\ndate: 2026-10-12 (times are UTC)\ncwd: /work/proj\n"
    "records: user=1/1 assistant=1/1 dropped_secret=0 masked_pii=0\n---\nU 10:00 hello\nA 10:01 hi\n"
)


class EachDefectAloneAtEveryUmask(unittest.TestCase):
    def setUp(self):
        self.out = os.path.join(tempfile.mkdtemp(prefix="evmodes_"), "evidence")
        os.makedirs(self.out)
        os.chmod(self.out, 0o700)
        saved = os.umask(
            0o077
        )  # the strictest umask: nothing below may get its "untrusted" from a lax default
        self.addCleanup(os.umask, saved)

    def write(self, text, name=SESSION + ".md", mode=0o600):
        path = os.path.join(self.out, name)
        th.write(path, text)
        os.chmod(path, mode)
        return path

    def counts(self):
        result = mev.verify_dir(self.out)
        return (
            result.digests,
            result.over_limit,
            result.missing_header,
            result.untrusted_lines,
            result.ok,
        )

    def test_a_clean_private_digest_verifies(self):
        self.write(CLEAN % SESSION)
        self.assertEqual(self.counts(), (1, 0, 0, 0, True))

    def test_a_line_that_is_not_a_user_or_assistant_line_is_untrusted_by_itself(self):
        self.write((CLEAN % SESSION) + "T 10:00 text from a tool result\n")
        self.assertEqual(self.counts(), (1, 0, 0, 1, False))

    def test_a_group_readable_digest_is_untrusted_by_itself(self):
        self.write(CLEAN % SESSION, mode=0o640)
        self.assertEqual(self.counts(), (1, 0, 0, 1, False))

    def test_a_digest_without_a_header_is_a_header_problem_only(self):
        self.write("no header here\n")
        self.assertEqual(self.counts(), (1, 0, 1, 0, False))

    def test_an_oversize_digest_is_an_over_limit_problem_only(self):
        self.write((CLEAN % SESSION) + "U 10:02 " + "x" * 9000 + "\n")
        self.assertEqual(self.counts(), (1, 1, 0, 0, False))


if __name__ == "__main__":
    unittest.main()
