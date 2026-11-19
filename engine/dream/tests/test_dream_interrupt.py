"""The dream run and Ctrl-C: a lane call that is waiting in the cost check (a ceiling with room for two calls at once, four workers) must not
launch after an interrupt (see the recall test of the same name), and the command ends with a message and exit code 130, not a traceback."""

import contextlib
import io
import os
import signal
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_dream_pipeline import Base, lane_output, lanes, run, th  # noqa: E402

WORKERS, CHUNKS, CAP, LIMIT = 4, 12, 0.6, 1.0


class DreamInterruptTests(Base):
    def test_the_finder_stage_does_not_launch_waiting_calls_after_an_interrupt(self):
        spend, lock, started = lanes.mrr.Spend(LIMIT), threading.Lock(), []

        def runner(argv, cwd, env, stdin_text):
            with lock:
                started.append(1)
                number = len(started)
            time.sleep(
                0.8 if number <= 2 else 0.05
            )  # the first two are still running when the interrupt arrives
            return 0, lane_output('{"findings": []}', 0.001), ""

        ctx = {
            "workers": WORKERS,
            "lane": ("/tmp/s.txt", lanes.Settings("m", CAP), runner, spend, "/tmp"),
            "note_dates": {},
        }
        digests = ["a digest"] * (run.DIGESTS_PER_LANE * CHUNKS)
        timer = threading.Timer(
            0.3, lambda: os.kill(os.getpid(), signal.SIGINT)
        )  # a real Ctrl-C, from another thread
        timer.daemon = True
        timer.start()
        with self.assertRaises(KeyboardInterrupt):
            run._finder_stage(digests, "# Index\n", {}, ctx)
        self.assertEqual(len(started), 2, "%d lane calls were started" % len(started))

    def test_the_command_reports_an_interrupt_and_exits_130(self):
        th.write(
            os.path.join(self.ops, "pinned.txt"),
            "- [Deploy schedule: Tuesdays](project-deploys.md) deploy day\n",
        )

        def runner(argv, cwd, env, stdin_text):
            raise KeyboardInterrupt

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
            "2",
        ]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = run.main(argv, runner=runner)
        text = out.getvalue() + err.getvalue()
        self.assertEqual(code, 130, text)
        self.assertIn("INTERRUPTED", text)
        self.assertNotIn("Traceback", text)


if __name__ == "__main__":
    unittest.main()
