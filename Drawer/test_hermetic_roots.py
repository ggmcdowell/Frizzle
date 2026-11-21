"""No hub test reaches the real state or config roots.

The test runners (the hub-root conftest.py for pytest, Drawer/smoke.py for each child test process) give the run a
temp HOME, so the ~-rooted [project] state_root, config_root and [mem] db resolve under it, never under the real
user home. This file fails when any of those paths, in this process or a child, resolves under the real roots.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "mem"))
import harness_config  # noqa: E402
import mem  # noqa: E402

HUB = HERE.parent
# The name the git hooks look the interpreter up by on PATH: python3 on POSIX; the Windows venv has only python.exe,
# which the hooks take after python3.
PYNAME = "python" if os.name == "nt" else "python3"
CHILD = ("import sys; sys.path[:0] = sys.argv[1:3]; import harness_config, mem; "
         "c = harness_config.load(sys.argv[3]); "
         "print(harness_config.path(c['project']['state_root'])); print(harness_config.path(c['project']['config_root'])); "
         "print(mem.live_db())")


class HermeticRootsTest(unittest.TestCase):
    def setUp(self):
        self.real = [Path(p).resolve() for p in harness_config.real_roots(harness_config.load(HUB))]

    def assertNotReal(self, path):
        path = Path(path).resolve()
        for root in self.real:
            self.assertFalse(path == root or root in path.parents, f"{path} is under the real root {root}")

    def test_the_real_roots_are_the_real_home_ones(self):
        home = harness_config.real_home().resolve()
        self.assertTrue(self.real and all(home in root.parents for root in self.real), self.real)

    def test_home_is_not_the_real_home(self):
        self.assertNotEqual(harness_config.real_home().resolve(), Path.home().resolve())

    def test_no_root_of_this_process_is_real(self):
        cfg = harness_config.load(HUB)
        for key in ("state_root", "config_root"):
            self.assertNotReal(harness_config.path(cfg["project"][key]))
        self.assertNotReal(harness_config.path(cfg["mem"]["db"]))
        self.assertNotReal(mem.live_db())
        self.assertNotReal(mem.state_root())

    def test_no_root_of_a_child_process_is_real(self):
        out = subprocess.run([sys.executable, "-B", "-c", CHILD, str(HERE), str(HERE / "mem"), str(HUB)],
                             capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual(3, len(out), out)
        for line in out:
            self.assertNotReal(line)

    def test_a_test_run_finds_its_own_interpreter_as_python3(self):
        # A fixture repo has no .venv, so its git hooks take python3 from PATH. On a Mac whose PATH python3 is the
        # stock 3.9 (a uv-managed .venv only) they answered "no working Python" and four test files went red
        # (incident e14ea0c2). The test runners put the interpreter that runs the tests first on PATH.
        env = harness_config.test_home({"PATH": "/usr/bin"}, "/nonexistent-home")
        self.assertEqual([os.path.dirname(sys.executable), "/usr/bin"], env["PATH"].split(os.pathsep))
        again = harness_config.test_home(env, "/nonexistent-home")
        self.assertEqual(env["PATH"], again["PATH"], "a nested run adds the folder once")
        found = shutil.which(PYNAME)
        self.assertTrue(found, f"no {PYNAME} on this test run's PATH")
        ok = subprocess.run([found, "-c", "import sys; sys.exit(sys.version_info < (3, 12))"]).returncode
        self.assertEqual(0, ok, f"{found} is older than Python 3.12")

    def test_importing_testgit_puts_the_interpreter_first_on_path(self):
        # A direct `python -m unittest` run has no conftest or smoke.py to set the test PATH: a git test file that
        # imports testgit must still find its own interpreter as python3, once, ahead of an old one.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            if os.name == "nt":  # PATHEXT finds a .cmd stand-in, not an extensionless script
                fake = Path(tmp) / "python.cmd"
                fake.write_text("@exit /b 1\r\n", encoding="utf-8")
            else:
                fake = Path(tmp) / "python3"
                fake.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
                fake.chmod(0o755)
            code = ("import sys, os, shutil; sys.path.insert(0, sys.argv[1]); import testgit; "
                    f"print(os.environ['PATH']); print(shutil.which({PYNAME!r}))")
            env = {"PATH": os.pathsep.join([tmp, "/usr/bin", "/bin"]), "HOME": tmp}
            out = subprocess.run([sys.executable, "-B", "-c", code, str(HERE)], capture_output=True, text=True,
                                 check=True, env=env).stdout.split("\n")
        own = os.path.dirname(sys.executable)
        self.assertEqual(own, out[0].split(os.pathsep)[0], out)
        self.assertEqual(1, out[0].split(os.pathsep).count(own), "the folder is added once")
        self.assertEqual(own, os.path.dirname(out[1]), out)

    def test_a_direct_unittest_run_is_isolated_at_import(self):
        # `python -m unittest` and `python test_x.py` meet no conftest and no smoke runner: importing harness_config
        # isolates them (NO_LOCAL_VAR=1, a temp HOME, the test interpreter first on PATH).
        probe = (
            "import os, shutil, sys, unittest\n"
            f"PYNAME = {PYNAME!r}\n"
            f"sys.path.insert(0, {str(HERE)!r})\n"
            "import harness_config\n"
            "class T(unittest.TestCase):\n"
            "    def test_it(self):\n"
            "        print('SEEN', os.environ.get('HARNESS_TEST_NO_LOCAL_PROBE'), os.environ.get(harness_config.NO_LOCAL_VAR),\n"
            "              os.environ['HOME'], os.environ.get(harness_config.REAL_HOME_VAR),\n"
            "              os.path.dirname(shutil.which(PYNAME)) == os.path.dirname(sys.executable))\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n")
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real-home"
            real.mkdir()
            script = Path(tmp) / "test_probe.py"
            script.write_text(probe, encoding="utf-8")
            env = {k: v for k, v in os.environ.items()
                   if k not in (harness_config.NO_LOCAL_VAR, harness_config.REAL_HOME_VAR)}
            env.update(HOME=str(real), HARNESS_TEST_NO_LOCAL_PROBE="x")
            for argv in ([sys.executable, "-B", "-m", "unittest", "test_probe"], [sys.executable, "-B", str(script)]):
                with self.subTest(argv[2:]):
                    run = subprocess.run(argv, capture_output=True, text=True, env=env, cwd=tmp, timeout=120)
                    seen = [line for line in run.stdout.splitlines() if line.startswith("SEEN")]
                    self.assertEqual(1, len(seen), run.stdout + run.stderr)
                    _, _, no_local, home, saved, own_python = seen[0].split(maxsplit=5)
                    self.assertEqual("1", no_local)
                    self.assertNotEqual(str(real), home)
                    self.assertNotEqual(home, saved)  # the real home is saved for the audit
                    self.assertEqual("True", own_python)

    def test_a_non_test_process_keeps_its_environment(self):
        run = subprocess.run([sys.executable, "-B", "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
                              "import harness_config, os; print(os.environ.get(harness_config.NO_LOCAL_VAR))", str(HERE)],
                             capture_output=True, text=True, timeout=120,
                             env={k: v for k, v in os.environ.items() if k != harness_config.NO_LOCAL_VAR})
        self.assertEqual("None", run.stdout.strip(), run.stderr)

    def test_mem_refuses_the_real_live_db(self):
        real_db = self.real[-1]
        with mock.patch.object(mem.sqlite3, "connect", side_effect=AssertionError("opened the real live DB")), \
                self.assertRaises(mem.LiveUnderTest):
            mem.Mem(real_db)


if __name__ == "__main__":
    unittest.main()
