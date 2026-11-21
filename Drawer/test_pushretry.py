"""test_pushretry -- Drawer/pushretry.py retries a push only on a remote-side fault:
a refusal returns at once, a stubbed 5xx push lands on try 2, and mem nightly pushes through it."""
import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pushretry  # noqa: E402

# A stub git: each call counts itself; the first FAILS calls print STDERR and exit 1, the rest exit 0.
STUB = """#!/bin/sh
n=$(cat "$STUB_DIR/count" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "$STUB_DIR/count"
if [ $n -le "$STUB_FAILS" ]; then printf '%s\\n' "$STUB_STDERR" >&2; exit 1; fi
echo pushed
"""
# Windows runs no shell script and CreateProcess finds only git.exe by a bare name: git.cmd runs the same stub in Python.
STUB_PY = """import os, sys
d = os.environ["STUB_DIR"]
try:
    n = int(open(os.path.join(d, "count")).read())
except OSError:
    n = 0
n += 1
open(os.path.join(d, "count"), "w").write(str(n))
if n <= int(os.environ["STUB_FAILS"]):
    sys.stderr.write(os.environ["STUB_STDERR"] + "\\n")
    sys.exit(1)
print("pushed")
"""
STUB_CMD = '@"%STUB_PY%" "%STUB_DIR%\\git_stub.py" %*\n'
FIVE_XX = "error: RPC failed; HTTP 502 curl 22 The requested URL returned error: 502"
REFUSED = " ! [rejected]        main -> main (non-fast-forward)\nerror: failed to push some refs"


class StubGit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        if os.name == "nt":
            (self.dir / "git_stub.py").write_text(STUB_PY)
            (self.dir / "git.cmd").write_text(STUB_CMD)
        else:
            (self.dir / "git").write_text(STUB)
            (self.dir / "git").chmod(0o755)
        self.sleeps = []

    def tearDown(self):
        self.tmp.cleanup()

    def push(self, fails, stderr):
        env = {"PATH": f"{self.dir}{os.pathsep}{os.environ['PATH']}", "STUB_DIR": str(self.dir),
               "STUB_FAILS": str(fails), "STUB_STDERR": stderr, "STUB_PY": sys.executable}
        out, err = io.StringIO(), io.StringIO()
        real = subprocess.run

        def run(argv, **kw):  # CreateProcess does not resolve a bare "git" to git.cmd: name the stub
            if os.name == "nt" and argv[0] == "git":
                argv = [str(self.dir / "git.cmd"), *argv[1:]]
            return real(argv, **kw)

        with mock.patch.dict(os.environ, env), mock.patch.object(subprocess, "run", run), \
                redirect_stdout(out), redirect_stderr(err):
            code = pushretry.main([str(self.dir), "origin", "HEAD:main"], sleep=self.sleeps.append)
        return code, int((self.dir / "count").read_text()), err.getvalue()

    def test_refusal_returns_at_once(self):
        code, calls, err = self.push(9, REFUSED)
        self.assertEqual((1, 1, []), (code, calls, self.sleeps))
        self.assertIn("non-fast-forward", err)

    def test_stubbed_5xx_lands_on_try_2(self):
        code, calls, _ = self.push(1, FIVE_XX)
        self.assertEqual((0, 2, [5]), (code, calls, self.sleeps))

    def test_lasting_5xx_gives_up_after_four_tries(self):
        code, calls, err = self.push(9, FIVE_XX)
        self.assertEqual((1, 4, [5, 15, 45]), (code, calls, self.sleeps))
        self.assertIn("returned error: 502", err)

    def test_hook_refusal_quoting_a_5xx_is_not_retried(self):
        code, calls, _ = self.push(9, "remote: hook says: returned error: 500\n ! [remote rejected] main (pre-receive hook declined)")
        self.assertEqual((1, 1), (code, calls))


class FaultMatchTest(unittest.TestCase):
    def test_remote_side_faults_match(self):
        for line in ("error: remote unpack failed: unable to create temporary object directory",
                     "error: RPC failed; curl 56 LibreSSL SSL_read: Connection reset by peer",
                     "fatal: the remote end hung up unexpectedly",
                     "fatal: unable to access 'https://x/': Connection timed out after 300000 milliseconds",
                     " ! [remote rejected] main -> main (missing necessary objects)"):
            self.assertTrue(pushretry.remote_fault(line), line)

    def test_refusals_do_not_match(self):
        for line in (REFUSED, "error: src refspec nope does not match any",
                     " ! [remote rejected] main -> main (pre-receive hook declined)",
                     "fatal: Authentication failed for 'https://x/'"):
            self.assertFalse(pushretry.remote_fault(line), line)


class MemNightlyPushTest(unittest.TestCase):
    def test_publish_export_retries_a_5xx_push(self):
        sys.path.insert(0, str(HERE / "mem"))
        import mem
        calls = []
        real = subprocess.run

        def fake(argv, **kw):
            if "push" in argv:
                calls.append(argv)
                if len(calls) == 1:
                    return subprocess.CompletedProcess(argv, 1, "", FIVE_XX)
            return real(argv, **kw)

        with tempfile.TemporaryDirectory() as tmp:
            folder, bare = Path(tmp) / "export", Path(tmp) / "backup.git"
            real(["git", "init", "-q", "--bare", str(bare)], check=True)
            folder.mkdir()
            (folder / "cards.jsonl").write_text("{}\n")
            with mock.patch.object(mem.subprocess, "run", fake), mock.patch.object(mem.pushretry, "BACKOFF_S", (0, 0, 0)):
                state = mem.publish_export(folder, str(bare))
            self.assertRegex(state, r"^[0-9a-f]{12} pushed$")
            self.assertEqual(2, len(calls))
            log = real(["git", "-C", str(bare), "log", "--oneline", "main"], capture_output=True, text=True)
            self.assertEqual(1, len(log.stdout.splitlines()))


if __name__ == "__main__":
    unittest.main()
