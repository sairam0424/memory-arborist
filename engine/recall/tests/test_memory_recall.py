"""Tests for the outside recall judge (memory_recall_run.py + memory_recall.py). No test spends a token: a fake runner stands in for claude."""

import ast
import contextlib
import glob
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "tests"))  # memtest_helpers when this file lives in scripts/recall/tests/
import memtest_helpers as th  # noqa: E402

sys.path.insert(0, th.SCRIPTS)  # python3 -I does not put the script dir on sys.path
sys.path.insert(0, os.path.dirname(HERE))  # scripts/recall/ itself
import memory_common as mc  # noqa: E402
import memory_recall as mrc  # noqa: E402
import memory_recall_run as mrr  # noqa: E402
import memory_snapshot as sn  # noqa: E402

TOOLS = ["Glob", "Grep", "Read"]
T0 = 1_700_000_000


def fake_output(answer, cost=0.01, tools=None, is_error=False, init=True):
    items = [
        {"type": "assistant", "message": {}},
        {
            "type": "result",
            "subtype": "success",
            "is_error": is_error,
            "result": answer,
            "total_cost_usd": cost,
            "num_turns": 1,
        },
    ]
    if init:
        items.insert(
            0,
            {
                "type": "system",
                "subtype": "init",
                "tools": TOOLS if tools is None else tools,
                "model": "m",
            },
        )
    return json.dumps(items)


def argv_value(argv, flag):
    return argv[argv.index(flag) + 1]


class TempRoot(unittest.TestCase):
    """Every temp directory a test (or the code under test) makes lives under one root that is removed afterwards."""

    @classmethod
    def setUpClass(cls):
        saved, root = tempfile.tempdir, tempfile.mkdtemp(prefix="memtests_")
        tempfile.tempdir = root
        cls.addClassCleanup(shutil.rmtree, root, True)
        cls.addClassCleanup(setattr, tempfile, "tempdir", saved)


class MemoryReadingRunner:
    """Pretends to be a model that answers with the text of every note in the arm's memory directory."""

    def __init__(self, cost=0.01, tools=None):
        self.calls, self.cost, self.tools = [], cost, tools
        self.seen = []  # what the child could list in its memory directory at the moment it was called

    def __call__(self, argv, cwd, env):
        self.calls.append((list(argv), cwd, dict(env)))
        memory = argv_value(argv, "--add-dir")
        self.seen.append(sorted(os.listdir(memory)))
        text = "\n".join(
            th.read(os.path.join(memory, name))
            for name in sorted(os.listdir(memory))
            if name.endswith(".md")
        )
        return 0, fake_output(text, cost=self.cost, tools=self.tools), ""


def question(i, kind="recall", **extra):
    rec = {
        "id": "%s%02d" % (kind[0], i),
        "kind": kind,
        "question": "What is magic phrase %d?" % i,
        "must": ["blue-ocelot-%d\\b" % i],
        "forbid": [],
    }
    rec.update(extra)
    return rec


def questions(n=25):
    return [question(i) for i in range(1, n + 1)]


def write_questions(items, name="questions.jsonl"):
    path = os.path.join(tempfile.mkdtemp(prefix="recq_"), name)
    th.write(path, "".join(json.dumps(q) + "\n" for q in items))
    return path


def memory_with_phrases(n=25, skip=()):
    files = {
        "MEMORY.md": "".join(
            "- [n%d](feedback-n%d.md) phrase %d\n" % (i, i, i) for i in range(1, n + 1)
        )
    }
    for i in range(1, n + 1):
        if i not in skip:
            files["feedback-n%d.md" % i] = th.note(
                "feedback-n%d" % i,
                body="The magic phrase %d is blue-ocelot-%d for sure." % (i, i),
            )
    return th.make_dir(files)


def run_main(argv, runner):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = mrc.main(list(argv), runner=runner)
    return code, out.getvalue(), err.getvalue()


