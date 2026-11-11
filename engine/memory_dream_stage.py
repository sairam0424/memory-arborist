"""Stage accepted dream hunks in a copy outside the memory directory, check the copy, write a report. Standard library only.

A run lives in <ops>/dream/<UTC stamp>/ (mode 0700, write-once): memory/ is a copy of a verified snapshot
with the accepted hunks applied; report.md and hunks.*.json describe what was and was not accepted.
Nothing here touches the live memory directory and nothing is deleted or moved. The checks never trust the
staged copy: they rebuild what it must contain (snapshot plus hunks) and compare byte for byte.
"""

import hashlib
import json
import os
import shutil
import sys
from collections import Counter, namedtuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memory_common as mc  # noqa: E402
import memory_lint as ml  # noqa: E402
import memory_scan as ms  # noqa: E402
import memory_snapshot as sn  # noqa: E402

PRIVATE_FILE_MODE = 0o600
StageResult = namedtuple("StageResult", "run_dir ok problems digest")


def _write_new(path, data):
    fd = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, PRIVATE_FILE_MODE
    )
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def _copy_tree(source, dest):
    for rel in mc.list_files(source):
        target = os.path.join(dest, rel)
        mc.makedirs_private(os.path.dirname(target))
        shutil.copyfile(os.path.join(source, rel), target)
        os.chmod(target, PRIVATE_FILE_MODE)


def _strict_text(path):
    with open(path, "rb") as fh:
        try:
            return fh.read().decode("utf-8")
        except UnicodeDecodeError:
            raise mc.HygieneError(
                "%s is not valid UTF-8 and cannot be edited"
                % mc.printable(os.path.basename(path))
            )


def text_with_hunks(text, hunks):
    """The note text after the hunks, applied last position first so earlier offsets stay valid."""
    for hunk in sorted(hunks, key=lambda h: h["start"], reverse=True):
        start, old = hunk["start"], hunk["old"]
        if text[start : start + len(old)] != old:
            raise mc.HygieneError(
                "hunk %s does not sit at its recorded position in %s"
                % (hunk["id"], mc.printable(hunk["file"]))
            )
        text = text[:start] + hunk["new"] + text[start + len(old) :]
    return text


def _by_file(accepted):
    grouped = {}
    for hunk in accepted:
        grouped.setdefault(hunk["file"], []).append(hunk)
    return grouped


def apply_hunks(staged_dir, accepted):
    for rel, hunks in _by_file(accepted).items():
        path = os.path.join(staged_dir, rel)
        data = text_with_hunks(_strict_text(path), hunks).encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)


# ------------------------------------------------------------------ checks on the staged copy


def staged_differs_from_expected(snapshot_dir, staged_dir, accepted):
    """Problems if the staged tree is not exactly snapshot plus hunks (extra, missing, linked or altered files)."""
    problems = []
    walk = mc.walk_files(staged_dir)
    if walk.symlinks or walk.special or walk.errors:
        problems.append("the staged copy holds links or special files")
    expected = set(mc.list_files(snapshot_dir))
    if set(walk.files) != expected:
        problems.append("the staged copy has extra or missing files")
    grouped = _by_file(accepted)
    for rel in sorted(expected & set(walk.files)):
        path = os.path.join(snapshot_dir, rel)
        want = (
            text_with_hunks(_strict_text(path), grouped[rel]).encode("utf-8")
            if rel in grouped
            else None
        )
        got = mc.sha256_file(os.path.join(staged_dir, rel))
        if got != (mc.sha256_bytes(want) if want is not None else mc.sha256_file(path)):
            problems.append("%s differs from snapshot plus hunks" % mc.printable(rel))
    return problems


def _error_counts(directory):
    """How many blocking findings of each (code, path) a tree has; one file can hold several of one kind."""
    return Counter(
        (f.code, f.path)
        for f in ml.lint_dir(directory).findings
        if ml.blocks_staging(f)
    )


def new_lint_errors(snapshot_dir, staged_dir):
    before, after = _error_counts(snapshot_dir), _error_counts(staged_dir)
    return sorted(key for key, count in after.items() if count > before[key])


def _block_keys(directory):
    return {
        (hit.path, hit.pattern)
        for hit in ms.scan_tree(directory).hits
        if hit.tier == "block"
    }


def new_block_hits(snapshot_dir, staged_dir):
    return sorted(_block_keys(staged_dir) - _block_keys(snapshot_dir))


def run_checks(snapshot_dir, staged_dir, accepted):
    problems = staged_differs_from_expected(snapshot_dir, staged_dir, accepted)
    if problems:
        return problems  # nothing else about a tampered tree is worth trusting
    errors = new_lint_errors(snapshot_dir, staged_dir)
    if errors:
        problems.append(
            "new lint errors: %d (first: %s in %s)"
            % (len(errors), errors[0][0], mc.printable(errors[0][1]))
        )
    blocks = new_block_hits(snapshot_dir, staged_dir)
    if blocks:
        problems.append(
            "new secret-shaped text: %d (first: %s in %s)"
            % (len(blocks), blocks[0][1], mc.printable(blocks[0][0]))
        )
    return problems


