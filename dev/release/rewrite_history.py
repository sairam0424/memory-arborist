#!/usr/bin/env python3
"""Rebuild a repository's history as a new root plus a replay of every later commit, in a fresh repository. Standard library plus git.

Use: before a repository goes public, drop the early commits that must not be published. The commit given with --root becomes
the new root commit (same tree, no parents); every later commit is recreated on top of it byte for byte, with only its parent
line changed, so tree, author, committer, dates, encoding of the names and message are exactly those of the original (sign-off
trailers survive). Tags named with --tag are recreated as annotated tags on the replayed commits, again byte for byte but for
the object line; --branch NAME=TAG points a branch at one of them.

The destination is a brand new repository that holds only what the new history reaches: no old commit, tree or blob, no
unreachable object, no tag or branch nobody asked for (a clone of the old repository would still carry them, so never publish
from it). It is built in a hidden folder next to the destination and moved into place only after it verified, so a failure
leaves no half-built destination. The source repository is only read (the destination may not lie inside it, and --ref must name one of its branches), with replacement refs and graft files switched off and
the GIT_DIR family of environment variables removed. History must be linear: a merge commit, a commit with any header besides
tree, parent, author and committer (a signature, an encoding, a tool's extra header) and a signed tag or a tag on a dropped
commit are refused, because replaying them would silently change them.

    python3 -I rewrite_history.py --source OLD --dest NEW --root COMMIT [--ref main] [--tag NAME ...] [--branch NAME=TAG ...]

Afterwards check the result with check_history.py and look at `git log` yourself. Exit codes: 0 done and verified; 1 the result
did not verify (nothing was written to the destination); 2 usage or git error.
"""

import argparse
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from collections import namedtuple

Commit = namedtuple("Commit", "sha tree parents author committer message raw")
ALLOWED_HEADERS = (b"tree", b"parent", b"author", b"committer")
SIGNATURE_MARKERS = (
    b"-----BEGIN PGP SIGNATURE-----",
    b"-----BEGIN SSH SIGNATURE-----",
    b"-----BEGIN SIGNED MESSAGE-----",
)
# Variables that point git at another repository or another set of objects; a git hook or `rebase --exec` exports some of them.
GIT_SCRUB = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
    "GIT_SHALLOW_FILE",
    "GIT_REPLACE_REF_BASE",
    "GIT_TEMPLATE_DIR",  # would copy files and hooks into a new repository
    "GIT_CONFIG_COUNT",  # config handed over through the environment (hooks path, reflogs, ...)
    "GIT_CONFIG_PARAMETERS",
)


class UsageError(Exception):
    pass


