"""Found by a second, fresh read-only reviewer of the fixes of the first review. Card masking held only when 1 to 3 digits came
before the card (a date or an order id in front of a spaced card leaked all of it), and the doctor still printed the tool names a
child reported and several common shapes of a path (a space in a folder name, a colon-separated list, a file: URL, ~/ and ./ paths)."""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import memtest_helpers as th  # noqa: E402

sys.path.insert(0, th.SCRIPTS)
sys.path.insert(0, os.path.join(th.SCRIPTS, "doctor"))
sys.path.insert(0, os.path.join(th.SCRIPTS, "recall"))
sys.path.insert(0, os.path.join(th.SCRIPTS, "doctor", "tests"))
import memory_doctor as doc  # noqa: E402
import memory_redact as mr  # noqa: E402

CARD = (
    "4111 1111 1111 1111"  # a test number that passes the Luhn check; no live account
)


class CardAfterManyDigitsTests(unittest.TestCase):
    def test_a_card_after_a_date_an_order_id_or_a_longer_number_is_masked(self):
        for before in (
            "2025-11-17",
            "ticket 12345",
            "12 27 123",
            "no. 1234-5678",
        ):
            with self.subTest(before):
                masked, changed = mr.mask_pii("%s %s end" % (before, CARD))
                self.assertTrue(changed)
                self.assertIn("[card]", masked)
                self.assertNotIn("4111", masked)
                self.assertTrue(
                    masked.startswith(before) and masked.endswith(" end"), masked
                )

    def test_a_card_followed_by_a_dash_and_digits_is_masked(self):
        for text in (
            "4111-1111-1111-1111-1227",
            "4111111111111111-1227",
            "4111 1111 1111 1111-12",
        ):
            with self.subTest(text):
                masked, _changed = mr.mask_pii(text)
                self.assertIn("[card]", masked)
                self.assertNotIn("4111", masked)

    def test_sanitize_reports_the_masking_of_a_card_after_a_date(self):
        result = mr.sanitize("2025-11-17 %s refund issued" % CARD)
        self.assertTrue(result.pii)
        self.assertNotIn("4111", result.excerpt)

    def test_numbers_that_are_not_cards_are_left_alone(self):
        for text in (
            "dates 2025-11-17 2025-11-18 2025-11-19 2025-11-20",
            "ids 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9",
            "totals 1234 5678 9012 3457 and 123",
            "a short number 4111 1111",
            "one long number 12345678901234567890123",
        ):
            with self.subTest(text):
                self.assertEqual(mr.mask_pii(text)[0], text)

    def test_a_long_run_of_digit_groups_costs_linear_time(self):
        import time

        text = "1234 " * 20000
        started = time.monotonic()
        mr.mask_pii(text)
        self.assertLess(time.monotonic() - started, 30.0)


class ChildToolNamesTests(unittest.TestCase):
    def test_the_names_of_the_tools_a_child_reports_are_not_printed(self):
        from test_doctor_checks import fake_lane_output  # noqa: E402

        reported = [
            "Read",
            "mcp__acme-corp-merger__search_deals",
            "mcp__clientx_crm__list_contacts",
        ]

        def runner(argv, cwd, env, stdin_text):
            return 0, fake_lane_output("{}", tools=reported), ""

        checks = doc.check_live(runner, "m")
        text = doc.render(checks)
        self.assertEqual(checks[0].status, doc.FAIL)
        for word in ("acme", "clientx", "search_deals", "list_contacts"):
            self.assertNotIn(word, text)
        self.assertIn("tool", checks[0].detail)


class ChildPathShapesTests(unittest.TestCase):
    def cleaned(self, text):
        return doc.scrub_paths(text)

    def test_a_folder_name_with_a_space_is_replaced_whole(self):
        out = self.cleaned("env: /Volumes/Client Work/Acme Corp/bin/node: No such file")
        for word in ("Client", "Acme", "Work", "node"):
            self.assertNotIn(word, out)
        self.assertIn("<path>", out)
        out = self.cleaned("/Volumes/Macintosh HD/Users/example/clients/acme/run.sh failed")
        self.assertNotIn("example", out)
        self.assertNotIn("acme", out)

    def test_every_entry_of_a_colon_separated_list_is_replaced(self):
        out = self.cleaned(
            "which: no claude in (/usr/local/bin:/opt/acme-corp/bin:/usr/bin)"
        )
        self.assertNotIn("acme", out)
        self.assertEqual(out.count("<path>"), 3)

    def test_a_file_url_is_replaced_but_other_urls_are_not(self):
        self.assertNotIn(
            "acme",
            self.cleaned("Cannot find module file:///srv/acme-merger/app/index.js"),
        )
        self.assertIn(
            "https://example.com/docs",
            self.cleaned("see https://example.com/docs for more"),
        )

    def test_a_path_under_the_home_folder_keeps_only_the_claude_folder_name(self):
        out = self.cleaned("failed: ~/clients/acme-corp/claude-wrap and ~/.claude/x")
        self.assertNotIn("acme", out)
        self.assertIn("~/.claude/x", out)

    def test_a_relative_path_with_a_dot_is_replaced(self):
        out = self.cleaned(
            "sh: ./clients/acme-merger/wrap.sh: not found and ../up/acme/x"
        )
        self.assertNotIn("acme", out)

    def test_a_slash_command_and_prose_with_a_slash_survive(self):
        self.assertEqual(
            self.cleaned("Not logged in - run /login"), "Not logged in - run /login"
        )
        self.assertEqual(
            self.cleaned("read/write failed and/or timed out"),
            "read/write failed and/or timed out",
        )


