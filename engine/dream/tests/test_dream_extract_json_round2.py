"""Found by a third verification pass: with `unambiguous` (the verifier, the only model gate) the distinct-object check compared every
object with every earlier one, so a reply of 200,000 characters holding 100,000 small objects cost tens of seconds of CPU. The answer
must not change: one distinct object is the answer, two different ones are unusable."""

import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_dream_pipeline import lanes  # noqa: E402


class UnambiguousCostTests(unittest.TestCase):
    def test_a_reply_of_many_equal_objects_is_one_answer_and_cheap(self):
        text = "{}" * 100_000
        started = time.process_time()
        self.assertEqual(lanes.extract_json(text, unambiguous=True), {})
        self.assertLess(
            time.process_time() - started, 5.0
        )  # about 20 s of CPU before the fix

    def test_many_equal_objects_and_one_different_are_unusable_and_cheap(self):
        for label, text in (
            ("last", "{}" * 99_990 + '{"a": 1}'),
            ("first", '{"a": 1}' + "{}" * 99_990),
            ("middle", "{}" * 50_000 + '{"a": 1}' + "{}" * 50_000),
        ):
            with self.subTest(label):
                started = time.process_time()
                self.assertIsNone(lanes.extract_json(text, unambiguous=True))
                self.assertLess(time.process_time() - started, 5.0)

    def test_the_answers_are_unchanged(self):
        same = lanes.extract_json('first {"a": 1} then {"a": 1} done', unambiguous=True)
        self.assertEqual(same, {"a": 1})
        self.assertIsNone(lanes.extract_json('{"a": 1} {"a": 2}', unambiguous=True))
        self.assertEqual(
            lanes.extract_json('[1, 2] {"a": 1} [3]', unambiguous=True), {"a": 1}
        )
        self.assertIsNone(lanes.extract_json("[1, 2] [3]", unambiguous=True))
        self.assertEqual(
            lanes.extract_json('{"a": 1} {"a": 2}'), {"a": 2}
        )  # the plain mode still takes the last value


if __name__ == "__main__":
    unittest.main()
