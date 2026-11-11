"""Behaviour tests for the memory_hygiene CLI: exit codes, logging, caps, error mapping, path rules, outputs."""

import contextlib
import io
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memtest_helpers as th  # noqa: E402

sys.path.insert(0, th.SCRIPTS)  # python3 -I does not put the script dir on sys.path
import memory_common as mc  # noqa: E402
import memory_hygiene as mh  # noqa: E402
import memory_snapshot as sn  # noqa: E402

SCRIPT = os.path.join(th.SCRIPTS, "memory_hygiene.py")
LONG = "this baseline line is long enough to count as content"
INDEX = "- [A](feedback-a.md) hook\n"
IDX_A = "- [A](a.md) hook\n"
NOTE_A = th.note("feedback-a")


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = mh.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def run_exit(*argv):
    """(exit code, stdout, stderr) of a call that must end in SystemExit."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), unittest.TestCase().assertRaises(SystemExit) as caught:
        mh.main(list(argv))
    return caught.exception.code, out.getvalue(), err.getvalue()


def run_script(*argv):
    return subprocess.run([sys.executable, "-I", SCRIPT, *argv], capture_output=True, text=True, timeout=60)


def numbered(count):
    return "".join("baseline line number %03d is long enough\n" % i for i in range(count))


class Env(unittest.TestCase):
    def setUp(self):
        self.mem = th.make_dir({"MEMORY.md": INDEX, "feedback-a.md": NOTE_A})
        self.root = tempfile.mkdtemp(prefix="memops_")
        self.ops = mc.ops_dir_for(self.mem, self.root)

    def cli(self, *argv):
        return run_cli("--dir", self.mem, "--ops-root", self.root, *argv)

    def on(self, directory, *argv):
        return run_cli("--dir", directory, "--ops-root", self.root, *argv)

    def log_text(self, ops=None):
        path = os.path.join(ops or self.ops, "log.md")
        return th.read(path) if os.path.exists(path) else ""

    def edit(self, text="edited by a live session\n"):
        th.write(os.path.join(self.mem, "feedback-a.md"), text, "a")

    def snap(self):
        code, out, err = self.cli("snapshot")
        self.assertEqual((code, err), (0, ""), out)
        return sn.list_snapshots(self.ops)[-1]

    def with_defaults(self):
        for name, value in (("DEFAULT_MEMORY_DIR", self.mem), ("DEFAULT_OPS_ROOT", self.root)):
            patcher = mock.patch.object(mc, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tamper(self, snap_id):
        copy = os.path.join(self.ops, "snapshots", snap_id, "memory", "feedback-a.md")
        os.chmod(copy, 0o600)
        th.write(copy, "tampered\n", "a")
        return copy


class UsageTests(Env):
    def test_usage_errors_exit_64_with_a_usage_line_on_stderr(self):
        bad = [[], ["bogus"], ["lint", "--nope"], ["restore"], ["restore", "--snapshot", "x"], ["restore", "--file", "x.md"],
               ["prune-plan", "--keep", "x"], ["log", "bogus", "x"], ["snapshot", "--no-log"], ["compare", "only-one"],
               ["verify-snapshot", "--bogus"], ["--bogus", "lint"]]
        for argv in bad:
            with self.subTest(argv):
                code, out, err = run_exit(*argv)
                self.assertEqual((code, out), (64, ""))
                self.assertTrue(err.startswith("usage: memory_hygiene.py"), err)
                self.assertIn("error:", err)

    @staticmethod
    def registered():
        return re.search(r"\{([a-z,-]+)\}", run_exit("--help")[1]).group(1).split(",")

    def test_help_exits_zero_on_stdout_and_the_docstring_lists_the_registered_commands(self):
        code, out, err = run_exit("--help")
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.startswith("usage: memory_hygiene.py"))
        self.assertIn(mh.__doc__.split("\n")[0], " ".join(out.split()))
        for name in self.registered():
            with self.subTest(name):
                code, out, err = run_exit(name, "--help")
                self.assertEqual((code, err), (0, ""))
                self.assertTrue(out.startswith("usage: memory_hygiene.py " + name), out)
        listed = re.search(r"^Commands: (.+)\.$", mh.__doc__, flags=re.M).group(1).split(", ")
        self.assertEqual((sorted(listed), len(listed)), (sorted(self.registered()), 9))
        deferred = re.search(r"Deferred to the dream phase: (.+)\.", mh.__doc__).group(1).split(", ")
        self.assertEqual(set(deferred) & set(self.registered()), set())

    def test_options_before_and_after_the_command_both_work_and_the_later_one_wins(self):
        for argv in (("--dir", self.mem, "--ops-root", self.root, "lint", "--no-log"), ("lint", "--dir", self.mem, "--ops-root", self.root, "--no-log"),
                     ("--dir", self.mem, "lint", "--ops-root", self.root, "--no-log"), ("--ops-root", self.root, "lint", "--dir", self.mem, "--no-log")):
            with self.subTest(argv):
                code, out, _ = run_cli(*argv)
                self.assertEqual((code, "NOTES n=1 " in out), (0, True))
        self.assertEqual(run_cli("--dir", "/nonexistent/first", "lint", "--dir", self.mem, "--ops-root", self.root, "--no-log")[0], 0)
        code, _out, err = run_cli("--dir", self.mem, "lint", "--dir", "/nonexistent/second", "--ops-root", self.root)
        self.assertEqual((code, "/nonexistent/second" in err), (1, True))


class ScanTests(Env):
    def leaky(self, extra=None):
        return th.make_dir({"MEMORY.md": "", "a.md": th.note("a", "leak " + th.fake_github_token()), **(extra or {})})

    def warn_dir(self, count, extra=None):
        return th.make_dir({"MEMORY.md": "", "a.md": "".join("u%d@example.com\n" % i for i in range(count)), **(extra or {})})

    def test_block_beats_skipped_beats_ok_and_warn_alone_is_ok(self):
        bad = self.leaky()
        os.symlink(os.path.join(bad, "a.md"), os.path.join(bad, "alias.md"))
        code, out, _ = self.on(bad, "scan", "--no-log")
        self.assertEqual(code, 2)
        self.assertIn("SCAN block=1 warn=0 skipped=1", out)
        os.symlink(os.path.join(self.mem, "feedback-a.md"), os.path.join(self.mem, "alias.md"))
        self.assertEqual(self.cli("scan", "--no-log")[0], 1)
        code, out, _ = self.on(th.make_dir({"MEMORY.md": "", "a.md": th.note("a", "write to who@example.com")}), "scan", "--no-log")
        self.assertEqual(code, 0)
        self.assertIn("WARN a.md:", out)
        self.assertIn("SCAN block=0 warn=1 skipped=0", out)

    def test_output_order_exact_lines_and_log_entry(self):
        bad = self.leaky({"b.md": "me@example.com\nyou@example.com\n"})
        os.symlink(os.path.join(bad, "a.md"), os.path.join(bad, "alias.md"))
        lines = self.on(bad, "scan")[1].splitlines()
        self.assertEqual(lines[0], "BLOCK a.md:3:GitHub Token")
        self.assertEqual(lines[1:3], ["WARN b.md:1:Email", "WARN b.md:2:Email"])
        self.assertEqual(lines[3:], ["SKIPPED alias.md: symlink, not scanned", "SCAN block=1 warn=2 skipped=1"])
        self.assertEqual(self.log_text(mc.ops_dir_for(bad, self.root)), "\n## [%s] scan | block=1 warn=2 skipped=1\n" % mc.local_date())

    def test_warn_hits_are_capped_at_twenty_unless_full(self):
        for count, shown, more in ((20, 20, None), (21, 20, 1), (25, 20, 5)):
            with self.subTest(count):
                out = self.on(self.warn_dir(count), "scan", "--no-log")[1]
                self.assertEqual(len(re.findall(r"^WARN ", out, flags=re.M)), shown)
                self.assertEqual("  ... +%s more warn hits (use --full)\n" % more in out, more is not None)
                self.assertEqual(bool(re.search(r"\+\d+ more", out)), more is not None)
                self.assertIn("SCAN block=0 warn=%d skipped=0" % count, out)
        full = self.on(self.warn_dir(25), "scan", "--no-log", "--full")[1]
        self.assertEqual(len(re.findall(r"^WARN ", full, flags=re.M)), 25)
        self.assertNotIn("more warn hits", full)

    def test_block_hits_are_always_listed_first_and_warns_are_sorted(self):
        files = {"z.md": "".join("leak %s\n" % th.fake_github_token() for _ in range(2)), "b.md": "me@example.com\n", "c.md": "x\nx\nmore@example.com\n"}
        bad = self.warn_dir(25, files)
        lines = self.on(bad, "scan", "--no-log")[1].splitlines()
        self.assertEqual(lines[:2], ["BLOCK z.md:1:GitHub Token", "BLOCK z.md:2:GitHub Token"])
        self.assertEqual(sum(line.startswith("WARN ") for line in lines), 20)
        full = self.on(bad, "scan", "--no-log", "--full")[1].splitlines()
        self.assertEqual(full[:2], lines[:2])
        order = [line.split()[1] for line in full if line.startswith("WARN ")]
        self.assertEqual(order, sorted(order, key=lambda s: (s.split(":")[0], int(s.split(":")[1]))))
        self.assertEqual((order[0], len(order), full[-1]), ("a.md:1:Email", 27, "SCAN block=2 warn=27 skipped=0"))


class LintTests(Env):
    def test_exit_code_by_severity_and_strict(self):
        padded = th.make_dir({"MEMORY.md": "x" * 160 + "\n", "a.md": th.note("a")})
        broken = th.make_dir({"MEMORY.md": INDEX, "a.md": th.note("a", body="[[missing-target]]")})
        for directory, flags, expected in ((self.mem, (), 0), (self.mem, ("--strict",), 0), (padded, (), 0), (padded, ("--strict",), 1),
                                           (broken, (), 1), (broken, ("--strict",), 1)):
            with self.subTest(padded=directory == padded, flags=flags):
                self.assertEqual(self.on(directory, "lint", "--no-log", *flags)[0], expected)
        self.assertIn("LINT errors=0 warn=1 ", self.on(padded, "lint", "--no-log")[1])

    def test_full_lifts_the_per_code_cap(self):
        many = th.make_dir({"MEMORY.md": IDX_A, "a.md": th.note("a", body="\n".join("[[miss%d]]" % i for i in range(12)))})
        short = self.on(many, "lint", "--no-log")[1]
        self.assertEqual(short.count("ERROR dangling-link"), 10)
        self.assertIn("... +2 more dangling-link (use --full)", short)
        full = self.on(many, "lint", "--no-log", "--full")[1]
        self.assertEqual(full.count("ERROR dangling-link"), 12)
        self.assertNotIn("more dangling-link", full)

    def test_a_missing_directory_or_a_file_is_one_error_line_and_prints_no_report(self):
        for target in ("/nonexistent/lint-dir", os.path.join(self.mem, "MEMORY.md")):
            for command in ("lint", "scan"):
                with self.subTest(command, target=target):
                    self.assertEqual(self.on(target, command), (1, "", "ERROR: directory not found: %s\n" % target))
        self.assertEqual(self.log_text(), "")

    def test_the_origin_check_looks_for_transcripts_beside_the_memory_dir(self):
        project = th.make_dir({"memory/MEMORY.md": INDEX, "memory/feedback-a.md": th.note("feedback-a", extra="originSessionId: abcd1234-ef56\n")})
        memory = os.path.join(project, "memory")
        self.assertIn("DEAD_ORIGIN=1", self.on(memory, "lint", "--no-log")[1])
        th.write(os.path.join(project, "abcd1234-ef56.jsonl"), "{}\n")
        self.assertIn("DEAD_ORIGIN=0", self.on(memory, "lint", "--no-log")[1])

    def test_the_log_entry_has_the_counts_and_the_effective_index_numbers(self):
        padded = th.make_dir({"MEMORY.md": IDX_A + "\n\n", "a.md": th.note("a", body="[[gone]]")})
        index_line = self.on(padded, "lint")[1].splitlines()[0]
        chars, headroom = re.search(r"chars=(\d+)", index_line).group(1), re.search(r"headroom=(\d+)", index_line).group(1)
        self.assertIn("lines=3 ", index_line)
        entry = self.log_text(mc.ops_dir_for(padded, self.root))
        self.assertRegex(entry, r"\n## \[\d{4}-\d\d-\d\d\] lint \| errors=1 warn=0\n")
        self.assertTrue(entry.endswith("- Index: chars=%s lines=1 headroom=%s\n" % (chars, headroom)), entry)

    def test_the_tripwire_is_fed_the_markdown_count_of_the_newest_snapshot(self):
        self.assertIn("TRIPWIRE=0", self.cli("lint", "--no-log")[1])
        self.snap()
        for name in ("n1.md", "n2.md", "n3.md"):
            th.write(os.path.join(self.mem, name), th.note(name[:-3]))
        th.write(os.path.join(self.mem, "extra.txt"), "x")
        self.snap()
        os.unlink(os.path.join(self.mem, "extra.txt"))
        self.assertIn("TRIPWIRE=0", self.cli("lint", "--no-log")[1])
        os.unlink(os.path.join(self.mem, "n3.md"))
        out = self.cli("lint", "--no-log")[1]
        self.assertIn("TRIPWIRE=1", out)
        self.assertIn("4 markdown files now vs 5 in the last snapshot", out)

    def test_a_staged_copy_never_reads_the_real_ops_snapshots_or_writes_a_log(self):
        staged = os.path.join(th.make_dir({"memory/MEMORY.md": "", **{"memory/%s.md" % n: th.note(n) for n in "abc"}}), "memory")
        staged_ops = mc.ops_dir_for(staged, self.root)
        sn.take_snapshot(staged, staged_ops)
        os.unlink(os.path.join(staged, "c.md"))
        self.assertIn("TRIPWIRE=1", self.on(staged, "lint", "--no-log")[1])
        with mock.patch.object(mc, "DEFAULT_OPS_ROOT", self.root), mock.patch.object(mc, "DEFAULT_MEMORY_DIR", self.mem):
            code, out, err = run_cli("--dir", staged, "lint")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("TRIPWIRE=0", out)
        self.assertNotIn("lint |", self.log_text(staged_ops))
        self.assertEqual(self.log_text(), "")


class CompareTests(Env):
    def pair(self, base_files, stage_files):
        return th.make_dir(base_files), th.make_dir(stage_files)

    def test_exact_lines_and_exit_codes_for_ok_lost_and_unverifiable(self):
        base, stage = self.pair({"a.md": "short\n" + LONG + "\n"}, {"b.md": LONG + "\n"})
        self.assertEqual(self.cli("compare", base, stage, "--no-log"), (0, "COMPARE baseline_files=1 lines_checked=1 lost_lines=0\n", ""))
        base, stage = self.pair({"a\x1bb.md": "short\n" + LONG + "\n"}, {"b.md": "nothing alike\n"})
        self.assertEqual(self.cli("compare", base, stage, "--no-log")[:2], (1, "LOST a?b.md:2\nCOMPARE baseline_files=1 lines_checked=1 lost_lines=1\n"))
        short, stage = self.pair({"a.md": "tiny\nmodified: whatever date string goes here\n"}, {"b.md": "x\n"})
        self.assertEqual(self.cli("compare", short, stage, "--no-log")[:2],
                         (1, "COMPARE baseline_files=1 lines_checked=0 lost_lines=0 (cannot verify: nothing to compare)\n"))
        empty, stage = self.pair({"x.txt": "no markdown"}, {"b.md": LONG + "\n"})
        code, out, _ = self.cli("compare", empty, stage, "--no-log")
        self.assertEqual((code, out.count("cannot verify")), (1, 1))
        self.assertIn("baseline_files=0 lines_checked=0", out)

    def test_lost_lines_are_capped_at_fifty(self):
        for count, more in ((50, None), (51, 1), (60, 10)):
            with self.subTest(count):
                base, stage = self.pair({"a.md": numbered(count)}, {"b.md": "other\n"})
                code, out, _ = self.cli("compare", base, stage, "--no-log")
                lines = out.splitlines()
                self.assertEqual(code, 1)
                self.assertEqual([x for x in lines if x.startswith("LOST")], ["LOST a.md:%d" % n for n in range(1, 51)])
                self.assertEqual(any(re.match(r"  \.\.\. \+\d+ more", x) for x in lines), more is not None)
                self.assertEqual("  ... +%s more" % more in lines, more is not None)
                self.assertEqual(lines[-1], "COMPARE baseline_files=1 lines_checked=%d lost_lines=%d" % (count, count))

    def test_files_scope_ignores_empty_names_and_empty_means_everything(self):
        base, stage = self.pair({"a.md": LONG + "\n", "b.md": LONG.replace("baseline", "second") + "\n"}, {"s.md": LONG + "\n"})
        for flags, code, counts in ((("--files", "a.md"), 0, "baseline_files=1"), (("--files", "a.md,"), 0, "baseline_files=1"),
                                    (("--files", "b.md"), 1, "baseline_files=1"), (("--files", ""), 1, "baseline_files=2"),
                                    (("--files", ","), 1, "baseline_files=2"), ((), 1, "baseline_files=2")):
            with self.subTest(flags):
                got, out, _ = self.cli("compare", base, stage, "--no-log", *flags)
                self.assertEqual(got, code)
                self.assertIn(counts, out)
        self.assertEqual(self.cli("compare", base, stage, "--no-log", "--files", "nope.md")[0::2], (1, "ERROR: not in the baseline: nope.md\n"))

    def test_snapshot_ids_resolve_and_latest_means_the_newest(self):
        th.write(os.path.join(self.mem, "feedback-a.md"), LONG + "\n", "a")
        first = self.snap()
        th.write(os.path.join(self.mem, "feedback-a.md"), NOTE_A + "second era line, long enough here\n")
        second = self.snap()
        stage = th.make_dir({"feedback-a.md": NOTE_A + "second era line, long enough here\n", "MEMORY.md": INDEX})
        self.assertEqual(self.cli("compare", "latest", stage, "--no-log")[0], 0)
        self.assertEqual(self.cli("compare", second, stage, "--no-log")[0], 0)
        code, out, _ = self.cli("compare", first, stage, "--no-log")
        self.assertEqual(code, 1)
        self.assertRegex(out, r"LOST feedback-a\.md:\d+")

    def test_refusals_are_one_error_line_each(self):
        stage = th.make_dir({"b.md": LONG + "\n"})
        link = os.path.join(tempfile.mkdtemp(), "live-link")
        os.symlink(self.mem, link)
        live = "ERROR: the live directory changes under you; compare against a snapshot id instead\n"
        for baseline in (self.mem, link, self.mem + "/../" + os.path.basename(self.mem)):
            with self.subTest(baseline):
                self.assertEqual(self.cli("compare", baseline, stage, "--no-log")[0::2], (1, live))
        self.assertEqual(self.cli("compare", "nosuch", stage)[0::2], (1, "ERROR: not a snapshot id or a directory: nosuch\n"))
        self.assertEqual(self.cli("compare", "late\x1bst", stage)[0::2], (1, "ERROR: not a snapshot id or a directory: late?st\n"))
        self.assertEqual(self.cli("compare", "latest", stage)[0::2], (1, "ERROR: no snapshots yet; run snapshot first\n"))
        self.snap()
        self.assertEqual(self.cli("compare", "latest", "/nonexistent/stage", "--no-log")[0::2], (1, "ERROR: not a directory: /nonexistent/stage\n"))

    def test_only_a_snapshot_baseline_is_logged_and_no_log_is_honoured(self):
        base, stage = self.pair({"a.md": LONG + "\n"}, {"b.md": LONG + "\n"})
        self.cli("compare", base, stage)
        self.assertEqual(self.log_text(), "")
        self.snap()
        self.cli("compare", "latest", stage, "--no-log")
        self.assertNotIn("compare |", self.log_text())
        partial = th.make_dir({"MEMORY.md": INDEX})
        stats = re.search(r"lines_checked=(\d+) lost_lines=(\d+)", self.cli("compare", "latest", partial)[1])
        self.assertNotEqual(stats.group(1), stats.group(2))
        self.assertEqual(self.log_text().count("compare |"), 1)
        self.assertTrue(self.log_text().endswith("] compare | lost_lines=%s of %s lines\n" % (stats.group(2), stats.group(1))), self.log_text())


class SnapshotTests(Env):
    def test_the_snapshot_line_has_every_field_in_order_and_the_log_matches(self):
        th.write(os.path.join(self.mem, "leak.md"), "x " + th.fake_github_token() + "\nwho@example.com\nme@example.com\n")
        os.symlink(os.path.join(self.mem, "leak.md"), os.path.join(self.mem, "one.md"))
        os.symlink(os.path.join(self.mem, "leak.md"), os.path.join(self.mem, "two.md"))
        code, out, err = self.cli("snapshot")
        snap_id = sn.list_snapshots(self.ops)[0]
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines(), ["WARN the copied notes contain 1 block-tier secret shapes; run scan to see where",
                                            "SNAPSHOT ok files=3 manifest_sha256_ok=3 id=%s scan_block=1 scan_warn=2 skipped_symlinks=2" % snap_id])
        self.assertEqual(self.log_text(), "\n## [%s] snapshot | %s\n- Files: 3\n- Scan: block=1 warn=2\n- Skipped (symlinks, special files): 2\n"
                         % (mc.local_date(), snap_id))
        self.assertEqual(run_exit("--dir", self.mem, "snapshot", "--no-log")[0], 64)
        self.assertEqual(self.cli("snapshot"), (0, "NOOP identical to %s\n" % snap_id, ""))
        self.assertEqual(self.log_text().count("snapshot |"), 1)

    def test_a_damaged_previous_snapshot_is_warned_about_before_the_new_line(self):
        first = self.snap()
        self.tamper(first)
        code, out, _ = self.cli("snapshot")
        second = sn.list_snapshots(self.ops)[-1]
        self.assertNotEqual(first, second)
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["WARN the previous snapshot failed verification (1 problems); this one replaces it as the reference",
                                            "SNAPSHOT ok files=2 manifest_sha256_ok=2 id=%s scan_block=0 scan_warn=0 skipped_symlinks=0" % second])

    def test_listing_shows_valid_snapshots_oldest_first_then_incomplete_ones(self):
        self.assertEqual(self.cli("snapshots"), (0, "", ""))
        first = self.snap()
        self.edit()
        second = self.snap()
        os.makedirs(os.path.join(self.ops, "snapshots", "bad\x1b[0m"))
        shutil.copytree(os.path.join(self.ops, "snapshots", first), os.path.join(self.ops, "snapshots", first + "\x1bX"))
        out = self.cli("snapshots")[1]
        self.assertNotIn("\x1b", out)
        self.assertEqual(out.splitlines(), ["%s files=2" % first, "%s?X files=2" % first, "%s files=2" % second, "bad?[0m incomplete (no valid manifest)"])

    def test_verify_picks_the_named_snapshot_and_indents_problem_lines(self):
        first = self.snap()
        self.edit()
        second = self.snap()
        copy = self.tamper(second)
        self.assertEqual(self.cli("verify-snapshot", "--snapshot", first)[:2], (0, "VERIFY ok files=2 id=%s\n" % first))
        self.assertEqual(self.cli("verify-snapshot")[:2], (1, "VERIFY FAIL files=2 id=%s\n  sha256 mismatch: feedback-a.md\n" % second))
        os.unlink(copy)
        self.assertIn("\n  missing: feedback-a.md\n", self.cli("verify-snapshot", "--snapshot", second)[1])
        self.assertEqual(self.cli("verify-snapshot", "--snapshot", "nosuch")[0::2], (1, "ERROR: no such snapshot: nosuch\n"))

    def test_every_stateful_command_needs_an_ops_root_for_a_non_default_dir(self):
        default_root = tempfile.mkdtemp()
        commands = [["snapshot"], ["snapshots"], ["verify-snapshot"], ["restore", "--snapshot", "latest", "--file", "x.md"], ["log", "decision", "x"], ["prune-plan"]]
        with mock.patch.object(mc, "DEFAULT_OPS_ROOT", default_root):
            for argv in commands:
                with self.subTest(argv):
                    self.assertEqual(run_cli("--dir", self.mem, *argv), (1, "", "ERROR: pass --ops-root when --dir is not the default memory directory\n"))
        self.assertEqual(os.listdir(default_root), [])


class RestoreTests(Env):
    def setUp(self):
        super().setUp()
        self.first = self.snap()
        self.edit()

    def restore(self, *flags, name="feedback-a.md", snapshot="latest"):
        return self.cli("restore", "--snapshot", snapshot, "--file", name, *flags)

    def live_text(self):
        return th.read(os.path.join(self.mem, "feedback-a.md"))

    def test_dry_run_changes_and_logs_nothing_but_a_refused_apply_exits_one_and_is_logged(self):
        before = self.live_text()
        self.assertEqual(self.restore(), (0, "would restore feedback-a.md from snapshot %s (live file differs)\n" % self.first, ""))
        self.assertEqual(self.live_text(), before)
        self.assertNotIn("restore |", self.log_text())
        code, out, _ = self.restore("--apply")
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("refused: live feedback-a.md differs"), out)
        self.assertIn("\n## [%s] restore | feedback-a.md (exit 1)\n- refused: live feedback-a.md differs" % mc.local_date(), self.log_text())
        self.assertIn("edited by a live session", self.live_text())

    def test_force_archives_the_live_copy_restores_and_logs_both_messages(self):
        code, out, _ = self.restore("--apply", "--force")
        self.assertEqual(code, 0)
        archive = re.search(r"^archived live copy -> (\S+)$", out, flags=re.M).group(1)
        self.assertIn("edited by a live session", th.read(archive))
        self.assertTrue(archive.startswith(os.path.join(self.ops, "archive") + os.sep))
        self.assertTrue(out.endswith("restored feedback-a.md from snapshot %s\n" % self.first))
        self.assertNotIn("edited by a live session", self.live_text())
        self.assertIn("restore | feedback-a.md (exit 0)\n- archived live copy -> ", self.log_text())
        self.assertIn("\n- restored feedback-a.md from snapshot %s\n" % self.first, self.log_text())
        self.assertEqual(self.restore("--apply")[:2], (0, "already identical: feedback-a.md\n"))

    def test_unsafe_names_unknown_files_and_unknown_snapshots_are_refused_with_exit_one(self):
        self.assertEqual(self.restore("--apply", name="../outside.md")[:2], (1, "refused: unsafe file name (../outside.md)\n"))
        self.assertIn("restore | ../outside.md (exit 1)\n- refused: unsafe file name", self.log_text())
        self.assertEqual(self.restore(name="nowhere.md")[:2], (1, "nowhere.md is in neither the snapshot nor the live directory\n"))
        self.assertEqual(self.restore("--apply", snapshot="nosuch"), (1, "", "ERROR: no such snapshot: nosuch\n"))
        self.assertNotIn("nosuch", self.log_text())


class LogAndPruneTests(Env):
    def test_log_prints_the_path_and_appends_entries_in_order(self):
        self.assertEqual(self.cli("log", "decision", "first subject", "--bullet", "why one", "--bullet", "why two"),
                         (0, os.path.join(self.ops, "log.md") + "\n", ""))
        self.cli("log", "recall", "second subject")
        self.assertEqual(self.log_text(), "\n## [%s] decision | first subject\n- why one\n- why two\n\n## [%s] recall | second subject\n"
                         % (mc.local_date(), mc.local_date()))

    def test_every_valid_op_is_accepted_and_unknown_ones_are_usage_errors(self):
        for op in sorted(mc.VALID_OPS):
            with self.subTest(op):
                self.assertEqual(self.cli("log", op, "s")[0], 0)
        self.assertEqual(self.log_text().count("\n## ["), len(mc.VALID_OPS))
        self.assertIn("invalid choice: 'bogus'", run_exit("log", "bogus", "x")[2])

    def make_snapshots(self, count):
        for i in range(count):
            self.edit("edit %d\n" % i)
            sn.take_snapshot(self.mem, self.ops, now=1_700_000_000 + i * 10)
        return sn.list_snapshots(self.ops)

    def test_the_default_keep_is_five_and_the_oldest_are_planned_first(self):
        ids = self.make_snapshots(7)
        code, out, _ = self.cli("prune-plan")
        lines = out.splitlines()
        self.assertEqual(code, 0)
        self.assertEqual(lines[0], "# plan only, nothing is removed; run each line as its own command")
        self.assertEqual(lines[1:], ["rm -r %s" % os.path.join(self.ops, "snapshots", i) for i in ids[:2]])
        self.assertEqual(len(sn.list_snapshots(self.ops)), 7)

    def test_nothing_to_prune_and_the_keep_validation(self):
        self.make_snapshots(2)
        self.assertEqual(self.cli("prune-plan")[1].splitlines()[1:], ["# nothing to prune"])
        self.assertEqual(self.cli("prune-plan", "--keep", "2")[1].splitlines()[1:], ["# nothing to prune"])
        self.assertEqual(len(self.cli("prune-plan", "--keep", "1")[1].splitlines()), 2)
        self.assertEqual(self.cli("prune-plan", "--keep", "0")[0::2], (1, "ERROR: --keep must be at least 1\n"))
        self.assertEqual(run_exit("--dir", self.mem, "prune-plan", "--keep", "x")[0], 64)

    def test_paths_with_spaces_and_shell_characters_are_quoted(self):
        root = os.path.join(tempfile.mkdtemp(), "ops root; echo pwned")
        mem = th.make_dir({"MEMORY.md": INDEX, "feedback-a.md": NOTE_A})
        ops = mc.ops_dir_for(mem, root)
        for i in range(2):
            th.write(os.path.join(mem, "feedback-a.md"), "edit %d\n" % i, "a")
            sn.take_snapshot(mem, ops, now=1_700_000_000 + i * 10)
        os.makedirs(os.path.join(ops, "snapshots", "20200101T000000Z"))
        code, out, _ = run_cli("--dir", mem, "--ops-root", root, "prune-plan", "--keep", "1")
        lines = out.splitlines()[1:]
        self.assertEqual(code, 0)
        self.assertEqual(shlex.split(lines[0]), ["rm", "-r", os.path.join(ops, "snapshots", sn.list_snapshots(ops)[0])])
        self.assertEqual(lines[1:], ["# incomplete snapshot (no valid manifest), review then remove: %s" % os.path.join(ops, "snapshots", "20200101T000000Z")])


class PathRuleTests(Env):
    def test_the_defaults_are_used_when_nothing_is_given_and_an_explicit_default_in_any_spelling_logs(self):
        self.with_defaults()
        self.assertEqual(run_cli("lint")[0], 0)
        self.assertIn("lint | errors=0", self.log_text())
        self.assertEqual(run_cli("snapshot")[0], 0)
        self.assertEqual(len(sn.list_snapshots(self.ops)), 1)
        link = os.path.join(tempfile.mkdtemp(), "alias")
        os.symlink(self.mem, link)
        for spelled in (os.path.join(self.mem, "..", os.path.basename(self.mem)), link):
            self.assertEqual(run_cli("--dir", spelled, "lint")[0], 0)
            self.assertIn("lint | errors=0", self.log_text(mc.ops_dir_for(spelled, self.root)))
        self.assertEqual(run_cli("--ops-root", self.root, "scan")[0], 0)
        self.assertIn("scan | block=0", self.log_text())

    def test_a_different_dir_without_an_ops_root_writes_no_log_and_no_warning(self):
        self.with_defaults()
        other = th.make_dir({"MEMORY.md": INDEX, "feedback-a.md": NOTE_A})
        for argv in (["lint"], ["scan"], ["compare", self.mem, other]):
            with self.subTest(argv):
                self.assertEqual(run_cli("--dir", other, *argv)[0::2], (0, ""))
        self.assertEqual(run_cli("--dir", other, "compare", "latest", self.mem)[0::2], (1, "ERROR: not a snapshot id or a directory: latest\n"))
        self.assertEqual(os.listdir(self.root), [])
        self.assertEqual(self.on(other, "lint")[0], 0)
        self.assertIn("lint | errors=0", self.log_text(mc.ops_dir_for(other, self.root)))

    def test_the_tilde_and_relative_paths_are_resolved_for_dir_and_ops_root(self):
        home = th.make_dir({"proj/memory/MEMORY.md": ""}, prefix="memhome_")
        with mock.patch.dict(os.environ, {"HOME": home}):
            code, out, err = run_cli("--dir", "~/proj/memory", "--ops-root", "~/opsdir", "snapshot")
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(len(sn.list_snapshots(os.path.join(home, "opsdir", "proj"))), 1)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(home)
        self.assertEqual(run_cli("--dir", "proj/memory", "--ops-root", "relops", "log", "decision", "relative")[0::2], (0, ""))
        self.assertIn("decision | relative", th.read(os.path.join(home, "relops", "proj", "log.md")))
        listing = run_cli("--dir", "proj/memory", "--ops-root", "opsdir", "snapshots")[1]
        self.assertEqual(listing, "%s files=1\n" % sn.list_snapshots(os.path.join(home, "opsdir", "proj"))[0])

    def test_empty_values_are_named_in_the_error(self):
        self.assertEqual(run_cli("--dir", "", "lint")[0::2], (1, "ERROR: --dir must not be empty\n"))
        self.assertEqual(run_cli("--dir", self.mem, "--ops-root", "", "snapshot")[0::2], (1, "ERROR: --ops-root must not be empty\n"))
        self.assertEqual(run_cli("lint", "--dir", "")[0::2], (1, "ERROR: --dir must not be empty\n"))


class ErrorAndLogFailureTests(Env):
    def test_failures_become_one_error_line_and_exit_one_never_a_traceback(self):
        cases = {"hygiene": (mc.HygieneError("custom refusal"), "custom refusal"), "os": (OSError(13, "Permission denied"), "[Errno 13] Permission denied"),
                 "value": (ValueError("bad value\x1b[0m"), "bad value?[0m")}
        for name, (error, text) in cases.items():
            for owner, target, command in ((mh.ml, "lint_dir", "lint"), (mh.ms, "scan_tree", "scan")):
                with self.subTest(name, command=command), mock.patch.object(owner, target, side_effect=error):
                    self.assertEqual(self.cli(command), (1, "", "ERROR: %s\n" % text))

    def test_a_log_failure_is_a_warning_and_never_changes_the_exit_code(self):
        leaky = th.make_dir({"MEMORY.md": "", "a.md": th.note("a", "leak " + th.fake_github_token())})
        self.snap()
        self.edit()
        stage = th.make_dir({"b.md": "other\n"})
        restore = ["restore", "--snapshot", "latest", "--file", "x.md", "--apply"]
        for directory, argv, expected, shown in ((self.mem, ["lint"], 0, "INDEX bytes="), (leaky, ["scan"], 2, "SCAN block=1"),
                                                 (self.mem, ["compare", "latest", stage], 1, "COMPARE baseline_files=2"),
                                                 (self.mem, ["snapshot"], 0, "SNAPSHOT ok files=2"), (self.mem, restore, 1, "neither")):
            with self.subTest(argv[0]), mock.patch.object(mc, "append_log", side_effect=OSError("disk full")):
                code, out, err = self.on(directory, *argv)
            self.assertEqual((code, err), (expected, "WARN could not write the log: disk full\n"))
            self.assertIn(shown, out)

    def test_an_ops_root_inside_the_memory_dir_warns_instead_of_failing_a_lint(self):
        inside = os.path.join(self.mem, "ops")
        code, out, err = run_cli("--dir", self.mem, "--ops-root", inside, "lint")
        self.assertEqual(code, 0)
        self.assertIn("LINT errors=0", out)
        self.assertEqual(err, "WARN could not write the log: the ops dir must not live inside the memory dir\n")
        self.assertFalse(os.path.exists(inside))

    def test_no_log_is_honoured_by_lint_scan_and_compare_but_the_default_logs(self):
        self.snap()
        before = self.log_text()
        stage = th.make_dir({"b.md": LONG + "\n"})
        for argv in (["lint"], ["scan"], ["compare", "latest", stage]):
            with self.subTest(argv):
                self.cli(*argv, "--no-log")
                self.assertEqual(self.log_text(), before)
                self.cli(*argv)
                self.assertNotEqual(self.log_text(), before)
                before = self.log_text()
        self.assertEqual(re.findall(r"\] (\w+) \|", self.log_text()), ["snapshot", "lint", "scan", "compare"])


class SubprocessTests(Env):
    def test_exit_codes_propagate_from_the_script_run_in_isolated_mode(self):
        leaky = th.make_dir({"MEMORY.md": "", "a.md": th.note("a", "leak " + th.fake_github_token())})
        base = ["--ops-root", self.root]
        cases = [(["--dir", self.mem, *base, "lint", "--no-log"], 0), (["--dir", leaky, *base, "scan", "--no-log"], 2),
                 (["--dir", "/nonexistent/memdir", *base, "lint"], 1), (["bogus"], 64), (["--dir", self.mem, *base, "verify-snapshot"], 1),
                 (["--dir", self.mem, *base, "snapshot"], 0), (["--dir", self.mem, *base, "verify-snapshot"], 0)]
        for argv, expected in cases:
            with self.subTest(argv):
                proc = run_script(*argv)
                self.assertEqual(proc.returncode, expected, proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)
        proc = run_script("--dir", leaky, *base, "scan", "--no-log")
        self.assertIn("SCAN block=1 warn=0 skipped=0", proc.stdout)
        self.assertNotIn(th.fake_github_token(), proc.stdout + proc.stderr)
        self.assertTrue(run_script("bogus").stderr.startswith("usage: memory_hygiene.py"))


if __name__ == "__main__":
    unittest.main()
