"""Tests for memory_scan (secret and PII shapes). Fake credentials are assembled at runtime."""

import os
import stat
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memtest_helpers as th  # noqa: E402

sys.path.insert(0, th.SCRIPTS)  # python3 -I does not put the script dir on sys.path
import memory_scan as ms  # noqa: E402


def names(hits):
    return {h.pattern for h in hits}


class BasePatternTests(unittest.TestCase):
    def test_every_base_pattern_has_a_sample_and_every_sample_a_pattern(self):
        self.assertEqual(
            sorted(name for name, _rx, _tier in ms.BASE_PATTERNS),
            sorted(th.pattern_samples()),
        )

    def test_every_base_pattern_detects_its_sample_at_block_tier(self):
        for name, sample in th.pattern_samples().items():
            with self.subTest(name):
                hits = ms.scan_text("prefix %s suffix" % sample)
                self.assertIn(name, names([h for h in hits if h.tier == "block"]))

    def test_multi_line_forms_are_found(self):
        text = "pass" + "word:\n    " + '"valuevalue123"'
        self.assertIn("Generic Secret Assign", names(ms.scan_text(text)))
        bearer = "Bear" + "er\n" + "A" * 24
        self.assertIn("Bearer Token", names(ms.scan_text(bearer)))

    def test_hit_line_number_points_at_the_match(self):
        hits = ms.scan_text("one\ntwo\n%s\nfour" % th.fake_github_token(), "n.md")
        self.assertEqual([h.line for h in hits if h.tier == "block"], [3])

    def test_matched_text_is_never_part_of_the_output(self):
        token = th.fake_github_token()
        rendered = " ".join(
            ms.format_hit(h) for h in ms.scan_text("x %s y" % token, "n.md")
        )
        self.assertNotIn(token, rendered)
        self.assertIn("n.md:1:", rendered)


class ExtraPatternTests(unittest.TestCase):
    def test_extra_credential_shapes(self):
        samples = {
            "AWS Temp Key": ("AS" + "IA") + "ABCDEFGHIJKLMNOP",
            "Google OAuth Token": "ya29" + ".a0Af" + "B" * 30,
            "JWT": "eyJ" + "A" * 12 + ".eyJ" + "B" * 12 + "." + "C" * 12,
            "npm Token": "npm_" + "aB3dE5" * 6,
            "Other Key Block": "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
            "AWS Secret Key (any case)": "AWS_SECRET"
            + "_ACCESS_KEY="
            + "aB3dE5gH7j" * 4,
        }
        for name, sample in samples.items():
            with self.subTest(name):
                hits = ms.scan_text(sample)
                self.assertTrue([h for h in hits if h.tier == "block"], name)

    def test_quoted_json_key_and_env_assignment_warn(self):
        json_style = '"api' + '_key": "' + "Zx9" * 6 + '"'
        env_style = "MY_SERVICE_" + "TOKEN=" + "aB3dE5gH7jK9mN1p"
        for text in (json_style, env_style):
            with self.subTest(text[:12]):
                self.assertTrue(any(h.tier == "warn" for h in ms.scan_text(text)))

    def test_pii_shapes_warn_only(self):
        for text in (
            "mail someone@example.com now",
            "pan ABCDE1234F here",
            "aadhaar 1234 5678 9012 ok",
            "call 98765 43210",
        ):
            with self.subTest(text):
                hits = ms.scan_text(text)
                self.assertTrue(hits)
                self.assertEqual({h.tier for h in hits}, {"warn"})


class EntropyTests(unittest.TestCase):
    def test_noisy_run_warns_but_natural_words_and_dated_slugs_do_not(self):
        self.assertTrue(
            any(
                h.tier == "warn"
                for h in ms.scan_text("Zq8Xv2Lm9Pk4Rt7Yw1Nb5Hd3Gc6Fs0JaUe8Ti2Oo")
            )
        )
        self.assertEqual(
            ms.scan_text("feedback-widget-hook-blocks-force-push-D"), []
        )
        self.assertEqual(
            ms.scan_text(
                "project-acmecorps-audit-paused-2026-01-15-parser-rewrite-critical-merged"
            ),
            [],
        )

    def test_mixed_case_snake_runs_warn(self):
        self.assertIn(
            "high-entropy",
            names(ms.scan_text("tok_aB3dE5gH7jK9" "mN1pQ3sT5vX7zA9cF1hJ3k")),
        )

    def test_hex_runs_warn_only_from_64_characters(self):
        self.assertEqual(ms.scan_text("commit " + "a1b2c3d4" * 5), [])
        self.assertIn("hex64", names(ms.scan_text("sha " + "a1b2c3d4" * 8)))
        self.assertIn("hex64", names(ms.scan_text("sha " + "a1b2c3d4" * 16)))

    def test_a_line_with_a_known_secret_is_not_double_reported(self):
        self.assertEqual(
            ms.summarize(
                ms.scan_text(th.fake_github_token() + "\nsomeone@example.com\n")
            ),
            (1, 1),
        )


