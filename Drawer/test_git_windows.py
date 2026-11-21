"""The Windows branches of the git tools: git_guard's command names and Git Bash paths, git-branch-policy's
junction, Recycle Bin fallback, process-group start and lock, and the hooks' interpreter list.

Each branch runs here with oscompat.WINDOWS patched: the Windows call it makes is faked, so the case proves the
branch is taken and what it asks for. The real calls are oscompat's own (test_oscompat).
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Drawer"))
import git_guard  # noqa: E402
import oscompat  # noqa: E402

_spec = importlib.util.spec_from_file_location("git_windows_policy", ROOT / "Drawer/git-branch-policy.py")
policy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(policy)


class GuardWindowsVerbs(unittest.TestCase):
    def test_taskkill_passes_only_a_literal_pid(self) -> None:
        ctx = {"cwd": "/work/user"}
        self.assertIsNone(git_guard.reason("taskkill //PID 4242 //F", ctx))
        self.assertIsNone(git_guard.reason("taskkill /PID 4242 /T /F", ctx))
        for line in ("taskkill //IM node.exe //F", "taskkill /FI 'IMAGENAME eq x' /F", "taskkill //F",
                     "taskkill //PID $(cat pid) //F"):
            with self.subTest(line):
                self.assertIn("delete guard refused", git_guard.reason(line, ctx) or "")

    def test_a_delete_inside_cmd_or_powershell_is_refused(self) -> None:
        ctx = {"cwd": "/work/user"}
        for line in ('powershell -c "Remove-Item -Recurse -Force C:/work/peer"', "cmd //c rd //s //q C:\\work\\peer",
                     'pwsh -c "Stop-Process -Name node"', "cmd //c del //s x"):
            with self.subTest(line):
                self.assertIn("delete guard refused", git_guard.reason(line, ctx) or "")
        self.assertIsNone(git_guard.reason('powershell -c "Get-ChildItem"', ctx))


class GuardNames(unittest.TestCase):
    def test_a_command_name_loses_its_exe_and_cmd_and_its_folder(self) -> None:
        for word, name in (("git.exe", "git"), ("/mingw64/bin/Git.EXE", "git"), ("rm.exe", "rm"),
                           ("C:/Git/usr/bin/rm.exe", "rm"), ("npm.cmd", "npm"), ("git", "git"), ("", "")):
            self.assertEqual(name, git_guard.cmd_name(word), word)

    def test_rm_exe_is_judged_like_rm(self) -> None:
        # A folder that exists and lies outside temp and any own worktree: /usr/lib, on Windows the stdlib folder.
        target = Path(os.__file__).parent.as_posix() if oscompat.WINDOWS else "/usr/lib"
        refused = git_guard.reason(f"rm -rf {target}", {"cwd": "/work/user"})
        self.assertTrue(refused)
        self.assertEqual(refused, git_guard.reason(f"rm.exe -rf {target}", {"cwd": "/work/user"}))

    def test_a_hooks_path_override_through_git_exe_is_refused(self) -> None:
        self.assertIn("core.hooksPath", git_guard.reason("git.exe -c core.hooksPath=/x commit -m x", {"cwd": "/work/user"}))


class GuardPaths(unittest.TestCase):
    def test_native_reads_a_git_bash_drive_and_tmp_on_windows_only(self) -> None:
        with mock.patch.object(oscompat, "WINDOWS", True), mock.patch.object(tempfile, "gettempdir", return_value="C:/T"):
            self.assertEqual("C:/work/x", git_guard.native("/c/work/x"))
            self.assertEqual("C:/T/a/b", git_guard.native("/tmp/a/b"))
            self.assertEqual("C:/T", git_guard.native("/var/tmp"))
            self.assertEqual("/tmpfile", git_guard.native("/tmpfile"))
            self.assertEqual("C:/x", git_guard.native("C:/x"))
        with mock.patch.object(oscompat, "WINDOWS", False):
            for text in ("/c/work/x", "/tmp/a", "C:/x"):
                self.assertEqual(text, git_guard.native(text))

    def test_below_ignores_case_where_the_platform_does(self) -> None:
        with mock.patch("os.sep", "/"), mock.patch("os.path.normcase", lambda text: text):
            self.assertFalse(git_guard.below("/Tmp/A/b", "/tmp/a"))
        with mock.patch("os.sep", "/"), mock.patch("os.path.normcase", lambda text: text.lower()):
            self.assertTrue(git_guard.below("/Tmp/A/b", "/tmp/a"))
            self.assertFalse(git_guard.below("/TMP/a", "/tmp/a"))

    def test_a_drive_path_is_absolute_on_windows(self) -> None:
        # With no folder the line can be in, a relative target is unplaceable; an absolute one is placed.
        with mock.patch.object(oscompat, "WINDOWS", True), mock.patch.object(tempfile, "gettempdir", return_value="C:/T"):
            why = git_guard.place_reason("C:/x", [], git_guard.Line("rm -rf C:/x", "abcd1234"))
        self.assertFalse(why and why.startswith("cannot place"), why)
        with mock.patch.object(oscompat, "WINDOWS", False):  # elsewhere C:/x is relative
            self.assertTrue(git_guard.place_reason("C:/x", [], git_guard.Line("rm -rf C:/x", "abcd1234")).startswith("cannot place"))


class PolicyWindows(unittest.TestCase):
    def test_the_interpreter_link_is_a_junction_on_windows(self) -> None:
        made = []
        fake = mock.Mock(CreateJunction=lambda target, link: made.append((target, link)))
        with mock.patch.object(oscompat, "WINDOWS", True), mock.patch.dict(sys.modules, {"_winapi": fake}):
            policy._link_venv(Path("hub/.venv"), Path("wt/.venv"))
        self.assertEqual([(str(Path("hub/.venv")), str(Path("wt/.venv")))], made)

    def test_the_interpreter_link_is_a_symlink_elsewhere(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target, link = Path(temp, "target"), Path(temp, "link")
            target.mkdir()
            with mock.patch.object(oscompat, "WINDOWS", False), mock.patch.object(Path, "symlink_to") as symlink:
                policy._link_venv(target, link)  # no real link: a symlink needs a privilege on Windows
            symlink.assert_called_once_with(target, target_is_directory=True)

    def test_install_would_set_long_paths_on_windows_only(self) -> None:
        source = Path(policy.__file__).read_text(encoding="utf-8")
        self.assertIn('if oscompat.WINDOWS:', source)
        self.assertIn('"core.longpaths", "true"', source)

    def test_trash_without_a_command_goes_to_the_recycle_bin_on_windows(self) -> None:
        with mock.patch.object(oscompat, "WINDOWS", True), mock.patch("shutil.which", return_value=None), \
                mock.patch("os.access", return_value=False), mock.patch.object(oscompat, "recycle") as recycle:
            policy._trash(Path("scratch"))
        recycle.assert_called_once_with(Path("scratch"))
        with mock.patch.object(oscompat, "WINDOWS", True), mock.patch("shutil.which", return_value=None), \
                mock.patch("os.access", return_value=False), mock.patch.object(oscompat, "recycle", side_effect=OSError("busy")):
            with self.assertRaisesRegex(policy.PolicyError, "trash failed: busy"):
                policy._trash(Path("scratch"))

    def test_a_timed_git_call_starts_in_its_own_group_and_kills_the_tree(self) -> None:
        with mock.patch.object(oscompat, "popen_group", wraps=oscompat.popen_group) as group, \
                mock.patch.object(oscompat, "kill_tree", wraps=oscompat.kill_tree) as kill:
            with self.assertRaises(policy.GitTimeout):
                policy._git(Path("."), "-c", "alias.slow=!sleep 5", "slow", timeout=0.2, check=False)
        group.assert_called_once()
        kill.assert_called_once()

    def test_the_freshness_lock_is_oscompat_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            subprocess.run(["git", "init", "-q", temp], check=True)
            with policy._FreshnessLock(Path(temp)):
                with self.assertRaises(policy.PolicyError):
                    policy._FreshnessLock(Path(temp), timeout=0.1).__enter__()


class Hooks(unittest.TestCase):
    def test_each_hook_tries_the_windows_interpreter_first_and_runs_python_in_utf8(self) -> None:
        for name in ("pre-commit", "pre-push"):
            text = (ROOT / "Drawer/git-hooks" / name).read_text(encoding="utf-8")
            self.assertIn("export PYTHONUTF8=1", text, name)
            candidates = text.split("for candidate in ", 1)[1].split("; do", 1)[0]
            self.assertEqual(["$repo/.venv/Scripts/python.exe", "$repo/.venv/bin/python",
                              "$(command -v python3 2>/dev/null)", "$(command -v python 2>/dev/null)"],
                             shlex.split(candidates), name)
        self.assertIn("winget install gitleaks.gitleaks", (ROOT / "Drawer/git-hooks/pre-commit").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
