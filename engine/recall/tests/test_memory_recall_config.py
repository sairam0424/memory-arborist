"""Provider and model configuration of the headless children: minimal environment by default, explicit opt-in to pass more, model aliases."""

import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ENGINE)
sys.path.insert(0, os.path.join(ENGINE, "recall"))
import memory_recall_run as mrr  # noqa: E402

BASE = {"PATH": "/bin", "HOME": "/h"}


class PassThroughTests(unittest.TestCase):
    def test_names_listed_in_the_pass_through_variable_are_forwarded(self):
        env = mrr.child_env(
            dict(
                BASE,
                MEMORY_SHEPHERD_PASS_ENV="API_TOKEN_X, PROXY_URL ",
                API_TOKEN_X="a",
                PROXY_URL="p",
                UNLISTED_Y="no",
            )
        )
        self.assertEqual(
            env, {"PATH": "/bin", "HOME": "/h", "API_TOKEN_X": "a", "PROXY_URL": "p"}
        )

    def test_nothing_extra_is_forwarded_without_the_variable(self):
        env = mrr.child_env(dict(BASE, API_TOKEN_X="a", AWS_SECRET_ACCESS_KEY="b"))
        self.assertEqual(env, BASE)

    def test_the_pass_through_variable_itself_is_never_forwarded(self):
        env = mrr.child_env(
            dict(BASE, MEMORY_SHEPHERD_PASS_ENV="API_TOKEN_X", API_TOKEN_X="a")
        )
        self.assertNotIn("MEMORY_SHEPHERD_PASS_ENV", env)

    def test_the_pass_through_variable_is_not_forwarded_even_when_it_lists_itself(self):
        env = mrr.child_env(
            dict(
                BASE,
                MEMORY_SHEPHERD_PASS_ENV="MEMORY_SHEPHERD_PASS_ENV,API_TOKEN_X",
                API_TOKEN_X="a",
            )
        )
        self.assertNotIn("MEMORY_SHEPHERD_PASS_ENV", env)
        self.assertEqual(env["API_TOKEN_X"], "a")

    def test_malformed_names_are_ignored(self):
        bad = [
            "",
            " ",
            "has space",
            "A=B",
            "1LEADING",
            "semi;colon",
            "dollar$",
            "x" * 65,
            "-dash",
        ]
        source = dict(
            BASE, MEMORY_SHEPHERD_PASS_ENV=",".join(bad) + ",GOOD_NAME", GOOD_NAME="ok"
        )
        for name in bad:
            source[name] = "leak"
        self.assertEqual(
            mrr.child_env(source), {"PATH": "/bin", "HOME": "/h", "GOOD_NAME": "ok"}
        )

    def test_names_that_are_not_set_are_skipped_silently(self):
        self.assertEqual(
            mrr.child_env(dict(BASE, MEMORY_SHEPHERD_PASS_ENV="NOT_SET_AT_ALL")), BASE
        )

    def test_only_the_first_32_entries_are_honoured(self):
        names = ["VAR_%d" % i for i in range(40)]
        source = dict(BASE, MEMORY_SHEPHERD_PASS_ENV=",".join(names))
        source.update({n: "v" for n in names})
        env = mrr.child_env(source)
        self.assertEqual(len([k for k in env if k.startswith("VAR_")]), 32)
        self.assertNotIn("VAR_39", env)


class ModelDefaultTests(unittest.TestCase):
    def default_model(self, extra):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/tmp"}
        env.update(extra)
        code = (
            "import sys; sys.path[:0] = [%r, %r]; import memory_recall_run as m; print(m.DEFAULT_MODEL)"
            % (ENGINE, os.path.join(ENGINE, "recall"))
        )
        done = subprocess.run(
            [sys.executable, "-I", "-c", code],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip()

    def test_the_default_judge_model_is_the_haiku_alias_not_a_provider_specific_id(
        self,
    ):
        self.assertEqual(self.default_model({}), "haiku")

    def test_the_environment_can_pin_a_full_model_id(self):
        self.assertEqual(
            self.default_model(
                {"MEMORY_SHEPHERD_JUDGE_MODEL": "some-provider.model-id-1"}
            ),
            "some-provider.model-id-1",
        )

    def test_an_empty_override_falls_back_to_the_alias(self):
        self.assertEqual(
            self.default_model({"MEMORY_SHEPHERD_JUDGE_MODEL": ""}), "haiku"
        )


if __name__ == "__main__":
    unittest.main()
