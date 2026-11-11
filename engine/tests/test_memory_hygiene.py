"""Tests for the memory_hygiene CLI, plus structural tripwires over all memory_*.py scripts."""

import ast
import contextlib
import glob
import io
import os
import re
import stat
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

LONG = "this baseline line is long enough to count as content"


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = mh.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.mem = th.make_dir(
            {
                "MEMORY.md": "- [A](feedback-a.md) hook\n",
                "feedback-a.md": th.note("feedback-a"),
            }
        )
        self.root = tempfile.mkdtemp(prefix="memops_")
        self.ops = mc.ops_dir_for(self.mem, self.root)

    def cli(self, *argv):
        return run_cli("--dir", self.mem, "--ops-root", self.root, *argv)

    def log_text(self):
        path = os.path.join(self.ops, "log.md")
        return th.read(path) if os.path.exists(path) else ""


class ParserTests(Base):
    def test_help_runs_under_isolated_mode(self):
        proc = subprocess.run(
            [
                sys.executable,
                "-I",
                os.path.join(th.SCRIPTS, "memory_hygiene.py"),
                "--help",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_usage_errors_exit_64_so_they_never_collide_with_scan_block_hits(self):
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as caught,
        ):
            mh.main(["bogus-command"])
        self.assertEqual(caught.exception.code, 64)

    def test_dir_and_ops_root_work_after_the_subcommand_too(self):
        code, out, _ = run_cli(
            "lint", "--dir", self.mem, "--ops-root", self.root, "--no-log"
        )
        self.assertIn("INDEX bytes=", out)
        self.assertEqual(code, 0)

    def test_a_non_default_dir_without_an_ops_root_never_touches_the_default_ops(self):
        default_root = tempfile.mkdtemp()
        with mock.patch.object(mc, "DEFAULT_OPS_ROOT", default_root):
            code, out, _ = run_cli("--dir", self.mem, "lint")
            self.assertEqual((code, os.listdir(default_root)), (0, []))
            code, _out, err = run_cli("--dir", self.mem, "snapshot")
        self.assertEqual(code, 1)
        self.assertIn("--ops-root", err)
        self.assertEqual(os.listdir(default_root), [])


class SnapshotCliTests(Base):
    def test_snapshot_noop_and_log_entry(self):
        code, out, _ = self.cli("snapshot")
        self.assertEqual(code, 0)
        self.assertRegex(
            out,
            r"SNAPSHOT ok files=2 manifest_sha256_ok=2 id=\S+ scan_block=0 scan_warn=0 skipped_symlinks=0",
        )
        self.assertRegex(self.cli("snapshot")[1], r"NOOP identical to \S+")
        self.assertEqual(
            len(
                re.findall(r"^## \[[\d-]+\] snapshot \| ", self.log_text(), flags=re.M)
            ),
            1,
        )

    def test_symlinks_are_reported_in_the_snapshot_line(self):
        os.symlink(
            os.path.join(self.mem, "feedback-a.md"), os.path.join(self.mem, "alias.md")
        )
        self.assertIn("skipped_symlinks=1", self.cli("snapshot")[1])

    def test_failures_are_one_line_errors_not_tracebacks(self):
        os.chmod(os.path.join(self.mem, "feedback-a.md"), 0)
        try:
            code, _out, err = self.cli("snapshot")
        finally:
            os.chmod(
                os.path.join(self.mem, "feedback-a.md"), stat.S_IRUSR | stat.S_IWUSR
            )
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("ERROR:"))
        self.assertNotIn("Traceback", err)

    def test_missing_memory_dir_is_an_error(self):
        self.assertEqual(
            run_cli(
                "--dir", "/nonexistent/memdir", "--ops-root", self.root, "snapshot"
            )[0],
            1,
        )

    def test_snapshots_lists_good_and_broken(self):
        self.cli("snapshot")
        os.makedirs(os.path.join(self.ops, "snapshots", "20200101T000000Z"))
        out = self.cli("snapshots")[1]
        self.assertRegex(out, r"files=2")
        self.assertIn("incomplete", out)

    def test_verify_command_reports_ok_fail_and_no_snapshots(self):
        self.assertEqual(self.cli("verify-snapshot")[0], 1)
        self.cli("snapshot")
        self.assertRegex(self.cli("verify-snapshot")[1], r"VERIFY ok files=2")
        snap_id = sn.list_snapshots(self.ops)[0]
        copy = os.path.join(self.ops, "snapshots", snap_id, "memory", "feedback-a.md")
        os.chmod(copy, 0o600)
        with open(copy, "a") as fh:
            fh.write("x")
        code, out, _ = self.cli("verify-snapshot")
        self.assertEqual(code, 1)
        self.assertIn("VERIFY FAIL", out)

    def test_corrupt_latest_manifest_does_not_break_lint(self):
        self.cli("snapshot")
        snap_id = sn.list_snapshots(self.ops)[0]
        path = os.path.join(self.ops, "snapshots", snap_id, "manifest.json")
        os.chmod(path, 0o600)
        open(path, "w").close()
        code, out, _ = self.cli("lint", "--no-log")
        self.assertIn("INDEX bytes=", out)