def luhn_valid_number(prefix, length):
    """A number of `length` digits that starts with `prefix` and passes the Luhn check (made here, never a live account)."""
    body = prefix + "0" * (length - len(prefix) - 1)
    for check in "0123456789":
        if mr._luhn_ok(body + check):
            return body + check
    raise AssertionError("no check digit")


class CardLengthsAndOverMaskingTests(unittest.TestCase):
    def test_the_shortest_and_the_longest_card_lengths_are_masked_and_the_lengths_outside_are_not(self):
        for length, masked in ((12, False), (13, True), (15, True), (16, True), (19, True), (20, False)):
            number = luhn_valid_number("4", length)
            with self.subTest(length):
                self.assertEqual("[card]" in mr.mask_pii("n " + number + " e")[0], masked, number)

    def test_a_card_in_the_usual_groupings_is_masked(self):
        for text in ("3782 822463 10005", "4111 1111 1111 1111", "4111-1111-1111-1111", "6011 0009 9013 9424"):
            with self.subTest(text):
                self.assertEqual(mr.mask_pii("x " + text + " y")[0], "x [card] y")

    def test_runs_that_are_not_written_like_a_card_are_not_masked(self):
        import random

        rng = random.Random(20261118)
        for sizes in ((3, 4, 3, 4, 3, 2), (1,) * 19, (2,) * 8, (5, 3, 5, 3)):
            for _ in range(300):
                text = "ref " + " ".join("".join(rng.choice("0123456789") for _ in range(n)) for n in sizes)
                self.assertNotIn("[card]", mr.mask_pii(text)[0], text)

    def test_a_very_long_run_of_one_digit_groups_is_cheap(self):
        import time

        started = time.monotonic()
        mr.mask_pii("1 " * 100000)
        self.assertLess(time.monotonic() - started, 20.0)


class CardWithATailTests(unittest.TestCase):
    def test_a_short_last_group_never_becomes_part_of_the_card(self):
        # 4111 1111 1111 1111 + "18" (and + "3") pass the Luhn check as 18 and 17 digits by chance: the tail is not the card's
        self.assertEqual(mr.mask_pii("pay 4111 1111 1111 1111 18 now")[0], "pay [card] 18 now")
        self.assertEqual(mr.mask_pii("pay 4111 1111 1111 1111 3 now")[0], "pay [card] 3 now")

    def test_two_cards_in_one_run_are_both_masked(self):
        first, second = luhn_valid_number("4111", 16), luhn_valid_number("5500", 16)
        spaced = lambda number: " ".join(number[i : i + 4] for i in range(0, 16, 4))  # noqa: E731
        masked, _changed = mr.mask_pii("x %s %s y" % (spaced(first), spaced(second)))
        self.assertNotIn("4111", masked)
        self.assertNotIn("5500", masked)
        self.assertTrue(masked.startswith("x [card]") and masked.endswith(" y"), masked)


class EmptyStderrTests(unittest.TestCase):
    def test_a_program_that_cannot_start_and_says_nothing_does_not_print_a_credential_message(self):
        import stat
        import tempfile

        bin_dir = tempfile.mkdtemp(prefix="docbin_")
        path = os.path.join(bin_dir, "claude")
        th.write(path, "#!/bin/sh\n")
        os.chmod(path, stat.S_IRWXU)
        for silence in ("", "   \n"):
            with self.subTest(repr(silence)):
                checks = doc.check_claude(lambda argv, env: (127, "", silence), {"PATH": bin_dir, "HOME": bin_dir})
                self.assertEqual(checks[0].status, doc.FAIL)
                self.assertNotIn("credential", checks[0].detail)
                self.assertNotIn("withheld", checks[0].detail)


class DeeplyNestedRegexTests(unittest.TestCase):
    def test_a_regex_nested_thousands_deep_is_a_bad_regex_not_a_crash(self):
        import memory_common as mc
        import memory_recall_run as mrr

        nested = "(" * 3000 + "a" + ")" * 3000
        text = '{"id":"q1","kind":"recall","question":"x","must":[%s]}\n' % __import__("json").dumps(nested)
        with self.assertRaises(mc.HygieneError):
            mrr.load_questions_text(text, require_count=False)
        ops = __import__("tempfile").mkdtemp(prefix="docops_")
        os.makedirs(os.path.join(ops, "recall"))
        th.write(os.path.join(ops, "recall", "questions.jsonl"), text)
        check = doc._questions_state(ops)
        self.assertEqual(check.status, doc.WARN)
        self.assertIn("bad regex", check.detail)


