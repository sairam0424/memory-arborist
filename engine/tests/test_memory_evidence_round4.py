"""Regression tests for the third independent review of the evidence modules (R3-1 .. R3-8) and the test-strength gaps it named."""

import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import memtest_helpers as th  # noqa: E402

sys.path.insert(0, th.SCRIPTS)  # python3 -I does not put the script dir on sys.path
import memory_digest as md  # noqa: E402
import memory_evidence as me  # noqa: E402
import memory_redact as mr  # noqa: E402

NOW = 1_800_000_000
DAY = 86_400
VALUE = "correct" + "horsebattery"


class TempRoot(unittest.TestCase):
    """Every temp directory a test makes lives under one root that is removed afterwards."""

    @classmethod
    def setUpClass(cls):
        saved, root = tempfile.tempdir, tempfile.mkdtemp(prefix="memtests_")
        tempfile.tempdir = root
        cls.addClassCleanup(shutil.rmtree, root, True)
        cls.addClassCleanup(setattr, tempfile, "tempdir", saved)


def sid(n):
    return "%08d-0000-4000-8000-%012d" % (n, n)


def user(text, ts="2026-10-12T10:00:00.000Z", **extra):
    rec = {
        "type": "user",
        "timestamp": ts,
        "entrypoint": "cli",
        "cwd": "/work/proj",
        "isSidechain": False,
        "message": {"role": "user", "content": text},
    }
    rec.update(extra)
    return rec


def assistant(text, ts="2026-10-12T10:00:05.000Z"):
    return {
        "type": "assistant",
        "timestamp": ts,
        "entrypoint": "cli",
        "cwd": "/work/proj",
        "isSidechain": False,
        "message": {
            "role": "assistant",
            "model": "claude-sonnet-5",
            "content": [{"type": "text", "text": text}],
        },
    }


def write_transcript(directory, n, records, age_days=9):
    path = os.path.join(directory, "%s.jsonl" % sid(n))
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    stamp = NOW - age_days * DAY - n
    os.utime(path, (stamp, stamp))
    return path


def basic():
    return [
        user("how do we snapshot the memory directory?"),
        assistant("Use memory_hygiene.py snapshot."),
    ]


class Base(TempRoot):
    def setUp(self):
        self.source = tempfile.mkdtemp(prefix="evsrc_")
        self.out = os.path.join(tempfile.mkdtemp(prefix="evout_"), "evidence")

    def build(self, **kw):
        defaults = dict(limit=60, now=NOW, quiet_seconds=3600)
        defaults.update(kw)
        return me.build_all(self.source, self.out, **defaults)


class WeakLabelTests(TempRoot):
    def test_bare_pass_pwd_and_pw_labels_are_found_whatever_the_value_looks_like(self):
        for text in (
            "DB_HOST=db.internal\nDB_USER=app\nDB_" + "PASS=" + VALUE,
            "pass" + ": " + VALUE,
            "Server=x;" + "Pwd=" + VALUE + ";",
            "DB_" + "PW=Hunter2Hunter2",
            "MY_" + "PW=lettersonlyvaluehere",
        ):
            with self.subTest(text=text[:20]):
                self.assertTrue(mr.has_secret(text), text)

    def test_words_that_merely_end_in_pass_are_kept(self):
        for text in (
            "bypass: check the second step",
            "compass=north",
            "the overpass: closed",
        ):
            with self.subTest(text=text):
                self.assertFalse(mr.has_secret(text), text)

    def test_token_names_with_underscores_are_cue_words(self):
        for name in (
            "TWILIO_AUTH_TOKEN",
            "SLACK_BOT_TOKEN",
            "GITHUB_ACCESS_TOKEN",
            "CF_API_TOKEN",
            "OAUTH_REFRESH_TOKEN",
        ):
            with self.subTest(name=name):
                self.assertTrue(
                    mr.has_secret("heroku config:set %s to a1b2c3d4e5f6" % name), name
                )

    def test_each_cue_word_form_drops_a_sentence_that_has_no_other_cue(self):
        sentences = {
            "credential": "the credential store is offline",
            "api" "key": "regenerate the apikey today",
            "private key": "the private key rotation is next week",
            "bearer": "the bearer scheme is explained here",
            "x token": "the refresh token lifetime is one hour",
            "camel": "read mySessionToken from the jar",
            "ssh key": "add the ssh key to the agent",
            "sshpass": "sshpass is installed",
            "docker login": "docker login again please",
            "--token": "pass --token on the command line",
            "cookie": "Cookie: a=b",
            "recovery": "keep the recovery codes safe",
            "backup": "print the backup codes",
            "seed": "write the seed phrase on paper",
            "mnemonic": "the mnemonic is twelve words",
        }
        for label, text in sentences.items():
            with self.subTest(label):
                self.assertTrue(mr.has_secret(text), text)

    def test_a_basic_scheme_value_is_found_without_any_cue_word(self):
        import base64

        blob = base64.b64encode(("admin:Sup3r" + "S3cr3tPw9").encode()).decode()
        self.assertTrue(mr.has_secret("send Basic " + blob + " in the header"))


