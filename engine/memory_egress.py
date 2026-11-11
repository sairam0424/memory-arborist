"""The gate in front of every call that can send note text to the model provider. Standard library only.

A fresh scan of the directory the model will be able to read, never the manifest's stored count (missing in older
snapshots, stale once patterns change, blind to skipped files). Block-tier secret shapes, or entries that could not be
scanned, refuse the run before any model call. Messages carry counts and file names only, never matched text. The
override is a command-line flag on purpose: an environment variable could be set by a project settings file.
"""

import os
import sys
from collections import Counter, namedtuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memory_common as mc  # noqa: E402
import memory_scan as ms  # noqa: E402

EXIT_SECRET_SHAPES = 4
FLAG = "--allow-secret-shapes"
FILES_LISTED = 10
MAX_NAME_CHARS = 80
WITHHELD = "<name withheld>"
Verdict = namedtuple(
    "Verdict", "block files skipped"
)  # shapes, ((file, shapes), ...), unscanned entries


class SecretShapesRefused(mc.HygieneError):
    """Raised before any model call; main() prints REFUSED and returns EXIT_SECRET_SHAPES."""


def scan_notes(directory):
    """Scan now. A directory that cannot even be listed is refused here, flag or not: the run could not read it either."""
    try:
        result = ms.scan_tree(directory)
    except OSError as err:
        raise SecretShapesRefused(
            "could not scan the notes (%s), so they cannot be shown to be free of secret shapes; nothing was sent"
            % type(err).__name__
        )
    per_file = Counter(hit.path for hit in result.hits if hit.tier == "block")
    return Verdict(
        sum(per_file.values()), tuple(sorted(per_file.items())), len(result.skipped)
    )


def _shapes(count):
    return "%d block-tier secret shape%s" % (count, "" if count == 1 else "s")


def _unscanned(count):
    unit = "entry (file or link)" if count == 1 else "entries (files or links)"
    return "%d %s could not be scanned" % (count, unit)


def _name(name):
    """A file name safe to print: a long or secret-shaped one is withheld."""
    shown = mc.printable(name)
    return WITHHELD if ms.safe_snippet(shown, MAX_NAME_CHARS) != shown else shown


def _listing(files):
    shown = ", ".join(
        "%s (%d)" % (_name(name), count) for name, count in files[:FILES_LISTED]
    )
    extra = len(files) - FILES_LISTED
    return shown + (
        ", +%d more file%s" % (extra, "" if extra == 1 else "s") if extra > 0 else ""
    )


def _found(verdict):
    found = []
    if verdict.block:
        count = len(verdict.files)
        found.append(
            "%s in %d file%s: %s"
            % (_shapes(verdict.block), count, "" if count == 1 else "s", _listing(verdict.files))
        )
    if verdict.skipped:
        found.append(_unscanned(verdict.skipped))
    return "; ".join(found)


def _refusal(verdict):
    advice = (
        "Remove the text from the note and take a fresh snapshot (memory_hygiene.py scan shows file:line:pattern for "
        "the live notes)"
    )
    if verdict.skipped:
        advice += "; for entries that could not be scanned, remove the symlink or fix the permissions"
    return (
        "the notes cannot be sent: %s. Nothing was sent to the model provider. %s, or pass %s if you accept sending it."
        % (_found(verdict), advice, FLAG)
    )


def require_clear(directory, allow=False):
    """The Verdict when the notes may be sent; raises SecretShapesRefused otherwise. With `allow`, one WARN line instead."""
    verdict = scan_notes(directory)
    if not (verdict.block or verdict.skipped):
        return verdict
    if not allow:
        raise SecretShapesRefused(_refusal(verdict))
    print("WARN %s: sending notes that were flagged: %s" % (FLAG, _found(verdict)))
    return verdict
