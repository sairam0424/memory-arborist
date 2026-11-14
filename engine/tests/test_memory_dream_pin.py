"""`memory_dream.py pin` creates pinned.txt for a first run, create-only, and prints counts rather than text."""

import contextlib
import io
import os
import stat
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import memtest_helpers as th  # noqa: E402

sys.path.insert(0, th.SCRIPTS)
import memory_common as mc  # noqa: E402
import memory_dream as dr  # noqa: E402
import memory_dream_pin as dp  # noqa: E402
import memory_snapshot as sn  # noqa: E402

INDEX = "# Memory Index\n\nSome intro text that is not an entry.\n\n- [Deploys](project-deploys.md) deploy day\n  - [Nested](project-nested.md) nested entry\n- plain bullet without a link\n- [Style](feedback-style.md) how to answer\n"
T0 = 1_700_000_000


class Base(unittest.TestCase):
    def setUp(self):
        self.mem = th.make_dir(
            {
                "MEMORY.md": INDEX,
                "project-deploys.md": th.note("project-deploys"),
                "feedback-style.md": th.note("feedback-style"),
            }
        )
        self.root = tempfile.mkdtemp(prefix="memops_")
        self.ops = mc.ops_dir_for(self.mem, self.root)

    def pin(self, *flags):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = dr.main(["--dir", self.mem, "--ops-root", self.root, "pin", *flags])
        return code, out.getvalue(), err.getvalue()

    def pinned_path(self):
        return os.path.join(self.ops, "pinned.txt")


class EntryLineTests(unittest.TestCase):
    def test_only_linked_entry_lines_are_returned_exactly_as_written(self):
        self.assertEqual(
            dp.entry_lines(INDEX),
            [
                "- [Deploys](project-deploys.md) deploy day",
                "  - [Nested](project-nested.md) nested entry",
                "- [Style](feedback-style.md) how to answer",
            ],
        )

    def test_a_windows_line_ending_is_dropped_and_nothing_else_matches(self):
        self.assertEqual(
            dp.entry_lines("- [A](a.md) x\r\nnot an entry\r\n"), ["- [A](a.md) x"]
        )
        self.assertEqual(dp.entry_lines(""), [])


class NoneTests(Base):
    def test_none_creates_an_empty_private_file_and_warns(self):
        code, out, _err = self.pin("--none")
        self.assertEqual(code, 0, out)
        self.assertEqual(th.read(self.pinned_path()), "")
        self.assertEqual(stat.S_IMODE(os.stat(self.pinned_path()).st_mode), 0o600)
        self.assertIn("PINNED 0 index lines", out)
        self.assertIn("WARNING no index line is protected", out)

    def test_the_dream_run_accepts_the_file_it_made(self):
        self.pin("--none")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(dr._load_pinned(self.ops, False), [])


class AllTests(Base):
    def test_all_pins_every_entry_line_of_the_newest_snapshot(self):
        sn.take_snapshot(self.mem, self.ops, now=T0)
        code, out, _err = self.pin("--all")
        self.assertEqual(code, 0, out)
        self.assertIn("PINNED 3 index lines", out)
        self.assertEqual(
            th.read(self.pinned_path()).splitlines(), dp.entry_lines(INDEX)
        )
        self.assertEqual(dr._load_pinned(self.ops, False), dp.entry_lines(INDEX))

    def test_the_output_names_counts_and_a_path_but_prints_no_note_text(self):
        sn.take_snapshot(self.mem, self.ops, now=T0)
        _code, out, _err = self.pin("--all")
        self.assertNotIn("deploy day", out)
        self.assertNotIn("Deploys", out)

    def test_all_without_a_snapshot_fails_and_writes_nothing(self):
        code, _out, err = self.pin("--all")
        self.assertEqual(code, 1)
        self.assertIn("ERROR", err)
        self.assertFalse(os.path.lexists(self.pinned_path()))

    def test_all_uses_the_snapshot_not_the_live_index(self):
        sn.take_snapshot(self.mem, self.ops, now=T0)
        th.write(
            os.path.join(self.mem, "MEMORY.md"),
            "# changed after the snapshot\n- [Z](z.md) z\n",
        )
        self.pin("--all")
        self.assertEqual(
            th.read(self.pinned_path()).splitlines(), dp.entry_lines(INDEX)
        )

    def test_a_snapshot_that_fails_verification_is_refused(self):
        snap = sn.take_snapshot(self.mem, self.ops, now=T0)["id"]
        damaged = os.path.join(
            self.ops, "snapshots", snap, "memory", "feedback-style.md"
        )
        os.chmod(damaged, 0o600)
        th.write(damaged, "tampered\n")
        code, _out, err = self.pin("--all")
        self.assertEqual(code, 1)
        self.assertIn("failed verification", err)
        self.assertFalse(os.path.lexists(self.pinned_path()))


class CreateOnlyTests(Base):
    def test_a_second_call_never_replaces_the_file(self):
        self.pin("--none")
        th.write(self.pinned_path(), "- [Mine](mine.md) chosen by hand\n")
        code, _out, err = self.pin("--none")
        self.assertEqual(code, 1)
        self.assertIn("never replaces", err)
        self.assertEqual(
            th.read(self.pinned_path()), "- [Mine](mine.md) chosen by hand\n"
        )

    def test_an_existing_draft_also_blocks_it(self):
        os.makedirs(self.ops, mode=0o700)
        th.write(os.path.join(self.ops, "pinned.txt.draft"), "- [A](a.md) a\n")
        code, _out, err = self.pin("--none")
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)
        self.assertFalse(os.path.lexists(self.pinned_path()))

    def test_a_symlink_at_the_target_is_not_followed(self):
        os.makedirs(self.ops, mode=0o700)
        victim = os.path.join(self.root, "victim.txt")
        th.write(victim, "keep me\n")
        os.symlink(victim, self.pinned_path())
        code, _out, _err = self.pin("--none")
        self.assertEqual(code, 1)
        self.assertEqual(th.read(victim), "keep me\n")

    def test_the_memory_directory_is_never_touched(self):
        before = {n: th.read(os.path.join(self.mem, n)) for n in os.listdir(self.mem)}
        sn.take_snapshot(self.mem, self.ops, now=T0)
        self.pin("--all")
        self.assertEqual(
            {n: th.read(os.path.join(self.mem, n)) for n in os.listdir(self.mem)},
            before,
        )


class ParserTests(Base):
    def test_one_of_the_two_modes_is_required_and_they_exclude_each_other(self):
        for flags in ((), ("--none", "--all")):
            with (
                self.subTest(flags),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                dr.build_parser().parse_args(["pin", *flags])

    def test_the_missing_pinned_message_names_the_way_out(self):
        with self.assertRaises(mc.HygieneError) as ctx:
            dr._load_pinned(self.ops, False)
        self.assertIn("pin --none", str(ctx.exception))
        self.assertIn("pin --all", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
