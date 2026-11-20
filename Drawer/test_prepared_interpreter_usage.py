"""A new seat has no py launcher, so each tool's printed usage names the host's
prepared interpreter. Defect caught: a usage line that still prints `py Drawer/...`, or the
unswapped `<python>` word."""
import re
import subprocess
import sys
import unittest
from pathlib import Path

HUB = Path(__file__).resolve().parent.parent
EXPECTED = ".venv/bin/python -B"
# tool -> the args that print its usage
TOOLS = {
    "Drawer/mem/mem.py": ["--help"],
    "Drawer/transcript.py": ["--help"],
    "Drawer/session_digest.py": [],
    "Drawer/seat-doctor.py": ["--help"],
}


class PrintedUsageNamesPreparedInterpreter(unittest.TestCase):
    def test_each_tool_prints_its_command_under_the_prepared_interpreter(self):
        wrong = []
        for tool, args in TOOLS.items():
            run = subprocess.run([sys.executable, "-B", tool, *args], cwd=HUB, capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", timeout=60)
            printed = " ".join((run.stdout + run.stderr).split())
            # `<python>` is the AGENTS.md word for the source text; a printer swaps in the command
            # The command stands alone: a stale folder in front of .venv (an old server venv) fails.
            named = re.search(r"(?<![\w/.-])" + re.escape(f"{EXPECTED} {tool}"), printed)
            if (not named or "<python>" in printed
                    or f"py {tool}" in printed.replace(f"{EXPECTED} {tool}", "")):
                wrong.append((tool, printed[:300]))
        self.assertEqual([], wrong)


if __name__ == "__main__":
    unittest.main()
