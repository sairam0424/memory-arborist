"""The judge grades at most MAX_ANSWER_CHARS of an answer, so a key cannot be made to run on an unbounded text."""

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ENGINE)
sys.path.insert(0, os.path.dirname(HERE))
import memory_recall_run as mrr  # noqa: E402


def runner_answering(text):
    def run(_argv, _cwd, _env):
        items = [
            {"type": "system", "subtype": "init", "tools": ["Read", "Grep", "Glob"]},
            {
                "type": "result",
                "is_error": False,
                "result": text,
                "total_cost_usd": 0.001,
            },
        ]
        return 0, json.dumps(items), ""

    return run


class AnswerCapTests(unittest.TestCase):
    def ask(self, question, text):
        settings = mrr.Settings("m", 0.1)
        return mrr.ask(
            question,
            runner_answering(text),
            settings,
            "/nonexistent/prompt",
            "/nonexistent/mem",
            mrr.Spend(5.0),
            cwd=os.getcwd(),
        )

    def test_the_cap_is_eight_thousand_characters(self):
        self.assertEqual(mrr.MAX_ANSWER_CHARS, 8000)

    def test_text_past_the_cap_is_not_graded(self):
        question = {
            "id": "q",
            "kind": "recall",
            "question": "?",
            "must": ["MARKER"],
            "forbid": [],
        }
        inside = self.ask(
            question, "x" * (mrr.MAX_ANSWER_CHARS - len("MARKER")) + "MARKER"
        )
        outside = self.ask(question, "x" * mrr.MAX_ANSWER_CHARS + "MARKER")
        self.assertTrue(inside["pass"])
        self.assertFalse(outside["pass"])

    def test_a_forbidden_phrase_past_the_cap_is_not_seen_either(self):
        question = {
            "id": "q",
            "kind": "recall",
            "question": "?",
            "must": ["good"],
            "forbid": ["BAD"],
        }
        record = self.ask(question, "good " + "x" * mrr.MAX_ANSWER_CHARS + " BAD")
        self.assertTrue(record["pass"])
        self.assertFalse(record["forbid_hit"])

    def test_short_answers_are_graded_as_before(self):
        question = {
            "id": "q",
            "kind": "recall",
            "question": "?",
            "must": ["good"],
            "forbid": ["BAD"],
        }
        self.assertTrue(self.ask(question, "good")["pass"])
        self.assertFalse(self.ask(question, "good BAD")["pass"])


if __name__ == "__main__":
    unittest.main()
