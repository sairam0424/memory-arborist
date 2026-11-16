"""Hardening tests for memory_scan and memory_common (lane H2): every pattern gets boundary, alphabet and tier checks.
Fake credentials are assembled at runtime from fragments."""

import contextlib
import hashlib
import os
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memtest_helpers as th  # noqa: E402

sys.path.insert(0, th.SCRIPTS)  # python3 -I does not put the script dir on sys.path
import memory_common as mc  # noqa: E402
import memory_scan as ms  # noqa: E402

IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


pairs = lambda text: {(h.pattern, h.tier) for h in ms.scan_text(text)}  # noqa: E731
found = lambda text: {h.pattern for h in ms.scan_text(text)}  # noqa: E731
rep = lambda count, char="A": char * count  # noqa: E731
quoted = lambda value: '"' + value + '"'  # noqa: E731


PW, SEC, TOK = "pass" + "word", "sec" + "ret", "tok" + "en"
AWSK = "aws_secret" + "_access_key"
UP_AWSK = "AWS_SECRET" + "_ACCESS_KEY"
VAL40 = "aB3/" * 10
AKIA, ASIA = "AK" + "IA", "AS" + "IA"
BR = "Bear" + "er"
SCHEMES = ("post" + "gres", "my" + "sql", "mongo" + "db", "re" + "dis", "post" + "gresql", "mongo" + "db+srv", "re" + "diss", "my" + "sqlx", "post" + "gresql+asyncpg", "my" + "sql+py" + "my" + "sql")
REPL, RLO, ZWSP = chr(0xFFFD), chr(0x202E), chr(0x200B)
ELLIPSIS, BOM = chr(0x2026), chr(0xFEFF)


key_header = lambda word: "-----BEGIN " + word + "PRIVATE KEY-----"  # noqa: E731
db_url = lambda scheme, user="u", host="hosts": scheme + "://" + user + "@" + host  # noqa: E731
env = lambda word, value, prefix="MY_", left=0, right=0, quote="": prefix + word + rep(left, " ") + "=" + rep(right, " ") + quote + value  # noqa: E731
mode_of = lambda path: stat.S_IMODE(os.stat(path).st_mode)  # noqa: E731


def block_table():
    return [
        (
            "AWS Secret Key",
            "block",
            [AWSK + " = " + VAL40, AWSK + ":" + VAL40, AWSK + '="' + VAL40, "secret" + "_key=" + VAL40, AWSK + rep(100, "x") + "=" + VAL40, AWSK + "=" + rep(20, " ") + VAL40, AWSK + "='" + VAL40],
            [AWSK + "=" + VAL40[:39], AWSK + rep(101, "x") + "=" + VAL40, AWSK + "=" + rep(21, " ") + VAL40, UP_AWSK + "=" + VAL40],
        ),
        ("GitHub Token", "block", ["gh" + "p_" + rep(36), "gh" + "s_" + rep(36), "gh" + "o_" + rep(36), "gh" + "u_" + rep(36), "gh" + "r_" + rep(36), "gh" + "p_" + rep(10) + "_" + rep(25)], ["gh" + "p_" + rep(35), "gh" + "x_" + rep(36), "gh" + "o_" + rep(35)]),
        ("Slack Token", "block", ["xo" + "x" + letter + "-" + rep(10, "1") for letter in "bpars"], ["xo" + "xb-" + rep(9, "1"), "xo" + "xz-" + rep(10, "1")]),
        ("Private Key Block", "block", [key_header(word) for word in ("", "RSA ", "EC ", "DSA ")], [key_header("OPENSSH "), key_header("FOO "), "-----BEGIN " + "PRIVATE KEY----", "----BEGIN " + "PRIVATE KEY-----"]),
        (
            "Generic Secret Assign",
            "block",
            [kw + " = " + quoted(rep(8, "x")) for kw in (PW, SEC, TOK, "api_key", "apikey")]
            + [PW.upper() + ": '" + rep(8, "x") + "'", TOK.title() + "=" + quoted(rep(512, "x")), PW + rep(20, " ") + "=" + rep(20, " ") + quoted(rep(8, "x")), PW + ' = "' + rep(8, "x") + "'", PW + " = '" + rep(8, "x") + '"'],
            [PW + " = " + quoted(rep(7, "x")), PW + " = " + quoted(rep(513, "x")), PW + rep(21, " ") + "=" + quoted(rep(8, "x")), PW + " = " + rep(8, "x"), PW + ' = "abc' + "'" + 'defgh"'],
        ),
        ("Bearer Token", "block", [BR + " " + rep(20), BR + rep(20, " ") + rep(20), BR + "\t" + rep(10) + "-._" + rep(10), BR + " " + "aB+/cD~eF" * 3 + "==", BR.upper() + "\n" + "A1" * 12, "bear" + "er " + "aB3" * 8], [BR + " " + rep(19), BR + rep(20), BR + rep(21, " ") + rep(20), "bear" + "er " + rep(20), BR + " " + rep(20, "="), BR + ' realm="example.com"', "bearer tokens are rotated hourly", BR + " /api/v1/chat/completions/stream", "bearer authentication-scheme-header-value"]),
        ("Database URL", "block", [db_url(s) for s in SCHEMES] + [db_url(SCHEMES[0], rep(200, "u"))], [db_url(SCHEMES[0], ""), db_url(SCHEMES[0], rep(201, "u")), db_url(SCHEMES[0], host="host"), db_url("amqp"), db_url(SCHEMES[0], "@@@@@@@@"), db_url(SCHEMES[0], "a b"), "use re" + "diss:// for TLS", "mongo" + "db+srv" + "://cluster0.example.net/db"]),
    ]


