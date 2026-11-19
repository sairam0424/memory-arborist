"""Found by the independent code review. A hunk of any size went through a token scan that is not linear in a long run of dotted
words. A card number followed by more digits (an expiry date, a security code) passed the
digest redaction because the whole run of digits failed the Luhn check, and a restore whose archive folder could not be made
raised a bare error that never named the staged copy it left inside the notes folder."""

import contextlib
import io
import os
import stat
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import memtest_helpers as th  # noqa: E402

sys.path.insert(0, th.SCRIPTS)
import memory_common as mc  # noqa: E402
import memory_dream_ground as dg  # noqa: E402
import memory_hygiene as mh  # noqa: E402
import memory_redact as mr  # noqa: E402
import memory_snapshot as sn  # noqa: E402

# Test numbers that pass the Luhn check; none is a live account.
CARD = "4111 1111 1111 1111"
CARD_PLAIN = "4111111111111111"
CARD_DASHED = "4111-1111-1111-1111"


class CardFollowedByDigitsTests(unittest.TestCase):
    def test_a_card_followed_by_an_expiry_date_or_a_code_is_masked(self):
        cases = (
            "the test card is %s 12/27 and so on" % CARD,
            "card %s 123, thanks" % CARD_PLAIN,
            "pay with %s 12/27" % CARD_DASHED,
            "card %s 12" % CARD_PLAIN,
            "card %s 1" % CARD,
            "card %s 1227 123" % CARD_PLAIN,
        )
        for text in cases:
            with self.subTest(text):
                masked, changed = mr.mask_pii(text)
                self.assertTrue(changed)
                self.assertIn("[card]", masked)
                self.assertNotIn("4111", masked)

    def test_a_card_preceded_by_digits_is_masked_and_the_digits_stay(self):
        masked, _changed = mr.mask_pii("ref 123 %s end" % CARD)
        self.assertEqual(masked, "ref 123 [card] end")

    def test_only_the_card_is_replaced_not_the_digits_around_it(self):
        masked, _changed = mr.mask_pii("%s 12/27" % CARD)
        self.assertEqual(masked, "[card] 12/27")

    def test_a_run_that_is_not_a_card_is_left_alone(self):
        for text in (
            "order 4111 1111 1111 1112 12",
            "numbers 1234 5678 9012 3456 78 here",
            "version 1 2 3 4 5 6 7 8 9",
        ):
            with self.subTest(text):
                masked, _changed = mr.mask_pii(text)
                self.assertNotIn("[card]", masked)

    def test_the_whole_run_that_passes_is_still_masked_as_one(self):
        self.assertEqual(mr.mask_pii("card %s end" % CARD)[0], "card [card] end")

    def test_sanitize_reports_the_masking(self):
        result = mr.sanitize("the test card is %s 12/27 and so on" % CARD)
        self.assertTrue(result.pii)
        self.assertNotIn("4111", result.excerpt)


class RestoreWithAnUncreatableArchiveTests(unittest.TestCase):
    def setUp(self):
        self.mem = th.make_dir({"MEMORY.md": "# Index\n", "a.md": th.note("a")})
        self.root = tempfile.mkdtemp(prefix="restops_")
        self.ops = mc.ops_dir_for(self.mem, self.root)
        self.snap = sn.take_snapshot(self.mem, self.ops, now=1_700_000_000)["id"]
        th.write(os.path.join(self.mem, "a.md"), "changed\n")
        self.archive = os.path.join(self.ops, "archive")
        os.makedirs(self.archive, mode=0o700, exist_ok=True)
        os.chmod(
            self.archive, stat.S_IRUSR | stat.S_IXUSR
        )  # a folder in which the dated archive folder cannot be made
        self.addCleanup(os.chmod, self.archive, stat.S_IRWXU)

    def restore(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = mh.main(
                [
                    "--dir",
                    self.mem,
                    "--ops-root",
                    self.root,
                    "restore",
                    "--snapshot",
                    self.snap,
                    "--file",
                    "a.md",
                    "--apply",
                    "--force",
                ]
            )
        return code, out.getvalue(), err.getvalue()

    def test_the_failure_is_a_message_that_names_the_staged_copy_and_the_note_is_untouched(
        self,
    ):
        if os.geteuid() == 0:
            self.skipTest("root can write anywhere")
        code, out, err = self.restore()
        self.assertEqual(code, 1, out + err)
        self.assertIn("the live note is untouched", err)
        self.assertIn(".restore-", err)
        self.assertIn("safe to remove by hand", err)
        self.assertEqual(th.read(os.path.join(self.mem, "a.md")), "changed\n")
        self.assertNotIn("Traceback", out + err)


class FullDigestFolderTests(unittest.TestCase):
    """A routine rerun on a folder that has reached its size cap said it had skipped nothing and then that no digest was written."""

    def test_a_session_that_already_has_its_digest_is_counted_as_existing_and_no_note_is_printed(self):
        import test_memory_evidence as tev

        saved, root = tempfile.tempdir, tempfile.mkdtemp(prefix="memtests_")
        tempfile.tempdir = root
        self.addCleanup(setattr, tempfile, "tempdir", saved)
        source = tempfile.mkdtemp(prefix="evsrc_")
        out = os.path.join(tempfile.mkdtemp(prefix="evout_"), "evidence")
        tev.write_transcript(source, 1, tev.basic_session())
        args = ["build", "--source", source, "--out", out, "--quiet-seconds", "3600", "--now", str(tev.NOW)]
        code, text, err = tev.run_cli(*args)
        self.assertEqual(code, 0, text + err)
        with open(os.path.join(out, tev.sid(99) + ".md"), "w", encoding="utf-8") as fh:
            fh.write("x" * tev.me.MAX_TOTAL_BYTES)
        code, text, err = tev.run_cli(*args)
        self.assertEqual(code, 0, text + err)
        self.assertIn("skipped_existing=1", text)
        self.assertNotIn("NOTE no digest was written", text + err)
        self.assertNotIn("size_cap=0", text + err)


class HunkSizeTests(unittest.TestCase):
    def test_a_new_text_over_the_limit_is_refused_before_any_scan(self):
        big = "word " * (dg.MAX_HUNK_CHARS // 5 + 1)  # both texts long, so neither the growth nor the shrink rule fires
        problem = dg._size_problem(big, big + "x")
        self.assertIn("too long to check", problem)
        self.assertIsNone(dg._size_problem("word " * 100, "word " * 100 + "more"))

    def test_the_old_text_may_be_shorter_than_the_limit_but_the_new_text_may_not_pass_it(self):
        self.assertIn("too long to check", dg._size_problem("x" * 5000, "x" * (dg.MAX_HUNK_CHARS + 1)))

    def test_the_longest_text_that_is_let_through_scans_in_a_bounded_time(self):
        import time

        started = time.process_time()
        dg._added_tokens("a." * (dg.MAX_HUNK_CHARS // 2))
        self.assertLess(time.process_time() - started, 30.0)


if __name__ == "__main__":
    unittest.main()
