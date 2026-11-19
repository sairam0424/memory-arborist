"""Slow-copy check for the timing tests. Each test below guards a function against a slow or hanging path with a time bound. For each of
them this script runs the test twice in a fresh process: once as it is (it must pass), and once with the function it times replaced by one
that calls the original and then spins for (bound + 0.3) seconds of CPU (it must FAIL). A timing test that survives its slow copy guards
nothing.

usage: python3 -I harness_timing.py [N ...]      (no arguments = every test; the spins add up to a few minutes, four run at a time)

The guarded bounds are CPU time (time.process_time, or the child's CPU through RUSAGE_CHILDREN for a test that runs a process), not wall-clock
time, so that a loaded machine, a stalled clock or a sleeping laptop cannot make a good test fail; a spin burns CPU, so it is still caught.
"""

import concurrent.futures
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(os.path.dirname(os.path.dirname(HERE)), "engine")

# (test file under engine/, "Class.method", name of the module alias inside the test file, attribute to slow down, the bound of the test in seconds)
TESTS = [
    (
        "tests/test_memory_round2.py",
        "LintTruthTests.test_frontmatter_comment_stripping_is_linear",
        "ml",
        "lint_dir",
        3.0,
    ),
    (
        "tests/test_memory_confirm_gate_round3.py",
        "RoundThreeTests.test_the_name_patterns_stay_linear_on_a_long_run_of_name_starts",
        "gate",
        "decision",
        3.0,
    ),
    (
        "tests/test_memory_confirm_gate_round3.py",
        "RoundThreeTests.test_the_patterns_stay_linear_on_text_built_to_make_them_search",
        "gate",
        "needs_confirmation",
        10.0,
    ),
    (
        "tests/test_memory_evidence_round4.py",
        "BoundTests.test_characters_that_explode_under_normalisation_stay_cheap",
        "mr",
        "sanitize",
        6.0,
    ),
    (
        "tests/test_memory_evidence_round4.py",
        "BoundTests.test_a_giant_user_block_is_never_scanned",
        "md",
        "_human_piece",
        1.0,
    ),
    (
        "tests/test_memory_dream_ground_round2.py",
        "ShapeAndFormatTests.test_a_huge_new_text_is_rejected_before_it_is_scanned",
        "dg",
        "ground",
        3.0,
    ),
    (
        "tests/test_memory_evidence_round2.py",
        "SessionRobustnessTests.test_bash_input_openers_without_a_closer_do_not_hang",
        "md",
        "_record_text",
        1.0,
    ),
    (
        "tests/test_memory_evidence_round3.py",
        "RegexCostTests.test_flag_lookalike_runs_do_not_cost_quadratic_time",
        "mr",
        "sanitize",
        3.0,
    ),
    (
        "tests/test_memory_evidence_round3.py",
        "VerifyCoverageTests.test_a_huge_file_with_a_digest_name_is_not_read_whole",
        "me",
        "verify_dir",
        2.0,
    ),
    (
        "tests/test_memory_review_round2_engine.py",
        "CardAfterManyDigitsTests.test_a_long_run_of_digit_groups_costs_linear_time",
        "mr",
        "mask_pii",
        30.0,
    ),
    (
        "tests/test_memory_review_round2_engine.py",
        "CardLengthsAndOverMaskingTests.test_a_very_long_run_of_one_digit_groups_is_cheap",
        "mr",
        "mask_pii",
        20.0,
    ),
    (
        "dream/tests/test_dream_pipeline_round2.py",
        "ExtractJsonTests.test_pathological_replies_are_cheap",
        "lanes",
        "extract_json",
        2.0,
    ),
    (
        "tests/test_memory_scan.py",
        "SafetyTests.test_adversarial_inputs_finish_quickly",
        "ms",
        "scan_text",
        20.0,
    ),
    (
        "tests/test_memory_scan.py",
        "SafetyTests.test_many_block_hits_scale_linearly",
        "ms",
        "scan_text",
        20.0,
    ),
    (
        "recall/tests/test_memory_recall_wizard.py",
        "CandidateTests.test_the_worst_keys_the_grammar_accepts_stay_fast_on_long_text",
        "re",
        "search",
        10.0,
    ),
    (
        "tests/test_memory_nudge.py",
        "MessageTests.test_a_huge_index_is_read_in_bounded_form_and_reported_as_at_least",
        "nudge",
        "nudge_line",
        2.0,
    ),
    (
        "tests/test_memory_nudge.py",
        "GitRootTests.test_a_deep_path_without_any_repository_is_cheap_and_finds_nothing",
        "nudge",
        "memory_dir_for_cwd",
        1.0,
    ),
    (
        "tests/test_memory_review_fixes_engine.py",
        "HunkSizeTests.test_the_longest_text_that_is_let_through_scans_in_a_bounded_time",
        "dg",
        "_added_tokens",
        30.0,
    ),
    # runs the script as a process: the slow copy is a copy of the engine whose main() spins first
    (
        "tests/test_memory_nudge.py",
        "HookProcessTests.test_it_is_fast_enough_for_every_session_start",
        "SUBPROCESS",
        "memory_headroom_nudge.py",
        3.0,
    ),
]
SLACK = 0.3


