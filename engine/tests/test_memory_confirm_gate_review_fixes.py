"""Found by the independent code review of the confirmation hook: forms of a gated command that the text scan read as something
else (a nested quote, an escaped #, a ) that closes nothing, a module name, a script name last), a read error that threw away
what it had read, and a scan with no size bound. Each command below reaches the script with the gated flag under bash 5 and zsh (the bracket cases are a syntax error in the bash 3.2 that ships with macOS)."""

import json
import os
import re
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import memtest_helpers as th  # noqa: E402

sys.path.insert(0, th.SCRIPTS)
import memory_confirm_gate as gate  # noqa: E402

HOOKS_JSON = os.path.join(os.path.dirname(th.SCRIPTS), "hooks", "hooks.json")

NESTED_QUOTES = (  # a quoted string inside $( ) or backticks inside double quotes, holding a separator
    'python3 -I engine/memory_dream.py --dir "$(cd "/srv/R&D/memory" && pwd)" promote 20261114T210119Z --apply --expect 0123456789abcdef',
    'python3 -I memory_dream.py --ops-root "$(echo "/x;y")" promote RUN --apply',
    'python3 -I memory_dream.py --dir "$(dirname "/srv/a;b/memory")" promote RUN --apply',
    'python3 -I memory_dream.py --dir "`dirname "/srv/a|b/memory"`" promote RUN --apply',
    'python3 -I memory_hygiene.py --dir "$(cd "/srv/R&D" && pwd)" restore --snapshot S --file a.md --apply --force',
)
ESCAPED_HASH = (  # a # after an escaped character is part of the word, not a comment
    "python3 -I engine/memory_dream.py --dir /Users/example/Notes\\ #2/memory promote RUN --apply --expect 0123456789abcdef",
    "python3 -I memory_dream.py --ops-root ~/Backups\\;#2 promote RUN --apply",
    "python3 -I memory_hygiene.py --dir /Users/example/Notes\\ #2/memory restore --snapshot S --file a.md --apply --force",
    "python3 -I memory_dream.py --dir /x\\(#y promote RUN --apply",
)
CLOSERS_THAT_CLOSE_NOTHING = (  # a ) inside ${ } or a case arm does not end the $( ) around it
    "python3 memory_dream.py promote RUN $(case x in x) echo;; esac) --apply",
    "python3 memory_dream.py --dir $(echo ${v/)/}; echo /x) promote RUN --apply",
    "python3 memory_dream.py --dir $(echo ${v:-)}; echo /x) promote RUN --apply",
)
OTHER_SPELLINGS = (  # the module form, another case, the script read from stdin
    "python3 -m memory_dream --dir /x promote RUN --apply --expect D",
    "cd engine && python3 -B -m memory_dream promote RUN --apply --expect D",
    "python3 -m memory_hygiene restore --snapshot S --file a.md --apply --force",
    "python3 -m memory_dream_run run --allow-secret-shapes",
    "python3 MEMORY_DREAM.PY promote RUN --apply",
    "python3 -B - --dir /x promote RUN --apply --expect D < memory_dream.py",
    "python3 - --arm live --allow-secret-shapes < recall/memory_recall.py",
)
STILL_QUIET = (
    'echo "$(echo "a;b")"; python3 -I memory_dream.py promote RUN | cat; echo --apply',
    "python3 -I memory_dream.py --dir /Users/example/Notes\\ #2/memory promote RUN",
    "python3 -I memory_dream.py promote RUN # --apply",
    "python3 -m memory_dream promote RUN",
    "python3 -m memory_dream_run run",
    "python3 -m memory_dream promote RUN; echo --apply",
    # a quote, a ) or a # inside another quote means nothing: each of these stays one command per segment
    'echo "it\'s"; python3 -I memory_dream.py promote RUN; echo --apply',
    'echo "a)"; python3 -I memory_dream.py promote RUN; echo --apply',
    'echo "a #b"; python3 -I memory_dream.py promote RUN; echo --apply',
    "echo 'a #b'; python3 -I memory_dream.py promote RUN; echo --apply",
)


def payload(command, tool="Bash"):
    return json.dumps({"tool_name": tool, "tool_input": {"command": command}})


class ScannerFixTests(unittest.TestCase):
    def assert_asks(self, commands):
        for command in commands:
            with self.subTest(command):
                self.assertIn("ask", gate.decision(payload(command)))

    def test_a_nested_quote_inside_a_substitution_does_not_hide_the_flag(self):
        self.assert_asks(NESTED_QUOTES)

    def test_a_hash_after_an_escaped_character_is_not_a_comment(self):
        self.assert_asks(ESCAPED_HASH)

    def test_a_hash_inside_braces_quotes_or_a_word_is_not_a_comment(self):
        self.assert_asks(
            (
                "python3 -I memory_dream.py promote RUN ${v:- #x} --apply",
                'python3 -I memory_dream.py promote RUN "a #b" --apply',
                "python3 -I memory_dream.py promote RUN 'a #b' --apply",
                "python3 -I memory_dream.py promote RUN x#y --apply",
            )
        )

    def test_a_closing_bracket_that_closes_nothing_does_not_split_the_command(self):
        self.assert_asks(CLOSERS_THAT_CLOSE_NOTHING)

    def test_the_module_form_another_case_and_a_script_read_from_stdin_are_gated(self):
        self.assert_asks(OTHER_SPELLINGS)

    def test_the_forms_that_do_not_apply_anything_stay_quiet(self):
        for command in STILL_QUIET:
            with self.subTest(command):
                self.assertEqual(gate.decision(payload(command)), "")

    def test_a_real_comment_and_a_nested_quote_still_split_where_bash_splits(self):
        split = gate.logical_commands
        self.assertEqual(
            split('echo "$(echo "a;b")"; echo x'), ['echo "$(echo "a;b")"', " echo x"]
        )
        self.assertEqual(split("echo a # b; c\necho d"), ["echo a ", "echo d"])
        self.assertEqual(split("echo a\\ #b; c"), ["echo a\\ #b", " c"])


