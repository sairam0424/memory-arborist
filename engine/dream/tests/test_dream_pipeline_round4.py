"""Spend accounting of the lanes and the per-writer hunk cap."""

import glob
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_dream_pipeline import (  # noqa: E402
    Base,
    ScriptedRunner,
    lane_output,
    lanes,
    mrr,
    run,
    writer_hunk,
)

SETTINGS = lanes.Settings("m", 0.5)


def lane_call(runner, spend):
    return lanes.ask_lane("LANE:finder body", "/tmp/sys.txt", SETTINGS, runner, spend, cwd="/tmp")


class FailedLaneCostTests(unittest.TestCase):
    def test_a_non_zero_exit_that_printed_its_cost_is_counted_and_still_raises(self):
        spend = mrr.Spend(5.0)
        with self.assertRaises(mrr.AskError):
            lane_call(lambda argv, cwd, env, stdin: (1, lane_output("", cost=0.31), ""), spend)
        self.assertAlmostEqual(spend.total, 0.31)

    def test_a_non_zero_exit_with_unreadable_output_counts_nothing(self):
        spend = mrr.Spend(5.0)
        with self.assertRaises(mrr.AskError):
            lane_call(lambda argv, cwd, env, stdin: (1, "garbage", ""), spend)
        self.assertEqual(spend.total, 0.0)

    def test_a_timeout_is_charged_the_lane_cap_and_still_raises_as_a_timeout(self):
        spend = mrr.Spend(5.0)

        def hangs(argv, cwd, env, stdin):
            raise mrr.AskTimeout("timed out")

        with self.assertRaises(mrr.AskTimeout):
            lane_call(hangs, spend)
        self.assertAlmostEqual(spend.total, SETTINGS.per_q_usd)

    def test_run_lane_turns_a_timeout_into_none_and_keeps_the_charge(self):
        spend = mrr.Spend(5.0)

        def hangs(argv, cwd, env, stdin):
            raise mrr.AskTimeout("timed out")

        self.assertIsNone(lanes.run_lane("LANE:finder x", "/tmp/sys.txt", SETTINGS, hangs, spend, cwd="/tmp"))
        self.assertAlmostEqual(spend.total, SETTINGS.per_q_usd)


class FailedLaneToolsTests(unittest.TestCase):
    def test_a_failed_lane_that_reports_a_tool_aborts_the_run_and_keeps_its_cost(self):
        spend = mrr.Spend(5.0)
        runner = lambda argv, cwd, env, stdin: (1, lane_output("", cost=0.2, tools=["Read", "Bash"]), "")  # noqa: E731
        with self.assertRaises(mrr.ToolsMismatch):
            lane_call(runner, spend)
        self.assertAlmostEqual(spend.total, 0.2)
        self.assertTrue(spend.stopped)

    def test_a_failed_lane_with_an_empty_tool_list_is_only_an_error(self):
        spend = mrr.Spend(5.0)
        runner = lambda argv, cwd, env, stdin: (1, lane_output("", cost=0.2, tools=[]), "")  # noqa: E731
        with self.assertRaises(mrr.AskError) as caught:
            lane_call(runner, spend)
        self.assertNotIsInstance(caught.exception, mrr.ToolsMismatch)
        self.assertIsNone(spend.stopped)


class WorkdirTests(Base):
    def test_a_pipeline_run_leaves_no_temporary_folder_behind(self):
        self.pipeline(ScriptedRunner())
        self.assertEqual(glob.glob(os.path.join(tempfile.gettempdir(), "dream_lanes_*")), [])


def distinct_hunks(count):
    return [writer_hunk(new="Deploys run on Thursday afternoons, variant %d." % i) for i in range(count)]


def run_writer(items):
    runner = ScriptedRunner(writer={"hunks": items})
    ctx = {"workers": 1, "lane": ("/tmp/sys.txt", SETTINGS, runner, mrr.Spend(5.0), "/tmp")}
    return run._writer_stage([("project-deploys.md", "LANE:writer prompt", "edit")], ctx)


class WriterCapTests(unittest.TestCase):
    def test_the_cap_is_five_hunks_per_writer(self):
        self.assertEqual(run.MAX_HUNKS_PER_WRITER, 5)

    def test_a_flood_of_valid_hunks_is_cut_to_the_cap_and_the_rest_is_counted(self):
        hunks, errors, over_cap = run_writer(distinct_hunks(8))
        self.assertEqual((len(hunks), errors, over_cap), (5, 0, 3))

    def test_exactly_the_cap_is_kept_whole_and_one_more_is_dropped(self):
        self.assertEqual(run_writer(distinct_hunks(5))[2], 0)
        hunks, _errors, over_cap = run_writer(distinct_hunks(6))
        self.assertEqual((len(hunks), over_cap), (5, 1))

    def test_invalid_items_do_not_use_up_the_cap(self):
        junk = [{"nonsense": i} for i in range(6)]
        hunks, errors, over_cap = run_writer(junk + distinct_hunks(3))
        self.assertEqual((len(hunks), errors, over_cap), (3, 6, 0))


if __name__ == "__main__":
    unittest.main()