class FileHandlingTests(unittest.TestCase):
    def scan_bytes(self, raw):
        root = th.make_dir({"f.md": raw})
        return ms.scan_tree(root)

    def test_nul_bytes_do_not_exempt_a_file(self):
        res = self.scan_bytes(b"\x00" + th.fake_github_token().encode())
        self.assertEqual(ms.summarize(res.hits)[0], 1)

    def test_utf16_files_with_a_bom_are_decoded_and_scanned(self):
        res = self.scan_bytes(("x " + th.fake_github_token()).encode("utf-16"))
        self.assertEqual(ms.summarize(res.hits)[0], 1)

    def test_symlinks_and_unreadable_files_are_reported_not_ignored(self):
        root = th.make_dir({"ok.md": "fine\n", "secret.md": "locked\n"})
        os.symlink(os.path.join(root, "ok.md"), os.path.join(root, "link.md"))
        os.chmod(os.path.join(root, "secret.md"), 0)
        try:
            res = ms.scan_tree(root)
        finally:
            os.chmod(os.path.join(root, "secret.md"), stat.S_IRUSR | stat.S_IWUSR)
        reasons = {path: reason for path, reason in res.skipped}
        self.assertIn("link.md", reasons)
        self.assertIn("secret.md", reasons)

    def test_missing_directory_is_an_error_not_a_clean_result(self):
        with self.assertRaises(FileNotFoundError):
            ms.scan_tree("/nonexistent/scan/dir")


class NewCredentialShapeTests(unittest.TestCase):
    """Shapes found missing against public documentation, each under its existing pattern name."""

    SAMPLES = {
        "GitHub Token": ("gh" + "o_" + "A" * 36, "gh" + "r_" + "aB3dE5gH7jK9" "mN1pQ3sT5vX7zA9cF1hJ3kLm"),
        "OpenAI Key": ("sk-" + "proj-" + "aB3_" * 12, "key=sk-" + "admin-" + "A" * 60 + "-_"),
        "Stripe Secret": ("rk_" + "live_" + "A" * 24, "sk_" + "org_" + "aB3dE5" * 5, "sk_" + "org_live_" + "A" * 24, "sk_" + "org_test_" + "A" * 24),
        "Slack Token": ("xo" + "xe-1-" + "a" * 12, "xa" + "pp-1-A0123ABCDEF-1234567890123-" + "a" * 20),
        "Bearer Token": ("bear" + "er " + "aB+/cD~e1" * 3 + "==", "BEAR" + "ER\n" + "A1" * 12),
        "Database URL": ("post" + "gresql://u:p@host.example.net/db", "mongo" + "db+srv://u:p@cluster0.example.net/db", "post" + "gresql+asyncpg://u:p@host.example.net/db", "my" + "sql+py" + "my" + "sql://u:p@host.example.net/db"),
        "AWS Temp Key": ("AB" + "IA" + "ABCDEFGHIJKLMNOP", "AC" + "CA" + "0123456789ABCDEF"),
    }

    def test_each_new_shape_hits_at_block_tier_under_its_existing_name(self):
        for name, samples in self.SAMPLES.items():
            for sample in samples:
                with self.subTest(name=name, sample=sample[:24]):
                    hits = ms.scan_text("note: %s end" % sample)
                    self.assertIn((name, "block"), {(h.pattern, h.tier) for h in hits})

    def test_ordinary_prose_about_these_formats_has_no_block_hit(self):
        prose = (
            "use re" + "diss:// for TLS",
            "the gho" + "_ prefix marks OAuth tokens",
            "bearer tokens are rotated hourly",
            "Authorization: Bear" + "er $OPENAI_API_KEY",
            "xapp" + "-level tokens are scoped to the app",
            "ta" + "sk" "-admin-panel-redesign-notes-and-more-things-2026-11-10",
            "di" + "sk-admin-" + "x" * 50,
            "ACCA exam results",
            "sk_org_settings_default_value_name_here",
            "net" + "work_live_" + "ABCDEFGHIJKLMNOPQRSTUVWXY",
            "A1B2C3D4" + "AC" + "CA" + "0123456789ABCDEF0123",
            "bear" + "er /api/v1/chat/completions/stream",
            "ta" + "sk-" + "ant-design-pro-components-table-migration-to-the-new-api-2026",
            "di" + "sk_" + "test_ConfigurationFixtureBuilderX",
            "x" + "AB" + "IA" + "ABCDEFGHIJKLMNOP",
            "use post" + "gresql+asyncpg:// for async access",
            "bear" + "er " + "a" * 101 + "1" + "a" * 20,
        )
        for text in prose:
            with self.subTest(text=text[:40]):
                self.assertEqual(ms.summarize(ms.scan_text(text))[0], 0)

    def test_the_lowercase_bearer_digit_window_is_a_hundred_characters(self):
        self.assertIn("Bearer Token", names(ms.scan_text("bear" + "er " + "a" * 99 + "1" + "a" * 20)))
        self.assertNotIn("Bearer Token", names(ms.scan_text("bear" + "er " + "a" * 101 + "1" + "a" * 20)))

    def test_a_real_anthropic_key_is_still_found_after_the_left_anchor(self):
        key = "sk-" + "ant-" + "A" * 40
        for text in ("key=" + key, "- [x]( " + key + " )", "\n" + key):
            self.assertIn("Anthropic Key", names(ms.scan_text(text)))


