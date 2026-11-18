"""held_signals and interrupt_pending on their own, in this process: what the mask holds inside the block, that it is put back, that a signal
that arrived is consumed and not delivered, and that a signal the parent set to ignore is left alone (on Linux a blocked and ignored signal
is kept pending, on macOS it is dropped, so the mask itself is what is checked)."""

import os
import signal
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import memtest_helpers as th  # noqa: E402

sys.path.insert(0, th.SCRIPTS)
import memory_dream_stage as ds  # noqa: E402

WATCHED = {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}


def current_mask():
    return set(signal.pthread_sigmask(signal.SIG_BLOCK, set()))


class HeldSignalsTests(unittest.TestCase):
    def setUp(self):
        self.received = []
        self.saved = {
            sig: signal.signal(sig, lambda signum, frame: self.received.append(signum))
            for sig in WATCHED
        }
        self.addCleanup(
            lambda: [signal.signal(sig, handler) for sig, handler in self.saved.items()]
        )
        self.before = current_mask()

    def test_the_three_signals_are_blocked_inside_and_the_mask_is_put_back_after(self):
        with ds.held_signals():
            self.assertTrue(WATCHED <= current_mask())
        self.assertEqual(current_mask(), self.before)

    def test_the_mask_is_put_back_after_an_error_too(self):
        with self.assertRaises(RuntimeError):
            with ds.held_signals():
                raise RuntimeError("the promote failed")
        self.assertEqual(current_mask(), self.before)

    def test_a_signal_that_arrives_inside_is_seen_and_then_consumed_not_delivered(self):
        with ds.held_signals():
            self.assertFalse(ds.interrupt_pending())
            signal.raise_signal(signal.SIGTERM)
            signal.raise_signal(signal.SIGHUP)
            self.assertTrue(ds.interrupt_pending())
            self.assertEqual(self.received, [])  # held: the handler has not run
        self.assertEqual(
            self.received,
            [],
            "a signal that arrived during the block was delivered at its end",
        )
        self.assertEqual(set(signal.sigpending()) & WATCHED, set())
        self.assertFalse(ds.interrupt_pending())

    def test_a_signal_the_parent_ignored_is_not_held(self):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        with ds.held_signals():
            self.assertNotIn(signal.SIGHUP, current_mask())
            self.assertIn(signal.SIGTERM, current_mask())
            signal.raise_signal(
                signal.SIGHUP
            )  # ignored: it neither interrupts nor reaches the handler
            self.assertFalse(ds.interrupt_pending())
        self.assertEqual(self.received, [])
        self.assertEqual(current_mask(), self.before)

    def test_outside_the_block_nothing_is_pending(self):
        self.assertFalse(ds.interrupt_pending())

    def test_say_never_raises_when_both_streams_are_gone(self):
        class Dead:
            def write(self, text):
                raise OSError(5, "Input/output error")

            def flush(self):
                raise OSError(5, "Input/output error")

        real_out, real_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = Dead(), Dead()
        try:
            ds.say("a line")
            ds.say_err("another")
        finally:
            sys.stdout, sys.stderr = real_out, real_err


if __name__ == "__main__":
    unittest.main()
