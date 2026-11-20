"""test_long_job -- Drawer/long-job.py: a detached named job outlives its caller, a second start of one
name does not rerun it, wait returns by 270 s, and a dead runner reads lost."""
import contextlib
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "long-job.py"


def load_module():
    spec = importlib.util.spec_from_file_location("long_job", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class LongJobTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.env = dict(os.environ, HOME=str(self.home))
        root = load_module().harness_config.load(HERE.parent)["project"]["state_root"]
        self.jobs = self.home / os.path.relpath(os.path.expanduser(root), os.path.expanduser("~")) / "jobs"
        self.pids = []

    def tearDown(self):
        for pid in self.pids:
            if not isinstance(pid, int):
                continue
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        self.tmp.cleanup()

    def cli(self, *args, timeout=60):
        return subprocess.run([sys.executable, "-B", str(SCRIPT), *args], cwd=HERE.parent, env=self.env,
                              capture_output=True, text=True, timeout=timeout)

    def status(self, name):
        out = self.cli("status", "--name", name)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def start_running(self, name, *argv):
        """Start a job and return its running status line: under load start can return while it is starting."""
        out = self.cli("start", "--name", name, "--", *argv)
        self.assertEqual(out.returncode, 0, out.stderr)
        line = self.until(name, "running")
        self.pids.append(line["runner_pid"])
        return line

    def until(self, name, state, limit=20):
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            line = self.status(name)
            if line["state"] == state:
                if state == "running":  # running always names the runner
                    self.assertIsInstance(line["runner_pid"], int, line)
                return line
            time.sleep(0.2)
        self.fail(f"{name} never reached {state}: {line}")

    def test_job_outlives_its_caller(self):
        mark = self.home / "mark"
        job = f"import time, pathlib; time.sleep(3); pathlib.Path({str(mark)!r}).write_text('ok')"
        # The caller (a worker stand-in) starts the job, then lives on in its own process group; kill the
        # whole group as a dying worker's tree would be killed.
        start = [sys.executable, "-B", str(SCRIPT), "start", "--name", "outlive", "--", sys.executable, "-c", job]
        caller = subprocess.Popen(["sh", "-c", '"$@"; exec sleep 60', "caller", *start], cwd=HERE.parent,
                                  env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  start_new_session=True)
        self.pids.append(caller.pid)
        line = self.until("outlive", "running")
        self.pids.append(line["runner_pid"])
        os.killpg(caller.pid, signal.SIGKILL)
        caller.wait(timeout=10)
        done = self.until("outlive", "done", limit=30)
        self.assertEqual(done["exit_code"], 0)
        self.assertEqual(mark.read_text(), "ok")
        self.assertTrue((self.jobs / "outlive.json").is_file())
        self.assertTrue((self.jobs / "outlive.log").is_file())

    def test_second_start_does_not_rerun(self):
        count = self.home / "count"
        job = f"import pathlib; p = pathlib.Path({str(count)!r}); p.write_text(p.read_text() + 'x' if p.exists() else 'x')"
        first = self.cli("start", "--name", "once", "--", sys.executable, "-c", job)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.until("once", "done")
        second = self.cli("start", "--name", "once", "--", sys.executable, "-c", job)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout)["state"], "done")
        self.assertIn("nothing started", second.stderr)
        time.sleep(1)
        self.assertEqual(count.read_text(), "x")

    def test_second_start_reports_running_job(self):
        self.start_running("busy", sys.executable, "-c", "import time; time.sleep(30)")
        second = self.cli("start", "--name", "busy", "--", "true")
        self.assertEqual(json.loads(second.stdout)["state"], "running")
        self.assertIn("nothing started", second.stderr)

    def test_wait_returns_by_270_seconds(self):
        mod = load_module()
        self.assertEqual(mod.WAIT_S, 270)
        self.start_running("slow", sys.executable, "-c", "import time; time.sleep(60)")
        began = time.monotonic()
        out = self.cli("wait", "--name", "slow", "--timeout", "1")
        self.assertLess(time.monotonic() - began, 10)
        self.assertEqual(out.returncode, mod.EXIT_TIMEOUT)
        self.assertEqual(json.loads(out.stdout)["state"], "running")
        # A longer --timeout is capped at WAIT_S: shrink the cap in-process and ask for far more.
        mod.STATE = self.jobs
        mod.WAIT_S = 1
        began = time.monotonic()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mod.wait("slow", 9999), mod.EXIT_TIMEOUT)
        self.assertLess(time.monotonic() - began, 10)

    def test_wait_returns_job_exit_code(self):
        self.cli("start", "--name", "seven", "--", sys.executable, "-c", "raise SystemExit(7)")
        out = self.cli("wait", "--name", "seven", "--timeout", "30")
        self.assertEqual(out.returncode, 7)
        self.assertEqual(json.loads(out.stdout)["state"], "done")

    def test_dead_runner_reads_lost(self):
        line = self.start_running("doomed", sys.executable, "-c", "import time; time.sleep(60)")
        os.kill(line["runner_pid"], signal.SIGKILL)
        self.until("doomed", "lost")
        out = self.cli("wait", "--name", "doomed")
        self.assertEqual(out.returncode, load_module().EXIT_LOST)
        again = self.cli("start", "--name", "doomed", "--", "true")
        self.assertEqual(json.loads(again.stdout)["state"], "lost")

    def test_absent_and_default_cwd_on_main_clone(self):
        out = self.cli("wait", "--name", "nobody", "--timeout", "0")
        self.assertEqual(out.returncode, 2)
        self.cli("start", "--name", "where", "--", "pwd")
        line = self.until("where", "done")
        mod = load_module()
        self.assertEqual(Path(line["cwd"]), mod.harness_config.main_checkout(HERE.parent))

    def test_no_runner_pid_reads_starting_not_running(self):
        mod = load_module()
        mod.STATE = self.jobs
        self.jobs.mkdir(parents=True)
        record, _ = mod.paths("early")
        mod.write(record, {"name": "early", "started": mod.now(), "argv": ["true"], "cwd": "/"})
        self.assertEqual(mod.status("early")["state"], "starting")
        old = mod.datetime.datetime.now(mod.datetime.timezone.utc) - mod.datetime.timedelta(seconds=mod.RUNNER_GRACE_S + 5)
        mod.write(record, {"name": "early", "started": old.isoformat(timespec="seconds"), "argv": ["true"], "cwd": "/"})
        self.assertEqual(mod.status("early")["state"], "lost")

    def test_runner_that_finishes_before_start_sees_its_pid_keeps_its_result(self):
        # Under load start can read the record before the runner's first write, and then find the runner
        # exited because it already ran the job and wrote its finish. start must not write over that finish.
        mod = load_module()
        mod.STATE = self.jobs
        record, _ = mod.paths("quick")

        class Runner:
            pid = 999999

            def poll(self):
                rec = mod.read(record)
                rec.update(runner_pid=self.pid, finished=mod.now(), exit_code=0)
                mod.write(record, rec)
                return 0

        real = mod.subprocess.Popen
        mod.subprocess.Popen = lambda *a, **k: Runner()
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(mod.start("quick", "/", ["true"]), 0)
        finally:
            mod.subprocess.Popen = real
        line = mod.status("quick")
        self.assertEqual(line["state"], "done", line)
        self.assertEqual(line["exit_code"], 0)

    def test_pid_alive(self):
        sys.path.insert(0, str(HERE))
        import gatelib
        self.assertTrue(gatelib.pid_alive(os.getpid()))
        proc = subprocess.Popen(["true"])
        proc.wait()
        self.assertFalse(gatelib.pid_alive(proc.pid))
        self.assertTrue(gatelib.pid_alive(None))

    def test_pid_alive_windows_never_kills(self):
        """Windows branch: OpenProcess and GetExitCodeProcess, never os.kill."""
        from unittest import mock
        sys.path.insert(0, str(HERE))
        import gatelib

        class Code:
            def __init__(self, value):
                self.value = value

        class Kernel:
            def __init__(self, handle, code=259, read_ok=True):
                self.handle, self.code, self.read_ok, self.closed, self.opened = handle, code, read_ok, [], []

            def OpenProcess(self, access, inherit, pid):
                self.opened.append((access, pid))
                return self.handle

            def GetExitCodeProcess(self, handle, ref):
                ref.value = self.code
                return self.read_ok

            def CloseHandle(self, handle):
                self.closed.append(handle)

        def probe(kernel, err=0):
            with mock.patch.object(gatelib.os, "name", "nt"), \
                    mock.patch.object(gatelib.os, "kill", side_effect=AssertionError("os.kill on Windows")), \
                    mock.patch.object(gatelib, "_kernel32",
                                      return_value=(kernel, lambda: err, Code, lambda c: c)):
                return gatelib.pid_alive(4242)

        running = Kernel(7)
        self.assertTrue(probe(running))
        self.assertEqual(running.opened, [(0x1000, 4242)])
        self.assertEqual(running.closed, [7])
        exited = Kernel(8, code=0)
        self.assertFalse(probe(exited))
        self.assertEqual(exited.closed, [8])
        self.assertFalse(probe(Kernel(None), err=87))   # ERROR_INVALID_PARAMETER: no such pid
        self.assertTrue(probe(Kernel(None), err=5))     # ERROR_ACCESS_DENIED: it runs
        self.assertTrue(probe(Kernel(9, read_ok=False)))  # unsure read fails closed
        with mock.patch.object(gatelib.os, "name", "nt"), \
                mock.patch.object(gatelib.os, "kill", side_effect=AssertionError("os.kill on Windows")), \
                mock.patch.object(gatelib, "_kernel32", side_effect=OSError("no kernel32")):
            self.assertTrue(gatelib.pid_alive(4242))


if __name__ == "__main__":
    unittest.main()
