"""Tests for Drawer/steer_check.py, the steer check before an irreversible step.

Run: .venv/bin/python -B -m unittest Drawer/test_steer_check.py. Hermetic: the state root is a temp folder.
"""
import io
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import steer_check  # noqa: E402

SESSION = "4d7fc871-0000-4000-8000-000000000001"
T = 1761774180.0


class SteerCheckTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.cfg = {"project": {"state_root": str(self.root / "state")}}

    def check(self, step="upload commit", session=SESSION):
        return steer_check.check(self.cfg, session, step, now=T + 120)

    def test_no_stop_is_clear(self):
        self.assertIsNone(self.check())
        self.assertEqual([], steer_check.pauses(self.cfg))

    def test_a_flight_stop_pauses_and_go_lifts_it(self):
        steer_check.stop(self.cfg, SESSION[:8], "Flight holds the build")
        stopped = self.check()
        steer_check.go(self.cfg, SESSION)
        self.assertEqual((True, None), ("Flight holds the build" in (stopped or ""), self.check()))

    def test_each_pause_writes_one_row_for_the_watchdog(self):
        self.check()
        steer_check.stop(self.cfg, SESSION, "hold")
        self.check()
        rows = steer_check.pauses(self.cfg)
        self.assertEqual([(SESSION[:8], "upload commit")], [(r["session"], r["step"]) for r in rows])

    def test_the_cli_exits_75_on_a_pause_and_0_when_clear(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            clear = steer_check.main(["check", SESSION, "--step", "push"], self.cfg)
            steer_check.stop(self.cfg, SESSION, "hold")
            paused = steer_check.main(["check", SESSION, "--step", "push"], self.cfg)
            bad = steer_check.main(["check", SESSION, "--bogus"], self.cfg)
        self.assertEqual((75, 0, 1), (paused, clear, bad))
        self.assertTrue(out.getvalue().startswith(f"CLEAR {SESSION[:8]} push"))
        self.assertIn(f"PAUSE {SESSION[:8]} push", out.getvalue())

    def test_a_stop_with_a_step_pauses_only_that_step(self):
        steer_check.stop(self.cfg, SESSION, "hold the upload", step="upload commit")
        other = self.check("hub publish")
        same = self.check()
        self.assertIsNone(other)
        self.assertIn("STOP for 4d7fc871 at upload commit: hold the upload", same or "")

    def test_a_stop_with_no_step_pauses_every_step(self):
        steer_check.stop(self.cfg, SESSION, "hold all")
        self.assertIsNotNone(self.check("hub publish"))
        self.assertIsNotNone(self.check())

    def test_the_cli_stop_takes_a_step(self):
        with mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(0, steer_check.main(["stop", SESSION, "hold", "the", "upload", "--step", "upload commit"],
                                                 self.cfg))
            self.assertEqual(1, steer_check.main(["stop", SESSION, "--step", "upload commit"], self.cfg))
        self.assertEqual({"why": "hold the upload", "step": "upload commit"},
                         {k: v for k, v in steer_check.read_state(self.cfg)["stops"][SESSION[:8]].items() if k != "t"})

    def test_a_session_name_matches_a_stop_set_by_uuid8(self):
        # Check BOX-PRODUCTCODE-<uuid8> keyed on "BOX-PRODUCT" and missed the session.
        name = f"BOX-PRODUCTCODE-{SESSION[:8]}"
        steer_check.stop(self.cfg, SESSION[:8], "hold")
        self.assertIsNotNone(self.check("push", session=name))
        with mock.patch("sys.stdout", io.StringIO()):
            steer_check.main(["go", name], self.cfg)
        self.assertIsNone(self.check("push", session=SESSION))
        self.assertEqual(SESSION[:8], steer_check.uuid8(SESSION))
        self.assertEqual(SESSION[:8], steer_check.uuid8(SESSION[:8]))
        self.assertEqual(SESSION[:8], steer_check.uuid8(name))


    def test_a_stopped_task8_exits_75_and_go_lifts_it(self):
        # Legs share one session id, so Flight holds one task's push by its task8.
        task = "09f59dc3-5dcf-4a02-89de-488151981b27"
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            stopped = steer_check.main(["stop", task[:8], "hold", "this", "task"], self.cfg)
            paused = steer_check.main(["check", task[:8], "--step", "push"], self.cfg)
            by_uuid = steer_check.main(["check", task, "--step", "push"], self.cfg)
            gone = steer_check.main(["go", task[:8]], self.cfg)
            clear = steer_check.main(["check", task[:8], "--step", "push"], self.cfg)
        self.assertEqual((0, 75, 75, 0, 0), (stopped, paused, by_uuid, gone, clear))
        self.assertIn(f"PAUSE {task[:8]} push", out.getvalue())

    def test_check_all_pauses_on_any_subject(self):
        steer_check.stop(self.cfg, "09f59dc3", "hold the task")
        self.assertIn("hold the task", steer_check.check_all(self.cfg, [SESSION, "09f59dc3"], "push") or "")
        self.assertIsNone(steer_check.check_all(self.cfg, [SESSION, "", "deadbeef"], "push"))


if __name__ == "__main__":
    unittest.main()