def extra_table():
    sixteen = "A+/_-.=A" * 2
    key_context = ["authorization", "passwd", "api_key", "api-key", "apikey", "secret_key", "secret-key", "secretkey", "access_key", "access-key", "accesskey"]
    quoted_keys = [PW, "passwd", SEC, TOK, "api_key", "api-key", "apikey", "access_key", "access-key", "accesskey"]
    env_words = ("KEY", "SECRET", "TOKEN", "PASSWORD", "PASSWD")
    return [
        (
            "AWS Secret Key (any case)",
            "block",
            [UP_AWSK + "=" + VAL40, "aws" + "secretkey:" + VAL40, UP_AWSK + rep(100, "x") + "=" + VAL40, UP_AWSK + "=" + rep(20, " ") + VAL40, UP_AWSK + ":'" + VAL40, UP_AWSK + ':"' + VAL40],
            [UP_AWSK + "=" + VAL40[:39], UP_AWSK + rep(101, "x") + "=" + VAL40, UP_AWSK + "=" + rep(21, " ") + VAL40, "SECRET" + "_ACCESS_KEY=" + VAL40],
        ),
        ("Google Refresh Token", "block", ["1//" + rep(30), "x 1//" + rep(30), '"1//' + rep(30), "=1//" + rep(30), "1//" + "A_-" * 10], ["1//" + rep(29), "a1//" + rep(30), "91//" + rep(30), "A1//" + rep(30), ":1//" + rep(30), "/1//" + rep(30)]),
        ("Other Key Block", "block", [key_header(w) for w in ("OPENSSH ", "ENCRYPTED ", "PGP ")], [key_header("RSA "), key_header("FOO ")]),
        (
            "Key Context",
            "warn",
            [k + "=" + rep(16) for k in key_context] + ["authorization: " + sixteen, "AUTHORIZATION=" + rep(16), "api_key" + '": "' + rep(16), "api_key" + "': '" + rep(16), "authorization" + rep(10, " ") + ":" + rep(10, " ") + rep(16)],
            ["authorization=" + rep(15), "api_keys" + "=" + rep(16), "xapi" + "_key=" + rep(16), "authorization" + rep(11, " ") + ":" + rep(16), "authorization:" + rep(11, " ") + rep(16)],
        ),
        (
            "Quoted Key Value",
            "warn",
            [quoted(k) + ": " + quoted(rep(8, "x")) for k in quoted_keys] + [quoted(PW.upper()) + ":" + quoted(rep(512, "x")), quoted(TOK) + rep(10, " ") + ":" + rep(10, " ") + quoted(rep(8, "x"))],
            [quoted(PW) + ": " + quoted(rep(7, "x")), quoted(PW) + ": " + quoted(rep(513, "x")), quoted(PW) + rep(11, " ") + ": " + quoted(rep(8, "x")), quoted(PW) + ":" + rep(11, " ") + quoted(rep(8, "x")), quoted(PW) + ": " + quoted("abcd\nefghij"), quoted(PW) + ': "abcd' + '"efghij"'],
        ),
        (
            "Env Assignment",
            "warn",
            [env(w, rep(12)) for w in env_words] + [env("TOKEN", rep(12), left=5, right=5), env("TOKEN", rep(12), quote='"'), env("TOKEN", rep(12), quote="'"), env("KEY", rep(12), prefix=rep(40)), "KEY" + rep(40) + "=" + rep(12)],
            [env("TOKEN", rep(11)), env("token", rep(12), prefix="my_"), "MYVAR=" + rep(12), "abc" + "KEY=" + rep(12), env("TOKEN", rep(12), left=6), env("TOKEN", rep(12), right=6), env("KEY", rep(12), prefix=rep(41)), "KEY" + rep(41) + "=" + rep(12)],
        ),
        (
            "Email",
            "warn",
            ["a@b.co", "first.last+tag@sub.example.com", "x@y-z.io", "a@b." + rep(24, "c"), "a@x.b.com", "a@" + rep(63, "9") + ".com", "a@x." + rep(63, "9") + ".com", "a@b.c.d.e.f.g.com"],
            ["a@b.c", "no-at-sign.com", "@b.com", "a@.com", "a@b", "a@x..com", "a@" + rep(64, "9") + ".com", "a@x." + rep(64, "9") + ".com", "a@b.c.d.e.f.g.h.com"],
        ),
        ("Aadhaar", "warn", ["1234 5678 9012", "x 1234 5678 9012 y", "1234\t5678\t9012"], ["12345 5678 9012", "1234 5678 90123", "1234 5678 901", "1234  5678 9012"]),
        ("PAN", "warn", ["ABCDE1234F", " ABCDE1234F ", "(ABCDE1234F)"], ["XABCDE1234F", "ABCDE1234FG", "abcde1234f", "ABCDE12345", "ABCDEF1234F", "ABCD1234F"]),
        (
            "Phone",
            "warn",
            ["98765 43210", "98765-43210", "+91 98765 43210", "+91-98765-43210", "555-123-4567", "555.123.4567", "555 123 4567", "+12398765 43210", "+998765 43210"],
            ["898765 43210", "98765 432101", ".98765 43210", "98765 43210.", "1555-123-4567", "555-123-45678", "555-123-4567.1", "555/123/4567", "555-123-456", "9876543210", "+123498765 43210"],
        ),
    ]