def run_digest(staged_dir, accepted):
    """One hex digest over the accepted hunks and the bytes of every touched staged note: what a human's yes is bound to."""
    clean = [
        {k: h[k] for k in sorted(h) if k != "score"}
        for h in sorted(accepted, key=lambda h: h["id"])
    ]
    parts = [json.dumps(clean, sort_keys=True, ensure_ascii=True)]
    parts += [
        "%s %s" % (rel, mc.sha256_file(os.path.join(staged_dir, rel)))
        for rel in sorted(_by_file(accepted))
    ]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ the report


def _fence(text):
    longest, run = 0, 0
    for char in text:
        run = run + 1 if char == "`" else 0
        longest = max(longest, run)
    return "`" * max(3, longest + 1)


def _block(text):
    fence = _fence(text)
    return "%s\n%s\n%s" % (fence, text, fence)


def _hunk_section(hunk):
    evidence = "\n".join(
        '  - session %s: "%s"' % (e["session"], mc.printable(e["quote"]))
        for e in hunk["evidence"]
    )
    return (
        "### %s  %s  (%s)\n\nWhy: %s\n\nEvidence:\n%s\n\nOld:\n\n%s\n\nNew:\n\n%s\n"
        % (
            mc.printable(hunk["id"]),
            mc.printable(hunk["file"]),
            hunk["kind"],
            mc.printable(hunk["reason"]),
            evidence,
            _block(hunk["old"]),
            _block(hunk["new"]),
        )
    )


def build_report(run_id, snap_id, accepted, rejected, deferred, problems, digest):
    parts = [
        "# Dream run %s\n\nSnapshot: %s\n\nRun digest: %s\n\nAccepted hunks: %d. Rejected: %d. Deferred past the cap: %d.\n"
        % (run_id, snap_id, digest, len(accepted), len(rejected), len(deferred)),
        "## Checks on the staged copy\n\n"
        + (
            "\n".join("- PROBLEM: " + p for p in problems)
            if problems
            else "- all clean: the staged copy equals snapshot plus hunks, no new lint errors, no new secret-shaped text"
        )
        + "\n",
        "## Accepted hunks\n\n"
        + ("\n".join(_hunk_section(h) for h in accepted) or "none\n"),
        "## Rejected hunks\n\n"
        + (
            "\n".join(
                "- %s: %s" % (mc.printable(i), mc.printable(why)) for i, why in rejected
            )
            or "none"
        )
        + "\n",
        "## Deferred hunks\n\n"
        + (
            "\n".join(
                "- %s (%s)" % (mc.printable(h["id"]), mc.printable(h["file"]))
                for h in deferred
            )
            or "none"
        )
        + "\n",
    ]
    return "\n".join(parts)


# ------------------------------------------------------------------ the run


def stage_run(ops, snap_id, accepted, rejected, deferred, now=None):
    snapshot_dir = os.path.join(ops, "snapshots", snap_id, "memory")
    ok, _count, problems = sn.verify_snapshot(ops, snap_id)
    if not ok:
        raise mc.HygieneError(
            "snapshot %s failed verification (%d problems); take a fresh one"
            % (snap_id, len(problems))
        )
    run_id = mc.utc_stamp(now)
    run_dir = os.path.join(ops, "dream", run_id)
    if os.path.lexists(run_dir):
        raise mc.HygieneError(
            "a dream run named %s already exists; runs are write-once" % run_id
        )
    mc.makedirs_private(os.path.join(run_dir, "memory"))
    staged_dir = os.path.join(run_dir, "memory")
    _copy_tree(snapshot_dir, staged_dir)
    apply_hunks(staged_dir, accepted)
    found = run_checks(snapshot_dir, staged_dir, accepted)
    digest = run_digest(staged_dir, accepted)
    files = {
        "hunks.accepted.json": json.dumps(accepted, indent=1, sort_keys=True),
        "hunks.rejected.json": json.dumps(
            [list(r) for r in rejected], indent=1, sort_keys=True
        ),
        "hunks.deferred.json": json.dumps(deferred, indent=1, sort_keys=True),
        "report.md": build_report(
            run_id, snap_id, accepted, rejected, deferred, found, digest
        ),
    }
    encoded = {
        name: text.encode("utf-8") for name, text in files.items()
    }  # fails before anything is written
    for name, data in encoded.items():
        _write_new(os.path.join(run_dir, name), data)
    return StageResult(run_dir, not found, found, digest)