class LiveCheckFailsAloneTests(unittest.TestCase):
    def test_a_live_check_that_raises_is_one_failure_and_the_report_is_still_printed(self):
        import contextlib
        import io
        import stat
        import tempfile
        from unittest import mock

        bin_dir = tempfile.mkdtemp(prefix="docbin_")
        path = os.path.join(bin_dir, "claude")
        th.write(path, "#!/bin/sh\n")
        os.chmod(path, stat.S_IRWXU)
        mem, root = th.make_dir({"MEMORY.md": "# Index\n"}), tempfile.mkdtemp(prefix="docops_")
        out = io.StringIO()
        with mock.patch.object(doc, "check_live", side_effect=OSError(28, "No space left on device")), contextlib.redirect_stdout(out):
            code = doc.main(["--dir", mem, "--ops-root", root, "--live"], environ={"PATH": bin_dir, "HOME": bin_dir}, probe=lambda argv, env: (0, "2.1.325 (Claude Code)" if argv[1] == "--version" else "", ""))
        self.assertEqual(code, 1)
        self.assertIn("FAIL live", out.getvalue())
        self.assertIn("DOCTOR ok=", out.getvalue())


class ModelStringTests(unittest.TestCase):
    def runner(self, argv, cwd, env, stdin_text):
        from test_doctor_checks import fake_lane_output

        return 0, fake_lane_output("{}"), ""

    def test_a_model_string_that_is_a_path_or_a_url_is_not_printed_but_an_alias_is(self):
        for model in ("projects/acme-prod-421913/locations/us/models/m", "https://acme-corp.openai.azure.com/deploy/x"):
            with self.subTest(model):
                text = doc.render(doc.check_live(self.runner, model))
                self.assertNotIn("acme", text)
                self.assertIn("custom model", text)
        self.assertIn("us.anthropic.claude-sonnet-5-5", doc.render(doc.check_live(self.runner, "us.anthropic.claude-sonnet-5-5")))

    def test_invisible_and_line_separator_characters_in_a_model_string_are_removed(self):
        text = doc.render(doc.check_live(self.runner, "m\u2028x\u2060y\u00ady"))
        for char in ("\u2028", "\u2060", "\u00ad"):
            self.assertNotIn(char, text)


class HunkCapEdgeTests(unittest.TestCase):
    def test_the_cap_is_inclusive_and_never_replaces_the_growth_and_shrink_reasons(self):
        import memory_dream_ground as dg

        top = dg.MAX_HUNK_CHARS
        self.assertIsNone(dg._size_problem("x" * top, "y" * top))
        self.assertIn("too long", dg._size_problem("x" * (top + 1), "y" * (top + 1)))
        self.assertIn("grows", dg._size_problem("x" * 100, "y" * (top + 1)))
        self.assertIn("removes", dg._size_problem("x" * (top * 2), "y" * 100))


class RestoreSiblingFailureTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        import memory_snapshot as sn

        self.sn = sn
        self.mem = th.make_dir({"MEMORY.md": "# Index\n", "a.md": th.note("a")})
        self.root = tempfile.mkdtemp(prefix="restops_")
        self.ops = __import__("memory_common").ops_dir_for(self.mem, self.root)
        self.snap = sn.take_snapshot(self.mem, self.ops, now=1_700_000_000)["id"]
        th.write(os.path.join(self.mem, "a.md"), "changed\n")

    def restore(self, replace):
        import contextlib
        import io
        from unittest import mock

        import memory_hygiene as mh

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(self.sn.os, "replace", replace), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = mh.main(["--dir", self.mem, "--ops-root", self.root, "restore", "--snapshot", self.snap, "--file", "a.md", "--apply", "--force"])
        return code, out.getvalue() + err.getvalue()

    def test_a_placement_that_fails_after_a_good_archive_names_the_staged_copy(self):
        real, calls = os.replace, {"n": 0}

        def replace(src, dst):
            calls["n"] += 1
            if calls["n"] == 2:  # the move of the staged copy onto the note
                raise PermissionError(13, "Permission denied")
            return real(src, dst)

        code, text = self.restore(replace)
        self.assertEqual(code, 1, text)
        self.assertIn("original was put back", text)
        self.assertIn(".restore-", text)
        self.assertEqual(th.read(os.path.join(self.mem, "a.md")), "changed\n")

    def test_a_placement_and_a_put_back_that_both_fail_say_where_the_note_is(self):
        real, calls = os.replace, {"n": 0}

        def replace(src, dst):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise OSError(5, "Input/output error")
            return real(src, dst)

        code, text = self.restore(replace)
        self.assertEqual(code, 1, text)
        self.assertIn("could not put the original back", text)
        self.assertIn("archive", text)
        self.assertIn(".restore-", text)
        self.assertNotIn("Traceback", text)

    def test_an_os_error_without_a_message_still_gives_a_readable_archive_error(self):
        def replace(src, dst):
            raise OSError()

        code, text = self.restore(replace)
        self.assertEqual(code, 1, text)
        self.assertIn("could not archive", text)
        self.assertIn("OSError", text)


if __name__ == "__main__":
    unittest.main()