# (pattern, tier, prefix, suffix, minimum, maximum or None, characters the value may be made of, characters it may not contain)
RUNS = [
    ("AWS Access Key", "block", AKIA, "", 16, None, "A0", "a-"),
    ("AWS Temp Key", "block", ASIA, "", 16, None, "A0", "a-"),
    ("GitHub Token", "block", "gh" + "p_", "", 36, None, "aA0_", ".-"),
    ("GitHub PAT", "block", "github" + "_pat_", "", 22, None, "aA0_", ".-"),
    ("OpenAI Key", "block", "sk-", "T3Blbk" + "FJ", 20, 200, "aA0", "_-."),
    ("Anthropic Key", "block", "sk-" + "ant-", "", 40, None, "aA0_-", "."),
    ("Stripe Secret", "block", "sk_" + "live_", "", 24, None, "aA0", "_-"),
    ("Stripe Secret", "block", "sk_" + "test_", "", 24, None, "aA0", "_-"),
    ("Slack Token", "block", "xo" + "xb-", "", 10, None, "aA0-", "_."),
    ("AWS Secret Key", "block", AWSK + "=", "", 40, None, "aA0/+=", "_-."),
    ("AWS Secret Key (any case)", "block", UP_AWSK + "=", "", 40, None, "aA0/+=", "_-."),
    ("Google OAuth Token", "block", "ya29" + ".", "", 20, None, "aA0_-", "."),
    ("Google Refresh Token", "block", "1//", "", 30, None, "aA0_-", "."),
    ("JWT", "block", "eyJ", ".eyJ" + rep(8) + "." + rep(8), 8, 300, "aA0_-", ".+"),
    ("JWT", "block", "eyJ" + rep(8) + ".eyJ", "." + rep(8), 8, 300, "aA0_-", ".+"),
    ("JWT", "block", "eyJ" + rep(8) + ".eyJ" + rep(8) + ".", "", 8, None, "aA0_-", ".+"),
    ("npm Token", "block", "npm_", "", 36, None, "aA0", "_-"),
    ("Bearer Token", "block", BR + " ", "", 20, None, "aA0_-.+~", "/$=!"),
    ("GitHub Token", "block", "gh" + "o_", "", 36, None, "aA0_", ".-"),
    ("GitHub Token", "block", "gh" + "u_", "", 36, None, "aA0_", ".-"),
    ("GitHub Token", "block", "gh" + "r_", "", 36, None, "aA0_", ".-"),
    ("OpenAI Key", "block", "sk-" + "proj-", "", 40, None, "aA0_-", "."),
    ("OpenAI Key", "block", "sk-" + "svcacct-", "", 40, None, "aA0_-", "."),
    ("OpenAI Key", "block", "sk-" + "admin-", "", 40, None, "aA0_-", "."),
    ("Stripe Secret", "block", "rk_" + "live_", "", 24, None, "aA0", "_-"),
    ("Stripe Secret", "block", "rk_" + "test_", "", 24, None, "aA0", "_-"),
    ("Stripe Secret", "block", "sk_" + "org_", "", 24, None, "aA0", "_-"),
    ("Slack Token", "block", "xo" + "xe-", "", 10, None, "aA0-", "_."),
    ("Slack Token", "block", "xa" + "pp-1-", "", 10, None, "aA0-", "_."),
    ("AWS Temp Key", "block", "AB" + "IA", "", 16, None, "A0", "a-"),
    ("AWS Temp Key", "block", "AC" + "CA", "", 16, None, "A0", "a-"),
    ("Key Context", "warn", "api" + "_key=", "", 16, None, "aA0+/_-.=", "!"),
    ("Env Assignment", "warn", "MY_" + "TOKEN=", "", 12, None, "aA0+/_-.=", "!"),
    ("hex64", "warn", "", "", 64, None, "0aA", "g"),
    ("Email", "warn", "", "@b.co", 1, None, "aA0._%+-", ""),
    ("Email", "warn", "a@", ".co", 1, 63, "aA0-", "_"),
    ("Email", "warn", "a@b.", ".co", 1, 63, "aA0-", "_"),
    ("Email", "warn", "a@b.", "", 2, None, "aA", "0-"),
]


class PatternTableTests(unittest.TestCase):
    def test_value_alphabets_and_length_bounds(self):
        for name, tier, prefix, suffix, low, high, accepted, rejected in RUNS:
            sample = lambda ch, count: prefix + ch * count + suffix  # noqa: E731
            with self.subTest(name=name, prefix=prefix[:12]):
                for ch in accepted:
                    self.assertIn((name, tier), pairs(sample(ch, low)), ch)
                for ch in rejected:
                    self.assertNotIn(name, found(sample(ch, low)), ch)
                self.assertNotIn(name, found(sample(accepted[-1], low - 1)))
                if high:
                    self.assertIn(name, found(sample(accepted[-1], high)))
                    self.assertNotIn(name, found(sample(accepted[-1], high + 1)))

    def check(self, table):
        for name, tier, positives, negatives in table:
            for text in positives:
                with self.subTest(name=name, positive=text[:40]):
                    self.assertIn((name, tier), pairs(text))
            for text in negatives:
                with self.subTest(name=name, negative=text[:40]):
                    self.assertNotIn(name, found(text))

    def test_every_pattern_boundaries_and_tier(self):
        self.check(block_table())
        self.check(extra_table())

    def test_the_tables_cover_every_pattern_and_the_pattern_tiers_match(self):
        tabled = {name: tier for name, tier, _p, _n in block_table() + extra_table()} | {row[0]: row[1] for row in RUNS}
        declared = {name: tier for name, _rx, tier in ms.BASE_PATTERNS + ms.EXTRA_PATTERNS}
        self.assertEqual(tabled, declared)
        self.assertEqual(found("x " + "0123456789abcdef" * 4), {"hex64"})


