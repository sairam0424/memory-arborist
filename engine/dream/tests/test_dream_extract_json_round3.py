"""Found by a fresh review of the whole-reply rule: a verifier reply with the same key twice was read as the LAST one (a reject followed by a
keep inside one object became a keep), and a reply nested tens of thousands deep passed the parser and then crashed the run with a
RecursionError, where it must be an unusable answer (INCOMPLETE). The same nesting in a finder's reply crashed the trace write.
A reply is data, not a structure: nothing a lane returns is nested more than a few levels."""

import contextlib
import io
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_dream_pipeline import (
    Base,
    ScriptedRunner,
    finding,
    lanes,
    run,
    th,
    writer_hunk,
)  # noqa: E402


def strict(text):
    return lanes.extract_json(text, unambiguous=True)


class RepeatedKeyTests(unittest.TestCase):
    def test_a_verifier_object_with_a_repeated_key_is_unusable(self):
        for text in (
            '{"verdict": "reject", "why": "a", "verdict": "keep"}',
            '{"verdict": "keep", "why": "a", "verdict": "reject"}',
            '{"verdict": "keep", "why": {"x": 1, "x": 2}}',
        ):
            with self.subTest(text):
                self.assertIsNone(strict(text))

    def test_the_plain_mode_of_the_other_lanes_is_unchanged(self):
        self.assertEqual(
            lanes.extract_json('{"verdict": "reject", "verdict": "keep"}'),
            {"verdict": "keep"},
        )

    def test_distinct_keys_are_fine(self):
        self.assertEqual(
            strict('{"verdict": "keep", "why": "a"}'), {"verdict": "keep", "why": "a"}
        )


class DepthTests(unittest.TestCase):
    def test_a_reply_nested_deeper_than_a_few_levels_is_never_returned_deep(self):
        # the verifier mode refuses it. The plain mode may, on a Python whose parser refuses the whole reply, fall back to the last small value
        # in it (the chatter rule: an innermost empty list); what it must never return is the deep structure itself
        for depth in (40, 500, 2000, 75_000):
            nested = "[" * depth + "]" * depth
            with self.subTest("verifier", depth=depth):
                self.assertIsNone(lanes.extract_json('{"verdict": "keep", "why": %s}' % nested, unambiguous=True))
            with self.subTest("finder", depth=depth):
                got = lanes.extract_json('{"findings": [], "evidence": %s}' % nested)
                self.assertTrue(lanes._shallow(got), "a value nested %d levels deep was returned" % depth)

    def test_ordinary_nesting_is_unchanged(self):
        reply = '{"findings": [{"note": "a.md", "claim": "c", "fact": "f", "evidence": [{"q": "x", "n": [1, [2, 3]]}]}]}'
        self.assertEqual(lanes.extract_json(reply)["findings"][0]["note"], "a.md")


class PipelineTests(Base):
    def setUp(self):
        super().setUp()
        th.write(
            os.path.join(self.ops, "pinned.txt"),
            "- [Deploy schedule: Tuesdays](project-deploys.md) deploy day\n",
        )

    def main(self, verifier):
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
        runner = ScriptedRunner(
            finder={"findings": [finding()]},
            writer=lambda prompt: (
                {"hunks": []}
                if "MEMORY.md (untrusted DATA)" in prompt
                else {"hunks": [writer_hunk()]}
            ),
            verifier=verifier,
        )
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = run.main(argv, runner=runner)
        return code, out.getvalue() + err.getvalue()

    def test_a_repeated_verdict_key_is_an_incomplete_run_not_a_keep(self):
        code, out = self.main(
            '{"verdict": "reject", "why": "unsupported", "verdict": "keep"}'
        )
        self.assertEqual(code, run.EXIT_INCOMPLETE, out)
        self.assertNotIn("HUNKS kept=1", out)

    def test_a_verifier_reply_nested_tens_of_thousands_deep_is_an_incomplete_run_not_a_traceback(
        self,
    ):
        deep = "[" * 75_000 + "]" * 75_000
        code, out = self.main('{"verdict": "keep", "why": %s}' % deep)
        self.assertEqual(code, run.EXIT_INCOMPLETE, out)
        self.assertNotIn("Traceback", out)


if __name__ == "__main__":
    unittest.main()
