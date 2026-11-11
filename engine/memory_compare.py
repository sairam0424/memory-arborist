"""Compare a baseline directory with a staged one: no line of 20+ characters may disappear. Read-only."""

import os
import sys
from collections import Counter, namedtuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memory_common as mc  # noqa: E402

COMPARE_MIN_LINE = 20
CompareResult = namedtuple("CompareResult", "lost baseline_files lines_checked")


def _eligible_lines(text):
    for number, raw in enumerate(text.split("\n"), start=1):
        line = raw.strip()
        if len(line) >= COMPARE_MIN_LINE and not line.startswith("modified:"):
            yield number, line


def compare_dirs(baseline_dir, staging_dir, files=None):
    """Every eligible baseline line must still exist somewhere in staging, counted as a multiset."""
    for directory in (baseline_dir, staging_dir):
        if not os.path.isdir(directory):
            raise mc.HygieneError("not a directory: %s" % directory)
    if mc.same_dir(baseline_dir, staging_dir):
        raise mc.HygieneError(
            "baseline and staging are the same directory; nothing to compare"
        )
    baseline_md = mc.list_md(baseline_dir)
    scope = baseline_md if files is None else list(files)
    unknown = [name for name in scope if name not in baseline_md]
    if unknown:
        raise mc.HygieneError("not in the baseline: %s" % ", ".join(unknown))
    pool = Counter()
    for rel in mc.list_md(staging_dir):
        pool.update(
            line
            for _n, line in _eligible_lines(
                mc.read_text(os.path.join(staging_dir, rel))
            )
        )
    lost, checked = [], 0
    for rel in scope:
        for number, line in _eligible_lines(
            mc.read_text(os.path.join(baseline_dir, rel))
        ):
            checked += 1
            if pool[line] > 0:
                pool[line] -= 1
            else:
                lost.append((rel, number))
    return CompareResult(lost, len(scope), checked)
