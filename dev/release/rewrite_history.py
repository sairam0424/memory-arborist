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
leaves no half-built destination. The source repository is only read, with replacement refs and graft files switched off and
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
)


class UsageError(Exception):
    pass


def git_env():
    """The environment for every git call: this repository's real objects, and nothing inherited that changes that."""
    env = {key: value for key, value in os.environ.items() if key not in GIT_SCRUB}
    env.update(
        GIT_NO_REPLACE_OBJECTS="1",  # a replace ref would show the replacement, not the original commit
        GIT_GRAFT_FILE=os.devnull,
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_NO_LAZY_FETCH="1",
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


def linear_history(source, ref):
    """Commit ids oldest first; a merge commit is refused."""
    rows = git(source, "rev-list", "--reverse", "--parents", ref).decode().splitlines()
    shas = []
    for row in rows:
        fields = row.split()
        if len(fields) > 2:
            raise UsageError(
                "commit %s is a merge: a linear replay would flatten it" % fields[0][:7]
            )
        shas.append(fields[0])
    return shas


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
        if name == "main" or name in names or not valid_ref("heads", name):
            raise UsageError(
                "--branch needs a valid branch name that is not main and is not used twice: %s"
                % spec
            )
        names.append(name)


def check_destination(dest):
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
    git(staging, "init", "-q", "-b", "main", "--object-format=" + fmt)
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
    return mapping, made_tags, made_branches


def run(args):
    args.source, args.dest = (
        os.path.abspath(args.source),
        os.path.abspath(args.dest),
    )  # git runs from a temporary folder
    if not os.path.isdir(args.source):
        raise UsageError("not a directory: %s" % args.source)
    check_destination(args.dest)
    shas = linear_history(args.source, args.ref)
    full = (
        [s for s in shas if s.startswith(args.root)]
        if len(args.root) < 40
        else [s for s in shas if s == args.root]
    )
    if len(full) != 1:
        raise UsageError(
            "the root commit is not in the history of %s (or the prefix is ambiguous)"
            % args.ref
        )
    keep = shas[shas.index(full[0]) :]
    tags = list(args.tag or ())
    check_refs(args.source, set(keep), tags, args.branch or ())
    commits = [read_commit(args.source, sha) for sha in keep]
    os.makedirs(os.path.dirname(args.dest), exist_ok=True)
    staging, moved = tempfile.mkdtemp(prefix=".rewrite-", dir=os.path.dirname(args.dest)), False
    try:
        mapping, made_tags, made_branches = build(args, keep, commits, tags, object_format(args.source), staging)
        moved = move_into_place(staging, args.dest)
    except RuntimeError as err:
        print("VERIFY FAILED: %s (nothing was written to the destination)" % err, file=sys.stderr)
        return 1
    finally:
        if not moved:
            shutil.rmtree(staging, ignore_errors=True)  # a folder this run created, never one that was moved into place
    print(
        "REWRITE ok commits=%d dropped=%d root=%s tags=%s branches=%s"
        % (len(keep), len(shas) - len(keep), mapping[keep[0]][:7], ",".join(made_tags) or "-", ",".join(made_branches) or "-")
    )
    return 0


def move_into_place(staging, dest):
    """Rename the verified staging folder to the destination (an empty destination folder is replaced)."""
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
    try:
        return run(args)
    except UsageError as err:
        print("ERROR: %s" % err, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
