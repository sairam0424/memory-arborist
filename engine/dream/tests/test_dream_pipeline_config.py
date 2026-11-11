"""Model configuration of the dream lanes: a provider-neutral alias by default, overridable from the environment."""

import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.dirname(os.path.dirname(HERE))


class LaneModelTests(unittest.TestCase):
    def default_model(self, extra):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/tmp"}
        env.update(extra)
        paths = [ENGINE, os.path.join(ENGINE, "recall"), os.path.join(ENGINE, "dream")]
        code = (
            "import sys; sys.path[:0] = %r; import memory_dream_run as m; print(m.DEFAULT_MODEL)"
            % (paths,)
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

    def test_the_default_lane_model_is_the_sonnet_alias(self):
        self.assertEqual(self.default_model({}), "sonnet")

    def test_the_environment_can_pin_a_full_model_id(self):
        self.assertEqual(
            self.default_model(
                {"MEMORY_SHEPHERD_LANE_MODEL": "some-provider.model-id-2"}
            ),
            "some-provider.model-id-2",
        )

    def test_an_empty_override_falls_back_to_the_alias(self):
        self.assertEqual(
            self.default_model({"MEMORY_SHEPHERD_LANE_MODEL": ""}), "sonnet"
        )


if __name__ == "__main__":
    unittest.main()
