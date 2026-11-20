"""Hermetic tests for Drawer/flight-digest.py: one date's text from temp logs, at most 15 lines."""
from datetime import date
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("flight_digest", Path(__file__).with_name("flight-digest.py"))
dig = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dig)

DAY = date(2026, 9, 25)


class DigestTest(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = root
        for name in ("flight", "repo"):
            (root / name).mkdir(parents=True)
        self.enterContext(mock.patch.object(dig, "HUB", root / "repo"))
        self.enterContext(mock.patch.object(dig, "flight_dir", lambda: root / "flight"))
        self.enterContext(mock.patch.object(dig, "product_repo", lambda: None))
        repo = root / "repo"
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@t", "HOME": str(root), "PATH": "/usr/bin:/bin"}
        subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True, env=env)
        for when, subject in (("2026-09-24T10:00:00", "Day before"), ("2026-09-25T10:00:00", "Event wake: the watch"),
                              ("2026-09-26T10:00:00", "Day after")):
            subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", subject], check=True,
                           env=dict(env, GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when))
        log = [{"t": "2026-09-25T12:00:00", "event": "report", "text": "build report 1"},
               {"t": "2026-09-25T12:01:00", "event": "decision", "text": "ordered x"},
               {"t": "2026-09-24T12:01:00", "event": "decision", "text": "yesterday"}]
        (root / "flight" / "log.jsonl").write_text("".join(json.dumps(r) + "\n" for r in log), encoding="utf-8")
        (root / "flight" / "SCRATCHPAD.md").write_text(
            "# pad\n## State\n### Goal\ng\n### Next\n1. First item\n   goes on.\n2. Second\n3. Third\n4. Fourth\n"
            "### Open\nx\n", encoding="utf-8")

    def test_digest_of_one_day(self):
        text = dig.digest(DAY)
        lines = text.splitlines()
        self.assertLessEqual(len(lines), dig.MAX_LINES)
        self.assertEqual("**Digest**: daily digest 2026-09-25", lines[0])
        self.assertRegex(lines[1], r"^Hub main commits: 1 \([0-9a-f]+ Event wake\)$")
        self.assertEqual("Product main commits: not read", lines[2])
        self.assertEqual("Reports: 1 (build report 1)", lines[3])
        self.assertEqual("Decisions: 1 (ordered x)", lines[4])
        self.assertEqual(["Next 1: First item goes on.", "Next 2: Second", "Next 3: Third"], lines[5:])
        self.assertEqual(8, len(lines))
        self.assertFalse([line for line in lines if line.startswith(("Cycles:", "Trials:", "Sessions:", "Routes:"))])

    def test_an_empty_day_still_prints(self):
        lines = dig.digest(date(2026, 1, 1)).splitlines()
        self.assertFalse([line for line in lines if line.startswith(("Cycles:", "Trials:"))])
        self.assertIn("Hub main commits: 0", lines)
        self.assertIn("Reports: 0", lines)


if __name__ == "__main__":
    unittest.main()
