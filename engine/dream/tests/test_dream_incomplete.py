"""A run in which model calls failed says so: exit 5, an INCOMPLETE line, a banner on top of report.md, and the first line of
the child's own error instead of a bare 'claude exited with 1'. A missing pinned.txt stops the run before any paid call."""

import contextlib
import glob
import io
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_dream_pipeline import Base, ScriptedRunner, finding, lane_output, lanes, mrr, run, th, writer_hunk  # noqa: E402

PINNED = "- [Deploy schedule: Tuesdays](project-deploys.md) deploy day\n"


class FailureMessageTests(unittest.TestCase):
    def test_the_first_stderr_line_is_shown(self):
        message = mrr.failure_message(
            1, "", "\nNot logged in - run /login\nsecond line\n"
        )
        self.assertEqual(message, "claude exited with 1: Not logged in - run /login")

    def test_an_error_result_on_stdout_is_preferred_over_stderr(self):
        stdout = lane_output("Credit balance is too low", is_error=True)
        self.assertEqual(
            mrr.failure_message(1, stdout, "noise on stderr"),
            "claude exited with 1: Credit balance is too low",
        )

    def test_nothing_to_show_gives_the_bare_message(self):
        self.assertEqual(mrr.failure_message(3, "", "  \n"), "claude exited with 3")
        self.assertEqual(mrr.failure_message(3, "not json", ""), "claude exited with 3")

    def test_a_secret_shaped_line_is_withheld(self):
        message = mrr.failure_message(1, "", "bad key " + th.fake_github_token())
        self.assertEqual(message, "claude exited with 1: " + mrr.WITHHELD_FAILURE_LINE)
        self.assertNotIn(th.fake_github_token(), message)

    def test_a_line_that_mentions_a_credential_is_withheld_with_a_way_to_read_it(self):
        message = mrr.failure_message(1, "", "Invalid API key")
        self.assertNotIn("Invalid", message)
        self.assertIn("claude -p hi", message)

    def test_a_long_line_is_cut(self):
        message = mrr.failure_message(1, "", "x" * 500)
        self.assertLessEqual(
            len(message), len("claude exited with 1: ") + mrr.FAILURE_LINE_CHARS + 5
        )

    def test_ask_lane_carries_the_childs_own_error(self):
        def runner(argv, cwd, env, stdin_text):
            return 1, "", "Not logged in - run /login"

        with self.assertRaises(mrr.AskError) as ctx:
            lanes.ask_lane(
                "LANE:finder x",
                "/tmp/s.txt",
                lanes.Settings("m", 0.5),
                runner,
                mrr.Spend(5.0),
            )
        self.assertIn("claude exited with 1: Not logged in - run /login", str(ctx.exception))


class IncompleteRunTests(Base):
    def setUp(self):
        super().setUp()
        th.write(os.path.join(self.ops, "pinned.txt"), PINNED)

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
        return code, out.getvalue(), err.getvalue()

    def report(self):
        paths = glob.glob(os.path.join(self.ops, "dream", "*", "report.md"))
        self.assertEqual(len(paths), 1)
        with open(paths[0], encoding="utf-8") as fh:
            return fh.read()

    def test_a_clean_run_exits_zero_and_has_no_banner(self):
        code, out, _err = self.main(ScriptedRunner(finder={"findings": []}))
        self.assertEqual(code, 0, out)
        self.assertNotIn("INCOMPLETE", out)
        self.assertNotIn("INCOMPLETE", self.report())

    def test_the_ceiling_is_announced_before_the_first_model_call(self):
        code, out, _err = self.main(ScriptedRunner(finder={"findings": []}))
        self.assertIn("ESTIMATE dream lanes", out)
        self.assertIn("run ceiling 8.0 USD", out)
        self.assertLess(out.index("ESTIMATE"), out.index("PIPELINE"))

    def test_a_failed_lane_exits_five_and_says_so_in_the_output_and_the_report(self):
        code, out, _err = self.main(ScriptedRunner(finder="not json at all"))
        self.assertEqual(code, run.EXIT_INCOMPLETE)
        self.assertEqual(run.EXIT_INCOMPLETE, 5)
        self.assertIn("lane_errors=1", out)
        self.assertIn("INCOMPLETE 1 model call(s) failed or answered unusably", out)
        report = self.report()
        self.assertIn("**INCOMPLETE: 1 model call(s) failed or answered unusably", report)
        self.assertLess(
            report.index("INCOMPLETE"), report.index("## Checks on the staged copy")
        )

    def test_the_banner_does_not_change_the_lines_promote_reads(self):
        self.main(ScriptedRunner(finder="not json at all"))
        report = self.report()
        self.assertRegex(report, r"(?m)^Snapshot: \S+$")
        self.assertRegex(report, r"(?m)^Run digest: \S+$")

    def test_a_missing_pinned_file_stops_the_run_before_any_model_call(self):
        os.remove(os.path.join(self.ops, "pinned.txt"))
        runner = ScriptedRunner(finder={"findings": []})
        code, _out, err = self.main(runner)
        self.assertEqual(code, 1)
        self.assertIn("pinned.txt is missing", err)
        self.assertEqual(runner.calls, [])
        self.assertEqual(
            glob.glob(os.path.join(self.ops, "dream", "*", "report.md")), []
        )