class QuestionFileTests(TempRoot):
    def load(self, items, **kw):
        return mrr.load_questions(write_questions(items), **kw)

    def test_a_valid_file_loads(self):
        self.assertEqual(len(self.load(questions())), 25)

    def test_each_defect_is_refused(self):
        base = questions()
        cases = {
            "duplicate id": base + [dict(base[0])],
            "bad regex": [dict(base[0], must=["("])] + base[1:],
            "empty must": [dict(base[0], must=[])] + base[1:],
            "unknown kind": [dict(base[0], kind="guess")] + base[1:],
            "drift without truth": [question(99, "drift")] + base,
            "missing question": [{k: v for k, v in base[0].items() if k != "question"}]
            + base[1:],
            "too few": base[:10],
            "too many": questions(40),
        }
        for label, items in cases.items():
            with self.subTest(label):
                with self.assertRaises(mc.HygieneError):
                    self.load(items)

    def test_the_count_rule_can_be_waived_for_small_fixtures(self):
        self.assertEqual(len(self.load(questions(3), require_count=False)), 3)

    def test_a_line_that_is_not_json_is_refused_with_its_number(self):
        path = write_questions(questions())
        th.write(path, "{oops\n", "a")
        with self.assertRaises(mc.HygieneError) as ctx:
            mrr.load_questions(path)
        self.assertIn("line 26", str(ctx.exception))


class GradingTests(TempRoot):
    def test_every_must_has_to_match_and_no_forbid_may(self):
        q = {"must": ["alpha", "(?i)beta"], "forbid": ["gamma"]}
        self.assertTrue(mrr.grade(q, "alpha and BETA"))
        self.assertFalse(mrr.grade(q, "alpha only"))
        self.assertFalse(mrr.grade(q, "alpha beta gamma"))

    def test_forbid_hits_are_reported_separately(self):
        q = {"must": ["alpha"], "forbid": ["stale"]}
        self.assertTrue(mrr.forbid_hit(q, "alpha stale"))
        self.assertFalse(mrr.forbid_hit(q, "alpha"))

    def test_grading_spans_lines(self):
        self.assertTrue(
            mrr.grade({"must": ["one.*two"], "forbid": []}, "one\nthen two")
        )


class CommandTests(TempRoot):
    def test_the_command_is_an_argument_list_with_the_question_before_the_variadic_flags(
        self,
    ):
        argv = mrr.build_argv("What is X?", "model-id", "/tmp/p.txt", "/tmp/mem", 0.25)
        self.assertIsInstance(argv, list)
        self.assertEqual(argv[:4], ["claude", "--safe-mode", "-p", "What is X?"])
        self.assertEqual(argv_value(argv, "--tools"), "Read,Grep,Glob")
        self.assertEqual(argv_value(argv, "--permission-mode"), "dontAsk")
        self.assertEqual(argv_value(argv, "--append-system-prompt-file"), "/tmp/p.txt")
        self.assertEqual(argv_value(argv, "--max-budget-usd"), "0.25")
        self.assertIn("--no-session-persistence", argv)
        self.assertEqual(argv[-2:], ["--add-dir", "/tmp/mem"])

    def test_the_child_environment_is_minimal(self):
        fake = {
            "PATH": "/bin",
            "HOME": "/h",
            "AWS_REGION": "r",
            "AWS_SECRET_ACCESS_KEY": "do-not-pass",
            "ANTHROPIC_API_KEY": "nope",
            "GITHUB_TOKEN": "nope",
        }
        env = mrr.child_env(fake)
        self.assertEqual(env, {"PATH": "/bin", "HOME": "/h", "AWS_REGION": "r"})

    def test_the_visible_index_applies_the_native_caps(self):
        many = "".join("- line %d\n" % i for i in range(300))
        self.assertEqual(mrr.visible_index(many).count("\n"), 200)
        wide = "x" * 30000
        self.assertEqual(mc.utf16_len(mrr.visible_index(wide)), 25000)

    def test_the_system_prompt_file_holds_the_visible_index_and_the_directory(self):
        path = os.path.join(tempfile.mkdtemp(prefix="recp_"), "prompt.txt")
        mrr.write_system_prompt(path, "/some/mem", "- [a](a.md) hook\n")
        text = th.read(path)
        self.assertIn("/some/mem", text)
        self.assertIn("- [a](a.md) hook", text)
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")


class ParseTests(TempRoot):
    def test_the_result_element_is_found_among_other_items(self):
        out = mrr.parse_output(fake_output("the answer", cost=0.02))
        self.assertEqual(
            (out.text, out.cost, out.tools, out.is_error),
            ("the answer", 0.02, TOOLS, False),
        )

    def test_unparseable_or_resultless_output_is_an_error(self):
        for bad in (
            "not json",
            "[]",
            json.dumps([{"type": "system", "subtype": "init", "tools": TOOLS}]),
        ):
            with self.subTest(bad):
                with self.assertRaises(mrr.AskError):
                    mrr.parse_output(bad)

    def test_a_missing_init_cannot_prove_the_tool_list(self):
        self.assertIsNone(mrr.parse_output(fake_output("x", init=False)).tools)


