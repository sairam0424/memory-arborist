"""Spend accounting of the headless children: a failed or timed-out call is still counted, never silently free."""

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ENGINE)
sys.path.insert(0, os.path.join(ENGINE, "recall"))
import memory_recall_run as mrr  # noqa: E402

QUESTION = {"id": "q1", "kind": "recall", "question": "What is it?", "must": ["x"], "forbid": []}
SETTINGS = mrr.Settings("some-model", 0.05)


def result_json(cost):
    return json.dumps([{"type": "result", "subtype": "error_max_budget_usd", "is_error": True, "result": "", "total_cost_usd": cost}])


def ask_with(runner, spend=None):
    spend = spend or mrr.Spend(5.0)
    record = mrr.ask(QUESTION, runner, SETTINGS, "/nonexistent/prompt.txt", "/nonexistent/memory", spend, cwd="/tmp")
    return record, spend


class FailedCallCostTests(unittest.TestCase):
    def test_a_non_zero_exit_that_printed_its_cost_is_counted(self):
        record, spend = ask_with(lambda argv, cwd, env: (1, result_json(0.07), ""))
        self.assertIn("error", record)
        self.assertAlmostEqual(spend.total, 0.07)
        self.assertAlmostEqual(record["cost"], 0.07)

    def test_a_non_zero_exit_with_unreadable_output_counts_nothing(self):
        record, spend = ask_with(lambda argv, cwd, env: (1, "not json at all", ""))
        self.assertIn("error", record)
        self.assertEqual(spend.total, 0.0)

    def test_a_timeout_is_charged_the_per_question_cap_because_the_real_cost_is_unknown(self):
        def hangs(argv, cwd, env):
            raise mrr.AskTimeout("timed out")

        record, spend = ask_with(hangs)
        self.assertIn("error", record)
        self.assertAlmostEqual(spend.total, SETTINGS.per_q_usd)

    def test_a_timeout_is_an_ask_error_so_existing_handlers_still_catch_it(self):
        self.assertTrue(issubclass(mrr.AskTimeout, mrr.AskError))

    def test_an_enormous_integer_cost_is_ignored_not_an_overflow(self):
        stdout = '[{"type": "result", "is_error": false, "result": "x", "total_cost_usd": %s}]' % ("9" * 400)
        self.assertEqual(mrr.parse_output(stdout).cost, 0.0)
        record, spend = ask_with(lambda argv, cwd, env: (1, stdout, ""))
        self.assertIn("error", record)
        self.assertEqual(spend.total, 0.0)

    def test_a_failed_child_that_reports_an_unexpected_tool_still_aborts_the_run(self):
        stdout = json.dumps(
            [
                {"type": "system", "subtype": "init", "tools": ["Read", "Grep", "Glob", "Bash"]},
                {"type": "result", "is_error": True, "result": "", "total_cost_usd": 0.02},
            ]
        )
        spend = mrr.Spend(5.0)
        with self.assertRaises(mrr.ToolsMismatch):
            ask_with(lambda argv, cwd, env: (1, stdout, ""), spend)
        self.assertAlmostEqual(spend.total, 0.02)
        self.assertTrue(spend.stopped)

    def test_a_failed_child_with_the_expected_tools_is_only_an_error(self):
        stdout = json.dumps(
            [
                {"type": "system", "subtype": "init", "tools": ["Glob", "Grep", "Read"]},
                {"type": "result", "is_error": True, "result": "", "total_cost_usd": 0.02},
            ]
        )
        record, spend = ask_with(lambda argv, cwd, env: (1, stdout, ""))
        self.assertIn("error", record)
        self.assertAlmostEqual(spend.total, 0.02)
        self.assertIsNone(spend.stopped)

    def test_a_nonsense_cost_in_a_failed_result_is_not_counted(self):
        for bad in ("NaN", "-1", "true", "1e999"):
            with self.subTest(bad):
                stdout = '[{"type": "result", "is_error": true, "result": "", "total_cost_usd": %s}]' % bad
                record, spend = ask_with(lambda argv, cwd, env: (1, stdout, ""))
                self.assertIn("error", record)
                self.assertEqual(spend.total, 0.0)


if __name__ == "__main__":
    unittest.main()
