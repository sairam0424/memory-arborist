"""What a first run needs from the evidence builder: newest sessions first, sessions nobody typed in left out, and plain words
about what was left out and why (other entrypoints, record keys this tool does not know)."""

import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import memtest_helpers as th  # noqa: E402
from test_memory_evidence import (  # noqa: E402
    NOW,
    Base,
    assistant,
    basic_session,
    run_cli,
    sid,
    user,
    write_transcript,
)

sys.path.insert(0, th.SCRIPTS)
import memory_digest as md  # noqa: E402
import memory_evidence as me  # noqa: E402
import memory_evidence_notes as en  # noqa: E402


def session_with(entrypoint="cli", **extra):
    return [
        user(
            "how do we snapshot the memory directory?", entrypoint=entrypoint, **extra
        ),
        assistant("Use snapshot, it copies and verifies.", entrypoint=entrypoint),
    ]


class OrderTests(Base):
    def test_the_newest_idle_sessions_are_chosen_when_there_are_more_than_the_limit(
        self,
    ):
        for n in range(1, 6):
            write_transcript(
                self.source, n, basic_session(), age_days=30 - n
            )  # n=5 is the newest
        result = me.build_all(
            self.source, self.out, limit=2, now=NOW, quiet_seconds=3600
        )
        self.assertEqual(result.written, [sid(5), sid(4)])

    def test_list_sessions_returns_newest_first(self):
        for n in (1, 2, 3):
            write_transcript(self.source, n, basic_session(), age_days=30 - n)
        paths, _skipped = me.list_sessions(self.source, NOW, 3600)
        self.assertEqual(
            [os.path.basename(p) for p in paths],
            [sid(3) + ".jsonl", sid(2) + ".jsonl", sid(1) + ".jsonl"],
        )


class NoUserTests(Base):
    def test_a_session_with_no_typed_line_is_skipped_and_counted(self):
        write_transcript(
            self.source, 1, [assistant("I will start by reading the file.")]
        )
        result = me.build_all(self.source, self.out, now=NOW, quiet_seconds=3600)
        self.assertEqual(result.written, [])
        self.assertEqual(result.skipped["no_user"], 1)

    def test_a_session_with_a_typed_line_still_builds(self):
        write_transcript(self.source, 1, basic_session())
        self.assertEqual(
            me.build_all(self.source, self.out, now=NOW, quiet_seconds=3600).written,
            [sid(1)],
        )


class EntrypointTests(Base):
    def build(self, **kw):
        return me.build_all(self.source, self.out, now=NOW, quiet_seconds=3600, **kw)

    def test_other_entrypoints_are_left_out_and_named(self):
        write_transcript(self.source, 1, session_with("claude-vscode"))
        write_transcript(self.source, 2, session_with("sdk-py"))
        write_transcript(self.source, 3, session_with("sdk-py"))
        result = self.build()
        self.assertEqual(result.written, [])
        self.assertEqual(dict(result.entrypoints), {"claude-vscode": 1, "sdk-py": 2})

    def test_an_allowed_entrypoint_is_built(self):
        write_transcript(self.source, 1, session_with("claude-vscode"))
        result = self.build(entrypoints=md.DEFAULT_ENTRYPOINTS | {"claude-vscode"})
        self.assertEqual(result.written, [sid(1)])
        self.assertFalse(result.entrypoints)

    def test_an_entrypoint_name_from_a_transcript_is_cleaned_before_it_is_printed(self):
        write_transcript(
            self.source,
            1,
            session_with(
                "evil\x1b[31m name\nwith spaces and a very long tail " + "x" * 100
            ),
        )
        name = next(iter(self.build().entrypoints))
        self.assertRegex(name, r"^[A-Za-z0-9_-]{1,30}$")

    def test_a_non_text_entrypoint_does_not_crash_the_scan(self):
        write_transcript(self.source, 1, session_with(["list"]))
        write_transcript(self.source, 2, session_with({"dict": 1}))
        result = self.build()
        self.assertEqual(result.written, [])
        self.assertEqual(result.skipped["non_cli"], 2)

    def test_scan_sources_honours_the_allowed_set_too(self):
        write_transcript(self.source, 1, session_with("claude-vscode"))
        self.assertEqual(
            me.scan_sources(self.source, 5, now=NOW, quiet_seconds=3600)[0], 0
        )
        self.assertEqual(
            me.scan_sources(
                self.source,
                5,
                now=NOW,
                quiet_seconds=3600,
                entrypoints=md.DEFAULT_ENTRYPOINTS | {"claude-vscode"},
            )[0],
            1,
        )


