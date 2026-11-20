"""Hub tests never read the checkout's harness.local.toml.

A local toml can move [project] state_root and the [mem] paths to absolute folders (a second hub on one box).
The hub tests isolate by a temp HOME, so such a root would send their writes into live state. Both test runners
set HARNESS_NO_LOCAL_TOML=1: Drawer/smoke.py for each child test process, the hub-root conftest.py for pytest.
A test that reads a local toml on purpose drops the variable for its own child processes.
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_config  # noqa: E402


class LocalTomlIsolationTest(unittest.TestCase):
    def test_the_runner_sets_no_local_toml(self):
        self.assertEqual("1", os.environ.get(harness_config.NO_LOCAL_VAR),
                         "run the hub tests through Drawer/smoke.py or pytest from the hub root")

    def test_the_hub_config_skips_the_local_toml(self):
        self.assertEqual("", harness_config.load(Path(__file__).resolve().parents[1])["derived"]["local_override"])


if __name__ == "__main__":
    unittest.main()
