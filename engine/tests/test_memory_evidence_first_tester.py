"""First-tester findings on the evidence builder and on notes the secret scan cannot read: the builder must never write a digest its
own verify rejects, a full folder must not cost a parse of every transcript, a failed verify must name the files at fault, a long read
must say it started, and a note over the scan limit must be called what it is."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from collections import Counter
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import memtest_helpers as th  # noqa: E402
from test_memory_evidence import (  # noqa: E402
    NOW,
    Base,
    assistant,
    basic_session,
    sid,
    user,
    write_transcript,
)

sys.path.insert(0, th.SCRIPTS)
import memory_common as mc  # noqa: E402
import memory_dream_ground as dg  # noqa: E402
import memory_egress as eg  # noqa: E402
import memory_evidence as me  # noqa: E402
import memory_evidence_notes as en  # noqa: E402
import memory_hygiene as mh  # noqa: E402
import memory_scan as ms  # noqa: E402
import memory_snapshot as sn  # noqa: E402


def session_with(cwd=None, ts=None):
    """A short typed session whose records carry an unusual cwd and/or timestamp."""
    first, second = (
        user("how do we snapshot the memory directory?"),
        assistant("Use memory_hygiene.py snapshot."),
    )
    for rec in (first, second):
        if cwd is not None:
            rec["cwd"] = cwd
        if ts is not None:
            rec["timestamp"] = ts
    return [first, second]


class UnverifiableDigestTests(Base):
    def build_and_verify(self, records):
        write_transcript(self.source, 1, records)
        result = me.build_all(self.source, self.out, now=NOW, quiet_seconds=3600)
        return result, me.verify_dir(self.out)

    def test_a_relative_or_windows_cwd_is_written_as_unknown_and_verifies(self):
        for cwd in ("proj/sub", r"C:\Users\someone\proj", "~/work", "."):
            with self.subTest(cwd):
                self.setUp()
                result, verdict = self.build_and_verify(session_with(cwd=cwd))
                self.assertEqual(len(result.written), 1)
                self.assertTrue(verdict.ok, verdict)
                text = th.read(os.path.join(self.out, sid(1) + ".md"))
                self.assertIn("\ncwd: unknown\n", text)
                self.assertNotIn("someone", text)

    def test_an_absolute_cwd_is_kept(self):
        _result, verdict = self.build_and_verify(session_with(cwd="/work/proj"))
        self.assertTrue(verdict.ok)
        self.assertIn(
            "\ncwd: /work/proj\n", th.read(os.path.join(self.out, sid(1) + ".md"))
        )

    def test_an_impossible_clock_time_becomes_a_dashed_time_and_verifies(self):
        for stamp in (
            "2026-10-12T99:99:00.000Z",
            "2026-10-12T24:00:00.000Z",
            "2026-10-12T10:60:00.000Z",
        ):
            with self.subTest(stamp):
                self.setUp()
                _result, verdict = self.build_and_verify(session_with(ts=stamp))
                self.assertTrue(verdict.ok, verdict)
                body = th.read(os.path.join(self.out, sid(1) + ".md")).split("\n")[6:]
                self.assertTrue(
                    all(
                        line.startswith(("U --:-- ", "A --:-- "))
                        for line in body
                        if line
                    ),
                    body,
                )

    def test_whatever_else_makes_a_digest_unverifiable_it_is_not_written_and_is_counted(
        self,
    ):
        write_transcript(self.source, 1, basic_session())
        bad = me.Digest(sid(1), "not a digest at all\n", None, 1, 0, 0, 0)
        with mock.patch.object(me, "build_digest", return_value=bad):
            result = me.build_all(self.source, self.out, now=NOW, quiet_seconds=3600)
        self.assertEqual((result.written, result.skipped["invalid"]), ([], 1))
        self.assertEqual(self.digests(), [])

    def test_the_invalid_count_gets_its_own_note(self):
        lines = en.note_lines({}, {}, {"invalid": 2}, written=1)
        self.assertEqual(len(lines), 1)
        self.assertIn("2 session(s) were not written", lines[0])

    def test_would_verify_agrees_with_verify_on_a_good_and_a_bad_text(self):
        good = self.build_one(basic_session()).text
        self.assertTrue(me.would_verify(good, sid(1)))
        self.assertFalse(
            me.would_verify(good.replace("cwd: /work/proj", "cwd: relative"), sid(1))
        )
        self.assertFalse(
            me.would_verify(good, sid(2))
        )  # the header names another session


class FullFolderTests(Base):
    def fill(self):
        os.makedirs(self.out, mode=0o700)
        th.write(
            os.path.join(self.out, sid(99) + ".md"), "x" * (me.MAX_TOTAL_BYTES + 1)
        )
        os.chmod(os.path.join(self.out, sid(99) + ".md"), 0o600)

    def test_a_full_folder_parses_no_transcript(self):
        self.fill()
        for n in (1, 2, 3):
            write_transcript(self.source, n, basic_session())
        parsed = []
        with mock.patch.object(
            me, "build_digest", side_effect=lambda *a, **k: parsed.append(a) or None
        ):
            result = me.build_all(self.source, self.out, now=NOW, quiet_seconds=3600)
        self.assertEqual(parsed, [])
        self.assertEqual(result.skipped["size_cap"], 3)

    def test_the_note_says_up_to_because_some_waiting_sessions_would_be_skipped_anyway(
        self,
    ):
        text = en.note_lines({}, {}, {"size_cap": 4}, written=1)[0]
        self.assertTrue(text.startswith("NOTE up to 4 sessions"))
        self.assertIn("skipped anyway", text)


class ProgressTests(Base):
    def test_build_says_it_started_on_stderr_and_keeps_stdout_for_the_result(self):
        write_transcript(self.source, 1, basic_session())
        code, out, err = self.cli("build")
        self.assertEqual(code, 0, err)
        self.assertIn("BUILD-START sessions=1 limit=60", err)
        self.assertNotIn("BUILD-START", out)
        self.assertTrue(out.startswith("BUILD ok digests=1"))

    def test_scan_sources_says_it_started_on_stderr_and_stdout_stays_counts_only(self):
        write_transcript(self.source, 1, basic_session(), age_days=9)
        code, out, err = self.cli("scan-sources", "--sample", "5")
        self.assertEqual(code, 0, err)
        self.assertIn("SCAN-START sessions=1 sample=5", err)
        self.assertTrue(out.startswith("SOURCES scanned="))
        self.assertNotIn("START", out)

    def test_a_progress_callback_receives_the_line(self):
        write_transcript(self.source, 1, basic_session())
        seen = []
        me.build_all(
            self.source, self.out, now=NOW, quiet_seconds=3600, progress=seen.append
        )
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].startswith("BUILD-START"))


class VerifyNamesTests(Base):
    def make_bad(self):
        write_transcript(self.source, 1, basic_session())
        write_transcript(self.source, 2, basic_session(), age_days=6)
        me.build_all(self.source, self.out, now=NOW, quiet_seconds=3600)
        bad = os.path.join(self.out, sid(2) + ".md")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write("garbage\n")
        os.chmod(bad, 0o600)
        return sid(2) + ".md"

    def test_verify_names_the_first_bad_files(self):
        name = self.make_bad()
        verdict = me.verify_dir(self.out)
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.bad_names, (name,))

    def test_the_verify_command_prints_the_names(self):
        name = self.make_bad()
        code, out, _err = self.cli("verify")
        self.assertEqual(code, 1)
        self.assertIn("VERIFY-BAD first=%s" % name, out)

    def test_a_good_folder_prints_no_bad_line(self):
        write_transcript(self.source, 1, basic_session())
        me.build_all(self.source, self.out, now=NOW, quiet_seconds=3600)
        code, out, _err = self.cli("verify")
        self.assertEqual(code, 0)
        self.assertNotIn("VERIFY-BAD", out)

    def test_grounding_refuses_with_the_file_name(self):
        name = self.make_bad()
        with self.assertRaises(Exception) as ctx:
            dg.load_digests(self.out)
        self.assertIn(name, str(ctx.exception))
        self.assertIn("delete those files", str(ctx.exception))

    def test_an_empty_folder_is_told_to_build_first(self):
        os.makedirs(self.out, mode=0o700)
        text = me.failure_text(me.verify_dir(self.out))
        self.assertIn("no evidence digests yet", text)
        self.assertIn("idle for 6 hours", text)

    def test_names_are_capped_and_printable(self):
        names = ["x\x1b[31m%d.md" % i for i in range(20)]
        text = me.failure_text(
            me.Verify(20, 0, 20, 0, False, tuple(names[: me.MAX_BAD_NAMES]))
        )
        self.assertNotIn("\x1b", text)


class OversizeNoteTests(unittest.TestCase):
    def setUp(self):
        self.mem = th.make_dir(
            {
                "MEMORY.md": "# Index\n- [Big](big.md) big\n",
                "big.md": th.note("big") + "x" * 400,
                "small.md": th.note("small"),
            }
        )
        self.ops_root = tempfile.mkdtemp(prefix="bigops_")
        self.ops = mc.ops_dir_for(self.mem, self.ops_root)

    def test_the_scan_reason_names_the_limit_instead_of_a_python_error(self):
        with mock.patch.object(ms, "MAX_SCAN_BYTES", 200):
            result = ms.scan_tree(self.mem)
        self.assertIn(
            ("big.md", "larger than the 200 bytes scan limit, not scanned"),
            result.skipped,
        )

    def test_the_real_limit_is_written_in_mebibytes(self):
        self.assertEqual(ms.limit_text(), "50 MiB")
        with mock.patch.object(ms, "MAX_SCAN_BYTES", 3 * 1024 * 1024):
            self.assertEqual(ms.limit_text(), "3 MiB")

    def test_the_limit_error_is_still_a_value_error(self):
        with (
            mock.patch.object(ms, "MAX_SCAN_BYTES", 200),
            self.assertRaises(ValueError) as ctx,
        ):
            ms.scan_file(os.path.join(self.mem, "big.md"))
        self.assertIsInstance(ctx.exception, ms.ScanLimitError)

    def test_a_snapshot_says_how_many_entries_were_not_scanned(self):
        with mock.patch.object(ms, "MAX_SCAN_BYTES", 200):
            res = sn.take_snapshot(self.mem, self.ops, now=1_700_000_000)
        self.assertEqual(res["unscanned"], 1)
        manifest = json.loads(
            th.read(os.path.join(self.ops, "snapshots", res["id"], "manifest.json"))
        )
        self.assertEqual(manifest["scan"]["unscanned"], 1)

    def test_a_snapshot_with_nothing_unscanned_carries_no_such_key(self):
        res = sn.take_snapshot(self.mem, self.ops, now=1_700_000_000)
        self.assertNotIn("unscanned", res)

    def test_the_snapshot_command_prints_a_warning_that_names_the_consequence(self):
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(ms, "MAX_SCAN_BYTES", 200),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = mh.main(["--dir", self.mem, "--ops-root", self.ops_root, "snapshot"])
        self.assertEqual(code, 0, err.getvalue())
        text = out.getvalue()
        self.assertIn("WARN 1 note(s) were not scanned for secrets", text)
        self.assertIn("exit 4", text)
        self.assertIn("scan_block=0", text)  # the SNAPSHOT line itself is unchanged

    def test_the_egress_refusal_tells_the_user_to_shrink_the_note(self):
        with (
            mock.patch.object(ms, "MAX_SCAN_BYTES", 200),
            self.assertRaises(eg.SecretShapesRefused) as ctx,
        ):
            eg.require_clear(self.mem)
        message = str(ctx.exception)
        self.assertIn("could not be scanned", message)
        self.assertIn("shrink a note over 200 bytes", message)


if __name__ == "__main__":
    unittest.main()