class ShapeGapTests(TempRoot):
    def test_percent_encoded_base64_signatures_are_found(self):
        for i in range(20):
            raw = hashlib.sha256(b"sig%d" % i).digest()
            import base64
            import urllib.parse as up

            quoted = up.quote(base64.b64encode(raw).decode(), safe="")
            self.assertTrue(
                mr.has_secret(
                    "https://x.example/blob?sp=rl&se=2026-12-01T00:00:00Z&sig=" + quoted
                ),
                quoted,
            )

    def test_prefix_plus_hex_vendor_keys_are_found(self):
        keys = [
            "fc-" + "0123456789abcdef" * 2,
            "pplx-" + "0123456789abcdef" * 3,
            "sbp_" + "0123456789abcdef" * 2 + "01234567",
            "rubygems_" + "0123456789abcdef" * 3,
            "0123456789abcdef" * 2 + "-us21",
            "PMAK-"
            + "0123456789abcdef01234567"
            + "-"
            + "0123456789abcdef0123456789abcdef01",
        ]
        for key in keys:
            with self.subTest(key=key[:8]):
                self.assertTrue(mr.has_secret("here: " + key), key)

    def test_indian_ids_and_upi_handles_are_masked(self):
        cases = {
            "ABCDE1234F": "[pan]",
            "234567890123": "[id]",
            "2345-6789-0123": "[id]",
            "alex@" + "oksbi": "[upi]",
            "HDFC0001234": "[ifsc]",
        }
        for raw, marker in cases.items():
            with self.subTest(raw=raw):
                masked, changed = mr.mask_pii("id is " + raw + " ok")
                self.assertTrue(changed)
                self.assertIn(marker, masked)

    def test_a_wrapped_secret_scan_that_runs_out_of_budget_fails_closed(self):
        original = mr.WRAP_BUDGET_CHARS
        mr.WRAP_BUDGET_CHARS = 5
        try:
            import base64

            self.assertTrue(
                mr.has_secret(
                    "note "
                    + base64.b64encode(b"hello world hello world hello").decode()
                )
            )
        finally:
            mr.WRAP_BUDGET_CHARS = original


class BoundTests(TempRoot):
    def test_characters_that_explode_under_normalisation_stay_cheap(self):
        started = time.process_time()
        mr.sanitize("ﷺ" * 200000)
        self.assertLess(time.process_time() - started, 6.0)

    def test_a_giant_user_block_is_never_scanned(self):
        started = time.process_time()
        piece = md._human_piece("a " * 4_000_000)
        self.assertGreater(len(piece), mr.MAX_RECORD_CHARS)
        self.assertLess(time.process_time() - started, 1.0)

    def test_the_oversize_cap_is_checked_before_any_scanning(self):
        original = mr.has_secret

        def forbidden(_text):
            raise AssertionError("has_secret ran on an oversize record")

        mr.has_secret = forbidden
        try:
            self.assertTrue(mr.sanitize("x" * (mr.MAX_RECORD_CHARS + 1)).oversize)
        finally:
            mr.has_secret = original

    def test_multilingual_prose_keeps_its_text(self):
        for text in (
            "un café très chaud",
            "日本語のメモ",
            "Привет, как дела",
            "Ελληνικά σήμερα",
        ):
            with self.subTest(text=text):
                self.assertEqual(mr.sanitize(text).excerpt, text)


