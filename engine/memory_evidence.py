#!/usr/bin/env python3
"""Evidence digests of past Claude Code sessions for the dream phase. Standard library only; run as python3 -I.

Reads top-level session transcripts (<uuid>.jsonl, never subagent files, symlinks or sessions that are
still active) and writes ONE small write-once digest per session under <data root>/<project>/evidence/.
The digest rules live in memory_digest.py, the redaction in memory_redact.py. The tool never deletes:
purge-plan only prints a command for the user to run.
Commands: status, scan-sources, build, verify, purge-plan. Exit codes: 0 ok; 1 failure or a verify problem; 64 usage.
"""

import argparse
import math
import os
import re
import shlex
import sys
import time
from collections import Counter, namedtuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memory_common as mc  # noqa: E402
import memory_redact as mr  # noqa: E402
from memory_digest import (  # noqa: E402,F401  (re-exported for callers and tests)
    HEADER,
    LINE_FORMAT,
    MAX_DIGEST_BYTES,
    Digest,
    DEFAULT_ENTRYPOINTS,
    build_digest,
    is_human_session,
)
import memory_evidence_notes as en  # noqa: E402
from memory_evidence_verify import (  # noqa: E402,F401  (re-exported for callers and tests)
    HEADER_LINES,
    MAX_BAD_NAMES,
    MAX_LINE_CHARS,
    UUID,
    Verify,
    failure_text,
    verify_dir,
    would_verify,
)

MAX_TOTAL_BYTES = 524288
MAX_FILE_BYTES = 1024 * 1024 * 1024
QUIET_SECONDS = 6 * 3600
MIN_QUIET_SECONDS = 60
DEFAULT_LIMIT, DEFAULT_SAMPLE = 60, 30
PRIVATE_FILE_MODE = 0o600
EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 64

BuildResult = namedtuple(
    "BuildResult",
    "written skipped bytes_total secret_records pii_records failed entrypoints unknown_keys",
    defaults=(None, None),
)
ENTRYPOINT_NAME = re.compile(r"[A-Za-z0-9_-]{1,30}")


# ------------------------------------------------------------------ choosing and writing


def list_sessions(source, now, quiet_seconds, exclude_session=None):
    """(transcript paths NEWEST first, Counter of why others were left out). Top level only, symlinks never followed.
    Newest first because a proposal is only useful when its evidence is younger than the note it would change."""
    skipped, found = Counter(), []
    for name in sorted(os.listdir(source)):
        stem, path = name[: -len(".jsonl")], os.path.join(source, name)
        if not name.endswith(".jsonl"):
            continue
        if not UUID.fullmatch(stem):
            skipped["not_uuid"] += 1
        elif os.path.islink(path):
            skipped["symlink"] += 1
        elif not os.path.isfile(path):
            skipped["special"] += 1
        elif stem == exclude_session:
            skipped["current"] += 1
        else:
            info = os.stat(path)
            if info.st_size > MAX_FILE_BYTES:
                skipped["too_big"] += 1
            elif now - info.st_mtime < quiet_seconds:
                skipped["recent"] += 1
            else:
                found.append((info.st_mtime, name, path))
    return [path for _mtime, _name, path in sorted(found, reverse=True)], skipped


def _only_digests(out):
    """True when every entry of the directory is a regular, unlinked <session>.md file."""
    return all(
        n.endswith(".md")
        and UUID.fullmatch(n[: -len(".md")])
        and os.path.isfile(os.path.join(out, n))
        and not os.path.islink(os.path.join(out, n))
        for n in os.listdir(out)
    )