class AskTests(TempRoot):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="recask_")
        self.mem = memory_with_phrases(3)
        self.prompt = os.path.join(self.tmp, "p.txt")
        th.write(self.prompt, "prompt")

    def ask(self, runner, q=None, spend=None):
        return mrr.ask(
            q or question(1),
            runner,
            mrr.Settings("m", 0.25),
            self.prompt,
            self.mem,
            spend or mrr.Spend(5.0),
        )

    def test_a_good_answer_is_graded_and_only_a_hash_and_short_excerpt_are_kept(self):
        rec = self.ask(MemoryReadingRunner())
        self.assertTrue(rec["pass"])
        self.assertLessEqual(len(rec["excerpt"]), 200)
        self.assertEqual(len(rec["sha"]), 16)
        self.assertNotIn("answer", rec)

    def test_the_child_runs_in_an_empty_fresh_directory(self):
        runner = MemoryReadingRunner()
        self.ask(runner)
        cwd = runner.calls[0][1]
        self.assertEqual(os.listdir(cwd), [])

    def test_a_secret_shaped_excerpt_is_not_stored(self):
        runner = lambda argv, cwd, env: (
            0,
            fake_output("the key is " + th.fake_github_token()),
            "",
        )  # noqa: E731
        self.assertNotIn("ghp_", json.dumps(self.ask(runner)))

    def test_failures_become_ungraded_errors_not_passes(self):
        cases = {
            "exit": lambda a, c, e: (1, "", "boom"),
            "is_error": lambda a, c, e: (0, fake_output("x", is_error=True), ""),
            "garbage": lambda a, c, e: (0, "not json", ""),
            "timeout": lambda a, c, e: (_ for _ in ()).throw(mrr.AskError("timed out")),
        }
        for label, runner in cases.items():
            with self.subTest(label):
                rec = self.ask(runner)
                self.assertIsNotNone(rec["error"])
                self.assertNotIn("pass", rec)

    def test_a_tool_list_other_than_read_grep_glob_aborts_the_run(self):
        runner = MemoryReadingRunner(tools=["Bash", "Read"])
        with self.assertRaises(mrr.ToolsMismatch):
            self.ask(runner)

    def test_a_missing_tool_list_aborts_the_run(self):
        runner = lambda argv, cwd, env: (0, fake_output("x", init=False), "")  # noqa: E731
        with self.assertRaises(mrr.ToolsMismatch):
            self.ask(runner)

    def test_the_spend_ceiling_stops_before_the_next_call(self):
        spend = mrr.Spend(0.015)
        runner = MemoryReadingRunner(cost=0.01)
        self.ask(runner, spend=spend)
        self.ask(runner, spend=spend)
        with self.assertRaises(mrr.CostCeiling):
            self.ask(runner, spend=spend)
        self.assertEqual(len(runner.calls), 2)


