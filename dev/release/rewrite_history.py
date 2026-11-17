#!/usr/bin/env python3
"""Rebuild a repository's history as a new root plus a replay of every later commit, in a fresh repository. Standard library plus git.

Use: before a repository goes public, drop the early commits that must not be published. The commit given with --root becomes
the new root commit (same tree, no parents); every later commit is recreated on top of it with the SAME tree, author,
committer, dates and message (so sign-off trailers survive). Tags named with --tag are recreated as annotated tags on the
replayed commits with their original tagger and message; --branch NAME=TAG points a branch at one of them.

The destination is a brand new repository that holds only what the new history reaches: no old commit, tree or blob, and no
unreachable object (a clone of the old repository would still carry them, so never publish from it). The source repository is
only read. History must be linear: a merge commit, a signed commit or a tag on a dropped commit is refused, because replaying
it would silently change it.

    python3 -I rewrite_history.py --source OLD --dest NEW --root COMMIT [--ref main] [--tag NAME ...] [--branch NAME=TAG ...]

Afterwards check the result with check_history.py and look at `git log` yourself. Exit codes: 0 done and verified; 1 the result
did not verify; 2 usage or git error.
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile
from collections import namedtuple

IDENT = re.compile(rb"^(?:author|committer|tagger) (.*) <(.*)> (\d+) ([+-]\d{4})$")
Ident = namedtuple("Ident", "name email when")
Commit = namedtuple("Commit", "sha tree parents author committer message")
REFUSED_HEADERS = (b"gpgsig", b"mergetag", b"encoding")


class UsageError(Exception):
    pass


def git(repo, *args, data=None, env=None):
    merged = dict(os.environ, **(env or {}))
    try:
        done = subprocess.run(
            ["git", "-C", repo, *args],
            input=data,
            capture_output=True,
            check=True,
            env=merged,
        )
    except OSError:
        raise UsageError("git could not be started")
    except subprocess.CalledProcessError as err:
        detail = err.stderr.decode("utf-8", "replace").strip().splitlines()
        raise UsageError(
            "git %s failed%s" % (args[0], (": " + detail[-1]) if detail else "")
        )
    return done.stdout


def parse_ident(line):
    match = IDENT.match(line)
    if not match:
        raise UsageError("cannot read an identity line")
    name, email, epoch, zone = match.groups()
    return Ident(
        name.decode("utf-8", "replace"),
        email.decode("utf-8", "replace"),
        "%s %s" % (epoch.decode(), zone.decode()),
    )


def read_commit(repo, sha):
    head, _sep, message = git(repo, "cat-file", "commit", sha).partition(b"\n\n")
    tree, parents, author, committer = None, [], None, None
    for line in head.split(b"\n"):
        if line.split(b" ", 1)[0] in REFUSED_HEADERS:
            raise UsageError(
                "commit %s has a %s header: it cannot be replayed unchanged"
                % (sha[:7], line.split(b" ", 1)[0].decode())
            )
        if line.startswith(b"tree "):
            tree = line[5:].decode()
        elif line.startswith(b"parent "):
            parents.append(line[7:].decode())
        elif line.startswith(b"author "):
            author = parse_ident(line)
        elif line.startswith(b"committer "):
            committer = parse_ident(line)
    return Commit(sha, tree, parents, author, committer, message)


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


def identity_env(prefix, ident):
    return {
        prefix + "_NAME": ident.name,
        prefix + "_EMAIL": ident.email,
        prefix + "_DATE": ident.when,
    }


def replay(scratch, commits):
    """Recreate each commit in `scratch` on top of the previous one (the first has no parent). {old id: new id}."""
    mapping, parent = {}, None
    for commit in commits:
        args = ["commit-tree", commit.tree] + (["-p", parent] if parent else [])
        env = dict(
            identity_env("GIT_AUTHOR", commit.author),
            **identity_env("GIT_COMMITTER", commit.committer),
        )
        parent = git(scratch, *args, data=commit.message, env=env).decode().strip()
        mapping[commit.sha] = parent
    return mapping


def read_tag(source, name):
    raw = git(source, "cat-file", "tag", name)
    head, _sep, message = raw.partition(b"\n\n")
    target, tagger = None, None
    for line in head.split(b"\n"):
        if line.startswith(b"object "):
            target = line[7:].decode()
        elif line.startswith(b"tagger "):
            tagger = parse_ident(line)
        elif line.startswith(b"type ") and line != b"type commit":
            raise UsageError("tag %s does not point at a commit" % name)
    if target is None or tagger is None:
        raise UsageError("tag %s is not an annotated tag with a tagger" % name)
    return target, tagger, message


def create_tags(source, dest, names, mapping):
    created = {}
    for name in names:
        target, tagger, message = read_tag(source, name)
        if target not in mapping:
            raise UsageError(
                "tag %s points at a commit that is not in the new history (it comes before the new root)"
                % name
            )
        env = identity_env("GIT_COMMITTER", tagger)
        git(
            dest,
            "-c",
            "tag.gpgSign=false",
            "tag",
            "-a",
            name,
            "-F",
            "-",
            "--cleanup=verbatim",
            mapping[target],
            data=message,
            env=env,
        )
        created[name] = mapping[target]
    return created


def create_branches(dest, specs, tags):
    made = []
    for spec in specs or ():
        name, _eq, tag = spec.partition("=")
        if not name or tag not in tags:
            raise UsageError(
                "--branch needs NAME=TAG where TAG is one of the --tag names: %s" % spec
            )
        git(dest, "branch", "-f", name, tags[tag])
        made.append(name)
    return made


def check_refs(source, kept, tags, branches):
    """Refuse a tag on a dropped commit or a bad --branch spec before anything is created."""
    for name in tags:
        if read_tag(source, name)[0] not in kept:
            raise UsageError("tag %s points at a commit that is not in the new history (it comes before the new root)" % name)
    for spec in branches:
        name, _eq, tag = spec.partition("=")
        if not name or tag not in tags:
            raise UsageError("--branch needs NAME=TAG where TAG is one of the --tag names: %s" % spec)


def check_destination(dest):
    if os.path.exists(dest) and (not os.path.isdir(dest) or os.listdir(dest)):
        raise UsageError("the destination exists and is not empty: %s" % dest)


def publish(scratch, dest, tip):
    """Fetch only what `tip` reaches into a new repository and check it out as main."""
    os.makedirs(dest, exist_ok=True)
    git(dest, "init", "-q", "-b", "main")
    git(scratch, "update-ref", "refs/heads/rewritten", tip)
    git(dest, "fetch", "-q", scratch, "refs/heads/rewritten:refs/rewritten/tip")
    git(dest, "update-ref", "refs/heads/main", tip)
    git(dest, "update-ref", "-d", "refs/rewritten/tip")
    git(dest, "reset", "-q", "--hard")


def verify(dest, commits, mapping):
    """Every replayed commit must carry the same tree, identities, dates and message as its original."""
    for commit in commits:
        again = read_commit(dest, mapping[commit.sha])
        if (again.tree, again.author, again.committer, again.message) != (
            commit.tree,
            commit.author,
            commit.committer,
            commit.message,
        ):
            raise RuntimeError("commit %s differs after the replay" % commit.sha[:7])
    roots = git(dest, "rev-list", "--max-parents=0", "main").decode().split()
    if len(roots) != 1 or git(dest, "rev-list", "--count", "main").decode().strip() != str(
        len(commits)
    ):
        raise RuntimeError(
            "the new history is not one chain of %d commits" % len(commits)
        )


def run(args):
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
    check_refs(args.source, set(keep), args.tag or (), args.branch or ())
    commits = [read_commit(args.source, sha) for sha in keep]
    with tempfile.TemporaryDirectory(prefix="rewrite_") as work:
        scratch = os.path.join(work, "scratch.git")
        git(work, "clone", "--bare", "--no-hardlinks", "-q", args.source, scratch)
        mapping = replay(scratch, commits)
        publish(scratch, args.dest, mapping[keep[-1]])
    tags = create_tags(args.source, args.dest, args.tag or (), mapping)
    branches = create_branches(args.dest, args.branch, tags)
    try:
        verify(args.dest, commits, mapping)
    except RuntimeError as err:
        print("VERIFY FAILED: %s" % err, file=sys.stderr)
        return 1
    print(
        "REWRITE ok commits=%d dropped=%d root=%s tags=%s branches=%s"
        % (
            len(keep),
            len(shas) - len(keep),
            mapping[keep[0]][:7],
            ",".join(tags) or "-",
            ",".join(branches) or "-",
        )
    )
    return 0


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