class UnknownKeyTests(Base):
    def build(self):
        return me.build_all(self.source, self.out, now=NOW, quiet_seconds=3600)

    def test_a_user_record_with_a_key_nobody_knows_is_left_out_and_counted_by_name(
        self,
    ):
        records = [
            user("first typed line about the deploy day", brandNewKey=1),
            user("second typed line that is fine"),
        ]
        write_transcript(self.source, 1, records)
        result = self.build()
        self.assertEqual(result.written, [sid(1)])
        self.assertEqual(dict(result.unknown_keys), {"brandNewKey": 1})
        with open(os.path.join(self.out, sid(1) + ".md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("second typed line", text)
        self.assertNotIn("first typed line", text)

    def test_every_record_is_counted_and_a_session_of_only_such_records_has_no_digest(
        self,
    ):
        write_transcript(
            self.source,
            1,
            [
                user("typed line one that is long enough", newKey=1),
                user("typed line two that is long enough", newKey=2),
            ],
        )
        result = self.build()
        self.assertEqual(result.written, [])
        self.assertEqual(dict(result.unknown_keys), {"newKey": 2})

    def test_tool_results_and_agent_records_are_not_reported_as_unknown_keys(self):
        records = [
            user("a typed line that is long enough to keep"),
            {
                "type": "user",
                "entrypoint": "cli",
                "cwd": "/w",
                "isSidechain": False,
                "toolUseResult": {"stdout": "x"},
                "extraKey": 1,
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t", "content": "x"}
                    ],
                },
            },
            user(
                "agent message", extraAgentKey=1, origin={"kind": "task-notification"}
            ),
            user("sidechain message", extraSideKey=1, isSidechain=True),
        ]
        write_transcript(self.source, 1, records)
        self.assertFalse(self.build().unknown_keys)

    def test_key_names_are_cleaned_and_capped_per_record(self):
        weird = {("k%02d" % i): 1 for i in range(20)}
        weird["bad\x1b[0m key\n" + "y" * 80] = 1
        write_transcript(
            self.source, 1, [user("a typed line that is long enough to keep", **weird)]
        )
        counts = self.build().unknown_keys
        self.assertLessEqual(len(counts), md.MAX_UNKNOWN_KEYS_PER_RECORD)
        for name in counts:
            self.assertRegex(name, r"^[A-Za-z0-9_-]{1,30}$")


class NotesAndCommandTests(Base):
    def test_note_lines_are_empty_when_nothing_was_left_out(self):
        self.assertEqual(en.note_lines({}, {}), [])
        self.assertEqual(en.note_lines(None, None), [])

    def test_note_lines_name_the_entrypoints_and_the_flag_that_includes_them(self):
        text = "\n".join(en.note_lines({"sdk-py": 574, "claude-vscode": 3}, {}))
        self.assertIn("sdk-py=574", text)
        self.assertLess(text.index("sdk-py=574"), text.index("claude-vscode=3"))
        self.assertIn("--include-entrypoint NAME", text)

    def test_note_lines_name_the_unknown_keys_and_ask_for_a_report(self):
        text = "\n".join(en.note_lines({}, {"newKey": 4}))
        self.assertIn("newKey=4", text)
        self.assertIn("report", text)

    def test_only_a_few_names_are_shown(self):
        counter = {"n%02d" % i: i + 1 for i in range(20)}
        self.assertEqual(en.note_lines(counter, {})[0].count("="), en.MAX_NAMES_SHOWN)

    def test_the_build_command_prints_the_notes_and_the_flag_works(self):
        write_transcript(self.source, 1, session_with("claude-vscode"))
        base = [
            "build",
            "--source",
            self.source,
            "--out",
            self.out,
            "--now",
            str(NOW),
            "--quiet-seconds",
            "3600",
        ]
        code, out, _err = run_cli(*base)
        self.assertEqual(code, 0, out)
        self.assertIn("BUILD ok digests=0", out)
        self.assertIn(
            "NOTE sessions left out because of their entrypoint: claude-vscode=1", out
        )
        code, out, _err = run_cli(*base, "--include-entrypoint", "claude-vscode")
        self.assertIn("BUILD ok digests=1", out)
        self.assertNotIn("NOTE sessions left out", out)

    def test_a_bad_entrypoint_name_is_a_usage_error(self):
        for bad in ("has space", "x" * 31, "semi;colon", ""):
            with self.subTest(bad):
                code, _out, err = run_cli_expecting_exit(
                    "build", "--source", self.source, "--include-entrypoint", bad
                )
                self.assertEqual(code, 64, err)


