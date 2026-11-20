"""Drawer/session_digest.py: the help switch prints the usage; it never reads a file named -h."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().with_name("session_digest.py")
PY = ".venv/" + ("Scripts/python.exe" if sys.platform == "win32" else "bin/python") + " -B"


class HelpTests(unittest.TestCase):
    def test_help_prints_the_usage_with_the_prepared_interpreter_and_exits_zero(self):
        usage = ("Compact digest of one Claude Code transcript for a reader agent. Usage: "
                 f"{PY} Drawer/session_digest.py <file.jsonl>  (prints <= ~450 lines)\n")
        for switch in ("-h", "--help"):
            with self.subTest(switch):
                result = subprocess.run([sys.executable, "-B", str(SCRIPT), switch], capture_output=True,
                                        text=True, timeout=60, check=False)
                self.assertEqual((0, usage, ""), (result.returncode, result.stdout, result.stderr))


class UsageTests(unittest.TestCase):
    def test_digest_prints_peak_context_and_advisor_lines(self):
        # transcript.turn_context: the peak is the last executor iteration (2220), not the summed
        # top level (3385); the advisor iteration prints apart, once per distinct request.
        turn = {"input_tokens": 35, "cache_creation_input_tokens": 300, "cache_read_input_tokens": 3050,
                "output_tokens": 40, "iterations": [
                    {"type": "message", "input_tokens": 10, "cache_creation_input_tokens": 100,
                     "cache_read_input_tokens": 1000},
                    {"type": "advisor_message", "model": "claude-opus-5-5", "input_tokens": 5,
                     "cache_read_input_tokens": 50, "output_tokens": 7},
                    {"type": "message", "input_tokens": 20, "cache_creation_input_tokens": 200,
                     "cache_read_input_tokens": 2000}]}
        rows = [{"type": "assistant", "requestId": rid, "message": {"id": rid, "usage": turn}}
                for rid in ("r1", "r1", "r2")]
        with tempfile.TemporaryDirectory() as folder:
            session = Path(folder) / "s.jsonl"
            session.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
            result = subprocess.run([sys.executable, "-B", str(SCRIPT), str(session)], capture_output=True,
                                    text=True, timeout=60, check=False)
        self.assertEqual((0, ""), (result.returncode, result.stderr))
        lines = result.stdout.splitlines()
        self.assertEqual(["PEAK_CONTEXT", "2220"], next(l for l in lines if l.startswith("PEAK_CONTEXT")).split()[:2])
        self.assertEqual(["ADVISOR claude-opus-5-5 calls=2 input=110 output=14"],
                         [l for l in lines if l.startswith("ADVISOR")])


if __name__ == "__main__":
    unittest.main()
