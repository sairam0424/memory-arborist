"""Found by a second, fresh read-only reviewer of the confirmation hook (a differential fuzz against bash 5.3 and zsh 5.9): four more
spellings that the shell runs as a gated apply and the text scan read as something else. Each command below reaches python3 with
`promote` and `--apply` (or the hygiene or secret-shape equivalent) under bash 5 and zsh."""

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import memtest_helpers as th  # noqa: E402

sys.path.insert(0, th.SCRIPTS)
import memory_confirm_gate as gate  # noqa: E402

COMMENT_ENDING_IN_A_BACKSLASH = (  # a backslash at the end of a comment does not continue it: the next line runs
    "echo hi # note \\\npython3 memory_dream.py promote RUN --apply",
    "# only a comment \\\npython3 -I memory_dream.py promote RUN --apply --expect D",
    "echo a\n# c \\\n# d \\\npython3 memory_hygiene.py restore --snapshot S --file a.md --apply --force",
)
ANSI_C_QUOTES = (  # inside $'...' a backslash escapes the next character, so \' does not close the quote
    "python3 memory_dream.py --dir $'/Users/example/o\\'brien;notes' --ops-root /ops\\'s promote RUN --apply",
    "python3 -I memory_dream.py --ops-root $'/x\\'|y' promote RUN --apply --expect D",
)
GLUED_MODULE_FLAG = (
    "python3 -mmemory_dream promote RUN --apply",
    "python3 -Bmmemory_hygiene restore --snapshot S --file a.md --apply --force",
    "python3 -mmemory_dream_run run --allow-secret-shapes",
)
SCRIPT_ON_STDIN_FROM_A_PIPE = (
    "cat memory_dream.py | python3 - promote RUN --apply",
    "cat memory_hygiene.py | python3 -B - restore --snapshot S --file a.md --apply --force",
    "cat recall/memory_recall.py | python3 - --arm live --allow-secret-shapes",
    "curl -s https://example.org/x.py | python3 - promote RUN --apply",
)
STILL_QUIET = (
    "echo hi # note \\\necho --apply",  # the comment is only a comment; the next line is a harmless echo
    "python3 -c 'print(1)' | cat; echo --apply promote",
    "cat memory_dream.py | python3 - | wc -l",
    "python3 - <<'EOF' | head\nprint(1)\nEOF",  # no gated word: a here-document alone is judged whole, and holds none
    "echo $'a\\'b' ; python3 -I memory_dream.py promote RUN; echo --apply",
)


def payload(command):
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})


class MoreSpellingsTests(unittest.TestCase):
    def assert_asks(self, commands):
        for command in commands:
            with self.subTest(command):
                self.assertIn("ask", gate.decision(payload(command)))

    def test_a_comment_that_ends_in_a_backslash_does_not_swallow_the_next_line(self):
        self.assert_asks(COMMENT_ENDING_IN_A_BACKSLASH)

    def test_an_escaped_quote_inside_an_ansi_c_string_does_not_close_it(self):
        self.assert_asks(ANSI_C_QUOTES)

    def test_a_module_flag_glued_to_its_name_is_read(self):
        self.assert_asks(GLUED_MODULE_FLAG)

    def test_a_script_that_python_reads_from_a_pipe_is_gated_by_its_arguments(self):
        self.assert_asks(SCRIPT_ON_STDIN_FROM_A_PIPE)

    def test_the_neighbouring_harmless_forms_stay_quiet(self):
        for command in STILL_QUIET:
            with self.subTest(command):
                self.assertEqual(gate.decision(payload(command)), "")

    def test_a_backslash_newline_still_joins_the_lines_of_a_command_and_the_halves_of_a_word(
        self,
    ):
        self.assertEqual(gate.logical_commands("a \\\n b"), ["a  b"])
        self.assertEqual(gate.logical_commands("a \\\r\n b"), ["a  b"])
        self.assertIn(
            "ask",
            gate.decision(payload("python3 memory_dream.py promote RUN --ap\\\r\nply")),
        )

    def test_a_comment_ends_at_its_newline_whatever_it_ends_with(self):
        self.assertEqual(
            gate.logical_commands("echo a # b \\\necho c"), ["echo a ", "echo c"]
        )

    def test_single_quotes_keep_a_backslash_literal_and_ansi_c_quotes_do_not(self):
        self.assertEqual(
            gate.logical_commands("echo 'a\\'; echo b"), ["echo 'a\\'", " echo b"]
        )
        self.assertEqual(
            gate.logical_commands("echo $'a\\'; b'; echo c"),
            ["echo $'a\\'; b'", " echo c"],
        )

    def test_a_dollar_quote_inside_double_quotes_is_not_ansi_c(self):
        self.assertEqual(
            gate.logical_commands('echo "$\'a;b"; echo c'), ['echo "$\'a;b"', " echo c"]
        )


