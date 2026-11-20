"""Guard: gatelib.pid_state is the one pid probe. An os.kill signal-0 probe
elsewhere in Drawer/ ends the process it checks on Windows, so this test fails on one."""
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gatelib  # noqa: E402

PROBE = re.compile(r"os\.kill\(" + r"[^,()]+(?:\([^()]*\))?[^,()]*,\s*(?:0|signal\.SIG_DFL)\s*\)")


class PidProbeGuard(unittest.TestCase):
    def test_no_signal_zero_probe_outside_gatelib(self):
        hits = []
        for path in sorted(HERE.rglob("*.py")):
            if path.name in ("gatelib.py", "test_pid_probe.py") or "__pycache__" in path.parts:
                continue
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if PROBE.search(line):
                    hits.append("%s:%d: %s" % (path.relative_to(HERE), n, line.strip()))
        self.assertEqual(hits, [], "route each through gatelib.pid_state")

    def test_pattern_catches_probe_forms(self):
        for line in ("os.kill(pid, 0)", "os.kill(int(data['pid']), 0)", "os.kill( p ,0 )"):
            self.assertTrue(PROBE.search(line), line)
        self.assertFalse(PROBE.search("os.kill(pid, signal.SIGTERM)"))

    def test_pid_state_posix(self):
        self.assertEqual(gatelib.pid_state(None), "unsure")
        self.assertEqual(gatelib.pid_state(True), "unsure")
        cases = ((None, "live"), (ProcessLookupError(), "gone"), (PermissionError(), "live"),
                 (OSError(), "unsure"), (OverflowError(), "unsure"))
        for effect, want in cases:
            with mock.patch.object(gatelib.os, "name", "posix"), \
                    mock.patch.object(gatelib.os, "kill", side_effect=effect):
                self.assertEqual(gatelib.pid_state(4242), want, effect)


if __name__ == "__main__":
    unittest.main()
