"""Drawer/oscompat.py: each OS difference the hub meets. The POSIX branch runs for real here; a Windows branch
runs with oscompat.WINDOWS patched and its Windows call faked, so the case proves which call it makes."""
from __future__ import annotations

import errno
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import oscompat  # noqa: E402


class Interpreter(unittest.TestCase):
    def test_the_venv_interpreter_follows_the_platform(self):
        with mock.patch.object(oscompat, "WINDOWS", False):
            self.assertEqual(Path("h/.venv/bin/python"), oscompat.venv_python("h"))
        with mock.patch.object(oscompat, "WINDOWS", True):
            self.assertEqual(Path("h/.venv/Scripts/python.exe"), oscompat.venv_python("h"))
            self.assertEqual(Path("h/.venv/Scripts"), oscompat.venv_bin("h"))

    def test_the_shim_is_written_once_on_windows_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(oscompat.ensure_venv_shim(tmp))  # POSIX: never
            with mock.patch.object(oscompat, "WINDOWS", True):
                self.assertFalse(oscompat.ensure_venv_shim(tmp))  # no interpreter yet
                exe = Path(tmp, ".venv", "Scripts", "python.exe")
                exe.parent.mkdir(parents=True)
                exe.write_bytes(b"")
                self.assertTrue(oscompat.ensure_venv_shim(tmp))
                self.assertFalse(oscompat.ensure_venv_shim(tmp))
            shim = Path(tmp, ".venv", "bin", "python").read_bytes()
        self.assertEqual(oscompat.VENV_SHIM.encode(), shim)
        self.assertNotIn(b"\r", shim)


class Lock(unittest.TestCase):
    def test_a_second_handle_meets_a_busy_lock_and_a_released_one_is_free(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "x.lock")
            with open(path, "a+b") as one, open(path, "a+b") as two:
                oscompat.lock(one)
                with self.assertRaises(OSError):
                    oscompat.lock(two)
                with self.assertRaises(OSError):
                    oscompat.lock(two, blocking=True, timeout=0.1)
                oscompat.unlock(one)
                oscompat.lock(two)
                oscompat.unlock(two)

    def test_a_lock_fault_that_is_not_busy_raises_at_once_even_when_blocking(self):
        import fcntl
        fault = OSError(errno.ENOLCK, "no locks available")
        with tempfile.TemporaryFile() as f, mock.patch.object(fcntl, "flock", side_effect=fault) as flock:
            with self.assertRaises(OSError):
                oscompat.lock(f, blocking=True)
        self.assertEqual(1, flock.call_count)

    def test_windows_locks_one_byte_past_the_data_and_keeps_the_position(self):
        calls = []
        fake = mock.Mock(LK_NBLCK=2, LK_UNLCK=0, locking=lambda fd, mode, n: calls.append((os.lseek(fd, 0, 1), mode, n)))
        with tempfile.TemporaryFile() as f, mock.patch.object(oscompat, "WINDOWS", True), \
                mock.patch.dict(sys.modules, {"msvcrt": fake}):
            f.write(b"data")
            f.flush()
            oscompat.lock(f)
            oscompat.unlock(f)
            self.assertEqual(4, f.tell())
        self.assertEqual([(oscompat.LOCK_OFFSET, 2, 1), (oscompat.LOCK_OFFSET, 0, 1)], calls)


class Processes(unittest.TestCase):
    def test_a_group_start_and_a_tree_stop_by_platform(self):
        self.assertEqual({"start_new_session": True}, oscompat.popen_group())
        with mock.patch.object(oscompat, "WINDOWS", True), \
                mock.patch.object(oscompat.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, create=True), \
                mock.patch.object(oscompat.subprocess, "CREATE_NO_WINDOW", 0x8000000, create=True):
            self.assertEqual({"creationflags": 0x8000200}, oscompat.popen_group())
        done = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(oscompat, "WINDOWS", True), \
                mock.patch.object(oscompat.subprocess, "run", return_value=done) as run:
            oscompat.kill_tree(42)
            self.assertEqual(["taskkill", "/PID", "42", "/T", "/F"], run.call_args.args[0])
            run.return_value = subprocess.CompletedProcess([], 128, "", "not found")
            with self.assertRaises(ProcessLookupError):
                oscompat.kill_tree(42)

    def test_a_real_posix_group_is_stopped(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], **oscompat.popen_group())
        oscompat.kill_tree(child.pid)
        self.assertNotEqual(0, child.wait(timeout=10))
        with self.assertRaises(ProcessLookupError):
            oscompat.kill_tree(child.pid)


class Paths(unittest.TestCase):
    def test_a_git_bash_drive_path_maps_on_windows_only(self):
        self.assertEqual("/c/work/x", oscompat.native_path("/c/work/x"))
        with mock.patch.object(oscompat, "WINDOWS", True):
            self.assertEqual("C:/work/x", oscompat.native_path("/c/work/x"))
            self.assertEqual("D:/", oscompat.native_path("/d"))
            for same in ("/tmp/x", "/cd/x", "relative", "C:/x"):
                self.assertEqual(same, oscompat.native_path(same))

    def test_replace_retries_a_held_file_on_windows_and_raises_at_last(self):
        with mock.patch.object(oscompat, "WINDOWS", True), mock.patch.object(oscompat, "REPLACE_PAUSE", 0), \
                mock.patch.object(oscompat.os, "replace", side_effect=[PermissionError(), None]) as rep:
            oscompat.replace("a", "b")
        self.assertEqual(2, rep.call_count)
        with mock.patch.object(oscompat, "WINDOWS", True), mock.patch.object(oscompat, "REPLACE_PAUSE", 0), \
                mock.patch.object(oscompat.os, "replace", side_effect=PermissionError()) as rep, \
                self.assertRaises(PermissionError):
            oscompat.replace("a", "b")
        self.assertEqual(oscompat.REPLACE_TRIES, rep.call_count)

    def test_the_recycle_bin_is_windows_only(self):
        with self.assertRaises(OSError):
            oscompat.recycle(__file__)

    def test_git_shell_never_takes_the_wsl_launcher(self):
        self.assertEqual("sh", oscompat.git_shell("sh"))
        with mock.patch.object(oscompat, "WINDOWS", True), \
                mock.patch.object(oscompat.shutil, "which",
                                  lambda name: None if name == "git" else "C:/Windows/System32/bash.exe"), \
                self.assertRaises(OSError):
            oscompat.git_shell("bash")


if __name__ == "__main__":
    unittest.main()