MIXED = "aB3dE5gH7jK9mN1pQ2sT4vX6zA8cF0hJ"
LOWNUM = "a3b5c7d9e1f2g4h6i8j0kmnpqrstuvwxyz"
UPPLOW = "aBcDeFgHiJkLmNoPqRsTuVwXyZ"
B64DIG = "aB3dE5gH7jK9/mN+1pQ2sT4vX6zA8cF0hJ"
B64LET = "aBcDeFgHiJkL/mNoPqRsTuVwXyZ"


def profile(pool, *counts):
    """pool[i] appears counts[i] times, interleaved pass by pass so a separator symbol splits the run."""
    return "".join(sym for rnd in range(1, max(counts) + 1) for sym, count in zip(pool, counts) if count >= rnd)


def layered(pool, doubled, singles):
    return profile(pool, *([2] * doubled + [1] * singles))


class ScanTextShapeTests(unittest.TestCase):
    def test_hits_are_deduplicated_sorted_and_carry_the_path(self):
        token = th.fake_github_token()
        text = "a@b.co\n" + token + " " + token + " c@d.org\n\n" + "AB" + "CDE1234F"
        hits = ms.scan_text(text, "n.md")
        self.assertEqual([(h.path, h.line, h.pattern, h.tier) for h in hits], [("n.md", 1, "Email", "warn"), ("n.md", 2, "Email", "warn"), ("n.md", 2, "GitHub Token", "block"), ("n.md", 4, "PAN", "warn")])
        crlf = ms.scan_text("x\r\n" + token + "\r\nsomeone@example.org")
        self.assertEqual([(h.path, h.line, h.pattern) for h in crlf], [("", 2, "GitHub Token"), ("", 3, "Email")])
        self.assertEqual([h.line for h in ms.scan_text("\n\n" + token)], [3])
        self.assertEqual([h.line for h in ms.scan_text("a\rb\x0c\u2028someone@example.org")], [1])

    def test_entropy_guess_is_skipped_only_on_lines_with_a_block_hit(self):
        run = layered(MIXED, 12, 8)
        block_line = ASIA + rep(16) + " " + run
        self.assertNotIn("high-entropy", found(block_line))
        self.assertIn("high-entropy", found("someone@example.com " + run))
        self.assertIn("high-entropy", found(run))
        self.assertEqual([h.line for h in ms.scan_text(block_line + "\n" + run) if h.pattern == "high-entropy"], [2])


class EntropyDetectorTests(unittest.TestCase):
    def test_entropy_function_values(self):
        self.assertIsInstance(ms.shannon_entropy(""), float)
        self.assertEqual(ms.shannon_entropy("aaaa"), 0.0)
        self.assertAlmostEqual(ms.shannon_entropy("abcd"), 2.0)
        self.assertAlmostEqual(ms.shannon_entropy("aabb"), 1.0)
        self.assertAlmostEqual(ms.shannon_entropy("aaab"), 0.811278, places=5)

    def test_urlsafe_threshold_sits_between_4_125_and_4_25(self):
        above, below = layered(MIXED, 12, 8), layered(MIXED, 14, 4)
        self.assertEqual((len(above), len(below)), (32, 32))
        self.assertAlmostEqual(ms.shannon_entropy(above), 4.25)
        self.assertAlmostEqual(ms.shannon_entropy(below), 4.125)
        self.assertEqual(pairs("id " + above), {("high-entropy", "warn")})
        self.assertEqual(ms.scan_text("id " + below), [])
        thirty_one = layered(MIXED, 11, 9)
        self.assertEqual(len(thirty_one), 31)
        self.assertGreater(ms.shannon_entropy(thirty_one), 4.2)
        self.assertEqual(ms.scan_text(thirty_one), [])

    def test_class_rules_two_classes_plain_three_with_a_separator(self):
        plain_lower_digit = layered(LOWNUM, 12, 8)
        self.assertEqual(found(plain_lower_digit), {"high-entropy"})
        self.assertEqual(found(layered(UPPLOW, 12, 8)), {"high-entropy"})
        for separator in ("-", "_"):
            with self.subTest(separator):
                two_classes = plain_lower_digit[:-1] + separator
                self.assertEqual(ms.scan_text(two_classes), [])
                three_classes = layered(MIXED, 12, 8)[:-1] + separator
                self.assertEqual(found(three_classes), {"high-entropy"})
        self.assertEqual(ms.scan_text(layered("abcdefghijklmnopqrstuvwxyz", 12, 8)), [])
        self.assertEqual(ms.scan_text(layered("ABCDEFGHIJKLMNOPQRSTUVWXYZ", 12, 8)), [])
        self.assertEqual(ms.scan_text(layered("0123456789", 5, 5)), [])

    def test_base64_threshold_length_and_digit_requirement(self):
        above, below = layered(B64DIG, 16, 8), layered(B64DIG, 18, 4)
        self.assertEqual((len(above), len(below)), (40, 40))
        self.assertGreater(ms.shannon_entropy(above), 4.5)
        self.assertLess(ms.shannon_entropy(below), 4.5)
        self.assertGreater(ms.shannon_entropy(below), 4.2)
        self.assertEqual(pairs("blob " + above), {("high-entropy-b64", "warn")})
        self.assertEqual(ms.scan_text("blob " + below), [])
        short = layered(B64DIG, 15, 9)
        self.assertEqual(len(short), 39)
        self.assertGreater(ms.shannon_entropy(short), 4.5)
        self.assertEqual(ms.scan_text(short), [])
        no_digit = layered(B64LET, 16, 8)
        self.assertGreater(ms.shannon_entropy(no_digit), 4.5)
        self.assertEqual(ms.scan_text(no_digit), [])
        self.assertEqual(found(above + "=="), {"high-entropy-b64"})

    def test_base64_threshold_is_exclusive_and_padding_counts_up_to_two_equals(self):
        exact = profile(B64DIG, *([4] * 8 + [2] * 16))
        self.assertEqual((len(exact), ms.shannon_entropy(exact)), (64, 4.5))
        self.assertEqual(ms.scan_text(exact), [])
        run = layered(B64DIG, 20, 2)
        self.assertLess(ms.shannon_entropy(run + "="), 4.5)
        self.assertGreater(ms.shannon_entropy(run + "=="), 4.5)
        self.assertLess(ms.shannon_entropy(run + "==="), 4.5)
        for padded, expected in ((run, set()), (run + "=", set()), (run + "==", {"high-entropy-b64"}), (run + "===", {"high-entropy-b64"})):
            self.assertEqual(found(padded), expected, padded[-3:])


