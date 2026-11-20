"""Tests for Drawer/probes/probe.py, the hub probe that git-branch-policy.py land runs at the branch tip.

Run: .venv/bin/python -B -m unittest Drawer/test_probe.py. It runs the full probe with --json in a
subprocess, so each replay sees the real probe sys.argv (a replay that called
smoke.main() with no argv made smoke parse --json and exit 2). No replay uses the product project.
"""
import json
from pathlib import Path
import subprocess
import sys
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


if __name__ == "__main__":
    unittest.main()