class LintCliTests(Base):
    def test_exit_code_follows_errors_and_strict(self):
        bad = th.make_dir(
            {
                "MEMORY.md": "- [A](a.md) h\n",
                "a.md": th.note("a", body="[[missing-target]]"),
            }
        )
        code, out, _ = run_cli(
            "--dir", bad, "--ops-root", self.root, "lint", "--no-log"
        )
        self.assertEqual(code, 1)
        self.assertIn("dangling-link", out)
        self.assertEqual(self.cli("lint", "--no-log")[0], 0)
        padded = th.make_dir({"MEMORY.md": "x" * 160 + "\n", "a.md": th.note("a")})
        self.assertEqual(
            run_cli(
                "--dir", padded, "--ops-root", self.root, "lint", "--strict", "--no-log"
            )[0],
            1,
        )

    def test_lint_writes_a_log_entry_unless_told_not_to(self):
        self.cli("lint")
        self.assertRegex(self.log_text(), r"## \[[\d-]+\] lint \| errors=0 warn=0")

    def test_missing_directory_is_an_error(self):
        self.assertEqual(
            run_cli("--dir", "/nonexistent/x", "--ops-root", self.root, "lint")[0], 1
        )


class ScanCliTests(Base):
    def test_clean_directory_exits_zero(self):
        code, out, _ = self.cli("scan", "--no-log")
        self.assertEqual(code, 0)
        self.assertIn("SCAN block=0 warn=0 skipped=0", out)

    def test_block_hit_exits_two_without_echoing_the_secret(self):
        bad = th.make_dir(
            {"MEMORY.md": "", "a.md": th.note("a", "leak " + th.fake_github_token())}
        )
        code, out, err = run_cli(
            "--dir", bad, "--ops-root", self.root, "scan", "--no-log"
        )
        self.assertEqual(code, 2)
        self.assertIn("SCAN block=1", out)
        self.assertNotIn(th.fake_github_token(), out + err)

    def test_a_missing_directory_is_an_error_not_a_clean_result(self):
        code, out, err = run_cli(
            "--dir", "/nonexistent/scan", "--ops-root", self.root, "scan"
        )
        self.assertEqual(code, 1)
        self.assertNotIn("SCAN block=0", out)
        self.assertTrue(err.startswith("ERROR:"))
        self.assertEqual(self.log_text(), "")

    def test_skipped_files_are_listed_and_fail_the_run(self):
        os.symlink(
            os.path.join(self.mem, "feedback-a.md"), os.path.join(self.mem, "alias.md")
        )
        code, out, _ = self.cli("scan", "--no-log")
        self.assertEqual(code, 1)
        self.assertIn("SKIPPED alias.md", out)
        self.assertIn("skipped=1", out)


class CompareCliTests(Base):
    def twin(self):
        return th.make_dir({"MEMORY.md": "- [A](feedback-a.md) hook\n", "feedback-a.md": th.note("feedback-a")})

    def test_compare_reports_counts_and_uses_snapshot_ids(self):
        th.write(os.path.join(self.mem, "feedback-a.md"), LONG + "\n", "a")
        self.cli("snapshot")
        staging = th.make_dir(
            {
                "feedback-a.md": th.note("feedback-a"),
                "MEMORY.md": "- [A](feedback-a.md) hook\n",
            }
        )
        code, out, _ = self.cli("compare", "latest", staging, "--no-log")
        self.assertEqual(code, 1)
        self.assertRegex(out, r"LOST feedback-a\.md:\d+")
        self.assertRegex(
            out, r"COMPARE baseline_files=2 lines_checked=\d+ lost_lines=1"
        )

    def test_identical_trees_pass_and_an_empty_baseline_cannot_be_verified(self):
        self.assertEqual(self.cli("compare", self.twin(), self.mem, "--no-log")[0], 0)
        empty = th.make_dir({"x.txt": "no markdown"})
        code, out, _ = self.cli("compare", empty, self.mem, "--no-log")
        self.assertEqual(code, 1)
        self.assertIn("cannot verify", out)

    def test_files_scope_and_unknown_names(self):
        self.assertEqual(
            self.cli(
                "compare", self.twin(), self.mem, "--files", "feedback-a.md", "--no-log"
            )[0],
            0,
        )
        self.assertEqual(
            self.cli("compare", self.twin(), self.mem, "--files", "nope.md", "--no-log")[
                0
            ],
            1,
        )


