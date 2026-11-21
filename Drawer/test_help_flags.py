"""Tests that each stdin-reading hook script answers --help without reading stdin, and that the status
line exits while Claude Code holds its stdin open.

Run: .venv/bin/python -B -m unittest Drawer/test_help_flags.py. Each child gets a stdin pipe this test never
closes, so a script that reads stdin before its --help check times out here.
"""
import ast
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import oscompat  # noqa: E402
SCRIPTS = ["context-nudge.py", "flight-hooks.py", "project-gate.py", "flight-watchdog.py", "steer_check.py",
           "incident.py", "harness-conform.py"]


CRLF, LF = chr(13) + chr(10), chr(10)


def run_held(argv, data=b"", timeout=5.0, env=None):
    """Run argv with data written to a stdin pipe that stays open; (exit code, stdout, stderr, seconds)."""
    import time
    start = time.monotonic()
    child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             cwd=os.path.dirname(HERE), env=env)
    killer = threading.Timer(timeout, child.kill)  # a hung child is killed, so its pipes reach EOF
    killer.start()
    try:
        if data:
            child.stdin.write(data)
            child.stdin.flush()
        out, err = child.stdout.read(), child.stderr.read()  # EOF only when the child exits
        code = None if not killer.is_alive() else child.wait()
        # Text-mode output on Windows ends each line with CRLF; the docstring it is compared to has LF.
        return (code, out.decode("utf-8", "replace").replace(CRLF, LF),
                err.decode("utf-8", "replace").replace(CRLF, LF), time.monotonic() - start)
    finally:
        killer.cancel()
        if child.poll() is None:
            child.kill()
            child.wait()
        for pipe in (child.stdin, child.stdout, child.stderr):
            pipe.close()


class HelpFlagTests(unittest.TestCase):
    def setUp(self):
        home = tempfile.mkdtemp()
        self.env = dict(os.environ, HOME=home, CLAUDE_CODE_SESSION_ID="00000000-0000-4000-8000-0000000000aa")

    def test_each_script_answers_help_with_stdin_held(self):
        for name in SCRIPTS:
            path = os.path.join(HERE, name)
            with open(path, encoding="utf-8") as f:
                doc = ast.get_docstring(ast.parse(f.read()), clean=False)
            for flag in ("--help", "-h"):
                with self.subTest(script=name, flag=flag):
                    import threading
                    result = {}
                    t = threading.Thread(target=lambda: result.update(r=run_held(
                        [sys.executable, "-B", path, flag], env=self.env)), daemon=True)
                    t.start()
                    t.join(5)
                    self.assertFalse(t.is_alive(), f"{name} {flag} blocked past 5 s")
                    code, out, err, secs = result["r"]
                    self.assertEqual(code, 0, f"{name} {flag}: exit {code}, stderr {err!r}")
                    self.assertLess(secs, 5)
                    # The docstring and nothing else: no record line from incident.py, no deny from project-gate.
                    self.assertEqual(out.strip(), doc.strip())
                    self.assertEqual(err, "")

    def test_statusline_exits_with_stdin_held(self):
        line = json.dumps({"model": {"display_name": "Opus"}, "effort": {"level": "high"},
                           "context_window": {"used_percentage": 41.5},
                           "rate_limits": {"seven_day": {"used_percentage": 12}}}).encode() + b"\n"
        import threading
        result = {}
        t = threading.Thread(target=lambda: result.update(r=run_held(
            [oscompat.git_shell("bash"), os.path.join(HERE, "statusline-command.sh")], line, timeout=3, env=self.env)), daemon=True)
        t.start()
        t.join(3)
        self.assertFalse(t.is_alive(), "status line blocked past 3 s")
        code, out, err, secs = result["r"]
        self.assertEqual(code, 0, err)
        self.assertLess(secs, 3)
        for field in ("Opus", "effort:high", "ctx:41%", "wk:12%"):
            self.assertIn(field, out)


if __name__ == "__main__":
    unittest.main()
