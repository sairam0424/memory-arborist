"""Promote attempt markers: how a promote that was killed (SIGKILL, a power cut) is found and explained by the next promote of the same run.

A promote writes promote-started.json (the notes it is going to replace, the pre-promote snapshot) before it replaces the first note, and exactly
one outcome marker when it ends (promoted.json for a promote that replaced notes, whole or partly; promote-failed.json for one that replaced
nothing). A started marker with no outcome after it is a promote that died. Every marker is written complete or not at all. Standard library only.
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memory_common as mc  # noqa: E402
import memory_dream_stage as ds  # noqa: E402
import memory_registry as reg  # noqa: E402
import memory_snapshot as sn  # noqa: E402

STARTED_MARKER, PROMOTED_MARKER, FAILED_MARKER = "promote-started.json", "promoted.json", "promote-failed.json"


def write_marker_file(path, data):
    """A marker is complete or absent, never torn, and on disk before the caller goes on (sn._write_json_private: temp file, fsync, one os.replace,
    folder fsync). The caller holds the lock, so nobody takes the name between the check and the move. FileExistsError if the name is taken."""
    if os.path.lexists(path):
        raise FileExistsError("marker exists: " + path)
    sn._write_json_private(path, data)


def _markers(run_dir, base):
    """The marker files of one kind (base, base.1, base.2 ...) in the order they were written."""
    found = [n for n in os.listdir(run_dir) if re.fullmatch(re.escape(base) + r"(\.[0-9]+)?", n)]
    return sorted(found, key=lambda n: int(n[len(base) + 1 :] or 0))


def _outcomes(run_dir):
    return len(_markers(run_dir, FAILED_MARKER)) + len(_markers(run_dir, PROMOTED_MARKER))


def _open_attempt(run_dir):
    """(name, data or None when unreadable) of the newest started marker if no outcome marker was written after it began, else None. Each started
    marker records how many outcome markers existed when it began, so an outcome left by an older release (a failed marker with no started marker
    beside it) cannot be taken for the end of a newer attempt."""
    started = _markers(run_dir, STARTED_MARKER)
    if not started:
        return None
    try:
        data = json.loads(mc.read_text(os.path.join(run_dir, started[-1])))
    except (OSError, ValueError, RecursionError):
        data = None
    data = data if isinstance(data, dict) else None
    seen = data.get("outcomes") if data else None
    if isinstance(seen, int) and not isinstance(seen, bool) and _outcomes(run_dir) > seen:
        return None
    return started[-1], data


def dangling_attempts(run_dir):
    """1 when a promote of this run began and never recorded how it ended, else 0. A promote that is killed (SIGKILL, a power cut) cannot write
    anything more, so a started marker with no outcome after it is the only trace it leaves."""
    return 0 if _open_attempt(run_dir) is None else 1


def _in_place(run_dir, memory, rel, manifest):
    """True when the live note is the staged copy the promote installs (and not the snapshot's text): a note edited later is neither."""
    entry, live = manifest["files"].get(rel), os.path.join(memory, rel)
    if not (entry and os.path.isfile(live) and not os.path.islink(live)):
        return False
    digest = mc.sha256_file(live)
    return digest != entry["sha256"] and digest == mc.sha256_file(os.path.join(run_dir, "memory", rel))


def _died_problem(run_dir, memory, manifest):
    """None when no attempt is left without an outcome, or when none of its notes is in place; else why the run must not go on."""
    attempt = _open_attempt(run_dir)
    if attempt is None:
        return None
    name, data = attempt
    files = data.get("files") if data else None
    pre = data.get("pre_snapshot") if data else None
    if not (isinstance(files, list) and all(isinstance(f, str) for f in files) and isinstance(pre, str)):
        return (
            "an earlier promote of this run began and did not finish, and its record (%s) cannot be read, so it is not known whether notes were "
            "replaced: check the notes and `memory_hygiene.py snapshots` before doing anything else" % mc.printable(name)
        )
    placed = [rel for rel in files if _in_place(run_dir, memory, rel, manifest)]
    if not placed:
        return None
    return (
        "an earlier promote of this run did not finish: it was killed, or the machine stopped, before it recorded how it ended. It was going to replace "
        "%s; in place as that promote left them: %s (old copies in pre-promote snapshot %s). To undo, run "
        "`memory_hygiene.py restore --snapshot %s --file <note> --apply --force` for each of those notes; then promote this run again, or take a fresh "
        "snapshot (step 1) and run the dream again (step 4)"
        % (", ".join(map(mc.printable, files)), ", ".join(map(mc.printable, placed)), mc.printable(pre), mc.printable(pre))
    )


def refuse_if_attempt_died(run_dir, memory, ops, manifest):
    """Dry run or apply: an earlier attempt that died with notes in place stops the run, and says what to do. A promote that is running right
    now holds the lock and is not a dead one, so the lock is the test."""
    if dangling_attempts(run_dir):
        with reg.ops_lock(ops):
            problem = _died_problem(run_dir, memory, manifest)
        if problem:
            raise mc.HygieneError(problem)


def begin_attempt(run_dir, memory, manifest, files, pre_snapshot):
    """With the lock held and before the first note is replaced: refuse if an earlier attempt died with notes in place (checked again here,
    under the lock), close one that died with none in place, and record that this attempt began (durably: see write_marker_file)."""
    problem = _died_problem(run_dir, memory, manifest)
    if problem:
        raise mc.HygieneError(problem)
    try:
        dead = _open_attempt(run_dir)
        if dead:
            error = (
                "an earlier attempt did not finish (killed, or the machine stopped, before it recorded how it ended); when this attempt began none of "
                "its notes was in place (nothing had moved, or the notes were restored since)"
            )
            write_marker_file(mc.unique_path(os.path.join(run_dir, FAILED_MARKER)), {"status": "died-nothing-in-place", "files": [], "error": error, "closes": dead[0]})
            ds.say("an earlier promote of this run did not finish; none of its notes is in place now (nothing had moved, or they were restored): recorded as failed, going on")
        started = {"status": "started", "files": list(files), "pre_snapshot": pre_snapshot, "outcomes": _outcomes(run_dir)}
        write_marker_file(mc.unique_path(os.path.join(run_dir, STARTED_MARKER)), started)
    except OSError as err:
        raise mc.HygieneError(
            "nothing was replaced: this attempt could not be recorded in the run folder (%s)" % mc.clean_line(err.strerror or type(err).__name__, 80)
        )
