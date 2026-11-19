"""Mutation check for the dream modules (grounding, staging, promote, lanes, pipeline). Mutates a COPY under /tmp/evmut_dream.

usage: python3 -I harness_dream.py [G01 S02 ...]     (no arguments = every mutant; takes several minutes)
Prints one line per mutant: killed, SURVIVED, or MISSING-SNIPPET (the pattern no longer matches the reflowed source).
Every mutant must be killed. A MISSING-SNIPPET line means the pattern no longer matches the source, so that mutant has silently stopped
running: fix the pattern (two had, unnoticed, until 2026-11-18). The unmutated copy must pass every suite named in EXTRA_SUITES first. No mutant is documented as equivalent.
"""

import os
import re
import shutil
import subprocess
import sys

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "engine")
WORK = "/tmp/evmut_dream/scripts"
os.makedirs(WORK, exist_ok=True)
for f in sorted(os.listdir(SRC)):
    if f.startswith("memory_") and f.endswith(".py"):
        shutil.copy(os.path.join(SRC, f), os.path.join(WORK, f))
for sub in ("tests", "dream", "recall"):
    for root, dirs, files in os.walk(os.path.join(SRC, sub)):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        rel = os.path.relpath(root, SRC)
        os.makedirs(os.path.join(WORK, rel), exist_ok=True)
        for f in files:
            if f.endswith(".py"):
                shutil.copy(os.path.join(root, f), os.path.join(WORK, rel, f))
