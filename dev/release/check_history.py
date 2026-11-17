#!/usr/bin/env python3
"""Check every revision of a git repository against the maintainer's private word list. Standard library plus git; run as python3 -I.

Why this exists: `git log -p | grep -f words` misses a word that was written as split string fragments, in different case, with
punctuation between its parts, in a file name, in a commit message, in a tag, or in an author line. This tool normalises both
sides (lower case, letters and digits only) and looks in every object that any ref reaches (`git rev-list --objects --all`): the
content and the path of every blob of every commit, every commit and tag object (author, committer and tagger lines, extra
headers, messages, the name inside a tag object) and every ref name. Tags that point at a blob or a tree and tags of tags are
covered. It reads the real objects: replacement refs and graft files are switched off, and the GIT_DIR family of environment
variables is removed so that a hook or a rebase cannot point it at another repository.

It never prints a word from the list, nor a path that holds one: it prints entry NUMBERS (the position in the list after
comments and blank lines), counts and short commit ids. An identity (an email address you chose to keep in commit metadata) can
be allowed with --allow-identity N: hits of entry N in author, committer and tagger lines and in `Signed-off-by:` trailers then
pass, but the same entry in a file, a path, a message body or a ref name is still a hit.

    python3 -I check_history.py --repo . --words .release-guard-words [--allow-identity N ...]

Exit codes: 0 clean; 1 at least one hit; 2 usage or git error. A check that cannot be complete is an error, never a clean pass: a
missing or empty list, a list in which every entry is too short, a shallow or partial clone, a repository without commits, an
object that is missing. Entries shorter than MIN_LENGTH characters once normalised cannot be checked reliably and are skipped
(`skipped_short=`).
"""

import argparse
import os
import re
import subprocess
import sys
from collections import defaultdict

MIN_LENGTH = 4
SHORT_ID = 7
OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
CATEGORIES = ("content", "path", "message", "tag", "metadata", "ref")
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


def normalise(text):
    """Lower case, letters and digits only: quotes, commas, spaces, hyphens and underscores between fragments disappear."""
    return "".join(ch for ch in text.casefold() if ch.isalnum())


def read_entries(path):
    """The list's entries in order: one per line, `#` starts a comment, blank lines are ignored. A byte-order mark is not an entry."""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            lines = fh.read().splitlines()
    except OSError as err:
        raise UsageError(
            "cannot read the word list: %s" % (err.strerror or type(err).__name__)
        )
    except ValueError:
        raise UsageError("the word list is not valid UTF-8 text")
    entries = [line.split("#", 1)[0].strip() for line in lines]
    entries = [entry for entry in entries if entry]
    if not entries:
        raise UsageError("the word list has no entries")
    return entries


def git_env():
    """The environment for every git call: the real objects of THIS repository, and nothing inherited that changes that."""
    env = {key: value for key, value in os.environ.items() if key not in GIT_SCRUB}
    # a replace ref would hide the original commit from rev-list and cat-file, though a clone still carries it
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    env["GIT_GRAFT_FILE"] = os.devnull  # the same for .git/info/grafts
    env["GIT_NO_LAZY_FETCH"] = "1"
    return env


def run_git(repo, args, data=None):
    try:
        return subprocess.run(
            ["git", "-C", repo, *args], input=data, capture_output=True, env=git_env()
        )
    except OSError:
        raise UsageError("git could not be started")


def git(repo, *args, data=None):
    done = run_git(repo, args, data)
    if done.returncode:
        raise UsageError("git %s failed in the given path" % args[0])
    return done.stdout


def preflight(repo):
    """Refuse a repository in which a clean result would mean nothing: shallow, partial, or without commits."""
    if git(repo, "rev-parse", "--is-shallow-repository").strip() == b"true":
        raise UsageError(
            "the repository is shallow: older commits are missing, so a clean result would prove nothing"
        )
    partial = run_git(
        repo,
        [
            "config",
            "--get-regexp",
            r"^(remote\..*\.promisor|extensions\.partialclone)$",
        ],
    )
    if partial.returncode == 0 and partial.stdout.strip():
        raise UsageError(
            "the repository is a partial clone: some objects are not here, so a clean result would prove nothing"
        )


def reachable_ids(repo):
    """Every object id that any ref reaches, once each (commits, trees, blobs and tag objects)."""
    seen, ids = set(), []
    for line in git(repo, "rev-list", "--objects", "--all").split(b"\n"):
        token = line.split(b" ", 1)[0].decode("ascii", "replace")
        if OID.fullmatch(token) and token not in seen:
            seen.add(token)
            ids.append(token)
    return ids