def run_cli_expecting_exit(*argv):
    import contextlib
    import io

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = me.main(list(argv))
        except SystemExit as exit_info:
            code = exit_info.code
    return code, out.getvalue(), err.getvalue()


class SizeCapTests(Base):
    """The evidence folder is capped; the build has to say when the cap leaves sessions without a digest."""

    def seed(self, count):
        for n in range(1, count + 1):
            write_transcript(self.source, n, basic_session(), age_days=30 - n)

    def cli(self, *extra):
        return run_cli("build", "--source", self.source, "--out", self.out, "--now", str(NOW), "--quiet-seconds", "3600", *extra)

    def test_every_session_left_waiting_by_the_cap_is_counted_and_a_note_says_what_to_do(self):
        self.seed(6)
        one = len(me.build_all(self.source, self.out, limit=1, now=NOW, quiet_seconds=3600).written)
        self.assertEqual(one, 1)
        size = os.path.getsize(os.path.join(self.out, os.listdir(self.out)[0]))
        with mock.patch.object(me, "MAX_TOTAL_BYTES", size * 3 + 10):
            result = me.build_all(self.source, self.out, now=NOW, quiet_seconds=3600)
            self.assertEqual(len(result.written), 2)  # three fit in all, one was there already
            self.assertEqual(result.skipped["size_cap"], 3)  # the other three sessions are waiting
            code, out, _err = self.cli()
        self.assertEqual(code, 0, out)
        self.assertIn("skipped_size_cap=3", out)
        self.assertIn("NOTE 3 sessions got no digest because the evidence folder reached its 512 KiB cap", out)
        self.assertIn("purge-plan", out)

    def test_below_the_cap_there_is_no_cap_note(self):
        self.seed(3)
        _code, out, _err = self.cli()
        self.assertNotIn("512 KiB", out)


class NoDigestNoteTests(Base):
    def cli(self, *extra):
        return run_cli("build", "--source", self.source, "--out", self.out, "--now", str(NOW), "--quiet-seconds", "3600", *extra)

    def test_a_build_that_wrote_nothing_explains_the_commonest_reason(self):
        write_transcript(self.source, 1, basic_session(), age_days=0)  # idle for less than the quiet time
        code, out, _err = self.cli()
        self.assertEqual(code, 0)
        self.assertIn("BUILD ok digests=0", out)
        self.assertIn("skipped_recent=1", out)
        self.assertIn("NOTE no digest was written. Skipped, by count: recent=1", out)

    def test_a_build_that_wrote_something_prints_no_such_note(self):
        write_transcript(self.source, 1, basic_session(), age_days=9)
        _code, out, _err = self.cli()
        self.assertIn("BUILD ok digests=1", out)
        self.assertNotIn("no digest was written", out)

    def test_nothing_to_skip_and_nothing_written_prints_no_note(self):
        _code, out, _err = self.cli()
        self.assertNotIn("NOTE", out)


class UnknownKeyBoundTests(Base):
    def test_the_distinct_unknown_key_names_are_bounded_and_the_rest_are_counted_together(self):
        records = [user("typed line number %03d is long enough" % i, **{"newKey%03d" % i: 1}) for i in range(120)]
        write_transcript(self.source, 1, records, age_days=9)
        result = me.build_all(self.source, self.out, now=NOW, quiet_seconds=3600)
        counts = dict(result.unknown_keys)
        self.assertLessEqual(len(counts), md.MAX_UNKNOWN_KEY_NAMES + 1)
        self.assertEqual(sum(counts.values()), 120)
        self.assertIn("other", counts)


class HeadlessOptInTests(Base):
    def test_an_sdk_entrypoint_cannot_be_opted_in(self):
        import contextlib
        import io

        for name in ("sdk-py", "sdk-cli", "SDK-TS", "sdk"):
            with self.subTest(name):
                err = io.StringIO()
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                    try:
                        code = me.main(["build", "--source", self.source, "--out", self.out, "--include-entrypoint", name])
                    except SystemExit as exit_info:
                        code = exit_info.code
                self.assertEqual(code, 64)
                self.assertIn("headless runs", err.getvalue())


if __name__ == "__main__":
    unittest.main()