class EveryStageCountsTests(Base):
    """A failed or unusable answer from ANY lane makes the run incomplete (the first version counted only finders and writers)."""

    def setUp(self):
        super().setUp()
        th.write(os.path.join(self.ops, "pinned.txt"), "- [Deploy schedule: Tuesdays](project-deploys.md) deploy day\n")

    def main(self, runner):
        out, err = io.StringIO(), io.StringIO()
        argv = ["--dir", self.mem, "--ops-root", self.root, "run", "--snapshot", self.snap, "--workers", "1"]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = run.main(argv, runner=runner)
        return code, out.getvalue()

    @staticmethod
    def writer(prompt):
        return {"hunks": []} if "MEMORY.md (untrusted DATA)" in prompt else {"hunks": [writer_hunk()]}

    def model(self, **kw):
        base = dict(finder={"findings": [finding()]}, writer=self.writer, verifier={"verdict": "keep", "why": "supported"})
        base.update(kw)
        return ScriptedRunner(**base)

    def test_a_clean_run_with_a_kept_hunk_is_exit_zero(self):
        code, out = self.main(self.model())
        self.assertEqual(code, 0, out)
        self.assertNotIn("INCOMPLETE", out)

    def test_an_explicit_rejection_by_the_verifier_is_not_a_failure(self):
        code, out = self.main(self.model(verifier={"verdict": "reject", "why": "not supported"}))
        self.assertEqual(code, 0, out)
        self.assertIn("rejected_by_verifier=1", out)

    def test_a_verifier_call_that_fails_makes_the_run_incomplete(self):
        base = self.model()

        def runner(argv, cwd, env, stdin_text):
            if "LANE:verifier" in stdin_text:
                return 1, "", "Not logged in - run /login"
            return base(argv, cwd, env, stdin_text)

        code, out = self.main(runner)
        self.assertEqual(code, run.EXIT_INCOMPLETE)
        self.assertIn("LANE-ERROR claude exited with 1: Not logged in - run /login", out)
        self.assertIn("lane_errors=1", out)
        self.assertIn("INCOMPLETE 1 model call(s) failed or answered unusably", out)

    def test_a_verifier_answer_without_json_makes_the_run_incomplete_and_says_so(self):
        code, out = self.main(self.model(verifier="sorry, I cannot help with that"))
        self.assertEqual(code, run.EXIT_INCOMPLETE)
        self.assertIn("LANE-ERROR the answer held no JSON", out)

    def test_a_verdict_that_is_neither_keep_nor_reject_makes_the_run_incomplete(self):
        code, out = self.main(self.model(verifier={"verdict": "maybe"}))
        self.assertEqual(code, run.EXIT_INCOMPLETE)
        self.assertIn("LANE-ERROR the verifier's answer held no keep-or-reject verdict", out)

    def test_a_finder_answer_without_a_findings_list_prints_its_own_line(self):
        code, out = self.main(self.model(finder={"unexpected": 1}))
        self.assertEqual(code, run.EXIT_INCOMPLETE)
        self.assertIn("LANE-ERROR the finder's answer held no findings list", out)

    def test_a_writer_answer_with_a_malformed_hunk_prints_its_own_line(self):
        code, out = self.main(self.model(writer={"hunks": [{"file": "project-deploys.md"}]}))
        self.assertEqual(code, run.EXIT_INCOMPLETE)
        self.assertIn("LANE-ERROR the writer proposed a malformed hunk", out)


if __name__ == "__main__":
    unittest.main()
