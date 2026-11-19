"""The cost ceiling and the calls that are already running. The check used to look only at what had been spent, so with N workers the N calls
that were started before the first one finished all passed it and the ceiling could be passed by N times the cap of one call. A call that is
running now counts as spent, at its cap, for every call that wants to start: the ceiling can be passed by less than one call's cap."""

import os
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_memory_recall import (
    MemoryReadingRunner,
    TempRoot,
    fake_output,
    memory_with_phrases,
    question,
    th,
)  # noqa: E402

import memory_recall_run as mrr  # noqa: E402

LIMIT, CAP, WORKERS = 1.0, 0.25, 8


def run_threads(target, count=WORKERS):
    threads = [threading.Thread(target=target, daemon=True) for _ in range(count)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 10  # one deadline for all of them: a test that is going to fail must not take minutes to do it
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    assert not any(thread.is_alive() for thread in threads), "a worker never finished"


class SpendObjectTests(unittest.TestCase):
    def test_only_as_many_calls_start_as_the_ceiling_has_room_for(self):
        spend = mrr.Spend(LIMIT)
        entered, refused = [], []
        everyone_inside = threading.Barrier(4, timeout=10)

        def worker():
            try:
                with spend.call(CAP):
                    entered.append(1)
                    everyone_inside.wait()  # the four that got in are running at the same time
                    spend.add(CAP)
            except mrr.RunAborted:  # CostCeiling for the first to be refused, then the run is stopped for the rest
                refused.append(1)

        run_threads(worker)
        self.assertEqual((len(entered), len(refused)), (4, 4))
        self.assertLessEqual(spend.total, LIMIT + 1e-9)

    def test_a_call_waits_for_the_running_ones_instead_of_being_refused_while_the_real_spend_is_low(self):
        # a per-call cap is an upper bound, usually far above the real cost: the caps alone reaching the ceiling must slow the run down,
        # never stop it
        spend = mrr.Spend(LIMIT)
        running, peak, done, refused = [0], [0], [], []
        lock = threading.Lock()

        def worker():
            try:
                with spend.call(0.6):
                    with lock:
                        running[0] += 1
                        peak[0] = max(peak[0], running[0])
                    threading.Event().wait(0.05)  # the call takes a moment
                    spend.add(0.01)  # and costs a cent
                    with lock:
                        running[0] -= 1
                        done.append(1)
            except mrr.RunAborted:
                refused.append(1)

        run_threads(worker)
        self.assertEqual((len(done), len(refused)), (WORKERS, 0), "a run with a few cents spent was stopped by the caps of calls still running")
        self.assertLessEqual(peak[0], 2, "more calls ran at once than the ceiling has room for at their caps")

    def test_stopping_the_run_wakes_a_call_that_is_waiting(self):
        spend = mrr.Spend(LIMIT)
        holding, woke, outcome = threading.Event(), threading.Event(), []

        def holder():
            with spend.call(0.6):
                with spend.call(0.6):
                    holding.set()
                    woke.wait(10)

        def waiter():
            holding.wait(10)
            try:
                with spend.call(0.6):
                    outcome.append("started")
            except mrr.RunAborted:
                outcome.append("aborted")

        first, second = threading.Thread(target=holder, daemon=True), threading.Thread(target=waiter, daemon=True)
        first.start()
        second.start()
        holding.wait(10)
        threading.Event().wait(0.2)  # the waiter is now waiting: 1.2 held against a ceiling of 1.0
        self.assertEqual(outcome, [])
        spend.stop("the tool list was not empty")
        second.join(5)  # while the holder still holds its caps: only stop() can be what wakes the waiter now
        self.assertFalse(second.is_alive(), "the waiting call was not woken when the run was stopped")
        self.assertEqual(outcome, ["aborted"])
        woke.set()
        first.join(10)

    def finishes_within(self, spend, cap, seconds=3):
        """True when a call of this cap can start and end on `spend` in another thread within the time."""
        done = threading.Event()

        def worker():
            with spend.call(cap):
                done.set()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        thread.join(seconds)
        return done.is_set()

    def test_rounding_in_the_sums_never_leaves_a_call_waiting_for_one_that_is_not_running(self):
        spend = mrr.Spend(0.5)
        with spend.call(0.1):
            with spend.call(0.1):
                with spend.call(0.1):
                    pass
        spend.add(0.49999999999999994)  # one step under the ceiling; the three 0.1 holds add and take back with a residue of 2.8e-17
        self.assertEqual(spend.reserved, 0.0)
        self.assertTrue(self.finishes_within(spend, 0.1), "a call waited for ever although nothing was running")

    def test_a_cap_too_large_for_floating_point_sums_does_not_poison_the_counter(self):
        spend = mrr.Spend(1.5e308)
        with spend.call(1e308):
            with spend.call(1e308):  # the two holds add up to infinity
                pass
        self.assertEqual(spend.reserved, 0.0)
        self.assertTrue(self.finishes_within(spend, 1e308), "a call waited for ever after the holds overflowed")

    def test_the_hold_is_released_when_the_call_ends_however_it_ends(self):
        spend = mrr.Spend(LIMIT)
        with self.assertRaises(RuntimeError):
            with spend.call(CAP):
                raise RuntimeError("the child could not be started")
        self.assertEqual(spend.reserved, 0.0)
        with spend.call(CAP):
            pass
        self.assertEqual(spend.reserved, 0.0)

    def test_a_stopped_run_refuses_without_holding_anything(self):
        spend = mrr.Spend(LIMIT)
        spend.stop("the tool list was not empty")
        with self.assertRaises(mrr.RunAborted):
            with spend.call(CAP):
                self.fail("a stopped run started a call")
        self.assertEqual(spend.reserved, 0.0)

    def test_the_ceiling_reached_by_spending_alone_still_stops_the_next_call(self):
        spend = mrr.Spend(0.015)
        spend.add(0.02)
        with self.assertRaises(mrr.CostCeiling):
            with spend.call(CAP):
                self.fail("started over the ceiling")


class AskTests(TempRoot):
    def prepare(self):
        mem = memory_with_phrases()
        prompt = os.path.join(mem, "prompt.txt")
        th.write(prompt, "prompt")
        return mem, prompt

    def test_eight_workers_against_a_one_dollar_ceiling_start_four_calls_not_eight(
        self,
    ):
        mem, prompt = self.prepare()
        spend = mrr.Spend(LIMIT)
        inside = threading.Barrier(4, timeout=10)
        started, refused = [], []
        lock = threading.Lock()

        def runner(argv, cwd, env):
            with lock:
                started.append(1)
            inside.wait()  # four running at once; the fifth to ask is refused before it launches
            return 0, fake_output("x", cost=CAP), ""

        def worker():
            try:
                mrr.ask(question(1), runner, mrr.Settings("m", CAP), prompt, mem, spend)
            except (mrr.RunAborted, threading.BrokenBarrierError):
                refused.append(1)

        run_threads(worker)
        self.assertEqual(
            len(started), 4, "calls started: %d of %d" % (len(started), WORKERS)
        )
        self.assertLessEqual(spend.total, LIMIT + 1e-9)
        self.assertEqual(len(refused), 4)

    def test_a_runner_that_raises_does_not_leave_its_hold_behind(self):
        mem, prompt = self.prepare()
        spend = mrr.Spend(LIMIT)

        def runner(argv, cwd, env):
            raise OSError("the claude program is not there")

        with self.assertRaises(OSError):
            mrr.ask(question(1), runner, mrr.Settings("m", CAP), prompt, mem, spend)
        self.assertEqual(spend.reserved, 0.0)

    def test_the_ordinary_sequence_of_calls_is_unchanged(self):
        mem, prompt = self.prepare()
        spend = mrr.Spend(0.015)
        runner = MemoryReadingRunner(cost=0.01)
        mrr.ask(question(1), runner, mrr.Settings("m", CAP), prompt, mem, spend)
        mrr.ask(question(1), runner, mrr.Settings("m", CAP), prompt, mem, spend)
        with self.assertRaises(mrr.CostCeiling):
            mrr.ask(question(1), runner, mrr.Settings("m", CAP), prompt, mem, spend)
        self.assertEqual(len(runner.calls), 2)


if __name__ == "__main__":
    unittest.main()
