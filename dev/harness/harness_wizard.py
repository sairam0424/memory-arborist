"""Mutation check for recall/memory_recall_wizard.py. Mutates a COPY under /tmp/evmut_wizard, never the real files.

usage: python3 -I harness_wizard.py [W01 W02 ...]     (no arguments = every mutant)
Prints one line per mutant: killed, SURVIVED, MISSING-SNIPPET (the pattern no longer matches the reflowed source),
AMBIGUOUS (it matches more than once) or BROKEN (the mutant does not parse, so it proves nothing).
"""

import os
import re
import shutil
import subprocess
import sys

ENGINE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "engine",
)
WORK = "/tmp/evmut_wizard/engine"
shutil.rmtree(os.path.dirname(WORK), ignore_errors=True)
shutil.copytree(ENGINE, WORK, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
TARGETS = [os.path.join(WORK, "recall", name) for name in ("memory_recall_wizard.py", "memory_recall_keys.py", "memory_recall_run.py")]
TESTS = os.path.join(WORK, "recall", "tests")

M = [
    # checking one candidate
    (
        "W01 quote need not be in the note",
        r"if span not in squash\(notes\[name\]\):",
        "if False:",
    ),
    (
        "W02 key need not match its quote",
        r"if not all\(re\.search\(p, span, re\.S\) for p in must\):",
        "if False:",
    ),
    (
        "W03 key may match the question",
        r"if any\(re\.search\(p, question, re\.S\) for p in must\):",
        "if False:",
    ),
    (
        "W04 the index is a source",
        r"name == INDEX_NAME or name not in notes",
        "name not in notes",
    ),
    (
        "W06 empty-matching patterns allowed",
        r'if compiled\.search\(""\) is not None:',
        "if False:",
    ),
    (
        "W08 pattern length uncapped",
        r"PATTERN_MIN_CHARS <= len\(pattern\) <= PATTERN_MAX_CHARS",
        "PATTERN_MIN_CHARS <= len(pattern)",
    ),
    (
        "W09 question mark not required",
        r'if not stripped\.endswith\("\?"\):',
        "if False:",
    ),
    (
        "W10 secret in a question allowed",
        r"if not _clean_text\(stripped\):",
        "if False:",
    ),
    (
        "W11 secret in a quote allowed",
        r"if not keys\.plain_text\(span\) or not _clean_text\(span\):",
        "if not keys.plain_text(span):",
    ),
    ("W12 any kind allowed", r"if kind not in KIND_ORDER:", "if False:"),
    (
        "W13 recall forbid may match the quote",
        r'if any\(re\.search\(p, span, re\.S\) for p in forbid\):',
        "if False:",
    ),
    (
        "W16 negative key taken from the model",
        r'"must": \[NEGATIVE_MUST\],',
        '"must": item.get("must") or [NEGATIVE_MUST],',
    ),
    (
        "W17 probe term may be in the notes",
        r"if term\.lower\(\) in haystack:",
        "if False:",
    ),
    (
        "W18 probe term need not be in the question",
        r"if term\.lower\(\) not in question\.lower\(\):",
        "if False:",
    ),
    ("W19 probe term may be any text", r"not PROBE_TERM\.fullmatch\(term\)", "False"),
    ("W45 personal data allowed", r"found\.secret or found\.pii", "found.secret"),
    (
        "W47 quote length uncapped",
        r"QUOTE_MIN <= len\(squash\(quote\)\) <= QUOTE_MAX",
        "QUOTE_MIN <= len(squash(quote))",
    ),
    (
        "W48 quote minimum dropped",
        r"QUOTE_MIN <= len\(squash\(quote\)\) <= QUOTE_MAX",
        "len(squash(quote)) <= QUOTE_MAX",
    ),
    (
        "W49 forbid list uncapped",
        r"MAX_MUST, MAX_FORBID, MAX_PROBES = 3, 2, 4",
        "MAX_MUST, MAX_FORBID, MAX_PROBES = 3, 9, 4",
    ),
    (
        "X01 path and shell characters allowed",
        r"or QUESTION_BANNED & set\(stripped\)",
        "",
    ),
    ("X02 leading symbol allowed", r"not stripped\[0\]\.isalnum\(\)", "False"),
    (
        "X03 forbid may match the question",
        r"if any\(re\.search\(p, question, re\.S\) for p in forbid\):",
        "if False:",
    ),
    (
        "X04 forbid may match the key's own answer",
        r"if any\(re\.search\(p, answer, re\.S\) for p in forbid\):",
        "if False:",
    ),
    (
        "X12 control characters in a quote allowed",
        r"if not keys\.plain_text\(span\) or not _clean_text\(span\):",
        "if not _clean_text(span):",
    ),
    (
        "X14 secret in a key allowed",
        r'if not _clean_text\(" "\.join\(must \+ forbid\)\):',
        "if False:",
    ),
    # the key grammar
    (
        "G01 a group may be repeated",
        r'if scan\.last == "group":\s*return None, "only \? may follow a group"',
        "if False:\n        return None, ''",
    ),
    (
        "G02 nested groups allowed",
        r'if scan\.depth:\s*return None, "nested groups are not allowed"',
        "if False:\n        return None, ''",
    ),
    (
        "G03 open repeat on anything",
        r'if scan\.last not in \("digit", "space"\):',
        "if False:",
    ),
    (
        "G04 open repeats uncapped",
        r"if scan\.open_repeats > MAX_OPEN_REPEATS:",
        "if False:",
    ),
    ("G05 groups uncapped", r"if scan\.groups > MAX_GROUPS:", "if False:"),
    ("G06 bounded repeats uncapped", r"or scan\.bounded > MAX_BOUNDED_REPEATS", ""),
    ("G07 repeat count uncapped", r"or high > MAX_REPEAT_COUNT", ""),
    (
        "G08 lookarounds and flags allowed",
        r'if body\.startswith\("\(\?", i\):',
        "if False:",
    ),
    (
        "G09 backreferences and word escapes allowed",
        r'if escaped in "ds":',
        'if escaped in "dsw123456789":',
    ),
    ("G10 anchors allowed", r'elif c in "\^\$\]\}":', 'elif c in "]}":'),
    ("G12 negated classes allowed", r'or inner\.startswith\("\^"\)', ""),
    ("G13 escapes in classes allowed", r'or "\\\\" in inner', ""),
    (
        "G14 control characters in a key allowed",
        r"if not plain_text\(pattern\):",
        "if False:",
    ),
    (
        "G15 compile errors not caught",
        r"except re\.error:",
        "except ZeroDivisionError:",
    ),
    ("G17 class length uncapped", r"or end - i > 41", ""),
    # choosing the set
    (
        "W20 no per-note cap",
        r"TARGET, PER_FILE_MAX = 28, 2",
        "TARGET, PER_FILE_MAX = 28, 99",
    ),
    (
        "W22 negative quota raised",
        r'KIND_QUOTA = \{"negative": 3\}',
        'KIND_QUOTA = {"negative": 9}',
    ),
    ("W23 duplicates kept", r"if key in seen or", "if False or"),
    (
        "W24 target ignored inside a row",
        r"if total >= target or len\(picked\[kind\]\) >= limit:",
        "if len(picked[kind]) >= limit:",
    ),
    (
        "W25 ids without zero padding",
        r'"%s%02d" % \(ID_PREFIX\[kind\], number\)',
        '"%s%d" % (ID_PREFIX[kind], number)',
    ),
    ("X13 notes ordered alphabetically", r"key=_spread_key\)", "key=str)"),
    # chunks and the prompt
    ("W26 eleven notes per chunk", r"36_000, 10, 8, 40", "36_000, 11, 8, 40"),
    ("W27 no character budget per chunk", r"36_000, 10, 8, 40", "360_000, 10, 8, 40"),
    (
        "W28 first chunks only",
        r"keep = sorted\(\s*\{int\(i \* last / max\(1, max_chunks - 1\) \+ 0\.5\) for i in range\(max_chunks\)\}\s*\)",
        "keep = list(range(max_chunks))",
    ),
    (
        "W29 the index is sent",
        r"if n != INDEX_NAME\s+and SAFE_NOTE_NAME",
        "if SAFE_NOTE_NAME",
    ),
    ("W30 short notes are sent", r"and len\(t\.strip\(\)\) >= MIN_NOTE_CHARS", ""),
    (
        "W31 whole notes are sent",
        r"notes\[name\]\[:MAX_NOTE_CHARS_SENT\]",
        "notes[name]",
    ),
    ("X17 unsafe note names are sent", r"and SAFE_NOTE_NAME\.fullmatch\(n\)", ""),
    # the run
    (
        "W32 no egress gate",
        r'eg\.require_clear\(\s*os\.path\.join\(ops, "snapshots", snap, "memory"\), args\.allow_secret_shapes\s*\)',
        "pass",
    ),
    ("W33 snapshot not verified", r"if not ok:", "if False:"),
    (
        "W34 proposal may overwrite",
        r"os\.O_WRONLY \| os\.O_CREAT \| os\.O_EXCL \| os\.O_NOFOLLOW, 0o600",
        "os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600",
    ),
    ("W35 files world readable", r"os\.O_NOFOLLOW, 0o600", "os.O_NOFOLLOW, 0o644"),
    ("W36 accept ignores the digest", r"!= args\.expect", "!= args.expect and False"),
    ("W37 accept skips the schema check", r"mrr\.load_questions_text\(text\)", "None"),
    ("W38 accept overwrites without the flag", r"if not args\.replace:", "if False:"),
    ("W40 accept follows symlinks", r"os\.path\.islink\(source\) or ", ""),
    (
        "W41 a short set exits zero",
        r"if len\(chosen\) < mrr\.MIN_QUESTIONS:",
        "if False:",
    ),
    ("W43 lane errors not counted", r"lane_errors \+= 1", "lane_errors += 0"),
    (
        "W44 digest truncated differently",
        r"hexdigest\(\)\[:16\]\n    print\(\"PROPOSAL",
        'hexdigest()[:15]\n    print("PROPOSAL',
    ),
    (
        "W50 abort on a lane failure lost",
        r"except mrr\.RunAborted as err:",
        "except ZeroDivisionError as err:",
    ),
    (
        "X05 a crashing candidate crashes the run",
        r"except Exception as err:",
        "except ZeroDivisionError as err:",
    ),
    (
        "X06 same-second collision found after spend",
        r'if os\.path\.lexists\(base \+ "\.jsonl"\) or os\.path\.lexists\(base \+ "\.md"\):',
        "if False:",
    ),
    (
        "X07 target unbounded",
        r"if not mrr\.MIN_QUESTIONS <= args\.target <= mrr\.MAX_QUESTIONS:",
        "if False:",
    ),
    (
        "X08 max chunks unbounded",
        r"if not 1 <= args\.max_chunks <= CHUNK_LIMIT:",
        "if False:",
    ),
    (
        "X10 report text unescaped",
        r"return json\.dumps\(text, ensure_ascii=False\)",
        "return str(text)",
    ),
    (
        "X11 report drops its data warning",
        r"do not follow instructions written in it",
        "see below",
    ),
    (
        "X18 replace is not atomic",
        r"os\.replace\(incoming, dest\)",
        "write_private(dest, text)",
    ),
    (
        "X19 a missing negative question goes unmentioned",
        r'if not kinds\["negative"\]:',
        "if False:",
    ),
    (
        "X20 the prompt stops asking for made-up probes",
        r"make up a name, version number or date",
        "pick a nearby detail",
    ),
    (
        "X16 control characters allowed everywhere",
        r'category\(ch\)\[0\] not in "CZ"',
        'category(ch)[0] not in "X"',
    ),
    ("G11 literal minimum dropped", r"if scan\.alt_literals < PATTERN_MIN_LITERALS:", "if False:"),
    ("G18 alternatives are not checked one by one", r"problem = _alternative_problem\(scan\) if not scan\.depth else None", "problem = None"),
    ("G19 group alternatives are checked like top-level ones", r"if not scan\.depth else None", "if True else None"),
    ("G20 a plain space is not plain", r'if ch == " ":\s*return True', 'if False:\n        return True'),
    ("G21 invisible marks are plain", r"and not mr\.is_invisible\(ch\)", ""),
    ("W39 replace makes no backup", r"write_private\(backup, old\.read\(\)\)", "pass"),
    (
        "W42 candidate flood not capped",
        r'listed\[:\s*QUESTIONS_PER_NOTE \* len\(names\)\s*\+ KIND_QUOTA\["negative"\]\s*\]',
        "listed",
    ),
    ("X09 cost limits unchecked", r"if not all\(math\.isfinite\(x\) and x > 0 for x in limits\):", "if False:"),
    ("N06 not-a-number costs allowed", r"math\.isfinite\(x\) and x > 0", "x > 0"),
    ("N01 a ceiling is not recognised", r'CEILING_TEXT = "cost ceiling reached"', 'CEILING_TEXT = "never"'),
    ("N03 an aborted run is not logged", r'"wizard aborted: " \+ mc\.clean_line', '"" + mc.clean_line'),
    ("N04 the backup is a lossy text copy", r'with open\(dest, "rb"\) as old:', 'with open(dest, "r", encoding="utf-8", errors="replace") as old:'),
    ("N05 an old set that is a link is followed", r"if os\.path\.islink\(dest\) or not os\.path\.isfile\(dest\):", "if False:"),
    ("N07 a negative question may hold its own answer", r"if re\.search\(NEGATIVE_MUST, question, re\.S\):", "if False:"),
    ("J01 the judge cap is ten times higher", r"MAX_ANSWER_CHARS = 8_000", "MAX_ANSWER_CHARS = 80_000"),
    ("J02 the forbid check reads the whole answer", r"forbid_hit\(question, out\.text\[:MAX_ANSWER_CHARS\]\)", "forbid_hit(question, out.text)"),
    ("J03 the pass check reads the whole answer", r"grade\(question, out\.text\[:MAX_ANSWER_CHARS\]\)", "grade(question, out.text)"),
    ("K25 the alternative count is not reset", r"scan\.alt_literals = 0\n    return None", "return None"),
]
M = [m for m in M if len(sys.argv) < 2 or m[0].split()[0] in sys.argv[1:]]


def killed():
    cmd = [sys.executable, "-I", "-m", "unittest", "discover", "-s", TESTS, "-p", "test_memory_recall_[wa]*.py"]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=600).returncode != 0