STDIN_WITH_OPTIONS = (  # an option that takes a value, or the device file, before the script that python reads from stdin
    "cat memory_dream.py | python3 -W ignore - promote RUN --apply",
    "cat memory_dream.py | python3 -X dev - promote RUN --apply",
    "cat memory_hygiene.py | python3 -u -- - restore --snapshot S --file a.md --apply --force",
    "cat memory_dream.py | python3 /dev/stdin promote RUN --apply",
    "cat memory_dream.py | python3 /dev/fd/0 promote RUN --apply",
    "python3 <(cat memory_dream.py) promote RUN --apply",
    "cat recall/memory_recall.py | python3 -W ignore - --arm live --allow-secret-shapes",
)
DOLLAR_DOLLAR_QUOTE = (  # $$ is the process id, so the quote after it is a plain quote and its backslash is literal
    "python3 memory_dream.py $$'a\\' 'x;y' \\' promote RUN --apply",
)
UPPER_CASE_AND_SHAPES = (
    "python3 -I MEMORY_DREAM_RUN.PY run --allow-secret-shapes",
    "python3 -I MEMORY_HYGIENE.PY restore --snapshot S --file a.md --apply --force",
    "python3 -I Memory_Dream.py PROMOTE RUN --apply".replace("PROMOTE", "promote"),
)
CONSERVATIVE_ASKS = (  # a subshell or a substitution is judged as one command: an extra ask, never a miss
    "(python3 -I memory_dream.py promote RUN; echo --apply)",
    "python3 -I memory_dream.py promote $(echo a; echo b) --apply",
)
NEAR_MISSES_THAT_ARE_NOT_THE_WORDS = (
    "python3 -I memory_dream.py promoted RUN --apply",
    "python3 -I memory_hygiene.py restored --snapshot S --file a.md --apply",
    "python3 -I memory_dream.py promote RUN --applyx",
    "python3 -I memory_dream_run.py run --allow-secret-shapesx",
)
QUIET_INSIDE_FRAMES = (
    "python3 -I memory_dream.py promote $(echo x # it's\n); echo --apply",
    "echo 'C:\\'; python3 -I memory_dream.py promote RUN; echo --apply",
    "echo `echo a # it's\n`; python3 -I memory_dream.py promote RUN; echo --apply",
)


class RoundThreeTests(unittest.TestCase):
    def assert_asks(self, commands):
        for command in commands:
            with self.subTest(command):
                self.assertIn("ask", gate.decision(payload(command)))

    def assert_quiet(self, commands):
        for command in commands:
            with self.subTest(command):
                self.assertEqual(gate.decision(payload(command)), "")

    def test_an_option_with_a_value_or_the_device_file_before_the_stdin_script(self):
        self.assert_asks(STDIN_WITH_OPTIONS)

    def test_a_dollar_dollar_quote_is_not_an_ansi_c_quote(self):
        self.assert_asks(DOLLAR_DOLLAR_QUOTE)
        # after $$ the quote is a plain one: `\'` closes it and the text is then unbalanced, so it is judged whole; after a lone $ it is ANSI-C
        self.assertEqual(len(gate.logical_commands("echo $$'a\\'; b'; echo c")), 1)
        self.assertEqual(len(gate.logical_commands("echo $'a\\'; b'; echo c")), 2)

    def test_the_names_are_read_in_any_case(self):
        self.assert_asks(UPPER_CASE_AND_SHAPES)

    def test_a_subshell_or_a_substitution_is_judged_as_one_command(self):
        self.assert_asks(CONSERVATIVE_ASKS)

    def test_the_words_must_stand_alone(self):
        self.assert_quiet(NEAR_MISSES_THAT_ARE_NOT_THE_WORDS)

    def test_a_comment_or_a_backslash_inside_a_frame_is_read_as_the_shell_reads_it(self):
        self.assert_quiet(QUIET_INSIDE_FRAMES)

    def test_the_patterns_stay_linear_on_text_built_to_make_them_search(self):
        import time

        for command in ("python3 " * 30000, "python3 -W " * 20000, "python3 x " * 25000 + " promote --apply", "memory_dream.py " * 15000):
            with self.subTest(command[:20]):
                started = time.monotonic()
                gate.needs_confirmation(command)
                self.assertLess(time.monotonic() - started, 10.0)  # about 0.3 s; an unbounded pattern takes minutes

    def test_a_json_parser_that_runs_out_of_recursion_is_judged_on_the_raw_text(self):
        from unittest import mock

        with mock.patch.object(gate.json, "loads", side_effect=RecursionError("deep")):
            self.assertEqual(gate.decision("[[[["), "")
            self.assertIn("ask", gate.decision('{"command": "python3 memory_dream.py promote RUN --apply"} [[[['))

    def test_deeply_nested_json_is_silent_not_a_crash(self):
        self.assertEqual(gate.decision("[" * 5000 + "]" * 5000), "")

    def test_the_wait_for_stdin_is_shorter_than_every_hook_timeout(self):
        with open(os.path.join(os.path.dirname(th.SCRIPTS), "hooks", "hooks.json"), encoding="utf-8") as fh:
            hooks = json.load(fh)
        timeouts = [h["timeout"] for block in hooks["hooks"]["PreToolUse"] for h in block["hooks"]]
        self.assertTrue(timeouts)
        self.assertLess(gate.STDIN_WAIT_SECONDS, min(timeouts))

    def test_main_is_silent_and_exits_zero_whatever_goes_wrong_inside(self):
        from unittest import mock

        with mock.patch.object(gate, "_read_stdin", return_value=("x", False)), mock.patch.object(gate, "decision", side_effect=RuntimeError("boom")):
            self.assertEqual(gate.main(), 0)


if __name__ == "__main__":
    unittest.main()