G, S, P, L, U = (
    "memory_dream_ground.py",
    "memory_dream_stage.py",
    "memory_dream.py",
    "dream/memory_dream_lanes.py",
    "dream/memory_dream_run.py",
)
M = [
    (
        "G01 file name check off",
        G,
        r"if not isinstance\(name, str\) or not FILE_NAME\.fullmatch\(name\):",
        "if not isinstance(name, str):",
    ),
    ("G02 min old off", G, r"or len\(old\) < MIN_OLD", ""),
    (
        "G03 undecodable not blocked",
        G,
        r"if hunk\[\"file\"\] in undecodable:",
        "if False:",
    ),
    (
        "G04 overlapping count off",
        G,
        r"count, pos = count \+ 1, text\.find\(needle, pos \+ 1\)",
        "count, pos = count + 1, text.find(needle, pos + len(needle))",
    ),
    (
        "G05 instruction rule off",
        G,
        r"if INSTRUCTION_LIKE\.search\(new\) and not INSTRUCTION_LIKE\.search\(old\):",
        "if False:",
    ),
    (
        "G06 shrink rule off",
        G,
        r"if len\(old\) > SHRINK_CHECK_FROM and len\(new\) < len\(old\) \* SHRINK_FLOOR:",
        "if False:",
    ),
    (
        "G07 growth rule off",
        G,
        r"if len\(new\) > len\(old\) \* GROWTH_FACTOR \+ GROWTH_SLACK:",
        "if False:",
    ),
    (
        "G08 frontmatter guard off",
        G,
        r"elif start < _frontmatter_end\(text\):",
        "elif False:",
    ),
    (
        "G09 all quotes allowed",
        G,
        r'allowed = hunk\["old"\] \+ " " \+ " "\.join\(typed_quotes\)',
        'allowed = hunk["old"] + " " + " ".join(_squash(i["quote"]) for i in hunk["evidence"])',
    ),
    (
        "G10 figures not checked",
        G,
        r"found \+= \[m\.group\(0\) for m in FIGURE\.finditer\(text\)\]",
        "pass",
    ),
    (
        "G11 bare hosts not checked",
        G,
        r"found \+= \[\s*m\.group\(0\)\s*for m in DOMAIN\.finditer\(text\)\s*if m\.group\(1\)\.lower\(\) not in FILE_EXTENSIONS\s*\]",
        "pass",
    ),
    (
        "G12 pinned off",
        G,
        r"if line\.strip\(\) in \{p\.strip\(\) for p in pinned\}:",
        "if False:",
    ),
    (
        "G13 typed evidence not required",
        G,
        r'\(None if typed else "evidence needs a quote that a person typed"\)',
        "None",
    ),
    (
        "G14 date check off",
        G,
        r'if digest\.date == "unknown" or digest\.date < note_date:',
        "if False:",
    ),
    (
        "G15 overlap check off",
        G,
        r"if any\(hunk\[\"start\"\] < b and a < end for a, b in taken\):",
        "if False:",
    ),
    ("G16 reason normalised off", G, r" or mr\.normalize\(reason\) != reason", ""),
    (
        "G17 evidence shape off",
        G,
        r"if not all\(\s*isinstance\(i, dict\)\s*and isinstance\(i\.get\(\"session\"\), str\)\s*and isinstance\(i\.get\(\"quote\"\), str\)\s*for i in items\s*\):",
        "if False:",
    ),
    (
        "G18 description key guard off",
        G,
        r' or not edited\.startswith\("description:"\)',
        "",
    ),
    (
        "S01 equality check off",
        S,
        r"    problems = staged_differs_from_expected\(snapshot_dir, staged_dir, accepted\)\n    if problems:\n        return problems[^\n]*",
        "    problems = []",
    ),
    (
        "S02 link check off",
        S,
        r"if walk\.symlinks or walk\.special or walk\.errors:",
        "if False:",
    ),
    ("S03 file set check off", S, r"if set\(walk\.files\) != expected:", "if False:"),
    ("S04 fixed fence", S, r'return "`" \* max\(3, longest \+ 1\)', 'return "```"'),
    (
        "S05 digest without hunks",
        S,
        r"parts = \[json\.dumps\(clean, sort_keys=True, ensure_ascii=True\)\]",
        "parts = []",
    ),
    (
        "S06 digest without bytes",
        S,
        r'parts \+= \[\s*"%s %s" % \(rel, mc\.sha256_file\(os\.path\.join\(staged_dir, rel\)\)\)\s*for rel in sorted\(_by_file\(accepted\)\)\s*\]',
        "pass",
    ),
    (
        "S07 lint check off",
        S,
        r"errors = new_lint_errors\(snapshot_dir, staged_dir\)",
        "errors = []",
    ),
    (
        "S08 scan check off",
        S,
        r"blocks = new_block_hits\(snapshot_dir, staged_dir\)",
        "blocks = []",
    ),
    (
        "S09 strict utf8 off",
        S,
        r'return fh\.read\(\)\.decode\("utf-8"\)',
        'return fh.read().decode("utf-8", errors="replace")',
    ),
    (
        "P01 snapshot id check off",
        P,
        r"if not SNAP_ID\.fullmatch\(snap\) or snap not in sn\.list_snapshots\(ops\):",
        "if False:",
    ),
    (
        "P02 run dir links off",
        P,
        r"if walk\.symlinks or walk\.special or walk\.errors:",
        "if False:",
    ),
    (
        "P03 reground off",
        P,
        r"    accepted = _regrounded\(ops, snap, stored\)",
        "    accepted = stored",
    ),
    ("P04 expect off", P, r"if args\.expect != digest:", "if False:"),
    ("P11 partial reason keeps the one-note wording", P, r'            reason = str\(reason\)\.replace\("nothing was replaced", "this note was not replaced"\)[^\n]*\n', ""),
    ("P15 promoted marker written after the lock is released", P, r"\n        _record_promoted\(ops, run_dir, args\.run, done, pre, snap, archive_dir\)", "\n    _record_promoted(ops, run_dir, args.run, done, pre, snap, archive_dir)"),
    ("P16 vanished live note is a raw errno", P, r"        except OSError as err:  # another promote of this run may have the note out of the way for a moment", "        except ZeroDivisionError as err:  # another promote of this run may have the note out of the way for a moment"),
    ("P18 the way back is not flushed", S, r"        sys\.stdout\.flush\(\)\n        return\n", "        return\n"),
    ("P19 replacing printed before the lock is taken", P, r"    _apply_promotion\(args, memory, ops, run_dir, snap, touched\)\n    return EXIT_OK", '    for rel in touched:\n        print("replacing %s" % rel)\n    _apply_promotion(args, memory, ops, run_dir, snap, touched)\n    return EXIT_OK'),
    ("I01 signals not held while the promote replaces and records", P, r"with reg\.ops_lock\(ops\), ds\.held_signals\(\):", "with reg.ops_lock(ops):"),
    ("I02 a signal that arrived is not looked for between notes", P, r"        if ds\.interrupt_pending\(\):", "        if False:"),
    ("I03 held signals are delivered at the end instead of consumed", S, r"            signal\.sigwait\(arrived\)\n", "            break\n"),
    ("I04 a signal the parent ignored is held and counted", S, r"if signal\.getsignal\(sig\) is not signal\.SIG_IGN", "if True"),
    ("I05 main lets an interrupt through", P, r"    except KeyboardInterrupt:  # outside the part", "    except ZeroDivisionError:  # outside the part"),
    ("I06 a dead error stream makes the record fail", S, r"        sys\.stderr\.flush\(\)\n    except OSError:\n        pass\n", "        sys.stderr.flush()\n    except ZeroDivisionError:\n        pass\n"),
    ("I07 the signal mask is not restored", S, r"        mask\(signal\.SIG_SETMASK, old\)", "        pass"),
    ("I08 a closed output stream is not tried on the error stream", S, r"    except OSError:\n        pass\n    say_err\(line\)", "    except OSError:\n        pass\n    pass"),
    ("P12 unreadable marker counts as promoted", S, r'    if status == "promoted":\n        return "this run was already promoted"\n    if status != "partial":', '    if status != "partial":\n        return "this run was already promoted"\n    if False:'),
    ("P13 marker write failure not caught", P, r"    except OSError as oserr:\n        cause =", "    except ZeroDivisionError as oserr:\n        cause ="),
    ("P14 log write failure not caught", S, r"    except OSError as err:\n        print\(\n            \"NOTE: the log entry", "    except ZeroDivisionError as err:\n        print(\n            \"NOTE: the log entry"),
    (
        "P05 live sha recheck off",
        P,
        r"if mc\.sha256_file\(live\) != manifest\[\"files\"\]\[rel\]\[\"sha256\"\]:\n                raise _Partial",
        "if False:\n                raise _Partial",
    ),
    (
        "P06 unsafe name precheck off",
        P,
        r"        reason = sn\._unsafe_name\(memory, rel\)\n        if reason:",
        "        reason = None\n        if reason:",
    ),
    ("P07 run id check off", P, r"if not RUN_ID\.fullmatch\(run_id\):", "if False:"),
    (
        "P08 already promoted off",
        P,
        r"if os\.path\.lexists\(os\.path\.join\(run_dir, MARKER\)\):",
        "if False:",
    ),
    (
        "P09 different directory off",
        P,
        r"if not sn\._source_matches\(manifest\.get\(\"source\"\), memory\):",
        "if False:",
    ),
    (
        "P10 live changed check off",
        P,
        r'\s*or mc\.sha256_file\(live\) != manifest\["files"\]\[rel\]\["sha256"\]',
        "",
    ),
    (
        "L01 first json wins",
        L,
        r"found = values\[-1\] if values else None",
        "found = values[0] if values else None",
    ),
    ("L02 tools check off", L, r"if out\.tools != \[\]:", "if False:"),
    ("L03 the verifier accepts an object found in prose", L, r"values = \[\] if unambiguous else _json_values\(body\)", "values = _json_values(body)"),
    ("L06 a verifier object may repeat a key", L, r"json\.loads\(body, object_pairs_hook=_no_repeated_keys\) if unambiguous else json\.loads\(body\)", "json.loads(body)"),
    ("L07 a reply nested deeper than the limit is usable", L, r"    return found if _shallow\(found\) else None", "    return found"),
    ("L04 an over-long verifier reply is cut instead of refused", L, r"    if unambiguous and len\(text\) > MAX_REPLY_CHARS:", "    if False:"),
    ("L05 the verifier accepts an answer that is not an object", L, r"found = whole if not unambiguous or isinstance\(whole, dict\) else None", "found = whole"),
    (
        "U01 hunk validation off",
        U,
        r"            if not _valid_hunk\(item\):",
        '            if not (isinstance(item, dict) and isinstance(item.get("old"), str) and isinstance(item.get("new"), str)):',
    ),
    (
        "U02 default reject off",
        U,
        r'return False, "no usable verdict", True',
        'return True, "no usable verdict", True',
    ),
    # staging gate: which findings stop a staged edit, and counted per (code, path)
    ("S10 yaml codes not blocking", "memory_lint.py", r"finding\.code in STAGING_BLOCKING_CODES", 'finding.code in {"frontmatter-unclosed", "missing-description", "index-is-symlink"}'),
    ("S11 unterminated quote not blocking", "memory_lint.py", r'\s*"yaml-unterminated",', ""),
    ("S12 every warning blocks", "memory_lint.py", r'return finding\.severity == "ERROR" or', 'return finding.severity in ("ERROR", "WARN") or'),
    ("S13 gate keys on a set again", S, r"if count > before\[key\]", "if key not in before"),
    # egress gate
    ("E01 unscanned entries ignored", "memory_egress.py", r"if not \(verdict\.block or verdict\.skipped\):", "if not verdict.block:"),
    ("E02 warn tier counts as block", "memory_egress.py", r'hit\.tier == "block"', 'hit.tier != ""'),
    ("E03 refusal never raised", "memory_egress.py", r"if not allow:", "if False:"),
    ("E04 scan failure not converted", "memory_egress.py", r"except OSError as err:", "except ZeroDivisionError as err:"),
    ("E05 file list not capped", "memory_egress.py", r"files\[:FILES_LISTED\]", "files"),
    ("U03 dream gate call off", U, r"eg\.require_clear\(\s*os\.path\.join\(ops, \"snapshots\", snap, \"memory\"\),\s*args\.allow_secret_shapes,?\s*\)", "None"),
    ("U04 dream refusal exits as generic failure", U, r"except eg\.SecretShapesRefused as err:", "except ZeroDivisionError as err:"),
    ("R01 staged arm ungated", "recall/memory_recall.py", r"eg\.require_clear\(staged, args\.allow_secret_shapes\)", "None"),
    ("R02 live and baseline arms ungated", "recall/memory_recall.py", r"eg\.require_clear\(memory, args\.allow_secret_shapes\)", "None"),
    ("R03 recall refusal exits as generic failure", "recall/memory_recall.py", r"except eg\.SecretShapesRefused as err:", "except ZeroDivisionError as err:"),
    ("K01 counts take digits of other scripts", "memory_common.py", r"int\(text\) if text\.isascii\(\) and text\.isdigit\(\) else 0", "int(text) if text.isdigit() else 0"),
    ("K02 a very long digit string crashes (Python 3.11 and later)", "memory_common.py", r"    except ValueError:  # more digits than int\(\) will read\n        value = 0", "    except ZeroDivisionError:  # more digits than int() will read\n        value = 0"),
    ("R07 baseline save is not atomic", "recall/memory_recall.py",
     r'        fd, partial = tempfile\.mkstemp\(dir=os\.path\.dirname\(target\), prefix="\.baseline-", suffix="\.part"\)\n        with os\.fdopen\(fd, "w", encoding="utf-8"\) as fh:\n            json\.dump\(data, fh, indent=1, sort_keys=True\)\n            fh\.flush\(\)\n            os\.fsync\(fh\.fileno\(\)\)\n        written = True\n        os\.replace\(partial, target\)\n',
     '        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)\n        with os.fdopen(fd, "w", encoding="utf-8") as fh:\n            json.dump(data, fh, indent=1, sort_keys=True)\n'),
    ("R11 saved baseline readable by others", "recall/memory_recall.py", r'        with os\.fdopen\(fd, "w", encoding="utf-8"\) as fh:\n            json\.dump\(data, fh, indent=1, sort_keys=True\)\n            fh\.flush', '        os.fchmod(fd, 0o644)\n        with os.fdopen(fd, "w", encoding="utf-8") as fh:\n            json.dump(data, fh, indent=1, sort_keys=True)\n            fh.flush'),
    ("R12 baseline not synced before it replaces the old one", "recall/memory_recall.py", r"            os\.fsync\(fh\.fileno\(\)\)\n", "            pass\n"),
    ("R13 a failed save loses the previous-file note", "recall/memory_recall.py", r'USD; any previous file at that path is "\n            "untouched%s: fix that', 'USD; "\n            "%s: fix that'),
    ("R14 the new file is synced before it is flushed", "recall/memory_recall.py", r"            fh\.flush\(\)\n", "            pass\n"),
    ("R15 a folder that is writable but not searchable passes", "recall/memory_recall.py", r"os\.W_OK \| os\.X_OK", "os.W_OK"),
    ("R16 an over-long file name passes the preflight", "recall/memory_recall.py", r"    if len\(os\.fsencode\(os\.path\.basename\(real\)\)\) > SAVE_NAME_LIMIT:", "    if False:"),
    ("R17 a failed move tells the person to remove the complete copy", "recall/memory_recall.py", r"        if written:  # the copy is the whole result", "        if False:  # the copy is the whole result"),
    ("R08 save preflight accepts a named pipe", "recall/memory_recall.py", r"    if os\.path\.exists\(real\) and not os\.path\.isfile\(real\):", "    if False:"),
    ("R09 empty save path skips the preflight", "recall/memory_recall.py", r'    elif arm == "baseline":  # an empty string', '    elif arm == "baseline" and save:  # an empty string'),
    ("R10 missing save path is not noted", "recall/memory_recall.py", r'        print\("NOTE: no --save FILE:[^\n]*\n', "        pass\n"),
    ("Y01 running calls do not count as spent", "recall/memory_recall_run.py", r"if self\.running == 0 or self\.total \+ self\.reserved < self\.limit:", "if True:"),
    ("Y02 the hold of a call is never released", "recall/memory_recall_run.py", r"                self\.running -= 1\n", "                pass\n"),
    ("Y03 the judge's ask does not hold its cap", "recall/memory_recall_run.py", r"    with spend\.call\(settings\.per_q_usd\):\n        return _ask\(", "    if True:\n        return _ask("),
    ("Y04 the lanes' ask_lane does not hold its cap", L, r"    with spend\.call\(settings\.per_q_usd\):\n        return _ask_lane\(", "    if True:\n        return _ask_lane("),
    ("Y05 a waiting call is not woken when a call ends", "recall/memory_recall_run.py", r"                self\._changed\.notify_all\(\)", "                pass"),
    ("Y06 stopping the run does not wake a waiting call", "recall/memory_recall_run.py", r"self\.stopped = self\.stopped or reason\n            self\._changed\.notify_all\(\)", "self.stopped = self.stopped or reason\n            pass"),
    ("Y07 a call is refused instead of waiting for the running ones", "recall/memory_recall_run.py", r"                self\._changed\.wait\(\)", "                raise CostCeiling('the caps of the running calls reach the ceiling')"),
    ("Y08 the held sum is not reset when nothing is running", "recall/memory_recall_run.py", r"self\.reserved = self\.reserved - cap if self\.running else 0\.0", "self.reserved = self.reserved - cap"),
    ("R04 save preflight ignores links", "recall/memory_recall.py", r"real = os\.path\.realpath\(target\)", "real = target"),
    ("R05 save preflight ignores a locked file", "recall/memory_recall.py", r"    if os\.path\.lexists\(real\) and not os\.access\(real, os\.W_OK\):", "    if False:"),
    ("R06 late save failure not caught", "recall/memory_recall.py", r"    except OSError as err:  # the preflight catches", "    except ZeroDivisionError as err:  # the preflight catches"),
    # spend accounting and the per-writer cap
    ("C01 failed lane cost not counted", L, r"spend\.add\(failed\.cost if failed else 0\.0\)", "pass"),
    ("C02 lane timeout not charged", L, r"spend\.add\(settings\.per_q_usd\)", "pass"),
    ("C03 writer cap off", U, r"elif kept < MAX_HUNKS_PER_WRITER:", "elif True:"),
    ("C04 failed judge call cost not counted", "recall/memory_recall_run.py", r"_errored\(question, spend, err, failed\.cost if failed else 0\.0\)", "_errored(question, spend, err, 0.0)"),
    ("C05 judge timeout not charged", "recall/memory_recall_run.py", r"return _errored\(question, spend, err, settings\.per_q_usd\)", "return _errored(question, spend, err, 0.0)"),
    # a failed child is still held to its tool list; absurd costs; temp folders; abbreviations; names; emptied descriptions
    ("C06 failed lane tool check off", L, r"if failed and failed\.tools:", "if False:"),
    ("C07 failed judge tool check off", "recall/memory_recall_run.py", r"if failed and failed\.tools is not None and failed\.tools != EXPECTED_TOOLS:", "if False:"),
    ("C08 cost overflow not caught", "recall/memory_recall_run.py", r"except OverflowError:\s*return 0\.0", "except ZeroDivisionError:\n        return 0.0"),
    ("W01 dream workdir not removed", U, r'with tempfile\.TemporaryDirectory\(prefix="dream_lanes_"\) as workdir:', 'workdir = tempfile.mkdtemp(prefix="dream_lanes_")\n    for _once in (0,):'),
    ("A01 dream run subcommand allows abbreviations", U, r'"run", help="run the lanes, then ground and stage what they proposed", allow_abbrev=False', '"run", help="run the lanes, then ground and stage what they proposed"'),
    ("A02 recall parser allows abbreviations", "recall/memory_recall.py", r"super\(\)\.__init__\(\*args, allow_abbrev=False, \*\*kwargs\)", "super().__init__(*args, **kwargs)"),
    ("E07 file names printed as they are", "memory_egress.py", r"return WITHHELD if ms\.safe_snippet\(shown, MAX_NAME_CHARS\) != shown else shown", "return shown"),
    ("F01 emptied quotes count as a description", "memory_frontmatter.py", r'\.strip\(\)\.strip\(.{1,8}\)\.strip\(\)', ".strip()"),
    # scanner widening
    ("SC01 gho/ghu/ghr prefixes dropped", "memory_scan.py", r'"gh\[pousr\]"', '"gh[ps]"'),
    ("SC02 lowercase bearer needs no digit", "memory_scan.py", r"\(\?=\[[^\]]*\]\{0,100\}\[0-9\]\)", ""),
    ("SC03 stripe not anchored", "memory_scan.py", r"\(\?<!\[A-Za-z0-9\]\)\(\?:sk_\(\?:live", "(?:sk_(?:live"),
    ("SC04 aws abia/acca not anchored", "memory_scan.py", r"\(\?<!\[A-Za-z0-9\]\)A\(\?:BIA\|CCA\)", "A(?:BIA|CCA)"),
    ("SC06 anthropic key not anchored", "memory_scan.py", r'\(\?<!\[A-Za-z0-9\]\)sk-", "ant-', 'sk-", "ant-'),
    ("SC07 stripe org live/test group dropped", "memory_scan.py", r"sk_org\(\?:_\(\?:live\|test\)\)\?", "sk_org"),
    ("SC08 bearer digit window widened", "memory_scan.py", r"\{0,100\}\[0-9\]", "{0,1000}[0-9]"),
    ("SC09 database +driver group dropped", "memory_scan.py", r"\(\?:\\\+\[A-Za-z0-9_\]\{1,20\}\)\?:", ":"),
    ("SC05 openai project keys not anchored", "memory_scan.py", r"\(\?<!\[A-Za-z0-9\]\)sk-\(\?:proj", "sk-(?:proj"),
]
M = [m for m in M if len(sys.argv) < 2 or m[0].split()[0] in sys.argv[1:]]