def load_objects(repo, ids):
    """{id: (type, bytes)} through one `git cat-file --batch`. A missing object is an error."""
    out = git(
        repo, "cat-file", "--batch", data="".join(oid + "\n" for oid in ids).encode()
    )
    objects, missing, pos = {}, 0, 0
    try:
        while pos < len(out):
            end = out.index(b"\n", pos)
            header = out[pos:end].split()
            if len(header) == 2 and header[1] == b"missing":
                missing, pos = missing + 1, end + 1
                continue
            size = int(header[2])
            objects[header[0].decode()] = (
                header[1].decode(),
                out[end + 1 : end + 1 + size],
            )
            pos = end + 1 + size + 1
    except (ValueError, IndexError):
        raise UsageError("git cat-file gave output this tool cannot read")
    if missing:
        raise UsageError(
            "%d objects that a ref reaches are missing from the repository (run git fsck): a clean result would prove nothing"
            % missing
        )
    return objects


def ref_names(repo):
    return [
        name
        for name in git(repo, "for-each-ref", "--format=%(refname)")
        .decode("utf-8", "replace")
        .split("\n")
        if name
    ]


def split_object(raw):
    """(header lines, message) of a commit or tag object; a header's continuation lines stay attached to it."""
    head, _sep, message = raw.decode("utf-8", "replace").partition("\n\n")
    lines = []
    for line in head.split("\n"):
        if line.startswith(" ") and lines:
            lines[-1] += "\n" + line
        else:
            lines.append(line)
    return lines, message


def strip_trailers(message):
    return "\n".join(
        line
        for line in message.splitlines()
        if not line.lower().startswith("signed-off-by:")
    )


class Findings:
    """What was found, as {entry number: {category: set of short commit ids or 'ref'}}. No word is ever kept."""

    def __init__(self, entries, allowed):
        self.norm = [normalise(entry) for entry in entries]
        self.allowed = allowed
        self.hits = defaultdict(lambda: defaultdict(set))

    def entries_in(self, text):
        flat = normalise(text)
        return {
            i
            for i, word in enumerate(self.norm, start=1)
            if len(word) >= MIN_LENGTH and word in flat
        }

    def add(self, category, text, labels):
        for number in self.entries_in(text):
            self.hits[number][category].update(labels)

    def add_identity(self, text, labels):
        """Author, committer or tagger text: an allowed identity passes here."""
        for number in self.entries_in(text) - self.allowed:
            self.hits[number]["metadata"].update(labels)

    def add_message(self, message, labels, category="message"):
        """A message: an allowed identity passes only in Signed-off-by lines, not in the body."""
        in_body = self.entries_in(strip_trailers(message))
        for number in self.entries_in(message):
            if number in self.allowed and number not in in_body:
                continue
            self.hits[number][category].update(labels)


def scan_commit(findings, sha, raw):
    """One commit object. Returns its root tree id."""
    label = [sha[:SHORT_ID]]
    lines, message = split_object(raw)
    tree = ""
    for line in lines:
        key, _sep, value = line.partition(" ")
        if key == "tree":
            tree = value
        elif key == "author" or key == "committer":
            findings.add_identity(value, label)
        elif key != "parent":
            # gpgsig, mergetag, encoding, or a header some tool added (its continuation lines are joined by split_object)
            findings.add("metadata", line, label)
    findings.add_message(message, label)
    return tree


def scan_tag(findings, raw):
    """One annotated tag object: the name inside it, its tagger, any extra header and its message."""
    lines, message = split_object(raw)
    for line in lines:
        key, _sep, value = line.partition(" ")
        if key == "tagger":
            findings.add_identity(value, ["ref"])
        elif key not in ("object", "type"):
            findings.add("tag", value if key == "tag" else line, ["ref"])
    findings.add_message(message, ["ref"], "tag")


def tree_entries(raw, size):
    """[(mode, name, id)] of one tree object whose ids are `size` bytes long."""
    entries, pos = [], 0
    while pos < len(raw):
        space = raw.index(b" ", pos)
        nul = raw.index(b"\0", space)
        entries.append(
            (
                raw[pos:space],
                raw[space + 1 : nul].decode("utf-8", "replace"),
                raw[nul + 1 : nul + 1 + size].hex(),
            )
        )
        pos = nul + 1 + size
    return entries


def tree_files(objects, root, size, seen_trees):
    """[(path, blob id or None)] under one tree; None marks a submodule entry, which has a name but no blob here."""
    found, stack = [], [(root, "")]
    while stack:
        tree, prefix = stack.pop()
        seen_trees.add(tree)
        for mode, name, oid in tree_entries(objects[tree][1], size):
            if mode == b"160000":
                found.append((prefix + name, None))
            elif mode == b"40000":
                stack.append((oid, prefix + name + "/"))
            else:
                found.append((prefix + name, oid))
    return found