class RestoreCliTests(Base):
    def setUp(self):
        super().setUp()
        self.cli("snapshot")
        with open(os.path.join(self.mem, "feedback-a.md"), "a") as fh:
            fh.write("edited by a live session\n")

    def restore(self, *flags):
        return self.cli(
            "restore", "--snapshot", "latest", "--file", "feedback-a.md", *flags
        )

    def test_dry_run_then_guarded_apply_then_forced_apply(self):
        self.assertEqual(self.restore()[0], 0)
        self.assertIn(
            "edited by a live session", th.read(os.path.join(self.mem, "feedback-a.md"))
        )
        self.assertEqual(self.restore("--apply")[0], 1)
        self.assertIn("refused", self.log_text())
        self.assertEqual(self.restore("--apply", "--force")[0], 0)
        self.assertNotIn(
            "edited by a live session", th.read(os.path.join(self.mem, "feedback-a.md"))
        )
        self.assertIn("restore |", self.log_text())

    def test_unknown_snapshot_is_an_error(self):
        self.assertEqual(
            self.cli(
                "restore", "--snapshot", "19990101T000000Z", "--file", "feedback-a.md"
            )[0],
            1,
        )


class LogAndPruneCliTests(Base):
    def test_log_command_appends_and_rejects_unknown_ops(self):
        self.assertEqual(
            self.cli(
                "log", "decision", "kept the feature off", "--bullet", "Why: test"
            )[0],
            0,
        )
        self.assertIn("decision | kept the feature off", self.log_text())
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as caught,
        ):
            self.cli("log", "bogus", "x")
        self.assertEqual(caught.exception.code, 64)

    def test_prune_plan_prints_and_validates_keep(self):
        for i in range(3):
            with open(os.path.join(self.mem, "feedback-a.md"), "a") as fh:
                fh.write("edit %d\n" % i)
            sn.take_snapshot(self.mem, self.ops, now=1_700_000_000 + i * 10)
        code, out, _ = self.cli("prune-plan", "--keep", "1")
        self.assertEqual(code, 0)
        self.assertEqual(
            len([line for line in out.splitlines() if line.startswith("rm -r ")]), 2
        )
        self.assertEqual(self.cli("prune-plan", "--keep", "0")[0], 1)
        self.assertEqual(len(sn.list_snapshots(self.ops)), 3)


class TripwireTests(unittest.TestCase):
    FORBIDDEN_CALLS = {
        "unlink",
        "rmdir",
        "rmtree",
        "remove",
        "removedirs",
        "rename",
        "renames",
        "move",
        "truncate",
        "system",
        "popen",
    }
    REPLACE_ALLOWED_IN = {"_write_json_private", "_archive_file", "_swap_in"}

    def parsed_scripts(self):
        return {
            os.path.basename(p): ast.parse(th.read(p))
            for p in sorted(glob.glob(os.path.join(th.SCRIPTS, "memory_*.py")))
        }

    @staticmethod
    def call_name(node):
        fn = node.func
        return fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")

    def test_no_script_calls_delete_rename_or_shell_functions(self):
        offenders = [
            "%s:%d %s" % (name, n.lineno, self.call_name(n))
            for name, tree in self.parsed_scripts().items()
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and self.call_name(n) in self.FORBIDDEN_CALLS
        ]
        self.assertEqual(offenders, [])

    def test_os_replace_is_only_called_from_the_audited_functions(self):
        found = set()
        for name, tree in self.parsed_scripts().items():
            for func in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
                for n in ast.walk(func):
                    if (
                        isinstance(n, ast.Call)
                        and self.call_name(n) == "replace"
                        and isinstance(n.func, ast.Attribute)
                        and getattr(n.func.value, "id", "") == "os"
                    ):
                        found.add((name, func.name))
        self.assertTrue(found, "expected to find the audited os.replace calls")
        self.assertLessEqual({fn for _m, fn in found}, self.REPLACE_ALLOWED_IN, found)

    def test_scripts_never_open_files_for_writing_with_the_builtin_or_import_subprocess(
        self,
    ):
        bad = []
        for name, tree in self.parsed_scripts().items():
            for n in ast.walk(tree):
                if (
                    isinstance(n, ast.Call)
                    and getattr(n.func, "id", "") == "open"
                    and len(n.args) > 1
                ):
                    mode = getattr(n.args[1], "value", "")
                    bad += (
                        ["%s:%d open(%r)" % (name, n.lineno, mode)]
                        if isinstance(mode, str) and set(mode) & set("wax+")
                        else []
                    )
                if isinstance(n, (ast.Import, ast.ImportFrom)) and any(
                    a.name.split(".")[0] == "subprocess" for a in n.names
                ):
                    bad.append("%s:%d imports subprocess" % (name, n.lineno))
        self.assertEqual(bad, [])

    def test_every_script_stays_under_500_lines_and_functions_under_60(self):
        for name, tree in self.parsed_scripts().items():
            with self.subTest(name):
                self.assertLess(
                    len(th.read(os.path.join(th.SCRIPTS, name)).splitlines()), 500
                )
                long_funcs = [
                    (n.name, n.end_lineno - n.lineno + 1)
                    for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef)
                    and n.end_lineno - n.lineno + 1 > 60
                ]
                self.assertEqual(long_funcs, [])


if __name__ == "__main__":
    unittest.main()
