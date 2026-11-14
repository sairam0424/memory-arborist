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