class DecodeAndFileTests(unittest.TestCase):
    def test_decode_strips_nul_in_both_paths_and_handles_both_utf16_orders(self):
        self.assertEqual(ms.decode_for_scan(b"ab\x00cd"), "abcd")
        self.assertEqual(ms.decode_for_scan(b"\xff\xfe" + "hi".encode("utf-16-le")), "hi")
        self.assertEqual(ms.decode_for_scan(b"\xfe\xff" + "hi".encode("utf-16-be")), "hi")
        self.assertEqual(ms.decode_for_scan("a\x00b".encode("utf-16")), "ab")
        self.assertEqual(ms.decode_for_scan(b""), "")
        self.assertEqual(ms.decode_for_scan(b"ab\x80cd"), "ab" + REPL + "cd")
        self.assertEqual(ms.decode_for_scan(b"\xff"), REPL)
        truncated = ms.decode_for_scan(b"\xff\xfe" + b"h\x00i")
        self.assertTrue(truncated.startswith("h"))

    def test_size_limit_is_strictly_greater_than_and_label_defaults_to_the_path(self):
        root = th.make_dir({"ten.md": rep(10, "x"), "eleven.md": rep(11, "x"), "f.md": th.fake_github_token() + "\n"})
        self.assertEqual(ms.scan_file(os.path.join(root, "f.md"))[0].path, os.path.join(root, "f.md"))
        self.assertEqual(ms.scan_file(os.path.join(root, "f.md"), "label")[0].path, "label")
        with mock.patch.object(ms, "MAX_SCAN_BYTES", 10):
            self.assertEqual(ms.scan_file(os.path.join(root, "ten.md")), [])
            with self.assertRaises(ValueError):
                ms.scan_file(os.path.join(root, "eleven.md"))

    def test_real_limit_is_fifty_mebibytes(self):
        root = tempfile.mkdtemp()
        limit = 50 * 1024 * 1024
        for name, size in (("at.bin", limit), ("over.bin", limit + 1)):
            with open(os.path.join(root, name), "wb") as fh:
                fh.truncate(size)
        self.assertEqual(ms.scan_file(os.path.join(root, "at.bin")), [])
        with self.assertRaises(ValueError):
            ms.scan_file(os.path.join(root, "over.bin"))

    @unittest.skipIf(IS_ROOT, "permission bits do not bind root")
    def test_scan_tree_reports_every_kind_of_skip_with_its_reason(self):
        token = th.fake_github_token()
        root = th.make_dir({"ok.md": "fine\n", "leak.md": token + "\n", "sub/inner.md": "x " + token + "\n", "big.md": rep(500, "x"), "noperm.md": "locked\n", "locked/in.md": "hidden\n"})
        os.symlink(os.path.join(root, "ok.md"), os.path.join(root, "link.md"))
        os.mkfifo(os.path.join(root, "pipe"))
        os.chmod(os.path.join(root, "noperm.md"), 0)
        os.chmod(os.path.join(root, "locked"), 0)
        try:
            with mock.patch.object(ms, "MAX_SCAN_BYTES", 200):
                res = ms.scan_tree(root)
        finally:
            os.chmod(os.path.join(root, "locked"), 0o700)
            os.chmod(os.path.join(root, "noperm.md"), 0o600)
        self.assertEqual(res.skipped, [("big.md", "larger than the 200 bytes scan limit, not scanned"), ("link.md", "symlink, not scanned"), ("locked", "unreadable directory: Permission denied"), ("noperm.md", "unreadable file: PermissionError"), ("pipe", "special file (FIFO or socket), not scanned")])
        self.assertEqual([(h.path, h.line, h.pattern, h.tier) for h in res.hits], [("leak.md", 1, "GitHub Token", "block"), ("sub/inner.md", 1, "GitHub Token", "block")])

    def test_format_hit_summarize_and_a_clean_tree(self):
        res = ms.scan_tree(th.make_dir({"a.md": "x\n", "d/b.md": "y\n"}))
        self.assertEqual((res.hits, res.skipped), ([], []))
        hit = ms.Hit("a\x1b[31m" + RLO + "b.md", 7, "Email", "warn")
        self.assertEqual(ms.format_hit(hit), "a?[31m?b.md:7:Email")
        self.assertEqual(ms.format_hit(ms.Hit("n.md", 12, "GitHub Token", "block")), "n.md:12:GitHub Token")
        self.assertEqual(ms.summarize([]), (0, 0))
        hits = [ms.Hit("p", 1, "a", "block"), ms.Hit("p", 2, "b", "warn"), ms.Hit("p", 3, "c", "warn")]
        self.assertEqual(ms.summarize(hits), (1, 2))
        self.assertEqual(ms.summarize(hits[:1]), (1, 0))