# Mutants in these modules are also run against the tests that own them (the base set does not match those file names).
EXTRA_SUITES = {
    "memory_common.py": [("tests", "test_memory_positive_int_strict.py")],
    "dream/memory_dream_lanes.py": [("dream/tests", "test_dream_extract_json_round2.py"), ("dream/tests", "test_dream_extract_json_round3.py"), ("dream/tests", "test_dream_spend_inflight.py")],
    "memory_dream.py": [("tests", "test_memory_promote_round2.py"), ("tests", "test_memory_promote_interrupt.py")],
    "memory_dream_stage.py": [("tests", "test_memory_promote_round2.py"), ("tests", "test_memory_promote_interrupt.py"), ("tests", "test_memory_held_signals.py")],
    "memory_egress.py": [("tests", "test_memory_egress.py"), ("recall/tests", "test_memory_recall_egress.py")],
    "memory_scan.py": [("tests", "test_memory_scan.py"), ("tests", "test_memory_extra_scan_common.py")],
    "memory_lint.py": [("tests", "test_memory_lint.py"), ("tests", "test_memory_extra_lint.py")],
    "recall/memory_recall.py": [("recall/tests", "test_memory_recall_egress.py"), ("recall/tests", "test_memory_recall_save_preflight.py")],
    "recall/memory_recall_run.py": [("recall/tests", "test_memory_recall_cost.py"), ("recall/tests", "test_memory_recall_spend_inflight.py")],
}