def git_env():
    """The environment for every git call: this repository's real objects, and nothing inherited that changes that."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in GIT_SCRUB and not key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
    }
    env.update(
        GIT_NO_REPLACE_OBJECTS="1",  # a replace ref would show the replacement, not the original commit
        GIT_GRAFT_FILE=os.devnull,
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_NO_LAZY_FETCH="1",
        GIT_COMMITTER_NAME="rewrite_history",  # the reflog of a plumbing call names the committer: never the operator
        GIT_COMMITTER_EMAIL="rewrite_history@localhost.invalid",
        GIT_AUTHOR_NAME="rewrite_history",
        GIT_AUTHOR_EMAIL="rewrite_history@localhost.invalid",
    )
    return env


def git(repo, *args, data=None):
    try:
        done = subprocess.run(
            ["git", "-C", repo, *args],
            input=data,
            capture_output=True,
            check=True,
            env=git_env(),
        )
    except OSError:
        raise UsageError("git could not be started")
    except subprocess.CalledProcessError as err:
        detail = err.stderr.decode("utf-8", "replace").strip().splitlines()
        raise UsageError(
            "git %s failed%s" % (args[0], (": " + detail[-1]) if detail else "")
        )
    return done.stdout


def read_commit(repo, sha):
    """The commit with its raw bytes. Any header besides tree, parent, author and committer is refused."""
    raw = git(repo, "cat-file", "commit", sha)
    head, sep, message = raw.partition(b"\n\n")
    if not sep:
        raise UsageError("commit %s has no blank line before its message" % sha[:7])
    tree, parents, author, committer = None, [], None, None
    for line in head.split(b"\n"):
        key = line.split(b" ", 1)[0]
        if key not in ALLOWED_HEADERS:
            raise UsageError(
                "commit %s has a %s header: it cannot be replayed unchanged"
                % (sha[:7], key.decode("utf-8", "replace") or "continuation")
            )
        if key == b"tree":
            tree = line[5:].decode()
        elif key == b"parent":
            parents.append(line[7:].decode())
        elif key == b"author":
            author = line
        else:
            committer = line
    return Commit(sha, tree, parents, author, committer, message, raw)


MIN_ROOT_CHARS = 7


def ref_exists(source, full):
    """True when the ref exists under exactly this name (on a case-insensitive disk git also finds `V1` for `v1`)."""
    return full in git(source, "for-each-ref", "--format=%(refname)", full).decode().split()


def resolve_branch(source, ref):
    """The commit a branch of the source points at. Only a branch name is accepted: git would read a bare word as a tag of that name, a
    range, an option or a file, and rev-list would then walk another history without a word."""
    if not ref or ref.startswith("-") or not valid_ref("heads", ref) or not ref_exists(source, "refs/heads/" + ref):
        raise UsageError("--ref must be the name of a branch of the source, spelled exactly: %s" % ref)
    return git(source, "rev-parse", "--verify", "--quiet", "--end-of-options", "refs/heads/%s^{commit}" % ref).decode().strip()


def kept_history(source, tip, root):
    """(commit ids from the new root to the tip, oldest first; the length of the branch's first-parent chain). Only the commits from the
    root to the tip are replayed, so only a merge among THEM is refused: what lies before the root is dropped whole, merges included."""
    if len(root) < MIN_ROOT_CHARS:
        raise UsageError("--root needs at least %d characters of the commit id" % MIN_ROOT_CHARS)
    rows = [row.split() for row in git(source, "rev-list", "--first-parent", "--parents", "--end-of-options", tip, "--").decode().splitlines()]
    hits = [i for i, fields in enumerate(rows) if fields[0].startswith(root)]
    if len(hits) != 1:
        raise UsageError("the root commit is not in the first-parent history of the branch (or the prefix is ambiguous)")
    span = rows[: hits[0] + 1]  # newest first, ending at the root
    for fields in span[:-1]:
        if len(fields) > 2:
            raise UsageError("commit %s is a merge: a linear replay would flatten it" % fields[0][:7])
    return [fields[0] for fields in reversed(span)], len(rows)


def without_parents(raw):
    """A commit object with its parent lines removed: what must be identical between an original and its replay."""
    head, sep, message = raw.partition(b"\n\n")
    return (
        b"\n".join(
            line for line in head.split(b"\n") if not line.startswith(b"parent ")
        )
        + sep
        + message
    )


def replayed_bytes(commit, parent):
    """The original commit object with its parent lines replaced by one pointing at `parent` (none for the new root)."""
    kept = without_parents(commit.raw).split(b"\n", 1)
    if not kept[0].startswith(b"tree "):
        raise UsageError("commit %s does not start with its tree line" % commit.sha[:7])
    return (
        kept[0]
        + b"\n"
        + (b"parent " + parent.encode() + b"\n" if parent else b"")
        + kept[1]
    )


def replay(scratch, commits):
    """Recreate each commit in `scratch` on top of the previous one (the first has no parent). {old id: new id}."""
    mapping, parent = {}, None
    for commit in commits:
        parent = (
            git(
                scratch,
                "hash-object",
                "-t",
                "commit",
                "-w",
                "--stdin",
                data=replayed_bytes(commit, parent),
            )
            .decode()
            .strip()
        )
        mapping[commit.sha] = parent
    return mapping


def read_tag(source, name):
    """(target commit id, raw tag object) of an annotated tag that points at a commit and carries no signature."""
    ref = "refs/tags/" + name
    if not ref_exists(source, ref):
        raise UsageError("tag %s does not exist under exactly that name" % name)
    if git(source, "cat-file", "-t", ref).strip() != b"tag":
        raise UsageError("tag %s is not an annotated tag" % name)
    raw = git(source, "cat-file", "tag", ref)
    head, _sep, _message = raw.partition(b"\n\n")
    target, kind = None, None
    for line in head.split(b"\n"):
        if line.startswith(b"object "):
            target = line[7:].decode()
        elif line.startswith(b"type "):
            kind = line[5:]
    if target is None or kind != b"commit":
        raise UsageError("tag %s does not point at a commit" % name)
    if any(marker in raw for marker in SIGNATURE_MARKERS):
        raise UsageError("tag %s is signed: a replay would break the signature" % name)
    return target, raw


def create_tags(source, dest, names, mapping):
    created = {}
    for name in names:
        target, raw = read_tag(source, name)
        if target not in mapping:
            raise UsageError(
                "tag %s points at a commit that is not in the new history (it comes before the new root)"
                % name
            )
        moved = b"object " + mapping[target].encode() + raw[raw.index(b"\n") :]
        tag_id = (
            git(dest, "hash-object", "-t", "tag", "-w", "--stdin", data=moved)
            .decode()
            .strip()
        )
        git(dest, "update-ref", "refs/tags/" + name, tag_id)
        created[name] = mapping[target]
    return created


def create_branches(dest, specs, tags):
    made = []
    for spec in specs or ():
        name, _eq, tag = spec.partition("=")
        git(dest, "update-ref", "refs/heads/" + name, tags[tag])
        made.append(name)
    return made


def valid_ref(kind, name):
    done = subprocess.run(
        ["git", "check-ref-format", "refs/%s/%s" % (kind, name)],
        capture_output=True,
        env=git_env(),
    )
    return done.returncode == 0


def check_refs(source, kept, tags, branches):
    """Refuse a bad name, a duplicate, a tag on a dropped commit or a bad --branch spec before anything is created."""
    if len(set(tags)) != len(tags):
        raise UsageError("a --tag name is given twice")
    for name in tags:
        if read_tag(source, name)[0] not in kept:
            raise UsageError(
                "tag %s points at a commit that is not in the new history (it comes before the new root)"
                % name
            )
    names = []
    for spec in branches:
        name, _eq, tag = spec.partition("=")
        if not name or tag not in tags:
            raise UsageError(
                "--branch needs NAME=TAG where TAG is one of the --tag names: %s" % spec
            )
        if name.lower() == "main" or name.lower() in names or not valid_ref("heads", name):
            raise UsageError(
                "--branch needs a valid branch name that is not main and is not used twice: %s"
                % spec
            )
        names.append(name.lower())


def check_outside_source(source, dest):
    """The destination may not lie inside the source or its git folder: the build would write into the repository it is meant to leave
    alone (and the staging folder would be part of it)."""
    where = os.path.realpath(dest)
    for root in {os.path.realpath(source), os.path.realpath(git(source, "rev-parse", "--absolute-git-dir").decode().strip())}:
        if where == root or where.startswith(root + os.sep):
            raise UsageError("the destination is inside the source (%s): choose a folder outside it" % root)


def make_parents(path):
    """Create the missing folders above a destination; returns the ones this call created, outermost first."""
    missing, probe = [], path
    while not os.path.isdir(probe) and probe != os.path.dirname(probe):
        missing.append(probe)
        probe = os.path.dirname(probe)
    os.makedirs(path, exist_ok=True)
    return list(reversed(missing))


def remove_created(folders):
    for folder in reversed(folders):
        try:
            os.rmdir(folder)  # only a folder this run made and that is still empty
        except OSError:
            pass


class Interrupted(Exception):
    pass


def _interrupt(signum, _frame):
    raise Interrupted(signal.Signals(signum).name)


def scrub_build_leftovers(staging):
    """What the build leaves in the new repository's folder that belongs to no commit: reflogs (they name a committer and a time),
    ORIG_HEAD and an empty ref folder."""
    gitdir = os.path.join(staging, ".git")
    shutil.rmtree(os.path.join(gitdir, "logs"), ignore_errors=True)
    if os.path.lexists(os.path.join(gitdir, "ORIG_HEAD")):
        os.remove(os.path.join(gitdir, "ORIG_HEAD"))
    try:
        os.rmdir(os.path.join(gitdir, "refs", "rewritten"))
    except OSError:
        pass


def check_destination(dest):
    if os.path.islink(dest):
        raise UsageError("the destination is a symbolic link: give the real folder")
    if os.path.exists(dest) and (not os.path.isdir(dest) or os.listdir(dest)):
        raise UsageError("the destination exists and is not empty: %s" % dest)


def object_format(source):
    try:
        return (
            git(source, "rev-parse", "--show-object-format").decode().strip() or "sha1"
        )
    except UsageError:
        return "sha1"


def publish(scratch, staging, tip, fmt):
    """Fetch only what `tip` reaches (and no tag) into a new repository and check it out as main."""
    git(staging, "init", "-q", "-b", "main", "--template=", "--object-format=" + fmt)
    git(scratch, "update-ref", "refs/heads/rewritten", tip)
    git(
        staging,
        "fetch",
        "-q",
        "--no-tags",
        "--no-write-fetch-head",
        scratch,
        "refs/heads/rewritten:refs/rewritten/tip",
    )
    git(staging, "update-ref", "refs/heads/main", tip)
    git(staging, "update-ref", "-d", "refs/rewritten/tip")
    git(staging, "reset", "-q", "--hard")


def verify(dest, commits, mapping):
    """Every replayed commit must be the original but for its parent line, and the commits must form one chain."""
    previous = None
    for commit in commits:
        again = read_commit(dest, mapping[commit.sha])
        if without_parents(again.raw) != without_parents(commit.raw):
            raise RuntimeError("commit %s differs after the replay" % commit.sha[:7])
        if again.parents != ([previous] if previous else []):
            raise RuntimeError(
                "commit %s has the wrong parent after the replay" % commit.sha[:7]
            )
        previous = mapping[commit.sha]
    roots = git(dest, "rev-list", "--max-parents=0", "main").decode().split()
    if len(roots) != 1 or git(
        dest, "rev-list", "--count", "main"
    ).decode().strip() != str(len(commits)):
        raise RuntimeError(
            "the new history is not one chain of %d commits" % len(commits)
        )


def verify_contents(dest, source, tags, branches, mapping):
    """Only the requested refs exist, every tag is its source but for the object line, and no object is unreachable."""
    expected = (
        ["refs/heads/main"]
        + ["refs/tags/" + name for name in tags]
        + ["refs/heads/" + name for name in branches]
    )
    if sorted(
        git(dest, "for-each-ref", "--format=%(refname)").decode().split()
    ) != sorted(expected):
        raise RuntimeError("the destination has refs that were not asked for")
    for name in tags:
        target, raw = read_tag(source, name)
        again = git(dest, "cat-file", "tag", "refs/tags/" + name)
        if (
            again.split(b"\n", 1)[1] != raw.split(b"\n", 1)[1]
            or again.split(b"\n", 1)[0] != b"object " + mapping[target].encode()
        ):
            raise RuntimeError("tag %s differs after the replay" % name)
    reached = {
        line.split(b" ")[0]
        for line in git(dest, "rev-list", "--objects", "--all").split(b"\n")
        if line
    }
    present = {
        line.split(b" ")[0]
        for line in git(dest, "cat-file", "--batch-all-objects", "--batch-check").split(
            b"\n"
        )
        if line
    }
    if present - reached:
        raise RuntimeError(
            "the destination holds %d objects that no ref reaches"
            % len(present - reached)
        )


def build(args, keep, commits, tags, fmt, staging):
    """Create the whole result in `staging`; returns (mapping, tags made, branches made)."""
    with tempfile.TemporaryDirectory(prefix="rewrite_") as work:
        scratch = os.path.join(work, "scratch.git")
        git(work, "clone", "--bare", "--no-hardlinks", "-q", args.source, scratch)
        mapping = replay(scratch, commits)
        publish(scratch, staging, mapping[keep[-1]], fmt)
    made_tags = create_tags(args.source, staging, tags, mapping)
    made_branches = create_branches(staging, args.branch, made_tags)
    verify(staging, commits, mapping)
    verify_contents(staging, args.source, made_tags, made_branches, mapping)
    scrub_build_leftovers(staging)
    return mapping, made_tags, made_branches


def run(args):
    args.source, args.dest = (
        os.path.abspath(args.source),
        os.path.abspath(args.dest),
    )  # git runs from a temporary folder
    if not os.path.isdir(args.source):
        raise UsageError("not a directory: %s" % args.source)
    check_outside_source(args.source, args.dest)
    check_destination(args.dest)
    keep, chain_length = kept_history(args.source, resolve_branch(args.source, args.ref), args.root)
    dropped = chain_length - len(keep)
    tags = list(args.tag or ())
    check_refs(args.source, set(keep), tags, args.branch or ())
    commits = [read_commit(args.source, sha) for sha in keep]
    created = make_parents(os.path.dirname(args.dest))
    staging, moved = None, False
    try:
        staging = tempfile.mkdtemp(prefix=".rewrite-", dir=os.path.dirname(args.dest))
        mapping, made_tags, made_branches = build(args, keep, commits, tags, object_format(args.source), staging)
        moved = move_into_place(staging, args.dest)
    except RuntimeError as err:
        print("VERIFY FAILED: %s (nothing was written to the destination)" % err, file=sys.stderr)
        return 1
    finally:
        if not moved:
            if staging:
                shutil.rmtree(staging, ignore_errors=True)  # a folder this run created, never one that was moved into place
            remove_created(created)
    try:
        print(
            "REWRITE ok commits=%d dropped=%d root=%s tags=%s branches=%s"
            % (len(keep), dropped, mapping[keep[0]][:7], ",".join(made_tags) or "-", ",".join(made_branches) or "-")
        )
    except OSError:  # a closed output pipe does not undo a finished, verified run
        pass
    return 0


def move_into_place(staging, dest):
    """Rename the verified staging folder to the destination (an empty destination folder is replaced)."""
    mask = os.umask(0)
    os.umask(mask)
    os.chmod(staging, 0o777 & ~mask)  # mkdtemp made it private; the destination gets the mode a new folder would have
    try:
        os.rename(staging, dest)  # onto a missing destination, or onto an empty folder, which a rename replaces
    except OSError as err:
        raise UsageError("could not move the verified result into place: %s" % (err.strerror or type(err).__name__))
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="rewrite_history.py",
        description=__doc__.split("\n")[0],
        allow_abbrev=False,
    )
    parser.add_argument(
        "--source", required=True, help="the repository to read (it is not changed)"
    )
    parser.add_argument(
        "--dest",
        required=True,
        help="a new, empty or missing folder for the rewritten repository",
    )
    parser.add_argument(
        "--root",
        required=True,
        help="the commit whose tree becomes the new root (a full id or a prefix)",
    )
    parser.add_argument(
        "--ref",
        default="main",
        help="the branch whose history is rewritten (default: main)",
    )
    parser.add_argument(
        "--tag",
        action="append",
        help="an annotated tag to recreate on the replayed commit (repeatable)",
    )
    parser.add_argument(
        "--branch",
        action="append",
        help="NAME=TAG: a branch pointing at a recreated tag (repeatable)",
    )
    args = parser.parse_args(argv)
    saved = {}
    for name in ("SIGTERM", "SIGHUP", "SIGINT"):
        try:
            saved[name] = signal.signal(getattr(signal, name), _interrupt)
        except (ValueError, OSError):  # not the main thread
            pass
    try:
        return run(args)
    except UsageError as err:
        print("ERROR: %s" % err, file=sys.stderr)
        return 2
    except Interrupted as err:
        print("INTERRUPTED (%s): nothing was written to the destination" % err, file=sys.stderr)
        return 2
    except (OSError, MemoryError, RecursionError) as err:
        print("ERROR: unexpected %s: %s" % (type(err).__name__, getattr(err, "strerror", None) or "the run did not complete"), file=sys.stderr)
        return 2
    finally:
        for name, handler in saved.items():
            signal.signal(getattr(signal, name), handler)


if __name__ == "__main__":
    sys.exit(main())