def _check_dirs(source, out):
    if os.path.islink(out) or (os.path.exists(out) and not os.path.isdir(out)):
        raise mc.HygieneError("the evidence path must be a real directory, not a link or a file")
    if os.path.isdir(out) and not _only_digests(out):
        raise mc.HygieneError("the evidence directory already holds files that are not digests; pick a new or empty one")
    if not os.path.isdir(source):
        raise mc.HygieneError("transcript directory not found: %s" % mc.printable(source))
    if mc.is_within(source, out) or mc.is_within(out, source):
        raise mc.HygieneError("the evidence directory and the transcript directory must not contain each other")
    if mc.is_within(mc.DEFAULT_MEMORY_DIR, out):
        raise mc.HygieneError("the evidence directory must not live inside the memory directory")


def _digest_files(out):
    if not os.path.isdir(out):
        return []
    return sorted(
        os.path.join(out, name)
        for name in os.listdir(out)
        if name.endswith(".md")
        and os.path.isfile(os.path.join(out, name))
        and not os.path.islink(os.path.join(out, name))
    )


def _dir_bytes(out):
    return sum(os.path.getsize(path) for path in _digest_files(out))


def _write_once(target, text):
    fd = os.open(
        target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, PRIVATE_FILE_MODE
    )
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def _safe_digest(path, skipped, entrypoints=DEFAULT_ENTRYPOINTS):
    """The digest of one session, or None after counting why it could not be built."""
    try:
        return build_digest(path) if entrypoints == DEFAULT_ENTRYPOINTS else build_digest(path, entrypoints)
    except (OSError, ValueError, RecursionError, MemoryError):
        skipped["unreadable"] += 1
        return None


def _is_human(path, entrypoints=DEFAULT_ENTRYPOINTS):
    try:
        return is_human_session(path) if entrypoints == DEFAULT_ENTRYPOINTS else is_human_session(path, entrypoints)
    except (OSError, ValueError, RecursionError, MemoryError):
        return False


def _store(target, text, skipped):
    """Write one digest: 'written', 'skipped' (the file appeared in the meantime) or 'failed' (the write itself failed)."""
    try:
        _write_once(target, text)
    except FileExistsError:
        skipped["existing"] += 1
        return "skipped"
    except OSError:
        skipped["write_failed"] += 1
        return "failed"
    return "written"


def build_all(
    source,
    out,
    limit=DEFAULT_LIMIT,
    now=None,
    quiet_seconds=QUIET_SECONDS,
    exclude_session=None,
    entrypoints=DEFAULT_ENTRYPOINTS,
    progress=None,
):
    _check_dirs(source, out)
    paths, skipped = list_sessions(
        source, time.time() if now is None else now, quiet_seconds, exclude_session
    )
    if progress:
        progress("BUILD-START sessions=%d limit=%d (reading a large transcript folder can take a few minutes; nothing leaves this machine)" % (len(paths), limit))
    mc.makedirs_private(out)
    total, written, secret, pii, failed = _dir_bytes(out), [], 0, 0, None
    foreign, unknown = Counter(), Counter()
    for position, path in enumerate(paths):
        if len(written) >= limit:
            break
        session_id = os.path.basename(path)[: -len(".jsonl")]
        target = os.path.join(out, session_id + ".md")
        if os.path.lexists(target):  # first: a session that has its digest is not "waiting", full folder or not
            skipped["existing"] += 1
            continue
        if total >= MAX_TOTAL_BYTES:
            # the folder is already full: parsing more transcripts could only produce digests that do not fit
            skipped["size_cap"] += _waiting(paths[position:], out)
            break
        digest = _safe_digest(path, skipped, entrypoints)
        if digest is None:
            continue
        secret, pii = secret + digest.secret_records, pii + digest.pii_records
        unknown.update(dict(digest.unknown_keys))
        if digest.skip == "non_cli" and digest.entrypoint:
            foreign[digest.entrypoint] += 1
        if digest.skip:
            skipped[digest.skip] += 1
            continue
        size = len(digest.text.encode("utf-8"))
        if total + size > MAX_TOTAL_BYTES:
            # every session still waiting for a digest (the newest were taken first) is left out; say how many, not just one
            skipped["size_cap"] += _waiting(paths[position:], out)
            break
        if not would_verify(digest.text, session_id):
            skipped["invalid"] += 1
            continue
        outcome = _store(target, digest.text, skipped)
        if outcome == "failed":
            failed = session_id
            break
        if outcome == "written":
            total += size
            written.append(session_id)
    return BuildResult(written, skipped, _dir_bytes(out), secret, pii, failed, foreign, unknown)