class SafeSnippetTests(unittest.TestCase):
    def test_length_edge_is_exactly_the_limit(self):
        self.assertEqual(ms.safe_snippet(rep(40, "x")), rep(40, "x"))
        self.assertEqual(ms.safe_snippet(rep(41, "x")), "<redacted 41 chars>")
        self.assertEqual(ms.safe_snippet("abcde", limit=5), "abcde")
        self.assertEqual(ms.safe_snippet("abcdef", limit=5), "<redacted 6 chars>")

    def test_each_redaction_clause_works_alone(self):
        block_only = AKIA + rep(16)
        self.assertEqual(ms.summarize(ms.scan_text(block_only))[0], 1)
        entropy_only = layered(LOWNUM, 12, 8)
        base64_only = layered(B64DIG, 16, 8)
        mixed_only = ["Aa1" * 5 + "A", "Aa1" * 3 + "+" + "Aa1" * 3, "Aa1" * 3 + "/" + "Aa1" * 3]
        for text in [block_only, entropy_only, base64_only] + mixed_only:
            with self.subTest(text):
                self.assertEqual(ms.safe_snippet(text), "<redacted %d chars>" % len(text))

    def test_harmless_text_passes_through_unchanged(self):
        for text in ("feedback-alpha", "someone@example.com", "abcdefgh12345678", "Aa1" * 5, "plain words here", "", "n/a"):
            with self.subTest(text):
                self.assertEqual(ms.safe_snippet(text), text)
        self.assertEqual(ms.safe_snippet(12345), "12345")

    def test_control_and_bidi_characters_become_question_marks(self):
        self.assertEqual(ms.safe_snippet("a\x1b[31mb"), "a?[31mb")
        self.assertEqual(ms.safe_snippet("a" + RLO + "b" + ZWSP + "c"), "a?b?c")


@contextlib.contextmanager
def fixed_zone(spec):
    try:
        with mock.patch.dict(os.environ, {"TZ": spec}):
            time.tzset()
            yield
    finally:
        time.tzset()


