"""Tests for Drawer/task-lifecycle.py and mem complete, the checked task close.

Run: .venv/bin/python -B -m unittest Drawer/test_task_lifecycle.py. Hermetic: a scratch MEM_DB and a temp repo
with a bare origin.
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
CLI = HERE / "task-lifecycle.py"
MEM = HERE / "mem" / "mem.py"
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.invalid", "GIT_CONFIG_GLOBAL": os.devnull}
BODY = "Dusk bell: ring the bell at sunset. Why: no evening cue. Done when: the bell rings at dusk in a build."


class TaskLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = {**os.environ, **GIT_ENV, MEM_DB: str(self.tmp / "hub.db"), MEM_EMBED: "off",
                    "HARNESS_NO_LOCAL_TOML": "1"}
        self.env.pop("CLAUDE_CODE_SESSION_ID", None)
        self.origin = self.tmp / "origin.git"
        self.g(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.repo = self.tmp / "repo"
        self.g(self.tmp, "clone", "-q", str(self.origin), str(self.repo))
        self.g(self.repo, "checkout", "-q", "-b", "main")
        self.commit("README", push=True)
        self.task = self.json(MEM, "add", "--kind", "task", "--body", BODY)["id"]

    def g(self, cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, env=self.env, check=True, capture_output=True,
                              text=True).stdout.strip()

    def commit(self, name, push):
        (self.repo / name).write_text(name + "\n")
        self.g(self.repo, "add", "--", name)
        self.g(self.repo, "commit", "-q", "-m", name)
        if push:
            self.g(self.repo, "push", "-q", "origin", "HEAD:main")
        return self.g(self.repo, "rev-parse", "HEAD")

    def run_cli(self, script, *argv, cwd=None):
        run = subprocess.run([sys.executable, "-B", str(script), *argv], capture_output=True, text=True,
                             env=self.env, cwd=cwd)
        lines = run.stdout.splitlines()
        self.assertEqual(1, len(lines), run.stdout + run.stderr)
        return run.returncode, json.loads(lines[0])

    def json(self, script, *argv):
        code, value = self.run_cli(script, *argv, *(["--json"] if script == MEM else []))
        self.assertEqual(0, code, value)
        return value

    def refused(self, words, *argv):
        code, value = self.run_cli(CLI, *argv)
        self.assertNotEqual(0, code, value)
        self.assertIn(words, value["error"])
        return value

    def card(self):
        return self.json(MEM, "get", self.task)

    def begin(self):
        out = self.json(CLI, "begin", self.task[:8], "--session", "session-a")
        return out["claim_id"]

    def complete(self, claim_id, evidence, *rest):
        return ("complete", self.task[:8], "--claim-id", claim_id, "--evidence", evidence, *rest)

    def test_begin_prints_the_claim_id_card_and_done_when(self):
        out = self.json(CLI, "begin", self.task[:8], "--session", "session-a")
        self.assertEqual("CLAIMED", out["result"])
        self.assertTrue(out["claim_id"])
        self.assertEqual(BODY, out["card"])
        self.assertEqual("the bell rings at dusk in a build.", out["done_when"])
        self.assertEqual(("doing", out["claim_id"]), (self.card()["status"], self.card()["claim_id"]))
        self.refused("already claimed", "begin", self.task[:8], "--session", "session-b")

    def test_a_wrong_claim_id_is_refused_and_the_task_stays_doing(self):
        claim = self.begin()
        sha = self.commit("a.py", push=True)
        self.refused("is not the live claim", *self.complete("wrong-claim", f"Pushed {sha[:10]} with the bell test",
                                                            "--commit", f"{self.repo}={sha}"))
        self.assertEqual(("doing", claim), (self.card()["status"], self.card()["claim_id"]))

    def test_an_unpushed_sha_is_refused_and_the_task_stays_doing(self):
        claim = self.begin()
        sha = self.commit("a.py", push=False)
        self.refused("not on origin/main", *self.complete(claim, f"Committed {sha[:10]} with the bell test",
                                                         "--commit", f"{self.repo}={sha}", "--path", "a.py"))
        card = self.card()
        self.assertEqual(("doing", claim), (card["status"], card["claim_id"]))
        self.assertNotIn("delivered", card)

    def test_three_word_evidence_is_refused(self):
        claim = self.begin()
        sha = self.commit("a.py", push=True)
        self.refused("4 or more words", *self.complete(claim, f"pushed {sha[:10]} done",
                                                      "--commit", f"{self.repo}={sha}"))
        self.assertEqual("doing", self.card()["status"])

    def test_evidence_that_names_no_cited_sha_is_refused(self):
        claim = self.begin()
        sha = self.commit("a.py", push=True)
        self.refused("names no cited sha", *self.complete(claim, "the bell test is green now",
                                                         "--commit", f"{self.repo}={sha}"))
        self.refused("names no cited sha", *self.complete(claim, "the bell test is green at 1234abcd5",
                                                         "--commit", f"{self.repo}={sha}"))

    def test_a_lesson_naming_a_commit_id_is_refused(self):
        claim = self.begin()
        sha = self.commit("a.py", push=True)
        self.refused("names no commit id", *self.complete(
            claim, f"Pushed {sha[:10]} with the bell test", "--commit", f"{self.repo}={sha}",
            "--lesson", f"The bell needs a dusk signal; see {sha[:9]} for the wiring."))
        self.assertEqual(("doing", claim), (self.card()["status"], self.card()["claim_id"]))

    def test_a_pushed_sha_closes_with_delivered_and_the_claim_cleared(self):
        claim = self.begin()
        self.refused("still holds claim", "check-close", self.task[:8])
        sha = self.commit("a.py", push=True)
        out = self.json(CLI, *self.complete(claim, f"Pushed {sha[:10]}; the bell test is green",
                                            "--commit", f"{self.repo}={sha}", "--path", "a.py",
                                            "--lesson", "A dusk cue needs the sky clock, not the frame clock."))
        self.assertEqual(("COMPLETED", "done", True), (out["result"], out["status"], out["delivered"]))
        card = self.card()
        self.assertEqual(("done", 1), (card["status"], card["delivered"]))
        self.assertNotIn("claim_id", card)
        self.assertNotIn("claim_session", card)
        self.assertTrue(card["closed_at"])
        self.assertEqual([{"repo": str(self.repo.resolve()), "commit": sha}], card["commits"])
        self.assertIn(sha[:10], card["evidence"])
        lesson = self.json(MEM, "get", out["lesson"])
        self.assertEqual(("lesson", [f"task:{self.task[:8]}"]), (lesson["kind"], lesson["topics"]))
        self.assertEqual("complete", self.json(MEM, "log", self.task)[-1]["event"])
        check = self.json(CLI, "check-close", self.task[:8])
        self.assertEqual(("CLOSED", "done", True), (check["result"], check["status"], check["delivered"]))
        self.refused("holds no claim", *self.complete(claim, f"Pushed {sha[:10]}; the bell test is green",
                                                     "--commit", f"{self.repo}={sha}"))

    def test_a_critical_claim_with_no_evidence_label_is_refused_and_a_labeled_one_closes(self):
        # WRK-4: a crash, save loss, blocker, data loss or gate fault claim carries its label.
        claim = self.begin()
        sha = self.commit("a.py", push=True)
        rest = ("--commit", f"{self.repo}={sha}", "--path", "a.py")
        self.refused("evidence label", *self.complete(claim, f"Landed {sha[:10]}: fixed the boot crash on load.", *rest))
        self.assertEqual(("doing", claim), (self.card()["status"], self.card()["claim_id"]))
        out = self.json(CLI, *self.complete(claim, f"Landed {sha[:10]}: [test] fixed the boot crash on load.", *rest))
        self.assertEqual(("COMPLETED", "done", True), (out["result"], out["status"], out["delivered"]))

    def test_the_repo_name_hub_closes_against_the_hub_checkout_and_an_unknown_name_is_refused(self):
        (self.repo / "harness.toml").write_text('[project]\nmarker_file = "AGENTS.md"\nenv_prefix = "T"\n')
        self.commit("AGENTS.md", push=True)
        claim = self.begin()
        sha = self.commit("a.py", push=True)
        evidence = f"Pushed {sha[:10]}; the bell test is green"
        code, value = self.run_cli(CLI, *self.complete(claim, evidence, "--commit", f"hubb={sha}"), cwd=self.repo)
        self.assertNotEqual(0, code, value)
        self.assertIn("unknown repo name 'hubb'", value["error"])
        self.assertEqual("doing", self.card()["status"])
        code, out = self.run_cli(CLI, *self.complete(claim, evidence, "--commit", f"hub={sha}", "--path", "a.py"),
                                 cwd=self.repo)
        self.assertEqual(0, code, out)
        self.assertEqual("COMPLETED", out["result"])
        self.assertEqual([{"repo": str(self.repo.resolve()), "commit": sha}], self.card()["commits"])

    def test_no_file_changes_closes_without_a_sha(self):
        claim = self.begin()
        out = self.json(CLI, *self.complete(claim, "read only audit, nothing to change",
                                            "--no-file-changes", "an audit with no finding"))
        self.assertEqual(("done", True), (out["status"], out["delivered"]))
        self.assertNotIn("commits", out)

    def test_check_close_refuses_a_done_task_closed_outside_complete(self):
        self.json(MEM, "status", self.task, "done")
        self.refused("no delivery check", "check-close", self.task[:8])

    def test_a_reopen_drops_the_old_proof_so_a_later_done_is_unchecked(self):
        claim = self.begin()
        sha = self.commit("a.py", push=True)
        self.json(CLI, *self.complete(claim, f"Pushed {sha[:10]}; the bell test is green", "--commit", f"{self.repo}={sha}"))
        reopened = self.json(MEM, "status", self.task, "todo", "--reason", "the bell is silent in play")
        for k in ("delivered", "evidence", "commits"):
            self.assertNotIn(k, reopened)
        self.json(MEM, "status", self.task, "done", "--reason", "closed by hand")
        self.refused("no delivery check", "check-close", self.task[:8])

    def test_a_lesson_may_cite_a_card_id_and_a_date_number_but_no_sha(self):
        claim = self.begin()
        sha = self.commit("a.py", push=True)
        out = self.json(CLI, *self.complete(claim, f"Pushed {sha[:10]}; the bell test is green", "--commit",
                                            f"{self.repo}={sha}", "--lesson",
                                            f"Card {self.task[:8]} shows a dusk cue needs 20261116 sky ticks."))
        self.assertEqual("done", out["status"])

    def test_release_interrupt_and_note(self):
        claim = self.begin()
        noted = self.json(CLI, "note", self.task[:8], "halt after the bell test", "--stop", "--session", "flight")
        self.assertEqual(("NOTED", True), (noted["result"], noted["stop"]))
        self.refused("is not the live claim", "release", self.task[:8], "--claim-id", "nope")
        out = self.json(CLI, "release", self.task[:8], "--claim-id", claim, "--reason", "blocked on art")
        self.assertEqual(("RELEASED", "todo"), (out["result"], out["status"]))
        self.assertEqual("NO-CLAIM", self.json(CLI, "check-close", self.task[:8])["result"])
        self.refused("holds no claim", "interrupt", self.task[:8])


if __name__ == "__main__":
    unittest.main()
