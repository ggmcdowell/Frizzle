"""Tests for Drawer/task_delivery.py, the delivery check on a task close.

Run: .venv/bin/python -B -m unittest Drawer/test_task_delivery.py. Hermetic: temp repos with a bare origin.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import task_delivery  # noqa: E402
import testgit  # noqa: E402

testgit.arm()  # no git template in any fixture repo

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid", "GIT_CONFIG_GLOBAL": os.devnull}


def g(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, env=ENV, check=True, capture_output=True,
                          text=True).stdout.strip()


class CheckPathTests(unittest.TestCase):
    def test_a_windows_backslash_path_is_a_repo_path_only_on_windows(self):
        with mock.patch.object(task_delivery.oscompat, "WINDOWS", False):
            with self.assertRaises(task_delivery.DeliveryError):
                task_delivery.check_path("Drawer\\mem\\mem.py")
        with mock.patch.object(task_delivery.oscompat, "WINDOWS", True):
            self.assertEqual("Drawer/mem/mem.py", task_delivery.check_path("Drawer\\mem\\mem.py"))
            with self.assertRaises(task_delivery.DeliveryError):
                task_delivery.check_path("..\\x")


class TaskDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.origin = self.tmp / "origin.git"
        g(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.repo = self.tmp / "repo"
        g(self.tmp, "clone", "-q", str(self.origin), str(self.repo))
        g(self.repo, "checkout", "-q", "-b", "main")
        self.commit({"README": "seed\n"}, push=True)
        # The env vars hold for the module's git calls too (identity, no global config).
        patch = mock.patch.dict(os.environ, ENV)
        patch.start()
        self.addCleanup(patch.stop)

    def commit(self, files, push):
        for name, text in files.items():
            (self.repo / name).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / name).write_text(text)
        g(self.repo, "add", "--", *files)
        g(self.repo, "commit", "-q", "-m", "change")
        if push:
            g(self.repo, "push", "-q", "origin", "HEAD:main")
        return g(self.repo, "rev-parse", "HEAD")

    def verify(self, sha, paths):
        return task_delivery.verify([f"{self.repo}={sha}"], paths)

    def refused(self, sha, paths, words):
        with self.assertRaises(task_delivery.DeliveryError) as caught:
            self.verify(sha, paths)
        self.assertIn(words, str(caught.exception))

    def test_a_local_only_commit_is_refused(self):
        sha = self.commit({"a.py": "1\n"}, push=False)
        self.refused(sha, ["a.py"], "not on origin/main")

    def test_a_pushed_commit_that_misses_a_path_is_refused(self):
        self.commit({"b.py": "1\n"}, push=True)
        sha = self.commit({"a.py": "1\n"}, push=True)
        self.refused(sha, ["a.py", "b.py"], "no cited commit changes b.py")

    def test_a_dirty_named_path_is_refused(self):
        sha = self.commit({"a.py": "1\n"}, push=True)
        (self.repo / "a.py").write_text("2\n")
        self.refused(sha, ["a.py"], "a.py has uncommitted work")

    def test_a_pushed_commit_touching_every_path_passes(self):
        sha = self.commit({"a.py": "1\n", "d/b.py": "1\n"}, push=True)
        self.commit({"later.py": "1\n"}, push=True)
        out = self.verify(sha, ["a.py", "d/b.py"])
        self.assertEqual((True, sha), (out["delivered"], out["commits"][0]["commit"]))

    def test_the_check_fetches_before_it_judges(self):
        sha = self.commit({"a.py": "1\n"}, push=False)
        g(self.repo, "push", "-q", "origin", "HEAD:main")
        g(self.repo, "update-ref", "refs/remotes/origin/main", sha + "~1")
        self.assertTrue(self.verify(sha, ["a.py"])["delivered"])

    def test_no_file_changes_needs_a_reason_and_no_commits(self):
        out = task_delivery.verify(no_file_changes="read-only audit; findings went to mem")
        self.assertEqual("read-only audit; findings went to mem", out["no_file_changes"])
        for kwargs in ({"no_file_changes": "  "}, {"no_file_changes": "x", "paths": ["a.py"]}, {}):
            with self.assertRaises(task_delivery.DeliveryError):
                task_delivery.verify(**kwargs)

    def test_the_cli_prints_one_json_line_and_exits_1_on_a_refusal(self):
        sha = self.commit({"a.py": "1\n"}, push=False)
        run = lambda *a: subprocess.run([sys.executable, "-B", str(HERE / "task_delivery.py"), "check", *a],
                                        capture_output=True, text=True, env=ENV)
        bad = run("--commit", f"{self.repo}={sha}", "--path", "a.py")
        g(self.repo, "push", "-q", "origin", "HEAD:main")
        good = run("--commit", f"{self.repo}={sha}", "--path", "a.py")
        self.assertEqual((1, 0), (bad.returncode, good.returncode))
        self.assertIn("error", json.loads(bad.stdout))
        self.assertTrue(json.loads(good.stdout)["delivered"])


if __name__ == "__main__":
    unittest.main()
