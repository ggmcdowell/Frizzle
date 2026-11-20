"""Selftest for Drawer/pad-guard.py.

Run: .venv/bin/python -B -m unittest Drawer/test_pad_guard.py.
Both pads are git-ignored, so no git check sees a cut. Flight's regex on its SHIPPED line cut the State
BACKLOG line for ~12 h; a worker edit can cut another session's notes the same way. The guard compares
each pad after a tool call to the copy it kept, and blocks once on a cut it must name.
"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "pad-guard.py"
WORKER = "d884447a-0000-4000-8000-000000000000"
FLIGHT = "5d45ae24-0000-4000-8000-000000000000"

HUB_PAD = """# Hub scratchpad

## box-productcode-41ee8d37 2026-10-29
- chest cue needs a ring
- camp panel too long

## box-harness-d884447a 2026-11-03
- my note
"""
FLIGHT_PAD = """# Flight scratchpad

## State
RESUMED 2026-11-02 22:48.
SHIPPED Nov 3 build 20261103.92753 (product 9542e3e). PRIOR: build 20261102.205802
BACKLOG: sweep wins 100%; Guard U expires; MANUAL view-combat step order stale
Updated 2026-11-03 by BOX-FLIGHT-5d45ae24.

### Next
1. ship it

## box-productcode-9b530739 2026-11-03: backlog
- fix the plate
"""


def load():
    spec = importlib.util.spec_from_file_location("pad_guard", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PadGuard(unittest.TestCase):
    def setUp(self):
        self.guard = load()
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.hub = root / "hub"
        (self.hub / "Flight").mkdir(parents=True)
        self.store = root / "store"
        self.hub_pad = self.hub / "SCRATCHPAD.md"
        self.flight_pad = self.hub / "Flight" / "SCRATCHPAD.md"
        self.hub_pad.write_text(HUB_PAD)
        self.flight_pad.write_text(FLIGHT_PAD)
        self.assertIsNone(self.check(WORKER))  # the first call keeps the copies

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, session, flight=False, tool="Edit"):
        event = {"session_id": session, "hook_event_name": "PostToolUse", "tool_name": tool}
        return self.guard.check(event, self.hub, self.store, flight)

    def test_a_worker_edit_that_cuts_another_heading_blocks_once_and_names_the_copy(self):
        self.hub_pad.write_text(HUB_PAD.replace("- camp panel too long\n", ""))
        out = self.check(WORKER)
        self.assertEqual("block", out["decision"])
        self.assertIn("box-productcode-41ee8d37", out["reason"])
        copy = Path(out["reason"].split("Restore it from ")[1].split(" ")[0])
        self.assertEqual(HUB_PAD, copy.read_text())
        self.assertIsNone(self.check(WORKER))  # once: the next call compares to the new text

    def test_a_flight_regex_that_cuts_its_state_backlog_line_blocks(self):
        # Flight owns State, but a cut labelled line still blocks.
        cut = FLIGHT_PAD.replace("BACKLOG: sweep wins 100%; Guard U expires; MANUAL view-combat step order stale\n", "")
        self.flight_pad.write_text(cut)
        out = self.check(FLIGHT, flight=True, tool="Bash")
        self.assertEqual("block", out["decision"])
        self.assertIn("BACKLOG:", out["reason"])
        self.assertIsNone(self.check(FLIGHT, flight=True))

    def test_a_labelled_line_cut_to_under_half_blocks(self):
        self.flight_pad.write_text(FLIGHT_PAD.replace("; Guard U expires; MANUAL view-combat step order stale", ""))
        self.assertIn("BACKLOG:", self.check(FLIGHT, flight=True)["reason"])

    def test_a_pad_that_shrank_by_more_than_the_share_blocks_even_in_own_headings(self):
        self.hub_pad.write_text(HUB_PAD + "## box-harness-d884447a 2026-11-03: backlog\n" + "- item\n" * 60)
        self.assertIsNone(self.check(WORKER))
        self.hub_pad.write_text(HUB_PAD)
        self.assertIn("shrank", self.check(WORKER)["reason"])

    def test_a_normal_append_and_own_heading_trims_pass(self):
        self.hub_pad.write_text(HUB_PAD + "- another note of mine\n")
        self.assertIsNone(self.check(WORKER))
        self.hub_pad.write_text(HUB_PAD.replace("- my note\n", "- my note, rewritten\n"))
        self.assertIsNone(self.check(WORKER))
        self.flight_pad.write_text(FLIGHT_PAD + "\n## box-harness-d884447a 2026-11-03: backlog\n- new item\n")
        self.assertIsNone(self.check(WORKER))
        # Flight folds a worker backlog entry into State: Flight curates its own pad.
        folded = self.flight_pad.read_text().replace("## box-productcode-9b530739 2026-11-03: backlog\n- fix the plate\n", "")
        self.flight_pad.write_text(folded.replace("step order stale", "step order stale; fix the plate"))
        self.assertIsNone(self.check(FLIGHT, flight=True))

    def test_a_worker_that_cuts_flight_state_blocks(self):
        self.flight_pad.write_text(FLIGHT_PAD.replace("1. ship it\n", ""))
        self.assertIn("State", self.check(WORKER)["reason"])

    def test_the_copies_stay_few(self):
        for n in range(12):
            self.hub_pad.write_text(HUB_PAD + "- note %d\n" % n)
            self.check(WORKER)
        self.assertLessEqual(len(list(self.store.glob("hub-*.md"))), self.guard.KEEP)

    def test_the_manifest_registers_the_guard_on_edit_write_and_bash(self):
        manifest = json.loads((HERE / "settings-manifest.json").read_text())
        rows = [r for r in manifest["hooks"]["PostToolUse"] if "pad-guard.py" in r["command"]]
        self.assertEqual(1, len(rows))
        for tool in ("Edit", "Write", "MultiEdit", "Bash"):
            self.assertRegex(tool, rows[0]["matcher"])


if __name__ == "__main__":
    unittest.main()
