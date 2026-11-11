"""Shared fixtures for the memory-shepherd tests. Fake credentials are assembled at runtime."""

import atexit
import os
import shutil
import stat
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)


def _remove_tree(root):
    """Remove a test tree even when a test left read-only directories in it on purpose."""
    for dirpath, dirnames, _files in os.walk(root):
        for name in dirnames:
            try:
                os.chmod(os.path.join(dirpath, name), stat.S_IRWXU)
            except OSError:
                pass
    shutil.rmtree(root, ignore_errors=True)


# One private temp root per test process: every tempfile.mkdtemp() below (and in the test files) lands inside it and the
# whole tree is removed at exit, so a run no longer leaves memtest_*, memops_* and tmp* folders behind.
PROCESS_ROOT = tempfile.mkdtemp(prefix="memtests_proc_")
tempfile.tempdir = PROCESS_ROOT
atexit.register(_remove_tree, PROCESS_ROOT)


def note(name, desc="a fine description", typ="feedback", body="body text", extra=""):
    return "---\nname: %s\ndescription: %s\nmetadata:\n  type: %s\n%s---\n\n%s\n" % (
        name,
        desc,
        typ,
        extra,
        body,
    )


def make_dir(files, prefix="memtest_"):
    root = tempfile.mkdtemp(prefix=prefix)
    for rel, content in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        mode = "wb" if isinstance(content, bytes) else "w"
        with open(path, mode) as fh:
            fh.write(content)
    return root


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def write(path, text, mode="w"):
    with open(path, mode, encoding="utf-8") as fh:
        fh.write(text)


def fake_github_token():
    return "gh" + "p_" + "aB3dE5gH7jK9" "mN1pQ3sT5vX7zA9cF1hJ3kLm"


def pattern_samples():
    """One runtime-assembled positive sample per base pattern of memory_scan."""
    return {
        "AWS Access Key": "AK" + "IA" + "ABCDEFGHIJKLMNOP",
        "AWS Secret Key": "aws_secret" + "_access_key = " + "a1B2" * 10,
        "GitHub Token": fake_github_token(),
        "GitHub PAT": "github" + "_pat_" + "A" * 25,
        "OpenAI Key": "sk-" + "A" * 24 + "T3Blbk" + "FJ",
        "Anthropic Key": "sk-" + "ant-" + "A" * 40,
        "Stripe Secret": "sk_" + "live_" + "A" * 24,
        "Slack Token": "xo" + "xb-" + "1234567890ab",
        "Private Key Block": "-----BEGIN " + "PRIVATE KEY-----",
        "Generic Secret Assign": "pass" + "word = " + '"hunter2hunter2"',
        "Bearer Token": "Bear" + "er " + "A" * 24,
        "Database URL": "post" + "gres://user:pw@host.example.com/db",
    }
