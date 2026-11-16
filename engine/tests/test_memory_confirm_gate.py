"""The confirmation hook asks before the commands that change live notes, and says nothing otherwise."""

import json
import os
import re
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import memtest_helpers as th  # noqa: E402

sys.path.insert(0, th.SCRIPTS)
import memory_confirm_gate as gate  # noqa: E402

HOOKS_JSON = os.path.join(os.path.dirname(th.SCRIPTS), "hooks", "hooks.json")
GATED_COMMANDS = (
    'python3 -I "${CLAUDE_PLUGIN_ROOT}/engine/memory_dream.py" promote 20261114T210119Z --apply --expect 0123456789abcdef',
    "python3 -I engine/memory_dream.py --dir /x --ops-root /y promote RUN --apply --expect D",
    "python3 -I memory_dream.py promote RUN --expect D --apply",
    "python3 -I memory_hygiene.py restore --snapshot S --file a.md --apply --force",
    "python3 -I memory_hygiene.py --dir /x restore --snapshot S --file a.md --apply",
    "python3 -I memory_dream_run.py run --allow-secret-shapes",
    "python3 -I recall/memory_recall.py --arm live --allow-secret-shapes",
    "cd /tmp && python3 -I memory_dream.py promote RUN --apply",
    # forms the first version missed (found by the independent review)
    'python3 -I "/p/engine/memory_dream.py" promote 20261114T210119Z \\\n    --apply --expect 0123456789abcdef',
    "python3 -I memory_dream.py \\\n  --dir /x promote RUN --apply",
    "python3 -I memory_hygiene.py restore --snapshot S --file a.md \\\n  --apply --force",
    "python3 -I memory_dream_run.py run \\\n  --allow-secret-shapes",
    'python3 -I memory_dream.py promote "$(ls -1t /ops/dream | head -1)" --apply --expect D',
    'python3 -I memory_dream.py --ops-root "/data/a;b" promote RUN --apply',
    'python3 -I memory_dream.py --dir "/srv/data/R&D/memory" promote RUN --apply',
    "(cd /x && python3 -I memory_dream.py promote RUN --apply)",
    "bash -c 'python3 -I memory_dream.py promote RUN --apply'",
    "python3 -I memory_dream.py\tpromote\tRUN\t--apply",
    "echo start\npython3 -I memory_dream.py promote RUN --apply",
)
QUIET_COMMANDS = (
    "python3 -I memory_dream.py promote 20261114T210119Z",
    "python3 -I memory_hygiene.py restore --snapshot S --file a.md",
    "python3 -I memory_hygiene.py snapshot",
    "python3 -I memory_dream_run.py run",
    "git commit -m 'fix the promote step'",
    "ls ~/.claude",
    "python3 -I memory_dream.py promote RUN | cat; echo --apply",
    "python3 -I memory_dream.py promote RUN && echo --apply",
    "echo --allow-secret-shapes",
    'echo "memory_dream.py promote RUN"; echo --apply',
    "python3 -I memory_dream.py promote RUN \\\n | cat\necho --apply",
    "",
)


def payload(command, tool="Bash"):
    return json.dumps(
        {"tool_name": tool, "tool_input": {"command": command}, "cwd": "/x"}
    )


class DecisionTests(unittest.TestCase):
    def test_the_commands_that_change_live_notes_get_an_ask(self):
        for command in GATED_COMMANDS:
            with self.subTest(command):
                answer = json.loads(gate.decision(payload(command)))
                out = answer["hookSpecificOutput"]
                self.assertEqual(out["hookEventName"], "PreToolUse")
                self.assertEqual(out["permissionDecision"], "ask")

    def test_the_reason_is_fixed_text_that_never_echoes_the_command(self):
        command = "python3 -I memory_dream.py promote RUN --apply --expect abc123SECRETLOOKINGTEXT"
        out = gate.decision(payload(command))
        self.assertEqual(
            json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"],
            gate.REASON,
        )
        self.assertNotIn("abc123", out)

    def test_everything_else_is_silent(self):
        for command in QUIET_COMMANDS:
            with self.subTest(command):
                self.assertEqual(gate.decision(payload(command)), "")

    def test_only_bash_calls_are_looked_at(self):
        self.assertEqual(gate.decision(payload(GATED_COMMANDS[0], tool="Write")), "")

    def test_bad_input_is_silent_and_never_raises(self):
        for text in (
            "",
            "not json",
            "[]",
            "null",
            '{"tool_name": "Bash"}',
            '{"tool_name": "Bash", "tool_input": []}',
            '{"tool_name": "Bash", "tool_input": {"command": 5}}',
            "{" * 5000,
        ):
            with self.subTest(text[:20]):
                self.assertEqual(gate.decision(text), "")