class ArmTests(TempRoot):
    def setUp(self):
        self.mem = memory_with_phrases()
        self.root = tempfile.mkdtemp(prefix="recops_")
        self.ops = mc.ops_dir_for(self.mem, self.root)
        self.snap = sn.take_snapshot(self.mem, self.ops, now=T0)["id"]
        self.qfile = write_questions(questions())

    def cli(self, *argv, runner=None):
        base = [
            "--questions",
            self.qfile,
            "--dir",
            self.mem,
            "--ops-root",
            self.root,
            "--no-log",
        ]
        return run_main(base + list(argv), runner or MemoryReadingRunner())

    def leftovers(self):
        return glob.glob(os.path.join(tempfile.gettempdir(), "recall_run_*"))

    def test_the_judge_leaves_no_temporary_copy_of_the_index_behind(self):
        code, out, _ = self.cli("--arm", "live")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.leftovers(), [])

    def test_an_aborted_run_leaves_no_temporary_copy_of_the_index_behind(self):
        code, out, _ = self.cli("--arm", "live", runner=MemoryReadingRunner(tools=["Bash"]))
        self.assertEqual(code, 1, out)
        self.assertIn("ABORTED", out)
        self.assertEqual(self.leftovers(), [])

    def test_the_live_arm_reads_the_frozen_snapshot_not_the_live_directory(self):
        runner = MemoryReadingRunner()
        os.remove(
            os.path.join(self.mem, "feedback-n3.md")
        )  # the live directory changes after the snapshot
        code, out, _ = self.cli("--arm", "live", runner=runner)
        self.assertEqual(code, 0, out)
        self.assertIn("pass=25/25", out)
        self.assertIn(
            os.path.join(self.ops, "snapshots", self.snap, "memory"),
            {argv_value(c[0], "--add-dir") for c in runner.calls},
        )

    def test_a_damaged_snapshot_is_refused(self):
        damaged = os.path.join(
            self.ops, "snapshots", self.snap, "memory", "feedback-n1.md"
        )
        os.chmod(damaged, 0o600)
        th.write(damaged, "tampered\n")
        code, _out, err = self.cli("--arm", "live")
        self.assertEqual(code, 1)
        self.assertIn("snapshot", err)

    def test_the_staged_arm_needs_a_directory_with_an_index(self):
        self.assertEqual(self.cli("--arm", "staged")[0], 1)
        self.assertEqual(
            self.cli("--arm", "staged", "--staged-dir", tempfile.mkdtemp())[0], 1
        )

    def test_the_empty_arm_sees_no_notes_and_fails_the_floor_of_passes(self):
        runner = MemoryReadingRunner()
        code, out, _ = self.cli("--arm", "empty", runner=runner)
        self.assertEqual(code, 0, out)
        self.assertIn("EMPTY pass=0", out)
        self.assertEqual([n for n in runner.seen[0] if n != "MEMORY.md"], [])

    def test_an_empty_arm_that_still_passes_many_questions_means_the_set_measures_priors(
        self,
    ):
        parrot = lambda argv, cwd, env: (
            0,
            fake_output(" ".join("blue-ocelot-%d" % i for i in range(1, 30))),
            "",
        )  # noqa: E731
        code, out, _ = self.cli("--arm", "empty", runner=parrot)
        self.assertEqual(code, 1)
        self.assertIn("measures model priors", out)

    def test_the_floor_of_graded_questions_gives_exit_3(self):
        code, out, _ = self.cli("--arm", "live", "--min", "99")
        self.assertEqual(code, 3)
        self.assertIn("graded=25", out)

    def test_repeats_report_the_noise_floor(self):
        calls = {"n": 0}

        def flaky(argv, cwd, env):
            calls["n"] += 1
            second_run = calls["n"] > 25
            answer = (
                "nothing"
                if second_run and "magic phrase 1?" in argv[3]
                else "blue-ocelot-%s" % argv[3].split()[-1].rstrip("?")
            )
            return 0, fake_output(answer), ""

        code, out, _ = self.cli(
            "--arm", "live", "--repeat", "2", "--workers", "1", runner=flaky
        )
        self.assertIn("NOISE flips=1", out)
        self.assertEqual(code, 0, out)

    def test_the_cost_ceiling_aborts_with_a_message_and_a_nonzero_exit(self):
        code, out, err = self.cli(
            "--arm",
            "live",
            "--total-max-usd",
            "0.03",
            "--workers",
            "1",
            runner=MemoryReadingRunner(cost=0.02),
        )
        self.assertEqual(code, 1)
        self.assertIn("cost ceiling", out + err)

    def test_failed_and_errored_question_ids_are_listed(self):
        def runner(argv, cwd, env):
            if "phrase 4?" in argv[3]:
                return 1, "", "boom"
            return MemoryReadingRunner()(argv, cwd, env) if "phrase 3?" not in argv[3] else (0, fake_output("nothing"), "")

        code, out, _ = self.cli("--arm", "live", runner=runner)
        self.assertRegex(out, r"FAILED \S*r03\S* ERRORED \S*r04\S*")

    def test_the_summary_lines_have_the_agreed_shape(self):
        code, out, _ = self.cli("--arm", "live", "--min", "20")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"RECALL arm=live run=1 graded=25 pass=25/25")
        self.assertIn("TOOLS ok=Glob,Grep,Read", out)
        self.assertRegex(out, r"COST total_usd=\d+\.\d+ limit=5\.0")