class PinningTests(Base):
    def test_a_source_scan_samples_only_human_sessions(self):
        for n in range(1, 4):
            write_transcript(
                self.source, n, [user("headless prompt text", entrypoint="sdk-py")]
            )
        for n in range(4, 6):
            write_transcript(self.source, n, basic())
        scanned, _secret, _pii = me.scan_sources(
            self.source, 2, now=NOW, quiet_seconds=3600
        )
        self.assertEqual(scanned, 2)


class WriteFailureTests(Base):
    def test_a_failed_write_stops_the_build_and_names_the_partial_file(self):
        for n in (1, 2, 3):
            write_transcript(self.source, n, basic(), age_days=20 - n)
        original = me._write_once
        calls = {"n": 0}

        def flaky(path, text):
            calls["n"] += 1
            if calls["n"] == 2:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(text[:50])
                raise OSError(28, "No space left on device")
            return original(path, text)

        me._write_once = flaky
        try:
            result = self.build()
        finally:
            me._write_once = original
        self.assertEqual(result.written, [sid(3)])
        self.assertEqual(result.failed, sid(2))
        self.assertEqual(result.skipped["write_failed"], 1)

    def test_the_command_reports_the_partial_file_and_exits_1(self):
        write_transcript(self.source, 1, basic())
        original = me._write_once
        me._write_once = lambda path, text: (_ for _ in ()).throw(
            OSError(28, "No space left on device")
        )
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer):
                code = me.main(
                    [
                        "build",
                        "--source",
                        self.source,
                        "--out",
                        self.out,
                        "--quiet-seconds",
                        "3600",
                        "--now",
                        str(NOW),
                    ]
                )
        finally:
            me._write_once = original
        self.assertEqual(code, 1)
        self.assertIn("WRITE_FAILED", buffer.getvalue())


class VerifyLimitsTests(Base):
    def make_good(self):
        write_transcript(self.source, 1, basic())
        self.build()
        self.path = os.path.join(self.out, sid(1) + ".md")
        os.chmod(self.path, 0o600)

    def rewrite(self, change):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(change(self.original))

    def test_content_that_build_never_writes_is_rejected(self):
        self.make_good()
        self.original = th.read(self.path)
        cases = {
            "impossible clock": lambda d: d.replace("U 10:00 how", "U 99:99 how", 1),
            "overlong body line": lambda d: d.replace(
                "U 10:00 how do we", "U 10:00 " + "word " * 120 + "how do we", 1
            ),
            "no body lines": lambda d: re.sub(r"user=[0-9]+/[0-9]+ assistant=[0-9]+/[0-9]+", "user=0/0 assistant=0/0", d.split("\n---\n")[0]) + "\n---\n",
        }
        for label, change in cases.items():
            with self.subTest(label):
                self.rewrite(change)
                self.assertFalse(me.verify_dir(self.out).ok, label)
        self.rewrite(lambda d: d)
        self.assertTrue(me.verify_dir(self.out).ok)

    def test_a_group_writable_evidence_directory_is_rejected(self):
        self.make_good()
        os.chmod(self.out, 0o777)
        self.addCleanup(os.chmod, self.out, 0o700)
        self.assertFalse(me.verify_dir(self.out).ok)

    def test_an_unreadable_digest_is_a_problem_not_a_crash(self):
        self.make_good()
        os.chmod(self.path, 0)
        self.addCleanup(os.chmod, self.path, 0o600)
        self.assertFalse(me.verify_dir(self.out).ok)


if __name__ == "__main__":
    unittest.main()
