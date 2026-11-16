"""Promote and restore edges found by the first tester: a refused promote must not claim to be replacing anything, a partial promote
must say how to undo itself, one that replaced nothing must stay usable, a lost race must be explained, and printed paths are safe."""

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import memtest_helpers as th  # noqa: E402
from test_memory_dream_cli import Base, STALE, run_cli  # noqa: E402
from test_memory_dream_cli_round2 import hunk  # noqa: E402

sys.path.insert(0, th.SCRIPTS)
import memory_common as mc  # noqa: E402
import memory_dream as dr  # noqa: E402
import memory_hygiene as mh  # noqa: E402
import memory_snapshot as sn  # noqa: E402

PUSH_NEW = "The push guard blocks git push outside the allow-listed repos."


class PromoteBase(Base):
    def two_note_run(self, incomplete=False):
        self.write_hunks(
            [
                hunk("feedback-push.md", STALE, PUSH_NEW, "h1"),
                hunk(
                    "feedback-other.md",
                    "Other fact one is longer than twenty characters.",
                    "Other fact one, revised to say more.",
                    "h2",
                ),
            ]
        )
        extra = ["--incomplete", "1"] if incomplete else []
        code, out, err = self.cli(
            "ground", "--hunks", self.hunks_path, "--now", str(1_700_000_100), *extra
        )
        self.assertEqual(code, 0, out + err)
        return os.path.join(self.ops, "dream", mc.utc_stamp(1_700_000_100))

    def with_swap(self, fail_on, action):
        real, calls = sn._swap_in, {"n": 0}

        def flaky(memory_dir, tmp, live, archive_dir, clean):
            calls["n"] += 1
            if calls["n"] == fail_on:
                raise mc.HygieneError("simulated failure")
            return real(memory_dir, tmp, live, archive_dir, clean)

        sn._swap_in = flaky
        try:
            return action()
        finally:
            sn._swap_in = real


class RefusalOutputTests(PromoteBase):
    def test_a_refused_apply_never_prints_a_replacing_line(self):
        run = self.two_note_run()
        code, out, err = self.cli(
            "promote", os.path.basename(run), "--apply", "--expect", "0" * 64
        )
        self.assertEqual(code, 1)
        self.assertNotIn("replacing", out)
        self.assertIn("--expect must equal the DIGEST", err)

    def test_a_dry_run_says_would_replace_and_an_accepted_apply_says_replacing(self):
        run = self.two_note_run()
        _code, out, _err = self.cli("promote", os.path.basename(run))
        self.assertIn("would replace feedback-push.md", out)
        code, out, _err = self.apply(run)
        self.assertEqual(code, 0, out)
        self.assertIn("replacing feedback-push.md", out)


class PartialPromoteTests(PromoteBase):
    def test_a_partial_promote_prints_the_snapshot_and_the_way_back(self):
        run = self.two_note_run()
        digest = self.digest_of(run)
        code, out, _err = self.with_swap(
            2,
            lambda: self.cli(
                "promote", os.path.basename(run), "--apply", "--expect", digest
            ),
        )
        self.assertEqual(code, 1)
        marker = json.loads(th.read(os.path.join(run, dr.MARKER)))
        self.assertEqual(marker["status"], "partial")
        self.assertIn("pre-promote snapshot " + marker["pre_snapshot"], out)
        self.assertIn(
            "memory_hygiene.py restore --snapshot " + marker["pre_snapshot"], out
        )

    def test_promoting_a_partial_run_again_explains_it_instead_of_saying_it_finished(
        self,
    ):
        run = self.two_note_run()
        digest = self.digest_of(run)
        self.with_swap(
            2,
            lambda: self.cli(
                "promote", os.path.basename(run), "--apply", "--expect", digest
            ),
        )
        marker = json.loads(th.read(os.path.join(run, dr.MARKER)))
        code, _out, err = self.cli("promote", os.path.basename(run))
        self.assertEqual(code, 1)
        self.assertIn("already partly promoted (1 notes replaced", err)
        self.assertIn("--snapshot " + marker["pre_snapshot"], err)
        self.assertIn("fresh snapshot", err)

    def test_the_documented_way_back_really_restores_the_replaced_note(self):
        run = self.two_note_run()
        digest = self.digest_of(run)
        before = {n: th.read(os.path.join(self.mem, n)) for n in os.listdir(self.mem)}
        self.with_swap(2, lambda: self.cli("promote", os.path.basename(run), "--apply", "--expect", digest))
        marker = json.loads(th.read(os.path.join(run, dr.MARKER)))
        replaced = marker["files"][0]
        self.assertNotEqual(th.read(os.path.join(self.mem, replaced)), before[replaced])  # it really was replaced
        code, out, _err = run_restore(self.mem, self.root, marker["pre_snapshot"], replaced)
        self.assertEqual(code, 0, out)
        self.assertEqual(th.read(os.path.join(self.mem, replaced)), before[replaced])

    def test_a_promote_that_replaced_nothing_leaves_the_run_usable(self):
        run = self.two_note_run()
        digest = self.digest_of(run)
        before = {n: th.read(os.path.join(self.mem, n)) for n in os.listdir(self.mem)}
        code, out, _err = self.with_swap(
            1,
            lambda: self.cli(
                "promote", os.path.basename(run), "--apply", "--expect", digest
            ),
        )
        self.assertEqual(code, 1)
        self.assertIn("NOTHING was replaced", out)
        self.assertFalse(os.path.lexists(os.path.join(run, dr.MARKER)))
        self.assertTrue([n for n in os.listdir(run) if n.startswith("promote-failed")])
        self.assertEqual(
            {n: th.read(os.path.join(self.mem, n)) for n in os.listdir(self.mem)},
            before,
        )
        code, out, err = self.apply(
            run
        )  # the cause is fixed: the same run goes through
        self.assertEqual(code, 0, out + err)
        self.assertIn("PROMOTED 2 notes", out)

    def test_a_lost_race_for_the_marker_is_explained_not_a_raw_errno(self):
        run = self.two_note_run()
        dr._write_marker(run, {"status": "promoted"})
        with self.assertRaises(mc.HygieneError) as ctx:
            dr._write_marker(run, {"status": "partial"})
        self.assertIn("another promote of this run finished first", str(ctx.exception))


