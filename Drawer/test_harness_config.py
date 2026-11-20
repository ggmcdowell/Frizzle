"""Drawer/harness_config.py: a linked task worktree reads the main checkout's harness.local.toml.

harness.local.toml is git-ignored, so a task worktree has none of its own. Without the main checkout's copy, a
state tool run in the worktree reads the default roots: another hub's state on a box with two hubs.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_config  # noqa: E402

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@example.invalid", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


class WorktreeLocalTomlTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.main, self.tree = base / "hub", base / "hub_wt" / "task"
        self.main.mkdir()
        env = {**harness_config.git_env(), **ENV}
        git = lambda *a: subprocess.run(["git", *a], cwd=self.main, env=env, check=True,  # noqa: E731
                                        capture_output=True, text=True)
        (self.main / "harness.toml").write_text('[project]\nmarker_file = ".m"\nstate_root = "~/default"\n')
        (self.main / ".m").write_text("")
        (self.main / ".gitignore").write_text("harness.local.toml\n")
        git("init", "-q", "-b", "main")
        git("add", ".")
        git("commit", "-q", "-m", "hub")
        git("worktree", "add", "-q", "-b", "task", str(self.tree))
        self.local = self.main / "harness.local.toml"
        self.local.write_text('[project]\nstate_root = "/elsewhere/state"\n')
        drop = mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if k != harness_config.NO_LOCAL_VAR},
                               clear=True)
        drop.start()
        self.addCleanup(drop.stop)

    def test_a_task_worktree_reads_the_main_checkouts_local_toml(self):
        cfg = harness_config.load(self.tree)
        self.assertEqual("/elsewhere/state", cfg["project"]["state_root"])
        self.assertEqual(str(self.local), cfg["derived"]["local_override"])
        self.assertEqual(str(self.tree), cfg["derived"]["root"])

    def test_the_worktrees_own_local_toml_wins(self):
        own = self.tree / "harness.local.toml"
        own.write_text('[project]\nstate_root = "/own/state"\n')
        cfg = harness_config.load(self.tree)
        self.assertEqual("/own/state", cfg["project"]["state_root"])
        self.assertEqual(str(own), cfg["derived"]["local_override"])

    def test_no_local_toml_skips_the_main_checkouts_copy_too(self):
        with mock.patch.dict(os.environ, {harness_config.NO_LOCAL_VAR: "1"}):
            cfg = harness_config.load(self.tree)
        self.assertEqual("~/default", cfg["project"]["state_root"])
        self.assertEqual("", cfg["derived"]["local_override"])

    def test_a_main_checkout_with_no_local_toml_keeps_the_defaults(self):
        self.local.unlink()
        self.assertEqual("~/default", harness_config.load(self.tree)["project"]["state_root"])


class GateArgvTest(unittest.TestCase):
    def test_python_word_is_the_hub_interpreter_and_sh_stays_bare_on_posix(self):
        with mock.patch.object(harness_config.oscompat, "WINDOWS", False):
            self.assertEqual(["sh", "tools/gate.sh", ".", sys.executable],
                             harness_config.gate_argv("sh tools/gate.sh . {python}"))

    def test_windows_runs_a_leading_sh_or_bash_through_git_for_windows(self):
        with mock.patch.object(harness_config.oscompat, "WINDOWS", True), \
                mock.patch.object(harness_config.oscompat, "git_shell", side_effect=lambda n: f"C:/Git/usr/bin/{n}.exe"):
            self.assertEqual(["C:/Git/usr/bin/sh.exe", "tools/gate.sh", "."], harness_config.gate_argv("sh tools/gate.sh ."))
            self.assertEqual(["C:/Git/usr/bin/bash.exe", "x"], harness_config.gate_argv("bash x"))
            self.assertEqual([sys.executable, "g.py"], harness_config.gate_argv("{python} g.py"))

    def test_an_unparsable_command_raises_value_error(self):
        with self.assertRaises(ValueError):
            harness_config.gate_argv("sh 'open")


if __name__ == "__main__":
    unittest.main()
