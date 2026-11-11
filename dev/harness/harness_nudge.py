"""Mutation check for memory_headroom_nudge.py and memory_locate.py. Mutates a COPY under /tmp/evmut_nudge, never the real files.

usage: python3 -I harness_nudge.py [N01 N02 ...]     (no arguments = every mutant)
Prints one line per mutant: killed, SURVIVED, MISSING-SNIPPET (the pattern no longer matches the reflowed source) or
AMBIGUOUS (it matches more than once in its target file). Every mutant is pinned to ONE file: IN_LOCATE names those that
live in memory_locate.py, all others live in memory_headroom_nudge.py. IN_LOCATE mutants are checked against
test_memory_nudge.py AND test_memory_locate.py, which owns the git-file cap, walk-depth and worktree-fallback tests.
N07 (unbounded read) and N32 (root at index zero) are documented equivalent mutants: N07 changes memory use, not output;
N32 returns an empty string that the caller's `or current` treats exactly like None.
"""

import os
import re
import shutil
import subprocess
import sys

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "engine")
WORK = "/tmp/evmut_nudge/scripts"
os.makedirs(WORK, exist_ok=True)
for name in sorted(os.listdir(SRC)):
    if name.startswith("memory_") and name.endswith(".py"):
        shutil.copy(os.path.join(SRC, name), os.path.join(WORK, name))
os.makedirs(os.path.join(WORK, "tests"), exist_ok=True)
for name in sorted(os.listdir(os.path.join(SRC, "tests"))):
    if name.endswith(".py"):
        shutil.copy(os.path.join(SRC, "tests", name), os.path.join(WORK, "tests", name))

