"""A card-run leg runs inside the Flight session, so it inherits <env_prefix>_FLIGHT and <env_prefix>_FLIGHT_ONESHOT.
The suite and the land probes must give the same verdict there as anywhere else.

E2E 2026-11-18 (incident befb286b): the land probe replay_state_over_cap ran in a one-shot Flight's leg; its Stop
replay took the one-shot branch and passed the 86-line State, so no hub change could land.
"""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

HUB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HUB / "Drawer"))
import harness_config  # noqa: E402

FLIGHT_VARS = {harness_config.env_name("FLIGHT"): "Flight", harness_config.env_name("FLIGHT_ONESHOT"): "1"}


class FlightEnvIsolationTest(unittest.TestCase):
    def test_the_state_cap_probe_blocks_inside_a_one_shot_flight(self):
        spec = importlib.util.spec_from_file_location("replay_cap", HUB / "Drawer" / "probes" / "replay_state_over_cap.py")
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)
        with mock.patch.dict(os.environ, FLIGHT_VARS):
            self.assertIsNone(probe.replay(HUB, None))

    def test_the_suite_drops_the_flight_session_vars(self):
        # conftest.py pops them, so a test that wants one sets it itself.
        run = subprocess.run([sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                              "Drawer/test_flight_hooks.py"], cwd=HUB, capture_output=True, text=True,
                             env={**os.environ, **FLIGHT_VARS}, timeout=600)
        self.assertEqual(0, run.returncode, run.stdout[-2000:])
        for name in FLIGHT_VARS:
            self.assertNotIn(name, os.environ)

    def test_the_smoke_drops_the_flight_session_vars(self):
        # smoke.py runs each file with unittest, where conftest.py does not load (P4 E2E, incident 091c2450).
        import smoke
        reds = smoke.hub_tests(HUB, files=["Drawer/test_flight_hooks.py"], env={**os.environ, **FLIGHT_VARS})
        self.assertEqual([], reds)


if __name__ == "__main__":
    unittest.main()
