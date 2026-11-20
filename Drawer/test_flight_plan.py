"""Tests for Drawer/flight-plan.py, Flight's board over mem.

Run: .venv/bin/python -B -m unittest Drawer/test_flight_plan.py. Hermetic: a scratch mem db and a temp state
root; the CLI case runs under a temp HOME.
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
CLI = HERE / "flight-plan.py"
SPEC = importlib.util.spec_from_file_location("flight_plan", CLI)
plan = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plan)


MEM_DB, MEM_EMBED = plan.harness_config.env_name("MEM_DB"), plan.harness_config.env_name("MEM_EMBED")  # the hub's prefix


def body(lead, files=""):
    return f"{lead}: do the thing. Why: it is due. Done when: a test proves it.\nFiles: {files}"


class FlightPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patch = mock.patch.dict(os.environ, {MEM_DB: str(self.tmp / "hub.db"), MEM_EMBED: "off"})
        patch.start()
        self.addCleanup(patch.stop)
        self.m = plan.mem.Mem(self.tmp / "hub.db")
        self.addCleanup(self.m.db.close)
        self.handoff = self.tmp / "flight-handoff"

    def add(self, lead, files="", **kw):
        return self.m.add("task", body(lead, files), **kw)["id"]

    def board(self):
        return plan.board(self.m, self.handoff)

    def verbs(self, b=None):
        return [(t["verb"], t["task8"]) for t in (b or self.board())["tasks"]]

    def test_chain_first_then_priority(self):
        lone = self.add("Lone p0", priority="p0")
        root = self.add("Root p2", priority="p2")
        mid = self.add("Mid", depends_on=[root])
        self.add("Leaf", depends_on=[mid])
        b = self.board()
        self.assertEqual([("GO", root[:8]), ("GO", lone[:8])], self.verbs(b))
        self.assertEqual([2, 0], [t["chain"] for t in b["tasks"]])
        out = plan.lines(b)
        self.assertTrue(out[1].startswith(f"GO {root[:8]} chain=2 p2 - -"), out)

    def test_milestone_tag(self):
        tagged = self.add("Tagged job", tags=["milestone:legs"])
        plain = self.add("Plain job")
        found = {t["task8"]: t["milestone"] for t in self.board()["tasks"]}
        self.assertEqual({tagged[:8]: "legs", plain[:8]: "-"}, found)

    def test_park_and_hold(self):
        doing = self.add("Doing job", files="Drawer/a.py")
        self.m.set_status(doing, "doing")
        parked = self.add("Parked job", status="parked")
        self.m.set_status(parked, "parked", reason="waits for the build box")
        first = self.add("First", files="Drawer/b.py", priority="p0")
        second = self.add("Second", files="Drawer/b.py, Drawer/c.py", priority="p1")
        blocked = self.add("Blocked", files="Drawer/a.py", priority="p1")
        rows = {t["task8"]: t for t in self.board()["tasks"]}
        self.assertNotIn(doing[:8], rows)
        self.assertEqual("PARK", rows[parked[:8]]["verb"])
        self.assertIn("waits for the build box", rows[parked[:8]]["reason"])
        self.assertEqual("GO", rows[first[:8]]["verb"])
        self.assertEqual(("HOLD", f"shares Drawer/b.py with {first[:8]}"),
                         (rows[second[:8]]["verb"], rows[second[:8]]["reason"]))
        self.assertEqual(f"shares Drawer/a.py with {doing[:8]}", rows[blocked[:8]]["reason"])

    def test_runs(self):
        self.assertEqual("RUNS none", plan.lines(self.board())[0])
        for task, kinds in (("aaaa1111-0000-4000-8000-000000000001", ("start",)),
                            ("bbbb2222-0000-4000-8000-000000000002", ("start", "result")),
                            ("cccc3333-0000-4000-8000-000000000003", ("start",))):
            (self.handoff / task[:8]).mkdir(parents=True)
            (self.handoff / task[:8] / "outbox.log").write_text(
                "".join(json.dumps({"row": k, "task": task, "session": "s"}) + "\n" for k in kinds), encoding="utf-8")
        self.assertEqual("RUNS 2: aaaa1111 cccc3333", plan.lines(self.board())[0])

    def test_cli_json(self):
        go = self.add("Cli job")
        root = plan.harness_config.load(HERE.parent)["project"]["state_root"]
        self.assertTrue(root.startswith("~/"), root)
        state = self.tmp / "home" / root[2:]
        state.mkdir(parents=True)
        env = {**os.environ, "HOME": str(self.tmp / "home")}
        run = subprocess.run([sys.executable, "-B", str(CLI), "--json"], capture_output=True, text=True, env=env)
        self.assertEqual(0, run.returncode, run.stderr)
        b = json.loads(run.stdout)
        self.assertEqual([], b["runs"])
        self.assertEqual([("GO", go[:8])], [(t["verb"], t["task8"]) for t in b["tasks"]])
        self.assertEqual({"runs", "tasks"}, set(b))
        bad = subprocess.run([sys.executable, "-B", str(CLI), "--bogus"], capture_output=True, text=True, env=env)
        self.assertEqual(2, bad.returncode)


if __name__ == "__main__":
    unittest.main()
