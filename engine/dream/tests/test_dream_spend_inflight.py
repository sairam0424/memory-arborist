"""The lanes and the cost ceiling: the calls that are running count as spent, at their cap, for a call that wants to start (see the recall
test of the same name). Eight workers against a one-dollar ceiling with a quarter-dollar cap start four calls, not eight."""

import os
import sys
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_dream_pipeline import lane_output, lanes  # noqa: E402

LIMIT, CAP, WORKERS = 1.0, 0.25, 8


class LaneSpendTests(unittest.TestCase):
    def test_eight_workers_against_a_one_dollar_ceiling_start_four_lane_calls_not_eight(
        self,
    ):
        spend = lanes.mrr.Spend(LIMIT)
        inside = threading.Barrier(4, timeout=10)
        started, refused, lock = [], [], threading.Lock()

        def runner(argv, cwd, env, stdin_text):
            with lock:
                started.append(1)
            inside.wait()
            return 0, lane_output('{"verdict": "keep", "why": "x"}', CAP, None), ""

        def worker():
            try:
                lanes.ask_lane(
                    "LANE:verifier x",
                    "/tmp/s.txt",
                    lanes.Settings("m", CAP),
                    runner,
                    spend,
                )
            except (lanes.mrr.RunAborted, threading.BrokenBarrierError):
                refused.append(1)

        threads = [threading.Thread(target=worker) for _ in range(WORKERS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(
            len(started), 4, "lane calls started: %d of %d" % (len(started), WORKERS)
        )
        self.assertLessEqual(spend.total, LIMIT + 1e-9)
        self.assertEqual(len(refused), 4)

    def test_a_lane_runner_that_raises_does_not_leave_its_hold_behind(self):
        spend = lanes.mrr.Spend(LIMIT)

        def runner(argv, cwd, env, stdin_text):
            raise OSError("the claude program is not there")

        with self.assertRaises(OSError):
            lanes.ask_lane(
                "LANE:verifier x", "/tmp/s.txt", lanes.Settings("m", CAP), runner, spend
            )
        self.assertEqual(spend.reserved, 0.0)


if __name__ == "__main__":
    unittest.main()