class LogicalCommandTests(unittest.TestCase):
    def test_separators_split_only_outside_quotes_and_substitutions(self):
        split = gate.logical_commands
        self.assertEqual(split("a | b; c && d\ne"), ["a ", " b", " c ", "", " d", "e"])
        self.assertEqual(split('echo "a;b|c" x'), ['echo "a;b|c" x'])
        self.assertEqual(split("echo 'a&b' x"), ["echo 'a&b' x"])
        self.assertEqual(split("echo $(ls | head -1) y"), ["echo $(ls | head -1) y"])
        self.assertEqual(split("echo `ls | head` y"), ["echo `ls | head` y"])
        self.assertEqual(split("a \\\n b"), ["a   b"])
        self.assertEqual(split("cmd 2>&1 | tee"), ["cmd 2>&1 ", " tee"])

    def test_an_escaped_quote_does_not_open_a_quote(self):
        self.assertEqual(gate.logical_commands("echo \\\" a; b"), ['echo \\" a', " b"])

    def test_a_very_long_padded_command_is_still_gated(self):
        padding = "x" * 200_000
        command = "echo %s; python3 -I memory_dream.py promote RUN --apply" % padding
        self.assertIn("ask", gate.decision(payload(command)))
        quiet = "python3 -I memory_dream.py promote RUN %s; echo --apply" % padding
        self.assertEqual(gate.decision(payload(quiet)), "")


class AbbreviationTests(unittest.TestCase):
    """An abbreviated flag must not slip past the hook: the two parsers that take --apply refuse abbreviations."""

    def test_an_abbreviated_apply_is_a_usage_error_in_both_commands(self):
        import contextlib
        import io

        import memory_dream as dr
        import memory_hygiene as mh

        for build, argv in (
            (dr.build_parser, ["promote", "20261114T210119Z", "--app", "--expect", "x"]),
            (dr.build_parser, ["promote", "20261114T210119Z", "--a"]),
            (mh.build_parser, ["restore", "--snapshot", "S", "--file", "a.md", "--app"]),
            (mh.build_parser, ["restore", "--snapshot", "S", "--file", "a.md", "--a"]),
        ):
            with self.subTest(argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
                build().parse_args(argv)
            self.assertEqual(ctx.exception.code, 64)

    def test_the_full_flag_still_parses(self):
        import memory_dream as dr

        self.assertTrue(dr.build_parser().parse_args(["promote", "20261114T210119Z", "--apply"]).apply)


class HooksFileTests(unittest.TestCase):
    def setUp(self):
        with open(HOOKS_JSON, encoding="utf-8") as fh:
            self.hooks = json.load(fh)

    @staticmethod
    def rule_regex(rule):
        inner = re.fullmatch(r"Bash\((.*)\)", rule).group(1)
        return re.compile(".*".join(re.escape(part) for part in inner.split("*")), re.S)

    def handlers(self):
        return [
            h for block in self.hooks["hooks"]["PreToolUse"] for h in block["hooks"]
        ]

    def test_every_gated_command_is_covered_by_one_of_the_if_rules(self):
        rules = [self.rule_regex(h["if"]) for h in self.handlers()]
        for command in GATED_COMMANDS:
            with self.subTest(command):
                self.assertTrue(any(rx.fullmatch(command) for rx in rules), command)

    def test_the_pre_tool_use_handlers_filter_on_bash_and_run_the_gate_in_exec_form(
        self,
    ):
        for block in self.hooks["hooks"]["PreToolUse"]:
            self.assertEqual(block["matcher"], "Bash")
        for handler in self.handlers():
            self.assertTrue(handler["if"].startswith("Bash("))
            self.assertEqual(handler["command"], "python3")
            self.assertEqual(
                handler["args"],
                ["-B", "-I", "${CLAUDE_PLUGIN_ROOT}/engine/memory_confirm_gate.py"],
            )
            self.assertLessEqual(handler["timeout"], 5)

    def test_the_description_mentions_both_hooks_and_that_nothing_is_denied(self):
        text = self.hooks["description"]
        self.assertIn("SessionStart", text)
        self.assertIn("PreToolUse", text)
        self.assertIn("never denies", text)


class ProcessTests(unittest.TestCase):
    """The real program, as Claude Code starts it: JSON on stdin, JSON or nothing on stdout, exit 0."""

    def run_gate(self, stdin_text):
        return subprocess.run(
            [
                sys.executable,
                "-B",
                "-I",
                os.path.join(th.SCRIPTS, "memory_confirm_gate.py"),
            ],
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_a_gated_command_prints_one_json_line_and_exits_zero(self):
        done = self.run_gate(payload(GATED_COMMANDS[0]))
        self.assertEqual(done.returncode, 0)
        self.assertEqual(
            json.loads(done.stdout)["hookSpecificOutput"]["permissionDecision"], "ask"
        )
        self.assertEqual(done.stderr, "")

    def test_a_quiet_command_and_garbage_both_print_nothing_and_exit_zero(self):
        for text in (payload("ls"), "garbage"):
            done = self.run_gate(text)
            self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))


if __name__ == "__main__":
    unittest.main()
