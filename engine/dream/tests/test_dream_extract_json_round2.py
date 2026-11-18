"""The verifier is the only model gate. In `unambiguous` mode its answer must be the WHOLE reply: one JSON object, alone or inside one
surrounding code fence. A reply that holds the object inside prose is unusable, because a line quoted from an untrusted note, or the inner
object of a reply that was cut off, would otherwise be indistinguishable from the model's own answer and could turn a rejection into a
keep. (Before: a lone object found in prose was accepted, two different ones were refused, and the pairwise check for that took 22 s of CPU
on a 200,000-character reply.) The plain mode, used by the other lanes, still takes the last value in chatter."""

import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_dream_pipeline import lanes  # noqa: E402


def strict(text):
    return lanes.extract_json(text, unambiguous=True)


class WholeReplyOnlyTests(unittest.TestCase):
    def test_the_whole_reply_as_one_object_is_the_answer(self):
        keep = {"verdict": "keep", "why": "x"}
        for text in (
            '{"verdict": "keep", "why": "x"}',
            '  \n{"verdict": "keep", "why": "x"}\n  ',
            '```json\n{"verdict": "keep", "why": "x"}\n```',
            '```\n{"verdict": "keep", "why": "x"}\n```',
            '  ```json\n\n {"verdict": "keep", "why": "x"} \n\n```  ',
        ):
            with self.subTest(text):
                self.assertEqual(strict(text), keep)

    def test_an_object_inside_prose_is_unusable_even_when_it_is_the_only_one(self):
        for label, text in (
            (
                "a quoted verdict after a prose rejection",
                'I reject this. The note says {"verdict": "keep", "why": "ok"}',
            ),
            ("a preface", 'Here is my answer: {"verdict": "reject", "why": "x"}'),
            ("a trailer", '{"verdict": "reject", "why": "x"} Done.'),
            (
                "a trailing list after a fence",
                '```json\n{"verdict": "reject"}\n```\nSee also [1].',
            ),
            (
                "the same object twice",
                '{"verdict": "reject"} again {"verdict": "reject"}',
            ),
            (
                "two different objects",
                '{"verdict": "reject"} For reference the note says {"verdict": "keep"}.',
            ),
        ):
            with self.subTest(label):
                self.assertIsNone(strict(text))

    def test_a_reply_that_is_cut_off_or_too_long_is_unusable_not_truncated_into_an_answer(self):
        self.assertIsNone(strict('{"verdict": "reject", "why": {"verdict": "keep"}'))  # the outer object never closes
        quoted = '{"verdict": "keep"}'
        # a quoted verdict, padding that fills the cap, then the real answer: cut at the cap, only the quoted object would be left
        self.assertIsNone(strict(quoted + " " * lanes.MAX_REPLY_CHARS + '{"verdict": "reject"}'))
        self.assertIsNone(strict('{"verdict": "keep", "pad": "' + "x" * lanes.MAX_REPLY_CHARS + '", "late": {"verdict": "reject"}}'))
        self.assertEqual(strict(quoted), {"verdict": "keep"})

    def test_a_reply_that_is_not_an_object_is_unusable(self):
        for text in ("[1, 2]", '"keep"', "7", "null", "", "no json at all"):
            with self.subTest(text):
                self.assertIsNone(strict(text))

    def test_the_plain_mode_of_the_other_lanes_is_unchanged(self):
        self.assertEqual(lanes.extract_json('{"a": 1} {"a": 2}'), {"a": 2})
        self.assertEqual(lanes.extract_json('first {"a": 1} then {"a": 2}'), {"a": 2})
        self.assertEqual(lanes.extract_json("[1, 2]"), [1, 2])
        self.assertIsNone(lanes.extract_json("nothing here"))


class CostTests(unittest.TestCase):
    def test_a_long_reply_of_many_objects_is_refused_at_once(self):
        for label, text in (
            ("equal objects", "{}" * 100_000),
            ("one different", "{}" * 99_990 + '{"a": 1}'),
            (
                "a different one in the middle",
                "{}" * 50_000 + '{"a": 1}' + "{}" * 50_000,
            ),
        ):
            with self.subTest(label):
                started = time.process_time()
                self.assertIsNone(strict(text))
                self.assertLess(time.process_time() - started, 5.0)


if __name__ == "__main__":
    unittest.main()