class SafetyTests(unittest.TestCase):
    def test_safe_snippet_redacts_token_like_text_and_strips_control_characters(self):
        self.assertEqual(ms.safe_snippet("short-target"), "short-target")
        self.assertIn("redacted", ms.safe_snippet(th.fake_github_token()))
        self.assertIn("redacted", ms.safe_snippet("x" * 200))
        self.assertNotIn("\x1b", ms.safe_snippet("a\x1b[31mb"))

    def test_adversarial_inputs_finish_quickly(self):
        big = 200_000
        cases = [
            "a" * big,
            "@" * big,
            "a@" * (big // 2),
            "secret_key " * (big // 11),
            "pass" + "word" * (big // 8),
            "Bear" + "er " + " " * big,
            "post" + "gres://" * (big // 11),
            "eyJ" * (big // 3),
            "sk-" * (big // 3),
            "pass" + 'word: "' * (big // 12),
            "authorization" + ": " * (big // 15),
            "1//" * (big // 3),
            "a" * 100 + "." * (big // 2),
            "9" * big,
            ("xa" + "pp-1-") * (big // 7),
            ("sk-" + "proj-") * (big // 8),
            ("sk-" + "admin-" + "a" * 39 + " ") * (big // 49),
            ("Bear" + "er /") * (big // 8),
            "Bear" + "er " + "/" * big,
            ("bear" + "er ") * (big // 7),
            ("re" + "diss://") * (big // 9),
            ("mongo" + "db+srv://") * (big // 14),
            ("post" + "gresql://") * (big // 13),
            ("AB" + "IA") * (big // 4),
            "sk_org_" * (big // 7),
            ("gh" + "o_") * (big // 4),
            ("rk_" + "live_") * (big // 8),
            ("post" + "gresql+psycopg2://") * (big // 18),
            ("ta" + "sk-" + "ant-") * (big // 9),
            ("di" + "sk_" + "test_") * (big // 10),
        ]
        started = time.monotonic()
        for text in cases:
            ms.scan_text(text)
        self.assertLess(
            time.monotonic() - started, 20, "a pattern backtracks super-linearly"
        )

    def test_many_block_hits_scale_linearly(self):
        text = "\n".join(th.fake_github_token() for _ in range(20_000))
        started = time.monotonic()
        hits = ms.scan_text(text)
        self.assertEqual(ms.summarize(hits)[0], 20_000)
        self.assertLess(time.monotonic() - started, 20)


if __name__ == "__main__":
    unittest.main()