def spin(seconds):
    end = time.process_time() + seconds
    while time.process_time() < end:
        pass


class ReProxy:
    """The `re` module of a test file, with search() made slow (the wizard test times re.search itself)."""

    def __init__(self, real, seconds):
        self._real, self._seconds = real, seconds

    def __getattr__(self, name):
        return getattr(self._real, name)

    def search(self, *args, **kwargs):
        result = self._real.search(*args, **kwargs)
        spin(self._seconds)
        return result


def run_one(path, name, alias, attr, bound, slow, engine_root):
    """Runs one test in this process; returns (passed, message)."""
    spec = importlib.util.spec_from_file_location(
        "timing_under_test", os.path.join(engine_root, path)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    seconds = bound + SLACK
    if slow and alias != "SUBPROCESS":
        target = getattr(module, alias)
        if alias == "re":
            setattr(module, "re", ReProxy(target, seconds))
        else:
            original = getattr(target, attr)

            def slowed(*args, **kwargs):
                result = original(*args, **kwargs)
                spin(seconds)
                return result

            setattr(target, attr, slowed)
    suite = unittest.defaultTestLoader.loadTestsFromName(name, module)
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    problems = [
        text.strip().splitlines()[-1] for _case, text in result.failures + result.errors
    ]
    return result.wasSuccessful() and result.testsRun == 1, "; ".join(problems)[:160]


def slow_engine_copy(seconds):
    """A copy of the engine whose nudge script spins before doing anything (for the test that runs it as a process)."""
    root = tempfile.mkdtemp(prefix="timing_engine_")
    copy = os.path.join(root, "engine")
    shutil.copytree(ENGINE, copy, ignore=shutil.ignore_patterns("__pycache__"))
    script = os.path.join(copy, "memory_headroom_nudge.py")
    text = open(script, encoding="utf-8").read()
    marker = "def main(argv=None):\n"
    assert text.count(marker) == 1
    spinner = (
        "    import time as _t\n    _end = _t.process_time() + %r\n    while _t.process_time() < _end:\n        pass\n"
        % seconds
    )
    open(script, "w", encoding="utf-8").write(text.replace(marker, marker + spinner))
    return copy


def child(index, slow):
    path, name, alias, attr, bound = TESTS[index]
    engine_root = (
        slow_engine_copy(bound + SLACK) if (slow and alias == "SUBPROCESS") else ENGINE
    )
    passed, message = run_one(path, name, alias, attr, bound, slow, engine_root)
    print("%s %s" % ("PASSED" if passed else "FAILED", message))
    return 0


def launch(index, slow):
    started = time.monotonic()
    done = subprocess.run(
        [
            sys.executable,
            "-B",
            "-I",
            os.path.abspath(__file__),
            "--one",
            str(index),
            "slow" if slow else "plain",
        ],
        capture_output=True,
        text=True,
        timeout=1200,
    )
    line = (
        done.stdout.strip().splitlines()[-1]
        if done.stdout.strip()
        else "NO OUTPUT: " + done.stderr.strip()[-200:]
    )
    return index, slow, line, time.monotonic() - started


def main(argv):
    if argv and argv[0] == "--one":
        return child(int(argv[1]), argv[2] == "slow")
    chosen = [int(a) for a in argv] if argv else list(range(len(TESTS)))
    jobs = [(i, slow) for i in chosen for slow in (False, True)]
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for index, slow, line, took in pool.map(lambda job: launch(*job), jobs):
            results[(index, slow)] = line
    wrong = []
    for i in chosen:
        plain, slowed = results[(i, False)], results[(i, True)]
        ok = plain.startswith("PASSED") and slowed.startswith("FAILED")
        print(
            "%2d %-4s %s :: %s"
            % (
                i,
                "ok" if ok else "BAD",
                TESTS[i][0].split("/")[-1],
                TESTS[i][1].split(".")[-1],
            )
        )
        if not ok:
            wrong.append(i)
            print("      as it is: %s\n      slow copy: %s" % (plain, slowed))
    print(
        "tests whose slow copy was NOT caught (or whose control failed):",
        wrong or "none",
    )
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