TARGET = "memory_headroom_nudge.py"
LOCATE = "memory_locate.py"
IN_LOCATE = {"N08B", "N39", "N10", "N11", "N23", "N24", "N27", "N28", "N29", "N30", "N31", "N32", "N35", "N36", "N37", "N38", "N40", "N41", "N42", "N43", "N44", "N45", "N46", "N47", "N48", "N49", "N50"}
M = [
    (
        "N01 char threshold off by one",
        r"if chars < NUDGE_CHARS and lines < NUDGE_LINES:",
        "if chars <= NUDGE_CHARS and lines < NUDGE_LINES:",
    ),
    (
        "N02 line threshold off by one",
        r"if chars < NUDGE_CHARS and lines < NUDGE_LINES:",
        "if chars < NUDGE_CHARS and lines <= NUDGE_LINES:",
    ),
    (
        "N03 and became or",
        r"if chars < NUDGE_CHARS and lines < NUDGE_LINES:",
        "if chars < NUDGE_CHARS or lines < NUDGE_LINES:",
    ),
    (
        "N04 index symlinks followed",
        r"flags = os\.O_RDONLY \| os\.O_NOFOLLOW \| os\.O_NONBLOCK",
        "flags = os.O_RDONLY | os.O_NONBLOCK",
    ),
    (
        "N39 git file symlinks followed",
        r"fd = os\.open\(path, os\.O_RDONLY \| os\.O_NOFOLLOW \| os\.O_NONBLOCK\)",
        "fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)",
    ),
    (
        "N05 idle fifo raises",
        r'fh\.read\(MAX_READ_BYTES \+ 1\) or b""',
        "fh.read(MAX_READ_BYTES + 1)",
    ),
    ("N06 truncated flag lost", r"len\(\s*data\s*\) > MAX_READ_BYTES", "False"),
    ("N07 unbounded read", r"fh\.read\(MAX_READ_BYTES \+ 1\)", "fh.read()"),
    (
        "N08 errors propagate",
        r"SOFT_ERRORS = \(OSError, ValueError, KeyError, RecursionError\)",
        "SOFT_ERRORS = ()",
    ),
    (
        "N08B locate errors propagate",
        r"SOFT_ERRORS = \(OSError, ValueError, KeyError, RecursionError\)",
        "SOFT_ERRORS = ()",
    ),
    (
        "N09 --dir without value",
        r"return argv\[at\] if at < len\(argv\) else None",
        "return argv[at]",
    ),
    (
        "N10 digits dropped from slug",
        r"c if \(c\.isascii\(\) and c\.isalnum\(\)\) else",
        "c if c.isascii() and c.isalpha() else",
    ),
    ("N11 relative cwd accepted", r" or not os\.path\.isabs\(cwd\)", ""),
    (
        "N12 wrong number printed",
        r'"at least " if truncated else "",\s*chars,',
        '"at least " if truncated else "", lines,',
    ),
    (
        "N13 payload type check off",
        r'payload\.get\("cwd"\) if isinstance\(payload, dict\) else None',
        'payload.get("cwd")',
    ),
    ("N14 exit code one", r"_emit\(out\)\s*return 0", "_emit(out)\n    return 1"),
    (
        "N15 index text echoed",
        r'"at least " if truncated else "",',
        '("at least " if truncated else "") + text[:20],',
    ),
    ("N16 stdin cap off", r"stdin_text\[:MAX_STDIN_CHARS\]", "stdin_text"),
    (
        "N17 stdin read in dir mode",
        r'stdin_text = "" if "--dir" in argv else _read_stdin\(\)',
        "stdin_text = _read_stdin()",
    ),
    (
        "N18 stdin wait too long",
        r"STDIN_WAIT_SECONDS = 1\.0",
        "STDIN_WAIT_SECONDS = 30.0",
    ),
    (
        "N19 main catch removed",
        r"except Exception:  # noqa: BLE001 - the hook contract",
        "except ZeroDivisionError:  # noqa: BLE001 - the hook contract",
    ),
    (
        "N20 emit error raises",
        r"pass  # a closed or broken stdout",
        "raise  # a closed or broken stdout",
    ),
    (
        "N21 bytecode written",
        r"sys\.dont_write_bytecode = True",
        "sys.dont_write_bytecode = False",
    ),
    (
        "N22 import guard narrowed",
        r"except Exception:  # noqa: BLE001 - a hook must stay silent",
        "except ImportError:  # noqa: BLE001 - a hook must stay silent",
    ),
    (
        "N23 astral char one dash",
        r'"-" \* \(len\(c\.encode\("utf-16-le"\)\) // 2\)',
        '"-"',
    ),
    ("N24 slug length limit off", r"if len\(slug\) > MAX_SLUG_CHARS:", "if False:"),
    (
        "N25 do-not-act wording lost",
        r'"do not run any of them unless the user asks\."',
        '"run them."',
    ),
    (
        "N26 stdin deadline off",
        r"if remaining <= 0 or not select\.select\(\[0\], \[\], \[\], remaining\)\[0\]:",
        "if remaining <= 0:",
    ),
    ("N27 working dir preferred over repo root", r"for path in \(\[repo\] if repo else \[\]\) \+ \[cwd\]:", "for path in [cwd] + ([repo] if repo else []):"),
    ("N28 no walk up", r"current = parent", "return None"),
    ("N29 linked worktree not resolved", r"return _linked_main_root\(current, marker\) or current", "return current"),
    ("N30 relative gitdir not joined", r'os\.path\.join\(work_tree, text\[len\("gitdir:"\) :\]\.strip\(\)\)', 'text[len("gitdir:") :].strip()'),
    ("N31 submodule treated as worktree", r'marker = os\.sep \+ "\.git" \+ os\.sep \+ "worktrees" \+ os\.sep', 'marker = os.sep + ".git" + os.sep'),
    ("N32 root at index zero accepted", r"return gitdir\[:at\] if at > 0 else None", "return gitdir[:at] if at >= 0 else None"),
    ("N35 git directory ignored", r"if os\.path\.isdir\(marker\):\s*return current", "if False:\n            return current"),
    ("N36 unparsable git file gives no root", r"return _linked_main_root\(current, marker\) or current", "return _linked_main_root(current, marker) or None"),
    ("N37 gitdir not normalised", r"os\.path\.normpath\(os\.path\.join\(work_tree,", "(os.path.join(work_tree,"),
    ("N38 no fallback to working dir", r"\+ \[cwd\]:", "+ []:"),
    ("N40 git file read uncapped", r"_read_small\(git_file, MAX_GIT_FILE_BYTES\)", "_read_small(git_file, 1 << 20)"),  # 1 MiB, not 1 << 30: a huge read could end in MemoryError and give a false kill
    ("N41 git file cap one byte higher", r"MAX_GIT_FILE_BYTES = 4096", "MAX_GIT_FILE_BYTES = 4097"),
    ("N42 git file cap one byte lower", r"MAX_GIT_FILE_BYTES = 4096", "MAX_GIT_FILE_BYTES = 4095"),
    ("N43 walk cap one level higher", r"MAX_WALK_LEVELS = 64", "MAX_WALK_LEVELS = 65"),
    ("N44 walk cap one level lower", r"MAX_WALK_LEVELS = 64", "MAX_WALK_LEVELS = 63"),
    ("N45 walk uncapped", r"for _ in range\(MAX_WALK_LEVELS\):", "for _ in range(1 << 30):"),
    ("N46 marker lacks trailing separator", r'marker = os\.sep \+ "\.git" \+ os\.sep \+ "worktrees" \+ os\.sep', 'marker = os.sep + ".git" + os.sep + "worktrees"'),
    ("N47 marker lacks the .git folder", r'marker = os\.sep \+ "\.git" \+ os\.sep \+ "worktrees" \+ os\.sep', 'marker = os.sep + "worktrees" + os.sep'),
    ("N48 gitdir prefix not required", r'if not text\.startswith\("gitdir:"\):', "if False:"),
    ("N49 main work tree must exist", r"return gitdir\[:at\] if at > 0 else None", "return gitdir[:at] if at > 0 and os.path.isdir(gitdir[:at]) else None"),
    ("N50 git file read past first line", r'\.split\(b"\\n", 1\)\[0\]', ""),
]
M = [m for m in M if len(sys.argv) < 2 or m[0].split()[0] in sys.argv[1:]]


