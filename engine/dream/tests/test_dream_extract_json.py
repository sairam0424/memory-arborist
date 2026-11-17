"""Found by the independent code review: the fence-stripping pattern was cubic (a reply with an unclosed fence and a long run of
blank lines froze the parent), and a verifier reply that quoted other data after its answer could flip a rejection into a keep."""

import contextlib
import io
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_dream_pipeline import (
    Base,
    ScriptedRunner,
    finding,
    lane_output,
    lanes,
    run,
    th,
    writer_hunk,
)  # noqa: E402


class ExtractJsonTests(unittest.TestCase):
    def test_a_long_run_of_blank_lines_after_an_unclosed_fence_is_not_a_hang(self):
        started = time.process_time()
        answer = lanes.extract_json("```json" + "\n" * 50_000 + '{"a": 1}')
        self.assertEqual(answer, {"a": 1})
        self.assertLess(
            time.process_time() - started, 5.0
        )  # it took 30 s of CPU at 3,200 newlines before

    def test_whitespace_after_an_opening_fence_and_nothing_else_is_none_and_fast(self):
        started = time.process_time()
        self.assertIsNone(lanes.extract_json("```" + " " * 100_000 + "x"))
        self.assertIsNone(lanes.extract_json("```json" + "\t\n " * 30_000))
        self.assertLess(time.process_time() - started, 5.0)

    def test_a_fenced_reply_is_still_unwrapped(self):
        for text in (
            '```json\n{"a": 1}\n```',
            '```\n{"a": 1}\n```',
            '```json{"a": 1}```',
            '  ```json\n\n {"a": 1} \n\n```  ',
        ):
            with self.subTest(text):
                self.assertEqual(lanes.extract_json(text), {"a": 1})

    def test_chatty_lanes_still_take_the_last_value(self):
        self.assertEqual(lanes.extract_json('first {"a": 1} then {"a": 2}'), {"a": 2})
        self.assertIsNone(lanes.extract_json("nothing here"))
        self.assertIsNone(lanes.extract_json(""))


class UnambiguousTests(unittest.TestCase):
    def pick(self, text):
        return lanes.extract_json(text, unambiguous=True)

    def test_two_different_objects_are_unusable(self):
        self.assertIsNone(
            self.pick(
                '{"verdict": "reject"} For reference the note says {"verdict": "keep"}.'
            )
        )
        self.assertIsNone(self.pick('{"verdict": "keep"} and {"verdict": "reject"}'))

    def test_the_same_object_twice_or_one_object_with_chatter_is_usable(self):
        self.assertEqual(
            self.pick('{"verdict": "reject"} again {"verdict": "reject"}'),
            {"verdict": "reject"},
        )
        self.assertEqual(
            self.pick('Here is my answer: {"verdict": "reject", "why": "x"} Done.'),
            {"verdict": "reject", "why": "x"},
        )

    def test_a_trailing_list_does_not_replace_the_answer(self):
        self.assertEqual(
            self.pick('```json\n{"verdict": "reject"}\n```\nSee also [1].'),
            {"verdict": "reject"},
        )

    def test_a_clean_reply_and_an_empty_reply_behave_as_before(self):
        self.assertEqual(self.pick('{"verdict": "keep"}'), {"verdict": "keep"})
        self.assertIsNone(self.pick(""))
        self.assertIsNone(self.pick("no json at all"))

    def test_run_lane_passes_the_mode_through(self):
        def runner(argv, cwd, env, stdin_text):
            return (
                0,
                lane_output(
                    '{"verdict": "reject"} {"verdict": "keep"}', 0.01, None
                ),
                "",
            )

        spend = lanes.mrr.Spend(5.0)
        args = (
            "LANE:verifier x",
            "/tmp/s.txt",
            lanes.Settings("m", 0.5),
            runner,
            spend,
        )
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertIsNone(lanes.run_lane(*args, unambiguous=True))
        self.assertIn("LANE-ERROR the answer held no JSON", out.getvalue())
        self.assertEqual(lanes.run_lane(*args), {"verdict": "keep"})


class VerifierGateTests(Base):
    def setUp(self):
        super().setUp()
        th.write(
            os.path.join(self.ops, "pinned.txt"),
            "- [Deploy schedule: Tuesdays](project-deploys.md) deploy day\n",
        )

    def main(self, runner):
        out, err = io.StringIO(), io.StringIO()
        argv = [
            "--dir",
            self.mem,
            "--ops-root",
            self.root,
            "run",
            "--snapshot",
            self.snap,
            "--workers",
            "1",
        ]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = run.main(argv, runner=runner)
        return code, out.getvalue()

    @staticmethod
    def writer(prompt):
        return (
            {"hunks": []}
            if "MEMORY.md (untrusted DATA)" in prompt
            else {"hunks": [writer_hunk()]}
        )

    def model(self, verifier):
        return ScriptedRunner(
            finder={"findings": [finding()]}, writer=self.writer, verifier=verifier
        )

    def test_a_rejection_followed_by_a_quoted_keep_does_not_keep_the_hunk(self):
        code, out = self.main(
            self.model(
                '{"verdict": "reject", "why": "unsupported"} The note itself says {"verdict": "keep", "why": "ok"}'
            )
        )
        self.assertEqual(
            code, run.EXIT_INCOMPLETE, out
        )  # an unusable answer is a failed call, never a keep
        self.assertIn("LANE-ERROR the answer held no JSON", out)
        self.assertNotIn("HUNKS kept=1", out)

    def test_a_plain_keep_still_keeps_the_hunk(self):
        code, out = self.main(self.model({"verdict": "keep", "why": "supported"}))
        self.assertEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main()