class BaselineAndRegressionTests(TempRoot):
    def setUp(self):
        self.mem = memory_with_phrases()
        self.root = tempfile.mkdtemp(prefix="recops_")
        self.ops = mc.ops_dir_for(self.mem, self.root)
        sn.take_snapshot(self.mem, self.ops, now=T0)
        self.qfile = write_questions(questions())
        self.baseline_path = os.path.join(
            tempfile.mkdtemp(prefix="recbase_"), "baseline.json"
        )

    def cli(self, *argv, runner=None):
        base = [
            "--questions",
            self.qfile,
            "--dir",
            self.mem,
            "--ops-root",
            self.root,
            "--no-log",
        ]
        return run_main(base + list(argv), runner or MemoryReadingRunner())

    def write_baseline(self):
        code, out, _ = self.cli("--arm", "baseline", "--save", self.baseline_path)
        self.assertEqual(code, 0, out)
        return json.loads(th.read(self.baseline_path))

    def test_the_baseline_file_carries_the_numbers_the_acceptance_check_reads(self):
        data = self.write_baseline()
        self.assertGreaterEqual(data["graded"], 20)
        self.assertLessEqual(data["empty_pass"], 3)
        self.assertLessEqual(data["noise_flips"], 2)
        self.assertEqual(set(data["live_runs"][0]), {q["id"] for q in questions()})
        self.assertEqual(oct(os.stat(self.baseline_path).st_mode & 0o777), "0o600")

    def test_the_baseline_is_refused_when_the_empty_arm_passes_too_much(self):
        parrot = lambda argv, cwd, env: (
            0,
            fake_output(" ".join("blue-ocelot-%d" % i for i in range(1, 30))),
            "",
        )  # noqa: E731
        code, out, _ = self.cli(
            "--arm", "baseline", "--save", self.baseline_path, runner=parrot
        )
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(self.baseline_path))

    def test_a_planted_deletion_is_detected_as_a_regression(self):
        self.write_baseline()
        staged = memory_with_phrases(skip=(7,))
        code, out, _ = self.cli(
            "--arm", "staged", "--staged-dir", staged, "--baseline", self.baseline_path
        )
        self.assertEqual(code, 1)
        self.assertIn("REGRESSIONS n=1 ids=r07", out)

    def test_an_identical_staged_copy_has_no_regressions(self):
        self.write_baseline()
        staged = memory_with_phrases()
        code, out, _ = self.cli(
            "--arm", "staged", "--staged-dir", staged, "--baseline", self.baseline_path
        )
        self.assertEqual(code, 0, out)
        self.assertIn("REGRESSIONS n=0", out)

    def test_one_lucky_failure_is_not_a_regression(self):
        base = {"live_runs": [{"r01": True, "r02": True}, {"r01": True, "r02": True}]}
        staged = [{"r01": False, "r02": True}, {"r01": True, "r02": True}]
        self.assertEqual(mrc.regressions(base, staged), [])
        self.assertEqual(
            mrc.regressions(base, [{"r01": False, "r02": True}] * 2), ["r01"]
        )

    def test_noise_is_the_number_of_questions_that_differ_between_two_runs(self):
        self.assertEqual(
            mrc.flips(
                {"a": True, "b": False, "c": True}, {"a": True, "b": True, "c": False}
            ),
            2,
        )

    def test_resurrected_stale_facts_are_counted_and_fail_the_run(self):
        items = questions()
        items[0] = dict(items[0], kind="supersede", id="s01", forbid=["STALE"])
        self.qfile = write_questions(items)
        self.write_baseline()
        stale = lambda argv, cwd, env: (
            0,
            fake_output(
                "STALE " + " ".join("blue-ocelot-%d" % i for i in range(1, 30))
            ),
            "",
        )  # noqa: E731
        code, out, _ = self.cli(
            "--arm",
            "staged",
            "--staged-dir",
            memory_with_phrases(),
            "--baseline",
            self.baseline_path,
            runner=stale,
        )
        self.assertEqual(code, 1)
        self.assertIn("RESURRECTED n=1", out)

    def test_a_recall_run_is_logged_unless_told_not_to(self):
        base = ["--questions", self.qfile, "--dir", self.mem, "--ops-root", self.root]
        run_main(
            base + ["--arm", "baseline", "--save", self.baseline_path],
            MemoryReadingRunner(),
        )
        self.assertIn("recall |", th.read(os.path.join(self.ops, "log.md")))


