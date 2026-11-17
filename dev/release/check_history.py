#!/usr/bin/env python3
"""Check every revision of a git repository against the maintainer's private word list. Standard library plus git; run as python3 -I.

Why this exists: `git log -p | grep -f words` misses a word that was written as split string fragments, in different case, with
punctuation between its parts, in a file name, in a commit message, in a tag, or in an author line. This tool normalises both
sides (lower case, letters and digits only) and looks in every blob of every commit reachable from any ref, every path, every
commit message, every tag message and ref name, and every author, committer and tagger line.

It never prints a word from the list, nor a path that holds one: it prints entry NUMBERS (the position in the list after
comments and blank lines), counts and short commit ids. An identity (an email address you chose to keep in commit metadata) can
be allowed with --allow-identity N: hits of entry N in author, committer and tagger lines and in `Signed-off-by:` trailers then
pass, but the same entry in a file, a path, a message body or a ref name is still a hit.

    python3 -I check_history.py --repo . --words .release-guard-words [--allow-identity N ...]

Exit codes: 0 clean; 1 at least one hit; 2 usage or git error (a missing or empty list is an error, never a clean pass).
Entries shorter than MIN_LENGTH characters once normalised cannot be checked reliably and are skipped (`skipped_short=`).
"""

import argparse
import os
import re
import subprocess
import sys
from collections import defaultdict

MIN_LENGTH = 4
SHORT_ID = 7
RECORD, FIELD = "\x1e", "\x1f"
CATEGORIES = ("content", "path", "message", "tag", "metadata", "ref")


class UsageError(Exception):
    pass


def normalise(text):
    """Lower case, letters and digits only: quotes, commas, spaces, hyphens and underscores between fragments disappear."""
    return "".join(ch for ch in text.casefold() if ch.isalnum())


def read_entries(path):
    """The list's entries in order: one per line, `#` starts a comment, blank lines are ignored."""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError as err:
        raise UsageError(
            "cannot read the word list: %s" % (err.strerror or type(err).__name__)
        )
    entries = [line.split("#", 1)[0].strip() for line in lines]
    entries = [entry for entry in entries if entry]
    if not entries:
        raise UsageError("the word list has no entries")
    return entries


def git(repo, *args, data=None):
    try:
        done = subprocess.run(
            ["git", "-C", repo, *args], input=data, capture_output=True, check=True
        )
    except (OSError, subprocess.CalledProcessError) as err:
        raise UsageError(
            "git %s failed in %s"
            % (
                args[0],
                "the given path"
                if isinstance(err, subprocess.CalledProcessError)
                else "this environment",
            )
        )
    return done.stdout


def commits_with_messages(repo):
    """[(full id, identity text, message)] oldest first, over every ref."""
    raw = git(
        repo, "log", "--all", "--reverse", "--format=%H%x1f%an %ae %cn %ce%x1f%B%x1e"
    ).decode("utf-8", "replace")
    found = []
    for record in raw.split(RECORD):
        record = record.strip("\n")
        if record:
            sha, identity, message = (record.split(FIELD) + ["", ""])[:3]
            found.append((sha.strip(), identity, message))
    return found


def blobs_by_commit(repo, shas):
    """({blob id: [paths]}, {blob id: [commit ids]}) from the tree of every commit."""
    paths, owners = defaultdict(set), defaultdict(list)
    for sha in shas:
        for item in git(repo, "ls-tree", "-r", "-z", "--full-tree", sha).split(b"\0"):
            if not item:
                continue
            meta, _tab, path = item.partition(b"\t")
            fields = meta.split()
            if fields[1] != b"blob":
                continue
            oid = fields[2].decode()
            paths[oid].add(path.decode("utf-8", "replace"))
            owners[oid].append(sha)
    return paths, owners


def blob_texts(repo, oids):
    """{blob id: text} through one `git cat-file --batch` pass; an object that is missing or not a blob is skipped."""
    texts, out = {}, git(repo, "cat-file", "--batch", data="".join(oid + "\n" for oid in oids).encode())
    pos = 0
    while pos < len(out):
        end = out.index(b"\n", pos)
        header = out[pos:end].split()
        if len(header) < 3:
            pos = end + 1
            continue
        size = int(header[2])
        if header[1] == b"blob":
            texts[header[0].decode()] = out[end + 1 : end + 1 + size].decode("utf-8", "replace")
        pos = end + 1 + size + 1
    return texts


def ref_texts(repo):
    """(ref name, tagger identity, message) for every ref; a lightweight tag shows its commit's message."""
    raw = git(repo, "for-each-ref", "--format=%(refname)%1f%(taggername) %(taggeremail)%1f%(contents)%1e").decode("utf-8", "replace")
    items = []
    for record in raw.split(RECORD):
        record = record.strip("\n")
        if record:
            items.append(tuple((record.split(FIELD) + ["", ""])[:3]))
    return items


def strip_trailers(message):
    return "\n".join(line for line in message.splitlines() if not line.lower().startswith("signed-off-by:"))


class Findings:
    """What was found, as {entry number: {category: set of short commit ids or 'ref'}}. No word is ever kept."""

    def __init__(self, entries, allowed):
        self.norm = [normalise(entry) for entry in entries]
        self.allowed = allowed
        self.hits = defaultdict(lambda: defaultdict(set))

    def entries_in(self, text):
        flat = normalise(text)
        return {i for i, word in enumerate(self.norm, start=1) if len(word) >= MIN_LENGTH and word in flat}

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


def scan(repo, entries, allowed):
    findings = Findings(entries, allowed)
    commits = commits_with_messages(repo)
    shas = [sha for sha, _identity, _message in commits]
    for sha, identity, message in commits:
        findings.add_identity(identity, [sha[:SHORT_ID]])
        findings.add_message(message, [sha[:SHORT_ID]])
    for name, tagger, message in ref_texts(repo):
        findings.add("ref", name, ["ref"])
        findings.add_identity(tagger, ["ref"])
        findings.add_message(message, ["ref"], "tag")
    paths, owners = blobs_by_commit(repo, shas)
    for oid, text in blob_texts(repo, list(paths)).items():
        findings.add("content", text, [sha[:SHORT_ID] for sha in owners[oid]])
    for oid, names in paths.items():
        for name in names:
            findings.add("path", name, [sha[:SHORT_ID] for sha in owners[oid]])
    return findings, shas, len(paths)


def report(findings, order, commit_count, blob_count, skipped_short, total_entries):
    lines = ["CHECK-HISTORY commits=%d blobs=%d entries=%d skipped_short=%d" % (commit_count, blob_count, total_entries, skipped_short)]
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
    lines.append("CLEAN" if not findings.hits else "NOT CLEAN entries=%s" % ",".join(str(n) for n in sorted(findings.hits)))
    return "\n".join(lines)


def run(args):
    entries = read_entries(args.words)
    allowed = set(args.allow_identity or ())
    if any(n < 1 or n > len(entries) for n in allowed):
        raise UsageError(
            "--allow-identity must be an entry number between 1 and %d" % len(entries)
        )
    if not os.path.isdir(args.repo):
        raise UsageError("not a directory: %s" % args.repo)
    findings, shas, blobs = scan(args.repo, entries, allowed)
    skipped = sum(1 for word in findings.norm if len(word) < MIN_LENGTH)
    print(report(findings, [sha[:SHORT_ID] for sha in shas], len(shas), blobs, skipped, len(entries)))
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