def _waiting(paths, out):
    """How many of these sessions have no digest in the folder yet (an upper bound: some would be skipped for another reason)."""
    return sum(1 for p in paths if not os.path.lexists(os.path.join(out, os.path.basename(p)[: -len(".jsonl")] + ".md")))


def _sample(paths, count):
    if count >= len(paths):
        return list(paths)
    return [paths[(i * len(paths)) // count] for i in range(count)]


def scan_sources(
    source, sample, now=None, quiet_seconds=QUIET_SECONDS, exclude_session=None, entrypoints=DEFAULT_ENTRYPOINTS, progress=None
):
    """Counts only: how many sampled sessions were read and how many of their texts hold a secret shape or PII."""
    if not os.path.isdir(source):
        raise mc.HygieneError(
            "transcript directory not found: %s" % mc.printable(source)
        )
    paths, _skipped = list_sessions(
        source, time.time() if now is None else now, quiet_seconds, exclude_session
    )
    scanned = secret = pii = 0
    if progress:
        progress("SCAN-START sessions=%d sample=%d (a large transcript folder can take a few minutes; nothing leaves this machine)" % (len(paths), sample))
    human = [path for path in paths if _is_human(path, entrypoints)]
    for path in _sample(human, sample):
        digest = _safe_digest(path, Counter(), entrypoints)
        if digest is not None and digest.skip in (None, "empty"):
            scanned, secret, pii = (
                scanned + 1,
                secret + digest.secret_records,
                pii + digest.pii_records,
            )
    return scanned, secret, pii


# ------------------------------------------------------------------ command line


def _finite_now(text):
    value = float(text)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("--now must be a finite positive number")
    return value


def _entrypoint_name(text):
    if not ENTRYPOINT_NAME.fullmatch(text):
        raise argparse.ArgumentTypeError("an entrypoint name is 1 to 30 letters, digits, underscores or hyphens")
    if text.casefold().startswith("sdk"):
        raise argparse.ArgumentTypeError("sdk-* entrypoints are headless runs, whose text a machine wrote: they cannot be treated as typed by a person")
    return text


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, "%s: error: %s\n" % (self.prog, message))


def _paths(args):
    project = os.path.dirname(os.path.abspath(mc.DEFAULT_MEMORY_DIR))
    source = mc.expand(args.source) if args.source else project
    default_out = os.path.join(
        mc.ops_dir_for(mc.DEFAULT_MEMORY_DIR, mc.DEFAULT_OPS_ROOT), "evidence"
    )
    return source, mc.expand(args.out) if args.out else default_out


def _skip_text(skipped):
    return " ".join(
        "skipped_%s=%d" % (reason, count) for reason, count in sorted(skipped.items())
    )


def _progress(text):
    """A start line for a long read, on stderr so stdout stays the parseable result."""
    print(text, file=sys.stderr)


def cmd_status(args):
    _source, out = _paths(args)
    print(
        "STATUS digests=%d bytes_total=%d out=%s"
        % (len(_digest_files(out)), _dir_bytes(out), mc.printable(out))
    )
    return EXIT_OK


def _quiet(args):
    return max(MIN_QUIET_SECONDS, args.quiet_seconds)


def _entrypoints(args):
    return DEFAULT_ENTRYPOINTS | frozenset(args.include_entrypoint)


def cmd_scan_sources(args):
    source, _out = _paths(args)
    scanned, secret, pii = scan_sources(
        source, args.sample, args.now, _quiet(args), args.exclude_session, _entrypoints(args), progress=_progress
    )
    print(
        "SOURCES scanned=%d secret_records=%d pii_records=%d" % (scanned, secret, pii)
    )
    return EXIT_OK


def cmd_build(args):
    source, out = _paths(args)
    res = build_all(
        source, out, args.limit, args.now, _quiet(args), args.exclude_session, _entrypoints(args), progress=_progress
    )
    other = {k: v for k, v in res.skipped.items() if k != "existing"}
    print(
        "BUILD ok digests=%d bytes_total=%d secret_records_dropped=%d skipped_existing=%d pii_records_masked=%d %s"
        % (
            len(res.written),
            res.bytes_total,
            res.secret_records,
            res.skipped["existing"],
            res.pii_records,
            _skip_text(other),
        )
    )
    for line in en.note_lines(res.entrypoints, res.unknown_keys, other, len(res.written)):
        print(line)
    if res.failed:
        partial = os.path.join(out, res.failed + ".md")
        print("WRITE_FAILED %s.md may be incomplete; check it with verify, then remove it yourself: rm %s" % (res.failed, shlex.quote(partial)))
        return EXIT_FAIL
    return EXIT_OK


def cmd_verify(args):
    _source, out = _paths(args)
    res = verify_dir(out)
    print(
        "VERIFY digests=%d over_8192=%d missing_header=%d untrusted_origin_lines=%d"
        % res[:4]
    )
    if not res.ok and res.bad_names:
        print("VERIFY-BAD first=%s" % ", ".join(mc.printable(name)[:80] for name in res.bad_names))
    return EXIT_OK if res.ok else EXIT_FAIL


def _is_evidence_dir(out):
    if os.path.islink(out) or not os.path.isdir(out) or os.path.basename(out) != "evidence":
        return False
    return bool(os.listdir(out)) and _only_digests(out)


def cmd_purge_plan(args):
    _source, out = _paths(args)
    print("# plan only, nothing is removed; run the line below yourself when you no longer need the digests")
    if _is_evidence_dir(out) and mc.printable(out) == out:
        print("rm -r %s" % shlex.quote(out))
    else:
        print("# refused: %s is not a directory named evidence that holds only <session>.md digests" % mc.printable(out))
    return EXIT_OK


def build_parser():
    parser = _Parser(prog="memory_evidence.py", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--source", default=None, help="transcript directory (default: this project's)"
    )
    common.add_argument(
        "--out",
        default=None,
        help="evidence directory (default: <data root>/<project>/evidence)",
    )
    common.add_argument(
        "--quiet-seconds",
        type=int,
        default=QUIET_SECONDS,
        help="skip sessions modified more recently",
    )
    common.add_argument(
        "--exclude-session",
        default=None,
        help="session id to leave out (the running one)",
    )
    common.add_argument(
        "--include-entrypoint",
        action="append",
        default=[],
        type=_entrypoint_name,
        metavar="NAME",
        help="also treat sessions with this entrypoint as typed by a person (default: only cli); repeatable",
    )
    common.add_argument("--now", type=_finite_now, default=None, help=argparse.SUPPRESS)
    for name, fn, extra in (
        ("status", cmd_status, ()),
        ("scan-sources", cmd_scan_sources, (("--sample", DEFAULT_SAMPLE),)),
        ("build", cmd_build, (("--limit", DEFAULT_LIMIT),)),
        ("verify", cmd_verify, ()),
        ("purge-plan", cmd_purge_plan, ()),
    ):
        command = sub.add_parser(name, parents=[common])
        for flag, default in extra:
            command.add_argument(flag, type=int, default=default)
        command.set_defaults(fn=fn)
    return parser


def main(argv=None):
    mc.safe_stdio()
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except (mc.HygieneError, OSError, ValueError) as err:
        print("ERROR: %s" % mc.printable(err), file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