class HashPathAndTimeTests(unittest.TestCase):
    def test_sha256_file_hashes_every_chunk(self):
        root = tempfile.mkdtemp()
        mib = 1 << 20
        for size in (0, 1, mib - 1, mib, mib + 1, 3 * mib + 7):
            with self.subTest(size=size):
                data = (bytes(range(256)) * (size // 256 + 1))[:size]
                path = os.path.join(root, "f%d" % size)
                with open(path, "wb") as fh:
                    fh.write(data)
                self.assertEqual(mc.sha256_file(path), hashlib.sha256(data).hexdigest())
        for name, tail in (("a", b"A"), ("b", b"B")):
            with open(os.path.join(root, name), "wb") as fh:
                fh.write(b"x" * mib + tail)
        self.assertNotEqual(mc.sha256_file(os.path.join(root, "a")), mc.sha256_file(os.path.join(root, "b")))
        self.assertEqual(mc.sha256_bytes(b"abc"), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")

    def test_is_within_rejects_prefix_siblings_and_same_dir_follows_symlinks(self):
        base = tempfile.mkdtemp()
        mem, evil = os.path.join(base, "mem"), os.path.join(base, "mem-evil")
        os.mkdir(mem)
        os.mkdir(evil)
        os.symlink(mem, os.path.join(base, "alias"))
        self.assertTrue(mc.is_within(mem, mem))
        self.assertTrue(mc.is_within(mem, os.path.join(mem, "a", "b")))
        self.assertTrue(mc.is_within(mem + os.sep, os.path.join(mem, "f")))
        self.assertFalse(mc.is_within(mem, evil))
        self.assertFalse(mc.is_within(mem, os.path.join(evil, "x")))
        self.assertFalse(mc.is_within(mem, base))
        self.assertTrue(mc.same_dir(mem, os.path.join(base, "alias")))
        self.assertFalse(mc.same_dir(mem, evil))

    def test_slug_ops_dir_and_expand(self):
        self.assertEqual(mc.slug_for("/x/projects/-slug/memory/"), "-slug")
        self.assertEqual(mc.ops_dir_for("/x/projects/-slug/memory", "/ops"), "/ops/-slug")
        self.assertEqual(mc.ops_dir_for("/x/projects/-slug/memory"), os.path.join(mc.DEFAULT_OPS_ROOT, "-slug"))
        home = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"HOME": home}):
            self.assertEqual(mc.expand("~"), os.path.abspath(home))
            self.assertEqual(mc.expand("~/x/../y"), os.path.join(os.path.abspath(home), "y"))
        self.assertEqual(mc.expand("a/../b"), os.path.join(os.getcwd(), "b"))

    def test_stamps_with_an_explicit_now_including_zero_and_the_local_zone(self):
        self.assertEqual(mc.utc_stamp(0), "19700101T000000Z")
        self.assertEqual(mc.utc_stamp(1700000000.9), "20231114T221320Z")
        with fixed_zone("UTC0"):
            self.assertEqual(mc.local_date(0), "1970-01-01")
        with fixed_zone("IST-5:30"):
            self.assertEqual(mc.local_date(1700000000), "2023-11-15")
            self.assertEqual(mc.utc_stamp(1700000000), "20231114T221320Z")
        with fixed_zone("PST8"):
            self.assertEqual(mc.local_date(1700000000), "2023-11-14")
            self.assertEqual(mc.local_date(1700000000 - 15 * 3600), "2023-11-13")

    def test_free_bytes_probes_the_nearest_existing_ancestor(self):
        base = tempfile.mkdtemp()
        seen = []
        with mock.patch.object(mc.shutil, "disk_usage", lambda path: seen.append(path) or mock.Mock(total=1000, free=7)):
            self.assertEqual(mc.free_bytes(os.path.join(base, "no", "such", "dir")), 7)
            self.assertEqual(mc.free_bytes(base), 7)
            self.assertEqual(mc.free_bytes("/nonexistent-top-level-h2/x/y"), 7)
        self.assertEqual(seen, [base, base, "/"])
        self.assertGreater(mc.free_bytes(os.path.join(base, "x")), 0)

    def test_unique_path_counts_dangling_symlinks_as_taken(self):
        root = tempfile.mkdtemp()
        dest = os.path.join(root, "x.md")
        os.symlink(os.path.join(root, "missing-target"), dest)
        self.assertEqual(mc.unique_path(dest), dest + ".1")
        os.symlink(dest, dest + ".1")
        self.assertEqual(mc.unique_path(dest), dest + ".2")


class TextHelperTests(unittest.TestCase):
    def test_clean_line_limit_edges_and_whitespace(self):
        self.assertEqual(mc.clean_line(rep(10), 10), rep(10))
        self.assertEqual(mc.clean_line(rep(11), 10), rep(9) + ELLIPSIS)
        self.assertEqual(mc.clean_line("  a \t b\n\nc  ", 50), "a b c")
        self.assertEqual((mc.clean_line(123, 5), mc.clean_line("", 5)), ("123", ""))

    def test_control_character_ranges_are_replaced_and_neighbours_kept(self):
        replaced = [0x00, 0x1F, 0x7F, 0x85, 0x9F, 0x200B, 0x200F, 0x202A, 0x202E, 0x2066, 0x2069, 0xFEFF]
        kept = [0x20, 0x7E, 0xA0, 0xA1, 0x200A, 0x2010, 0x2029, 0x202F, 0x2065, 0x206A, 0xFEFE, 0xFF00, 0xE9, 0x1F600]
        for code in replaced:
            with self.subTest(replaced=hex(code)):
                self.assertEqual(mc.printable("a" + chr(code) + "b"), "a?b")
                self.assertEqual(mc.clean_line("a" + chr(code) + "b", 20), "a b")
        for code in kept:
            with self.subTest(kept=hex(code)):
                self.assertEqual(mc.printable("a" + chr(code) + "b"), "a" + chr(code) + "b")
        self.assertEqual((mc.printable(None), mc.printable(42)), ("None", "42"))

    def test_read_text_replaces_invalid_bytes_and_keeps_line_endings(self):
        root = th.make_dir({"a.md": b"ab\xffcd\r\nx\n", "b.md": b"\xef\xbb\xbfhi"})
        self.assertEqual(mc.read_text(os.path.join(root, "a.md")), "ab" + REPL + "cd\r\nx\n")
        self.assertEqual(mc.read_text(os.path.join(root, "b.md")), BOM + "hi")
        self.assertEqual((mc.utf16_len(""), mc.utf16_len("a" + chr(0x1F600)), mc.utf16_len("a\ud800")), (0, 3, 2))


class MakedirsAndLogTests(unittest.TestCase):
    def test_created_parents_are_private_even_under_a_hostile_umask_and_old_ones_untouched(self):
        root = tempfile.mkdtemp()
        os.chmod(root, 0o755)
        target = os.path.join(root, "a", "b", "c")
        old = os.umask(0o277)
        try:
            mc.makedirs_private(target)
        finally:
            os.umask(old)
        self.assertEqual(mode_of(root), 0o755)
        for sub in ("a", "a/b", "a/b/c"):
            self.assertEqual(mode_of(os.path.join(root, sub)), 0o700)

    def test_directories_and_log_are_created_private_not_only_tightened_afterwards(self):
        root = tempfile.mkdtemp()
        old = os.umask(0)
        try:
            with mock.patch.object(mc.os, "chmod"), mock.patch.object(mc.os, "fchmod"):
                mc.makedirs_private(os.path.join(root, "a", "b"))
                mc.append_log(os.path.join(root, "ops"), "lint", "x", now=0)
        finally:
            os.umask(old)
        self.assertEqual([mode_of(os.path.join(root, sub)) for sub in ("a", "a/b", "ops")], [0o700] * 3)
        self.assertEqual(mode_of(os.path.join(root, "ops", "log.md")), 0o600)

    def test_racing_creation_and_unchmoddable_directories_are_tolerated(self):
        root = tempfile.mkdtemp()
        target, real_mkdir = os.path.join(root, "raced"), os.mkdir

        def racing(path, mode=0o777):
            real_mkdir(path, 0o755)
            raise FileExistsError(path)

        with mock.patch.object(mc.os, "mkdir", racing):
            mc.makedirs_private(target)
        self.assertEqual(mode_of(target), 0o700)
        with mock.patch.object(mc.os, "chmod", side_effect=PermissionError("not ours")):
            mc.makedirs_private(root)

    def test_append_log_is_append_only_private_and_exactly_formatted(self):
        ops = os.path.join(tempfile.mkdtemp(), "ops")
        with fixed_zone("IST-5:30"):
            first = mc.append_log(ops, "lint", "one", ["b1", "b2"], now=1700000000)
            second = mc.append_log(ops, "scan", "two", now=1700000000)
        self.assertEqual((first, second), (os.path.join(ops, "log.md"),) * 2)
        self.assertEqual(th.read(first), "\n## [2023-11-15] lint | one\n- b1\n- b2\n\n## [2023-11-15] scan | two\n")
        self.assertEqual((mode_of(first), mode_of(ops)), (0o600, 0o700))

    def test_append_log_tightens_a_loose_existing_log_and_keeps_its_content(self):
        ops = tempfile.mkdtemp()
        path = os.path.join(ops, "log.md")
        th.write(path, "old entry\n")
        os.chmod(path, 0o644)
        mc.append_log(ops, "snapshot", "new entry", now=0)
        self.assertEqual(mode_of(path), 0o600)
        text = th.read(path)
        self.assertTrue(text.startswith("old entry\n\n## ["))
        self.assertTrue(text.endswith("] snapshot | new entry\n"))

    def test_append_log_accepts_every_valid_op_and_nothing_else(self):
        ops = os.path.join(tempfile.mkdtemp(), "ops")
        valid = {"snapshot", "lint", "scan", "evidence", "recall", "dream", "promote", "restore", "trim", "ablate", "decision", "compare"}
        self.assertEqual(set(mc.VALID_OPS), valid)
        for op in sorted(valid):
            mc.append_log(ops, op, "s", now=0)
        text = th.read(os.path.join(ops, "log.md"))
        self.assertEqual(sorted(line.split(" ")[2] for line in text.split("\n") if line.startswith("## [")), sorted(valid))
        fresh = os.path.join(tempfile.mkdtemp(), "never")
        for bad in ("delete", "", "LINT", None, "lint "):
            with self.subTest(bad), self.assertRaises(ValueError):
                mc.append_log(fresh, bad, "s")
        self.assertFalse(os.path.exists(fresh))

    def test_append_log_caps_subject_at_200_and_bullets_at_400(self):
        ops = os.path.join(tempfile.mkdtemp(), "ops")
        mc.append_log(ops, "lint", rep(200), [rep(400, "b"), rep(401, "c")], now=0)
        mc.append_log(ops, "lint", rep(201), now=0)
        lines = th.read(os.path.join(ops, "log.md")).split("\n")
        headers = [line.split(" | ", 1)[1] for line in lines if line.startswith("## [")]
        bullets = [line for line in lines if line.startswith("- ")]
        self.assertEqual(headers, [rep(200), rep(199) + ELLIPSIS])
        self.assertEqual(bullets, ["- " + rep(400, "b"), "- " + rep(399, "c") + ELLIPSIS])


class WalkTests(unittest.TestCase):
    def test_walk_classifies_every_kind_of_entry_with_slash_separated_paths(self):
        root = th.make_dir({"a.md": "x", ".hidden": "h", "d/e/deep.md": "y", "d/z.txt": "z", "empty/.keep": ""})
        os.mkdir(os.path.join(root, "emptydir"))
        for target, link in (("a.md", "flink"), ("d", "dlink"), ("gone", "dangling"), ("a.md", "d/e/nested-link")):
            os.symlink(os.path.join(root, target), os.path.join(root, link))
        os.mkfifo(os.path.join(root, "pipe"))
        os.mkfifo(os.path.join(root, "d", "e", "pipe2"))
        walk = mc.walk_files(root)
        self.assertEqual(walk.files, [".hidden", "a.md", "d/e/deep.md", "d/z.txt", "empty/.keep"])
        self.assertEqual(walk.symlinks, ["d/e/nested-link", "dangling", "dlink", "flink"])
        self.assertEqual((walk.special, walk.errors), (["d/e/pipe2", "pipe"], []))
        self.assertEqual(mc.list_files(root), walk.files)

    @unittest.skipIf(IS_ROOT, "permission bits do not bind root")
    def test_walk_reports_unreadable_directories_as_errors_not_files(self):
        root = th.make_dir({"ok.md": "x", "sub/inner/f.md": "y", "sub/g.md": "z"})
        locked = os.path.join(root, "sub", "inner")
        os.chmod(locked, 0)
        try:
            walk = mc.walk_files(root)
        finally:
            os.chmod(locked, 0o700)
        self.assertEqual(walk.files, ["ok.md", "sub/g.md"])
        self.assertEqual(walk.errors, [("sub/inner", "Permission denied")])

    def test_walk_rejects_missing_roots_and_plain_files_and_lists_filter_by_suffix(self):
        root = th.make_dir({"a.md": "1", "b.txt": "2", "c.md.1": "3", "s/d.md": "4"})
        for bad in (os.path.join(root, "nope"), os.path.join(root, "a.md")):
            with self.subTest(bad), self.assertRaises(FileNotFoundError):
                mc.walk_files(bad)
        self.assertEqual(mc.list_files(root), ["a.md", "b.txt", "c.md.1", "s/d.md"])
        self.assertEqual(mc.list_md(root), ["a.md", "s/d.md"])