class IncompleteWarningTests(PromoteBase):
    def test_a_run_staged_after_failed_calls_is_flagged_when_it_is_promoted_or_previewed(
        self,
    ):
        run = self.two_note_run(incomplete=True)
        _code, out, _err = self.cli("promote", os.path.basename(run))
        self.assertIn("WARNING this run was INCOMPLETE", out)
        _code, out, _err = self.apply(run)
        self.assertIn("WARNING this run was INCOMPLETE", out)

    def test_a_clean_run_has_no_warning(self):
        run = self.two_note_run()
        _code, out, _err = self.cli("promote", os.path.basename(run))
        self.assertNotIn("WARNING", out)


def run_restore(memory, root, snapshot, name, *flags):
    import contextlib
    import io

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = mh.main(
            [
                "--dir",
                memory,
                "--ops-root",
                root,
                "restore",
                "--snapshot",
                snapshot,
                "--file",
                name,
                "--apply",
                "--force",
                *flags,
            ]
        )
    return code, out.getvalue(), err.getvalue()


class RestoreOutputTests(unittest.TestCase):
    def setUp(self):
        self.escape = "esc\x1b[31mRED\x1b[0m.md"
        self.mem = th.make_dir(
            {
                "MEMORY.md": "# Index\n",
                "a.md": th.note("a"),
                self.escape: th.note("esc"),
            }
        )
        self.root = tempfile.mkdtemp(prefix="restops_")
        self.ops = mc.ops_dir_for(self.mem, self.root)
        self.snap = sn.take_snapshot(self.mem, self.ops, now=1_700_000_000)["id"]

    def test_no_raw_escape_reaches_the_terminal_when_a_note_with_a_control_character_is_restored(
        self,
    ):
        th.write(os.path.join(self.mem, self.escape), "changed\n")
        code, out, _err = run_restore(self.mem, self.root, self.snap, self.escape)
        self.assertEqual(code, 0, out)
        self.assertNotIn("\x1b", out)

    def test_a_note_absent_from_the_snapshot_is_archived_with_a_printable_name(self):
        extra = "late\x1b]0;title\x07.md"
        th.write(os.path.join(self.mem, extra), "x\n")
        code, out, _err = run_restore(self.mem, self.root, self.snap, extra)
        self.assertEqual(code, 0, out)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x07", out)

    def test_a_failed_archive_names_the_staged_copy_and_says_the_note_is_untouched(
        self,
    ):
        th.write(os.path.join(self.mem, "a.md"), "changed\n")
        real = sn._archive_file

        def broken(live, archive_dir, clean):
            raise mc.HygieneError("could not archive a.md: Operation not permitted")

        sn._archive_file = broken
        try:
            code, out, err = run_restore(self.mem, self.root, self.snap, "a.md")
        finally:
            sn._archive_file = real
        self.assertEqual(code, 1)
        self.assertIn("the live note is untouched", err)
        self.assertIn(".restore-", err)
        self.assertIn("safe to remove by hand", err)
        self.assertEqual(th.read(os.path.join(self.mem, "a.md")), "changed\n")


if __name__ == "__main__":
    unittest.main()
