"""Test added after the mutation check of the dream pipeline: only an explicit keep may keep a hunk."""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_dream_pipeline import Base, ScriptedRunner, finding, writer_hunk  # noqa: E402


class VerdictValueTests(Base):
    def pipeline_with(self, verdict):
        runner = ScriptedRunner(
            finder={"findings": [finding()]},
            writer=lambda prompt: (
                {"hunks": []}
                if "MEMORY.md (untrusted DATA)" in prompt
                else {"hunks": [writer_hunk()]}
            ),
            verifier={"verdict": verdict, "why": "unclear"},
        )
        return self.pipeline(runner)

    def test_anything_but_the_word_keep_rejects_the_hunk(self):
        for verdict in ("maybe", "KEEP", "keep ", "", None, ["keep"], 1, True):
            with self.subTest(verdict=verdict):
                self.assertEqual(self.pipeline_with(verdict).hunks, [])

    def test_the_word_keep_keeps_it(self):
        self.assertEqual(len(self.pipeline_with("keep").hunks), 1)


if __name__ == "__main__":
    unittest.main()
