"""A character the terminal cannot encode must print as an escape, not end a command with a UnicodeEncodeError."""

import io
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import memtest_helpers as th  # noqa: E402

sys.path.insert(0, th.SCRIPTS)
import memory_common as mc  # noqa: E402

ENGINE = os.path.dirname(HERE)
MAIN_SCRIPTS = (
    "memory_dream.py",
    "memory_hygiene.py",
    "memory_evidence.py",
    os.path.join("recall", "memory_recall.py"),
    os.path.join("recall", "memory_recall_wizard.py"),
    os.path.join("dream", "memory_dream_run.py"),
    os.path.join("doctor", "memory_doctor.py"),
)


class SafeStdioTests(unittest.TestCase):
    def test_an_ascii_stream_prints_an_escape_instead_of_raising(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="ascii", errors="strict")
        saved, sys.stdout = sys.stdout, stream
        try:
            mc.safe_stdio()
            print("caf\u00e9 \u4e2d")
            stream.flush()
        finally:
            sys.stdout = saved
        self.assertEqual(raw.getvalue().decode("ascii"), "caf\\xe9 \\u4e2d\n")

    def test_a_stream_without_reconfigure_is_left_alone(self):
        saved = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
        try:
            mc.safe_stdio()  # StringIO has no reconfigure: no error
        finally:
            sys.stdout, sys.stderr = saved

    def test_every_script_calls_it_first_in_main(self):
        for rel in MAIN_SCRIPTS:
            with self.subTest(rel), open(os.path.join(ENGINE, rel), encoding="utf-8") as fh:
                text = fh.read()
            head = text[text.index("\ndef main(") :].split("\n", 3)
            self.assertEqual(head[2].strip(), "mc.safe_stdio()", rel)

    def test_a_real_script_prints_a_non_ascii_name_on_an_ascii_terminal(self):
        # lint reports a pointer that is not shaped like a note name, and prints it: here the name holds characters ASCII cannot hold
        memory = th.make_dir({"MEMORY.md": "# Index\n- [gone](na\u00efve-\u4e2d.md) - a pointer to nothing\n"})
        ops = tempfile.mkdtemp(prefix="stdio_ops_")
        done = subprocess.run(
            [sys.executable, "-X", "utf8=0", "-B", "-I", os.path.join(ENGINE, "memory_hygiene.py"),
             "--dir", memory, "--ops-root", ops, "lint"],
            capture_output=True, text=True, env=dict(os.environ, LC_ALL="C"), timeout=60,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("na\\xefve-\\u4e2d.md", done.stdout)


if __name__ == "__main__":
    unittest.main()
