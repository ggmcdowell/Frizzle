"""Tests for Drawer/probes/probe.py, the hub probe that git-branch-policy.py land runs at the branch tip.

Run: .venv/bin/python -B -m unittest Drawer/test_probe.py. It runs the full probe with --json in a
subprocess, so each replay sees the real probe sys.argv (a replay that called
smoke.main() with no argv made smoke parse --json and exit 2). No replay uses the product project.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HUB = Path(__file__).resolve().parents[1]


class ProbeTests(unittest.TestCase):
    def test_probe_json_runs_every_replay_and_exits_0(self):
        run = subprocess.run([sys.executable, "-B", str(HUB / "Drawer" / "probes" / "probe.py"), "--json"],
                             cwd=HUB, capture_output=True, text=True, timeout=240)
        shown = run.stdout + run.stderr
        self.assertEqual(run.returncode, 0, shown)
        last = json.loads(run.stdout.strip().splitlines()[-1])
        self.assertEqual(last["red"], [], shown)
        self.assertEqual(last["pass"], last["total"], shown)
        self.assertGreater(last["total"], 1, shown)

    # Defect (incident 30286e9e): on a seat that sets CLAUDE_CONFIG_DIR the land probe was red, as a replay scratched
    # HOME and the watchdog still read the real Claude folder. Injection: drop the env.pop in that replay.
    def test_a_seat_claude_config_dir_does_not_reach_a_replay(self):
        with tempfile.TemporaryDirectory() as other:
            run = subprocess.run([sys.executable, "-B", str(HUB / "Drawer" / "probes" / "replay_incident_line_live.py")],
                                 cwd=HUB, capture_output=True, text=True, timeout=240,
                                 env={**os.environ, "CLAUDE_CONFIG_DIR": other})
        self.assertEqual((0, "PASS"), (run.returncode, run.stdout.strip()), run.stdout + run.stderr)


if __name__ == "__main__":
    unittest.main()
