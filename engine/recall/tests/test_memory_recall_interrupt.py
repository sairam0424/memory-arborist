"""Ctrl-C and the paid calls that are waiting. Executor.map already cancels the calls that have not started when it raises, but a worker that
HAS started and is waiting inside the cost check (a ceiling with room for two calls at once, four workers) would launch its model call as soon
as a running one ended, after the person had asked to stop. The interrupt stops the spend counter before the pool is torn down: running calls
finish, waiting ones refuse. The interrupt is a real SIGINT sent to this process while two long calls are running and two workers wait, which is
what Ctrl-C does to the main thread that is blocked in the map. The three commands end with a message and exit code 130."""

import os
import signal
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "dream"))
from test_memory_recall import (  # noqa: E402
    T0,
    TempRoot,
    fake_output,
    mc,
    memory_with_phrases,
    question,
    run_main,
    sn,
    write_questions,
)
from test_memory_recall_wizard import T0 as WIZARD_T0, Base as WizardBase  # noqa: E402

import memory_dream_lanes as lanes  # noqa: E402
import memory_recall as mrc  # noqa: E402
import memory_recall_run as mrr  # noqa: E402
import memory_recall_wizard as wz  # noqa: E402

WORKERS, ITEMS, CAP, LIMIT = (
    4,
    12,
    0.6,
    1.0,
)  # room for two calls at once: two workers run, two wait in the cost check


class Waiting:
    """The first two calls are long (they are still running when the interrupt arrives); any later call would be short."""

    def __init__(self):
        self.started, self.lock = 0, threading.Lock()

    def call(self):
        with self.lock:
            self.started += 1
            number = self.started
        time.sleep(0.8 if number <= 2 else 0.05)


def ctrl_c_in(seconds):
    """A real SIGINT to this process after a while, from another thread, as the terminal would send it."""
    timer = threading.Timer(seconds, lambda: os.kill(os.getpid(), signal.SIGINT))
    timer.daemon = True
    timer.start()


class MapCallsTests(unittest.TestCase):
    def test_an_interrupt_stops_the_workers_that_are_waiting_in_the_cost_check(self):
        spend, waiting = mrr.Spend(LIMIT), Waiting()

        def work(item):
            with spend.call(CAP):
                waiting.call()
                spend.add(0.001)

        ctrl_c_in(0.3)
        with self.assertRaises(KeyboardInterrupt):
            mrr.map_calls(WORKERS, work, range(ITEMS), spend)
        self.assertEqual(
            waiting.started,
            2,
            "%d calls were started: the waiting ones launched after the interrupt"
            % waiting.started,
        )
        self.assertIn("interrupted", spend.stopped)

    def test_an_error_that_reaches_the_main_thread_also_stops_the_spend(self):
        spend = mrr.Spend(LIMIT)

        def work(item):
            if item == 0:
                raise RuntimeError("a worker failed")
            time.sleep(0.05)

        with self.assertRaises(RuntimeError):
            mrr.map_calls(WORKERS, work, range(ITEMS), spend)
        self.assertIn("interrupted", spend.stopped)

    def test_a_normal_map_returns_its_results_in_order_and_does_not_stop_the_run(self):
        spend = mrr.Spend(100.0)
        self.assertEqual(
            mrr.map_calls(3, lambda x: x * 2, range(10), spend),
            [0, 2, 4, 6, 8, 10, 12, 14, 16, 18],
        )
        self.assertIsNone(spend.stopped)


class JudgeTests(TempRoot):
    def setUp(self):
        self.mem = memory_with_phrases()
        self.work = tempfile.mkdtemp(prefix="intwork_")
        self.root = tempfile.mkdtemp(prefix="intops_")
        sn.take_snapshot(self.mem, mc.ops_dir_for(self.mem, self.root), now=T0)

    def test_the_judge_arm_does_not_launch_waiting_questions_after_an_interrupt(self):
        spend, waiting = mrr.Spend(LIMIT), Waiting()

        def runner(argv, cwd, env):
            waiting.call()
            return 0, fake_output("x", cost=0.001), ""

        questions = [dict(question(1), id="q%02d" % i) for i in range(ITEMS)]
        ctrl_c_in(0.3)
        with self.assertRaises(KeyboardInterrupt):
            mrc.run_arm(
                questions,
                runner,
                mrr.Settings("m", CAP),
                self.mem,
                self.work,
                "live",
                spend,
                WORKERS,
            )
        self.assertEqual(
            waiting.started, 2, "%d questions were asked" % waiting.started
        )

    def test_the_judge_reports_an_interrupt_and_exits_130(self):
        qfile = write_questions([question(i) for i in range(1, 30)])

        def runner(argv, cwd, env):
            raise KeyboardInterrupt

        argv = [
            "--questions",
            qfile,
            "--dir",
            self.mem,
            "--ops-root",
            self.root,
            "--no-log",
            "--workers",
            "2",
            "--arm",
            "live",
        ]
        code, out, err = run_main(argv, runner)
        self.assertEqual(code, 130, out + err)
        self.assertIn("INTERRUPTED", out + err)
        self.assertIn("may still have been billed", out + err)
        self.assertNotIn("Traceback", out + err)


class WizardTests(WizardBase):
    def test_the_wizard_does_not_launch_waiting_batches_after_an_interrupt(self):
        spend, waiting = mrr.Spend(LIMIT), Waiting()

        def runner(argv, cwd, env, stdin_text):
            waiting.call()
            return 0, fake_output('{"questions": []}', cost=0.001, tools=[]), ""

        notes = {"a.md": "---\nname: a\n---\nA fact about a.\n"}
        ctrl_c_in(0.3)
        with self.assertRaises(KeyboardInterrupt):
            wz._ask_chunks(
                [["a.md"] for _ in range(ITEMS)],
                notes,
                runner,
                lanes.Settings("m", CAP),
                spend,
                WORKERS,
            )
        self.assertEqual(waiting.started, 2, "%d batches were asked" % waiting.started)

    def test_the_wizard_reports_an_interrupt_and_exits_130(self):
        def runner(argv, cwd, env, stdin_text):
            raise KeyboardInterrupt

        code, out, err, _ = self.main(
            "--workers", "1", "--now", str(WIZARD_T0), runner=runner
        )
        self.assertEqual(code, 130, out + err)
        self.assertIn("INTERRUPTED", out + err)
        self.assertIn("may still have been billed", out + err)
        self.assertNotIn("Traceback", out + err)


if __name__ == "__main__":
    unittest.main()