def run_tests(fname=None):
    extra = [
        [sys.executable, "-I", "-m", "unittest", "discover", "-s", os.path.join(WORK, directory), "-p", pattern]
        for directory, pattern in EXTRA_SUITES.get(fname, [])
    ]
    cmds = extra + [
        [
            sys.executable,
            "-I",
            "-m",
            "unittest",
            "discover",
            "-s",
            os.path.join(WORK, "tests"),
            "-p",
            "test_memory_dream_*.py",
        ],
        [
            sys.executable,
            "-I",
            "-m",
            "unittest",
            "discover",
            "-s",
            os.path.join(WORK, "dream", "tests"),
            "-p",
            "test_dream_pipeline*.py",
        ],
    ]
    for cmd in cmds:
        try:
            if subprocess.run(cmd, capture_output=True, text=True, timeout=300).returncode != 0:
                return True
        except subprocess.TimeoutExpired:  # a mutant that makes a test wait for ever is caught too
            return True
    return False


# A red unmutated copy would make every mutant look killed.
if any(run_tests(name) for name in sorted({"memory_scan.py", "memory_egress.py", "recall/memory_recall_run.py", *EXTRA_SUITES})):
    print("BASELINE RED: the unmutated copy fails its own tests; fix that before reading any mutant result", flush=True)
    sys.exit(2)

survived = []
for name, fname, pattern, repl in M:
    path = os.path.join(WORK, fname)
    original = open(path, encoding="utf-8").read()
    matches = len(re.findall(pattern, original, flags=re.S))
    if matches != 1:
        label = "MISSING-SNIPPET" if matches == 0 else "AMBIGUOUS"
        print("%-38s %s" % (name, label), flush=True)
        survived.append("%s (%s)" % (name, label.lower()))
        continue
    mutated = re.sub(pattern, lambda m: repl, original, count=1, flags=re.S)
    open(path, "w", encoding="utf-8").write(mutated)
    try:
        compile(mutated, path, "exec")  # a mutant that does not even parse proves nothing
        killed = run_tests(fname)
        print("%-38s %s" % (name, "killed" if killed else "SURVIVED"), flush=True)
        if not killed:
            survived.append(name)
    finally:
        open(path, "w", encoding="utf-8").write(original)
print("survivors:", survived or "none", flush=True)
