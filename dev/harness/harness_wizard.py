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
TARGETS = [os.path.join(WORK, "recall", name) for name in ("memory_recall_wizard.py", "memory_recall_keys.py", "memory_recall_run.py")] + [os.path.join(WORK, "dream", "memory_dream_lanes.py")]
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
    # the key grammar
    ("G01 empty or repeated group branches allowed", r"if any\(not b for b in lowered\) or len\(set\(lowered\)\) != len\(lowered\):", "if False:"),
    ("G02 a branch may be the start of another", r"if any\(a != b and b\.startswith\(a\) for a in lowered for b in lowered\):", "if False:"),
    ("G03 branches compared case-sensitively", r"lowered = \[b\.lower\(\) for b in branches\]", "lowered = list(branches)"),
    ("G04 nested groups allowed", r'elif c == "\(":\s*return None, "nested groups are not allowed", i', 'elif c == "(":\n            current.append(c)'),
    ("G05 any character inside a group", r"elif c in GROUP_META:", "elif False:"),
    ("G06 any escape inside a group", r"if i \+ 1 >= len\(body\) or body\[i \+ 1\] not in ESCAPABLE:", "if i + 1 >= len(body):"),
    ("G07 anything may follow an open-ended repeat", r'if self\.open_pending and c != "\|":', "if False:"),
    ("G08 an alternative bar does not end the pending repeat", r"self\.last, self\.last_weight, self\.open_pending = None, 0, False", "self.last, self.last_weight = None, 0"),
    ("G09 counted repeats allowed", r'if c == "\{":\s*return "a repeat count is not allowed"', 'if False:\n            return ""'),
    ("G10 anchors allowed", r'if c in "\^\$\]\}":', 'if c in "]}":'),
    ("G11 a stray closing parenthesis allowed", r'if c == "\)":\s*return "unbalanced parenthesis"', 'if False:\n            return ""'),
    ("G12 every escape allowed", r"if escaped in ESCAPABLE:", "if True:"),
    ("G13 escapes allowed inside classes", r'or "\\\\" in inner', ""),
    ("G14 negated classes allowed", r'or inner\.startswith\("\^"\)', ""),
    ("G15 groups uncapped", r"if self\.groups > MAX_GROUPS:", "if False:"),
    ("G16 optional marks uncapped", r"if self\.optionals > MAX_OPTIONAL_MARKS:", "if False:"),
    ("G17 open-ended repeats uncapped", r"if self\.opens > MAX_OPEN_REPEATS:", "if False:"),
    ("G18 optional mark on anything", r'if self\.last not in \("char", "digit", "space"\):', "if False:"),
    ("G19 open-ended repeat on anything", r'if self\.last not in \("digit", "space"\):', "if False:"),
    ("G20 an optional character still counts as mandatory", r"self\.mandatory -= self\.last_weight", "pass"),
    ("G21 an optional character still counts as outside", r"self\.outside -= self\.last_weight", "pass"),
    ("G22 a group counts its longest branch", r"min\(sum\(1 for ch in b if ch\.isalnum\(\)\) for b in branches\)", "max(sum(1 for ch in b if ch.isalnum()) for b in branches)"),
    ("G23 literals outside groups not required", r"or self\.outside < PATTERN_MIN_OUTSIDE", ""),
    ("G24 mandatory literals not required", r"self\.mandatory < PATTERN_MIN_LITERALS", "False"),
    ("G25 the counts are not reset for the next alternative", r"self\.mandatory = self\.outside = 0", "pass"),
    ("G26 a non-capturing group is refused", r'if body\.startswith\("\(\?:", self\.i\):', "if False:"),
    ("G27 group flags and lookarounds allowed", r'elif body\.startswith\("\(\?", self\.i\):', "elif False:"),
    ("G28 compile errors not caught", r"except re\.error:", "except ZeroDivisionError:"),
    ("G29 class length uncapped", r"or end - self\.i > 41", ""),
    ("G30 a class may hold a bracket", r'or "\[" in inner', ""),
    ("G31 unbalanced group at the end allowed", r'return None, "unbalanced parenthesis", i', "return branches, None, i"),
    ("X18 replace is not atomic", r"os\.replace\(incoming, dest\)", "write_private(dest, data)"),
    ("N09 accept may overwrite a set that appeared meanwhile", r"write_private\(dest, data\)  # create-only", "_swap_in(dest, data)  # create-only"),
    ("N10 the proposal name may collide", r"base = _free_base\(base\)", "pass"),
    ("N11 a failed write is not logged", r'"wizard could not write its proposal: "', '""'),
    ("N12 coverage counts planned batches", r"asked = \[c for c, r in zip\(chunks, replies\) if r is not CEILING\]", "asked = list(chunks)"),
    ("N13 a tools report keeps the first stop reason", r"raise mrr\.ToolsMismatch\(reason\)\n    if out\.is_error", "raise mrr.ToolsMismatch(spend.stopped)\n    if out.is_error"),
    ("N14 the judge reads universal newlines differently", r'text = re\.sub\(r"\\r\\n\?", "\\n", text\)', "pass"),
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