class FallbackPinTests(unittest.TestCase):
    """The scan judges the WHOLE text, not its segments, when it cannot trust itself. The inputs here are gated only as one command."""

    def test_an_unbalanced_quote_is_judged_as_one_command(self):
        self.assertIn(
            "ask",
            gate.decision(
                payload("python3 -I memory_dream.py promote RUN; echo 'it --apply")
            ),
        )

    def test_a_here_document_is_judged_as_one_command(self):
        self.assertIn(
            "ask",
            gate.decision(
                payload("python3 -I memory_dream.py promote RUN <<EOF\n--apply\nEOF")
            ),
        )

    def test_a_backslash_carriage_return_newline_joins_a_split_flag(self):
        self.assertIn(
            "ask",
            gate.decision(
                payload("python3 -I memory_dream.py promote RUN --ap\\\r\nply")
            ),
        )


class ReadErrorTests(unittest.TestCase):
    def test_a_read_that_fails_midway_keeps_what_it_read_and_asks(self):
        first = payload("python3 -I memory_dream.py promote RUN --apply").encode()[:90]
        chunks = iter([first])

        def read(fd, size):
            try:
                return next(chunks)
            except StopIteration:
                raise ConnectionResetError("reset")

        with (
            mock.patch.object(gate.select, "select", return_value=([0], [], [])),
            mock.patch.object(gate.os, "read", read),
        ):
            text, cut = gate._read_stdin()
        self.assertTrue(cut)
        self.assertIn("memory_dream.py promote", text)
        self.assertIn("ask", gate.decision(text, cut))


class ScanBudgetTests(unittest.TestCase):
    def test_an_oversize_command_is_judged_whole_without_the_scan(self):
        padding = "|" * (gate.MAX_SCAN_CHARS + 1)
        gated = "echo %s; python3 -I memory_dream.py promote RUN --apply" % padding
        harmless = "echo %s; ls" % padding
        with mock.patch.object(
            gate, "logical_commands", side_effect=AssertionError("the scan ran")
        ):
            self.assertIn("ask", gate.decision(payload(gated)))
            self.assertEqual(gate.decision(payload(harmless)), "")

    def test_a_command_just_under_the_limit_is_still_scanned(self):
        command = "python3 -I memory_dream.py promote RUN %s; echo --apply" % (
            "x" * (gate.MAX_SCAN_CHARS - 200)
        )
        self.assertEqual(gate.decision(payload(command)), "")


class HooksRuleTests(unittest.TestCase):
    """The host's `if` rule decides whether the gate starts at all. Two things were measured on Claude Code 2.1.326 with a
    diagnostic plugin (2026-11-18): the glob is matched against the command WITHOUT its redirections (a script name that is only
    a `< file` or `> file` target does not match), so the rules key on the two flags, which are always arguments. The matcher is
    modelled here as that glob over the command with its redirections removed."""

    REDIRECTION = re.compile(r"\s*[<>]+\s*\S+")

    @staticmethod
    def rule_regex(rule):
        inner = re.fullmatch(r"Bash\((.*)\)", rule).group(1)
        return re.compile(".*".join(re.escape(part) for part in inner.split("*")), re.S)

    def setUp(self):
        with open(HOOKS_JSON, encoding="utf-8") as fh:
            hooks = json.load(fh)
        handlers = [h for block in hooks["hooks"]["PreToolUse"] for h in block["hooks"]]
        self.rules = [self.rule_regex(h["if"]) for h in handlers]

    def starts_the_gate(self, command):
        seen = self.REDIRECTION.sub("", command)
        return any(rx.fullmatch(seen) for rx in self.rules)

    def test_every_spelling_the_gate_recognises_also_starts_the_gate(self):
        for command in NESTED_QUOTES + ESCAPED_HASH + CLOSERS_THAT_CLOSE_NOTHING + OTHER_SPELLINGS:
            with self.subTest(command):
                self.assertTrue(self.starts_the_gate(command), command)

    def test_a_script_name_that_is_only_a_redirection_target_does_not_stop_the_gate_from_starting(self):
        command = "python3 -B - promote RUN --apply --expect abc < engine/memory_dream.py"
        self.assertTrue(self.starts_the_gate(command))
        name_rule = self.rule_regex("Bash(*memory_*)")  # the rule this replaced: it never saw the name
        self.assertIsNone(name_rule.fullmatch(self.REDIRECTION.sub("", command)))

    def test_a_flag_that_is_followed_by_more_arguments_starts_the_gate(self):
        for command in (
            "python3 -I recall/memory_recall.py --allow-secret-shapes --arm live",
            "python3 -I memory_dream_run.py run --allow-secret-shapes \\\n  --workers 2",
        ):
            with self.subTest(command):
                self.assertTrue(self.starts_the_gate(command), command)

    def test_a_command_without_either_flag_does_not_start_the_gate(self):
        for command in ("ls ~/.claude", "git commit -m 'fix the promote step'", "python3 -I memory_dream.py promote RUN"):
            with self.subTest(command):
                self.assertFalse(self.starts_the_gate(command), command)


if __name__ == "__main__":
    unittest.main()