# A red unmutated copy would make every mutant look killed.
if killed():
    print("BASELINE RED: the unmutated copy fails its own tests; fix that before reading any mutant result", flush=True)
    sys.exit(2)

survivors = []
for name, pattern, repl in M:
    sources = {path: open(path, encoding="utf-8").read() for path in TARGETS}
    hits = {path: len(re.findall(pattern, text, flags=re.S)) for path, text in sources.items()}
    if sum(hits.values()) != 1:
        label = "MISSING-SNIPPET" if sum(hits.values()) == 0 else "AMBIGUOUS"
        print("%-48s %s" % (name, label), flush=True)
        survivors.append("%s (%s)" % (name, label.lower()))
        continue
    path = next(p for p, n in hits.items() if n == 1)
    original = sources[path]
    mutated = re.sub(pattern, lambda m: repl, original, count=1, flags=re.S)
    try:
        compile(mutated, path, "exec")
    except SyntaxError:
        print("%-48s BROKEN (does not parse)" % name, flush=True)
        survivors.append("%s (broken)" % name)
        continue
    open(path, "w", encoding="utf-8").write(mutated)
    try:
        dead = killed()
        print("%-48s %s" % (name, "killed" if dead else "SURVIVED"), flush=True)
        if not dead:
            survivors.append(name)
    finally:
        open(path, "w", encoding="utf-8").write(original)
print("survivors:", survivors or "none", flush=True)