class SelftestTests(TempRoot):
    def test_the_zero_token_selftest_proves_the_grader_can_fail(self):
        runner = MemoryReadingRunner()
        code, out, _ = run_main(
            ["--selftest", "--questions", write_questions(questions())], runner
        )
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"selftest good=\d+/\d+ bad=0/\d+ flip-control=OK")
        self.assertEqual(runner.calls, [])

    def test_a_grader_that_passes_everything_fails_the_selftest(self):
        original = mrr.grade
        mrr.grade = lambda q, a: True
        try:
            code, out, _ = run_main(["--selftest"], MemoryReadingRunner())
        finally:
            mrr.grade = original
        self.assertEqual(code, 1)
        self.assertIn("bad=9/9", out)

    def test_a_grader_that_fails_everything_fails_the_selftest(self):
        original = mrr.grade
        mrr.grade = lambda q, a: False
        try:
            code, out, _ = run_main(["--selftest"], MemoryReadingRunner())
        finally:
            mrr.grade = original
        self.assertEqual(code, 1)
        self.assertIn("good=0/3", out)

    def test_the_selftest_reports_a_broken_question_file(self):
        code, out, _ = run_main(
            ["--selftest", "--questions", write_questions(questions(3))],
            MemoryReadingRunner(),
        )
        self.assertEqual(code, 1)


class SafetyTests(TempRoot):
    def test_no_shell_no_deletion_no_network_and_no_write_outside_the_ops_files(self):
        banned = {
            "system",
            "popen",
            "remove",
            "unlink",
            "rmdir",
            "rmtree",
            "replace",
            "rename",
            "removedirs",
        }
        # Changed on the owner's decision (2026-11-18): this test used to ban every replace. The judge's saved baseline is now written
        # next to its place and moved over it, so that a failed write cannot cut an older baseline short: exactly one os.replace, in
        # _save_baseline, none in the run module. It is a static tripwire, not a sandbox (a subprocess call can still do anything); it
        # looks at every REFERENCE to a banned name (so an alias or map(os.replace, ...) is seen), at imports, at getattr on os and at
        # opens that write.
        banned_refs = banned | {"move", "copy", "copy2", "copyfile", "link", "symlink", "truncate", "execv", "execve", "execl", "execvp", "chmod", "chown", "mkfifo"}
        allowed_os_replace = {mrr: 0, mrc: 1}
        for module in (mrr, mrc):
            with open(module.__file__, encoding="utf-8") as fh:
                source = fh.read()
            tree = ast.parse(source)
            nodes = list(ast.walk(tree))
            refs = [n.attr if isinstance(n, ast.Attribute) else n.id for n in nodes if isinstance(n, (ast.Attribute, ast.Name))]
            self.assertEqual(refs.count("replace"), allowed_os_replace[module], module.__name__)  # no other replace of any kind, called or not

            def is_os_replace(n):
                return isinstance(n, ast.Attribute) and n.attr == "replace" and isinstance(n.value, ast.Name) and n.value.id == "os"

            owners = {fn.name for fn in nodes if isinstance(fn, ast.FunctionDef) for n in ast.walk(fn) if is_os_replace(n)}
            self.assertEqual(owners, {"_save_baseline"} if allowed_os_replace[module] else set(), module.__name__)
            self.assertEqual(set(refs) & (banned_refs - {"replace"}), set(), module.__name__)
            from_imported = {a.name for n in nodes if isinstance(n, ast.ImportFrom) for a in n.names}
            self.assertEqual(from_imported & banned_refs, set(), module.__name__)
            self.assertEqual(
                [n for n in nodes if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("__import__", "eval", "exec")], [], module.__name__
            )
            self.assertEqual(
                [n for n in nodes if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "getattr" and n.args
                 and isinstance(n.args[0], ast.Name) and n.args[0].id in ("os", "shutil")],
                [], module.__name__,
            )
            for call in (n for n in nodes if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "open"):
                mode = call.args[1] if len(call.args) > 1 else next((k.value for k in call.keywords if k.arg == "mode"), None)
                self.assertTrue(
                    mode is None or (isinstance(mode, ast.Constant) and mode.value in ("r", "rb", "rt")), "%s opens a file for writing" % module.__name__
                )
            self.assertNotIn("shell=True", source)
            imports = {
                a.name.split(".")[0]
                for n in ast.walk(tree)
                if isinstance(n, ast.Import)
                for a in n.names
            }
            imports |= {
                n.module.split(".")[0]
                for n in ast.walk(tree)
                if isinstance(n, ast.ImportFrom) and n.module
            }
            self.assertEqual(
                imports & {"socket", "urllib", "http", "requests", "ssl", "shutil", "pathlib"},
                set(),
                module.__name__,
            )


if __name__ == "__main__":
    unittest.main()