def scan_trees(findings, objects, roots, size):
    """Paths and contents of every blob under the root tree of each commit, then of every tree or blob only a tag or a note reaches.
    Returns the number of distinct blobs scanned."""
    paths, owners, seen_trees = defaultdict(set), defaultdict(set), set()
    for sha, tree in roots:
        for path, blob in tree_files(objects, tree, size, seen_trees):
            findings.add("path", path, [sha[:SHORT_ID]])
            if blob:
                paths[blob].add(path)
                owners[blob].add(sha[:SHORT_ID])
    for oid, (kind, _raw) in list(objects.items()):
        if kind == "tree" and oid not in seen_trees:
            for path, blob in tree_files(objects, oid, size, seen_trees):
                findings.add("path", path, ["ref"])
                if blob:
                    owners[blob].add("ref")
        elif kind == "blob" and oid not in owners:
            owners[oid].add("ref")
    for blob, labels in owners.items():
        findings.add(
            "content", objects[blob][1].decode("utf-8", "replace"), sorted(labels)
        )
    return len(owners)


def scan(repo, entries, allowed):
    preflight(repo)
    commits = git(repo, "rev-list", "--all", "--reverse").decode().split()
    if not commits:
        raise UsageError(
            "the repository has no commits: a clean result would prove nothing"
        )
    objects = load_objects(repo, reachable_ids(repo))
    size = len(next(iter(objects))) // 2
    findings, roots = Findings(entries, allowed), []
    for name in ref_names(repo):
        findings.add("ref", name, ["ref"])
    try:
        roots = [(sha, scan_commit(findings, sha, objects[sha][1])) for sha in commits]
        return _scan_rest(findings, objects, roots, size, commits)
    except (ValueError, KeyError):
        raise UsageError("an object could not be read: the repository is inconsistent (run git fsck)")


def _scan_rest(findings, objects, roots, size, commits):
    for kind, raw in objects.values():
        if kind == "tag":
            scan_tag(findings, raw)
    return findings, commits, scan_trees(findings, objects, roots, size)


def report(findings, order, commit_count, blob_count, skipped_short, total_entries):
    lines = [
        "CHECK-HISTORY commits=%d blobs=%d entries=%d skipped_short=%d"
        % (commit_count, blob_count, total_entries, skipped_short)
    ]
    rank = {sha: i for i, sha in enumerate(order)}
    for number in sorted(findings.hits):
        parts = []
        for category in CATEGORIES:
            labels = findings.hits[number].get(category, set())
            if not labels:
                continue
            commits = sorted((label for label in labels if label in rank), key=rank.get)
            span = " first=%s last=%s" % (commits[0], commits[-1]) if commits else ""
            parts.append("%s=%d%s" % (category, len(labels), span))
        lines.append("ENTRY %d: %s" % (number, "; ".join(parts)))
    lines.append(
        "CLEAN"
        if not findings.hits
        else "NOT CLEAN entries=%s" % ",".join(str(n) for n in sorted(findings.hits))
    )
    return "\n".join(lines)


def run(args):
    entries = read_entries(args.words)
    allowed = set(args.allow_identity or ())
    if any(n < 1 or n > len(entries) for n in allowed):
        raise UsageError(
            "--allow-identity must be an entry number between 1 and %d" % len(entries)
        )
    skipped = sum(1 for entry in entries if len(normalise(entry)) < MIN_LENGTH)
    if skipped == len(entries):
        raise UsageError(
            "every entry is shorter than %d characters once normalised, so none can be checked"
            % MIN_LENGTH
        )
    if not os.path.isdir(args.repo):
        raise UsageError("not a directory: %s" % args.repo)
    findings, shas, blobs = scan(args.repo, entries, allowed)
    print(
        report(
            findings,
            [sha[:SHORT_ID] for sha in shas],
            len(shas),
            blobs,
            skipped,
            len(entries),
        )
    )
    return 1 if findings.hits else 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="check_history.py", description=__doc__.split("\n")[0], allow_abbrev=False
    )
    parser.add_argument(
        "--repo",
        default=".",
        help="the repository to check (default: the current directory)",
    )
    parser.add_argument(
        "--words",
        required=True,
        help="the private word list: one entry per line, # starts a comment",
    )
    parser.add_argument(
        "--allow-identity",
        type=int,
        action="append",
        help="entry number allowed in author, committer, tagger and Signed-off-by lines",
    )
    args = parser.parse_args(argv)
    try:
        return run(args)
    except UsageError as err:
        print("ERROR: %s" % err, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
