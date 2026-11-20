"""Tests for Drawer/flight-close.py, Flight's checked close.

Run: .venv/bin/python -B -m unittest Drawer/test_flight_close.py. Hermetic: a scratch MEM_DB and a temp repo with
a bare origin.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import harness_config  # noqa: E402
import testgit  # noqa: E402

MEM_DB, MEM_EMBED = harness_config.env_name("MEM_DB"), harness_config.env_name("MEM_EMBED")  # the hub's prefix
testgit.arm()  # no git template in any fixture repo
CLI = HERE / "flight-close.py"
LIFE = HERE / "task-lifecycle.py"
MEM = HERE / "mem" / "mem.py"
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.invalid", "GIT_CONFIG_GLOBAL": os.devnull}
BODY = "Dusk bell: ring the bell at sunset. Why: no evening cue. Done when: the bell rings at dusk in a build."


class FlightCloseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = {**os.environ, **GIT_ENV, MEM_DB: str(self.tmp / "hub.db"), MEM_EMBED: "off"}
        self.env.pop("CLAUDE_CODE_SESSION_ID", None)
        self.origin = self.tmp / "origin.git"
        self.g(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.repo = self.tmp / "repo"
        self.g(self.tmp, "clone", "-q", str(self.origin), str(self.repo))
        self.g(self.repo, "checkout", "-q", "-b", "main")
        self.base = self.commit("README")

    def g(self, cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, env=self.env, check=True, capture_output=True,
                              text=True).stdout.strip()

    def commit(self, name):
        (self.repo / name).write_text(name + "\n")
        self.g(self.repo, "add", "--", name)
        self.g(self.repo, "commit", "-q", "-m", name)
        self.g(self.repo, "push", "-q", "origin", "HEAD:main")
        return self.g(self.repo, "rev-parse", "HEAD")

    def run_json(self, script, *argv):
        run = subprocess.run([sys.executable, "-B", str(script), *argv], capture_output=True, text=True,
                             env=self.env)
        lines = run.stdout.splitlines()
        self.assertEqual(1, len(lines), run.stdout + run.stderr)
        return run.returncode, json.loads(lines[0])

    def ok(self, script, *argv):
        code, value = self.run_json(script, *argv)
        self.assertEqual(0, code, value)
        return value

    def task(self):
        return self.ok(MEM, "add", "--kind", "task", "--body", BODY, "--json")["id"]

    def complete(self, task, sha):
        claim = self.ok(LIFE, "begin", task[:8], "--session", "session-a")["claim_id"]
        self.ok(LIFE, "complete", task[:8], "--claim-id", claim, "--evidence",
                f"bell test green at {sha[:12]} pushed", "--commit", f"{self.repo}={sha}")

    def close(self, task):
        return self.run_json(CLI, task[:8], "--json")

    def test_close_when_done_with_evidence_and_sha_on_origin_main(self):
        task = self.task()
        sha = self.commit("bell")
        self.complete(task, sha)
        code, out = self.close(task)
        self.assertEqual((0, "CLOSE"), (code, out["result"]), out)
        self.assertEqual([f"{self.repo.resolve()}={sha}"], out["commits"])
        self.assertIn(sha[:12], out["evidence"])

    def test_text_line_names_the_sha(self):
        task = self.task()
        sha = self.commit("bell")
        self.complete(task, sha)
        run = subprocess.run([sys.executable, "-B", str(CLI), task[:8]],
                             capture_output=True, text=True, env=self.env)
        self.assertEqual(0, run.returncode, run.stdout + run.stderr)
        self.assertTrue(run.stdout.startswith(f"CLOSE {task[:8]}: "), run.stdout)
        self.assertIn(sha[:12], run.stdout)

    def test_hold_on_missing_evidence(self):
        task = self.task()
        self.ok(MEM, "status", task[:8], "done", "--json")
        code, out = self.close(task)
        self.assertEqual((1, "HOLD"), (code, out["result"]), out)
        self.assertIn("no evidence", out["why"])

    def test_hold_on_a_task_not_done(self):
        code, out = self.close(self.task())
        self.assertEqual((1, "HOLD"), (code, out["result"]), out)
        self.assertIn("not done", out["why"])

    def test_hold_on_a_sha_not_on_origin_main(self):
        task = self.task()
        sha = self.commit("bell")
        self.complete(task, sha)
        self.g(self.origin, "update-ref", "refs/heads/main", self.base)  # origin main drops the cited commit
        code, out = self.close(task)
        self.assertEqual((1, "HOLD"), (code, out["result"]), out)
        self.assertIn(f"commit {sha[:12]} is not on origin/main", out["why"])


if __name__ == "__main__":
    unittest.main()