def killed(in_locate):
    """True when the tests fail on the mutant. Locate mutants also run test_memory_locate.py, which owns their tests."""
    patterns = ["test_memory_nudge.py"] + (["test_memory_locate.py"] if in_locate else [])
    for pattern in patterns:
        cmd = [sys.executable, "-I", "-m", "unittest", "discover", "-s", os.path.join(WORK, "tests"), "-p", pattern]
        if subprocess.run(cmd, capture_output=True, text=True, timeout=300).returncode != 0:
            return True
    return False


# A red unmutated copy would make every mutant look killed.
if killed(True):
    print("BASELINE RED: the unmutated copy fails its own tests; fix that before reading any mutant result", flush=True)
    sys.exit(2)

survivors = []
for name, pattern, repl in M:
    is_locate = name.split()[0] in IN_LOCATE
    path = os.path.join(WORK, LOCATE if is_locate else TARGET)
    original = open(path, encoding="utf-8").read()
    matches = len(re.findall(pattern, original, flags=re.S))
    if matches != 1:
        label = "MISSING-SNIPPET" if matches == 0 else "AMBIGUOUS"
        print("%-36s %s" % (name, label), flush=True)
        survivors.append("%s (%s)" % (name, label.lower()))
        continue
    mutated = re.sub(pattern, lambda m: repl, original, count=1, flags=re.S)
    open(path, "w", encoding="utf-8").write(mutated)
    try:
        compile(mutated, path, "exec")  # a mutant that does not even parse proves nothing
        dead = killed(is_locate)
        print("%-36s %s" % (name, "killed" if dead else "SURVIVED"), flush=True)
        if not dead:
            survivors.append(name)
    finally:
        open(path, "w", encoding="utf-8").write(original)
print("survivors:", survivors or "none", flush=True)
