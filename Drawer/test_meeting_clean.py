"""test_meeting_clean -- Drawer/meeting_clean.py: a labeled timed transcript, an unlabeled audio memo, a
no-match --replace refused, a planted name left for the name check, dry-run rows that pass the mem
feedback: guard, and a dst inside the hub refused."""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import harness_config  # noqa: E402

SCRIPT = HERE / "meeting_clean.py"
MEM = HERE / "mem" / "mem.py"

TIMED = """[0:05] Alex Smith: the first boss should feel heavy
[0:09] Alex Smith: every hit shakes the camera
[0:20] Tester: um yeah
[0:31] Tester: okay
[1:12] Tester: what if the rain level slows the user
[01:40] Alex Smith: rain slows the boss too
0:02:15 Tester: Marguerite wrote the old score
"""

# One speaker, no stamps, about 310 words: the word-pace estimate opens a second heading. The lone
# "Note:" line must stay Alex's words, not open a speaker named Note.
MEMO = "\n".join([
    "I want the first boss to feel heavy and slow and every hit should shake the camera a little. " * 9,
    "Note: the rain level could slow the user down as well as the boss.",
    "uh yeah",
    "Then the second area opens with a long walk and no enemies for a while so the user can breathe. " * 7,
]) + "\n"


class MeetingCleanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="meeting-clean-")
        self.dir = Path(self.tmp.name)
        (self.dir / "transcript-2026-11-01.txt").write_text(TIMED, encoding="utf-8")
        (self.dir / "memo.txt").write_text(MEMO, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, src, dst, *args):
        out = subprocess.run([sys.executable, "-B", str(SCRIPT), str(self.dir / src), str(dst), *args],
                             capture_output=True, text=True, timeout=60)
        lines = [json.loads(ln) for ln in out.stdout.splitlines()]
        return out.returncode, lines

    def test_a_labeled_timed_transcript_drops_filler_joins_lines_and_heads_each_minute(self):
        dst = self.dir / "out" / "2026-11-01-boss.md"
        code, lines = self.cli("transcript-2026-11-01.txt", dst)
        self.assertEqual(code, 0, lines)
        text = dst.read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"^## (.*)$", text, re.M), ["00:00:05", "00:01:12", "00:02:15"])
        self.assertIn("**Alex:** the first boss should feel heavy every hit shakes the camera\n", text)
        self.assertNotIn("um yeah", text)
        self.assertNotIn("okay", text)
        self.assertIn("# Design meeting: Alex and Tester, 2026-11-01", text)
        self.assertNotIn("Discord", text)
        self.assertEqual(lines[-1]["blocks"], 3)
        self.assertFalse(lines[-1]["estimated"])

    def test_a_transcript_saved_with_a_bom_keeps_its_first_label(self):
        (self.dir / "bom.txt").write_bytes(b"\xef\xbb\xbf" + TIMED.encode("utf-8"))
        dst = self.dir / "out" / "bom.md"
        code, lines = self.cli("bom.txt", dst)
        self.assertEqual(code, 0, lines)
        self.assertEqual(re.findall(r"^## (.*)$", dst.read_text(encoding="utf-8"), re.M),
                         ["00:00:05", "00:01:12", "00:02:15"])

    def test_an_unlabeled_memo_takes_the_speaker_and_estimated_stamps(self):
        dst = self.dir / "memo.md"
        code, lines = self.cli("memo.txt", dst, "--speaker", "Alex")
        self.assertEqual(code, 0, lines)
        text = dst.read_text(encoding="utf-8")
        self.assertEqual(set(re.findall(r"^\*\*(.+?):\*\*", text, re.M)), {"Alex"})
        self.assertIn("Note: the rain level", text)
        self.assertNotIn("uh yeah", text)
        self.assertGreaterEqual(len(re.findall(r"^## ", text, re.M)), 2)
        self.assertIn("estimated at 150 words a minute", text)
        self.assertTrue(lines[-1]["estimated"])

    def test_an_unlabeled_memo_with_no_speaker_is_refused(self):
        dst = self.dir / "memo.md"
        code, lines = self.cli("memo.txt", dst)
        self.assertEqual(code, 1)
        self.assertIn("--speaker", lines[-1]["error"])
        self.assertFalse(dst.exists())

    def test_a_replace_that_matches_nothing_writes_no_file(self):
        dst = self.dir / "out.md"
        code, lines = self.cli("transcript-2026-11-01.txt", dst, "--replace", "Tester=>[a tester]",
                               "--replace", "Zebedee=>[a producer]")
        self.assertEqual(code, 1)
        self.assertIn("Zebedee", lines[-1]["error"])
        self.assertFalse(dst.exists())
        code, lines = self.cli("transcript-2026-11-01.txt", dst, "--replace", "Tester=>[a tester]")
        self.assertEqual((code, lines[-1]["replaced"]), (0, {"Tester": 3}), lines)

    def test_a_planted_name_is_left_for_the_name_check(self):
        code, lines = self.cli("transcript-2026-11-01.txt", self.dir / "out.md")
        self.assertEqual(code, 0, lines)
        self.assertIn("Marguerite(1)", lines[-1]["check_names"])
        self.assertFalse(any(n.startswith(("Alex", "Tester")) for n in lines[-1]["check_names"]))

    def test_dry_run_rows_carry_a_source_the_mem_feedback_guard_takes(self):
        dst = self.dir / "2026-11-01-memo.md"
        code, lines = self.cli("memo.txt", dst, "--speaker", "Alex", "--dry-run")
        self.assertEqual(code, 0, lines)
        self.assertFalse(dst.exists())
        rows, summary = lines[:-1], lines[-1]
        self.assertEqual((summary["result"], summary["dry_run"], len(rows)), ("ok", True, summary["blocks"]))
        env = dict(os.environ, **{harness_config.env_name("MEM_DB"): str(self.dir / "mem.db"),
                                  harness_config.env_name("MEM_EMBED"): "off"})
        bodies = ["Exploration (Alex): the first boss leans on weight and camera shake; open: how strong a shake.",
                  "Exploration (Alex): a quiet walk opens the second area; open: how long before the gate fight."]
        for row, body in zip(rows, bodies):
            self.assertRegex(row["source"], r"^feedback:meeting 2026-11-01-memo\.md#\d\d:\d\d:\d\d$")
            self.assertEqual((row["kind"], row["speakers"]), ("design", ["Alex"]))
            add = subprocess.run([sys.executable, "-B", str(MEM), "add", "--kind", "design", "--tag", "exploration",
                                  "--topic", "meeting", "--source", row["source"], "--body", body, "--new", "--json"],
                                 capture_output=True, text=True, env=env, timeout=120)
            self.assertEqual(add.returncode, 0, add.stdout + add.stderr)
            self.assertNotIn("error", json.loads(add.stdout))
        bad = subprocess.run([sys.executable, "-B", str(MEM), "add", "--kind", "design", "--source",
                              "meeting 2026-11-01-memo.md#00:00:00", "--body",
                              "Exploration (Alex): a third body the guard must refuse; open: none.", "--new", "--json"],
                             capture_output=True, text=True, env=env, timeout=120)
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("feedback:", json.loads(bad.stdout)["error"])

    def test_a_dst_inside_the_hub_is_refused(self):
        dst = HERE / "meeting-clean-test-out.md"
        code, lines = self.cli("transcript-2026-11-01.txt", dst)
        self.assertEqual(code, 1)
        self.assertIn("outside git", lines[-1]["error"])
        self.assertFalse(dst.exists())


if __name__ == "__main__":
    unittest.main()
