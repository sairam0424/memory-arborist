"""Leak audit for the memory test suite: run each tests/test_memory_*.py file on its own and count the new entries it leaves
in the system temp folder. A healthy suite prints "total new: 0" (since 2026-11-09 tests/memtest_helpers.py gives each test
process one private temp root that is removed at exit; before that one full run left about 580 folders).

usage: python3 -I leak_audit_per_file.py
Read-only except for what the tests themselves create; it deletes nothing.
"""

import collections
import os
import subprocess
import sys
import tempfile

T = tempfile.gettempdir()
TESTS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "engine", "tests")
files = sorted(
    f for f in os.listdir(TESTS) if f.startswith("test_memory_") and f.endswith(".py")
)
results = []
for name in files:
    before = set(os.listdir(T))
    subprocess.run(
        [sys.executable, "-I", "-m", "unittest", "discover", "-s", TESTS, "-p", name],
        capture_output=True,
        timeout=600,
    )
    new = set(os.listdir(T)) - before
    prefixes = collections.Counter(
        n.rstrip("0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")[:14]
        or n[:6]
        for n in new
    )
    results.append((len(new), name, prefixes.most_common(3)))
for count, name, common in sorted(results, reverse=True):
    if count:
        print("%5d  %-46s %s" % (count, name, common))
print(
    "files with no leak: %d of %d"
    % (sum(1 for r in results if r[0] == 0), len(results))
)
print("total new:", sum(r[0] for r in results))
