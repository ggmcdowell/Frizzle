"""Tests for Drawer/product-push.py on a bare-origin product fixture with a stub gate.

Each test builds origin.git (bare), a main clone product/, a peer clone and a linked product worktree product_wt/t1. The
stub push gate (.githooks/pre-push) passes when .gate-verdict in the worktree names HEAD's tree; the stub
[product] gate_command writes that file and logs each run.
"""
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import review_pass  # noqa: E402
import steer_check  # noqa: E402
import testgit  # noqa: E402

testgit.arm()  # no git template in any fixture repo

SPEC = importlib.util.spec_from_file_location("product_push", HERE / "product-push.py")
product_push = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(product_push)

SESSION = "0a1b2c3d-0000-4000-8000-000000000000"
TASK8 = "1234abcd"
HOOK = """#!/usr/bin/env python3
import subprocess, sys
from pathlib import Path
sys.stdin.read()
tree = subprocess.run(["git", "rev-parse", "HEAD^{tree}"], capture_output=True, text=True).stdout.strip()
verdict = Path(".gate-verdict")
sys.exit(0 if verdict.is_file() and verdict.read_text().strip() == tree else 1)
"""
GATE = """import os, subprocess, sys
from pathlib import Path
tree = subprocess.run(["git", "rev-parse", "HEAD^{tree}"], capture_output=True, text=True).stdout.strip()
Path(".gate-verdict").write_text(tree)
with open(sys.argv[1], "a") as log:
    log.write(tree + "\\n")
peer = os.environ.get("PEER_PUSH_ONCE")
if peer and Path(peer).is_dir() and not Path(peer, ".pushed").exists():
    Path(peer, ".pushed").write_text("x")
    Path(peer, "peer.txt").write_text("peer\\n")
    for args in (["add", "peer.txt"], ["commit", "-qm", "peer"], ["push", "-q", "origin", "HEAD:main"]):
        subprocess.run(["git", *args], cwd=peer, check=True, capture_output=True)
"""


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True).stdout.strip()


class ProductPushTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        empty = base / "empty.gitconfig"
        empty.write_text("", encoding="utf-8")
        env = {"GIT_CONFIG_GLOBAL": str(empty), "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
               "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@example.invalid", "CLAUDE_CODE_SESSION_ID": SESSION}
        patch = mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("PEER_PUSH_ONCE", None)
        # Product-push reads the task's tags from mem; here a table answers, never the live mem.
        self.tags, self.tag_calls = {}, []

        def tags(hub, task):
            self.tag_calls.append(task)
            value = self.tags.get(task, [])
            if isinstance(value, Exception):
                raise value
            return value

        tag_patch = mock.patch.object(review_pass, "task_tags", tags)
        tag_patch.start()
        self.addCleanup(tag_patch.stop)
        self.origin = base / "origin.git"
        git(base, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.main = base / "product"
        git(base, "clone", "-q", str(self.origin), str(self.main))
        (self.main / ".githooks").mkdir()
        (self.main / ".githooks" / "pre-push").write_text(HOOK, encoding="utf-8")
        (self.main / ".gitignore").write_text(".gate-verdict\n", encoding="utf-8")
        (self.main / "product.txt").write_text("one\n", encoding="utf-8")
        git(self.main, "checkout", "-q", "-b", "main")
        git(self.main, "add", ".")
        git(self.main, "commit", "-qm", "base")
        git(self.main, "push", "-q", "origin", "main")
        self.peer = base / "peer"
        git(base, "clone", "-q", str(self.origin), str(self.peer))
        self.wt = base / "product_wt" / "t1"
        git(self.main, "worktree", "add", "-q", "-b", "task/t1", str(self.wt), "origin/main")
        self.gate_log = base / "gate.log"
        gate = base / "gate.py"
        gate.write_text(GATE, encoding="utf-8")
        self.cfg = {"project": {"state_root": str(base / "state")},
                    "product": {"gate_command": f"{{python}} {gate} {self.gate_log}"}}

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self, repo, name, text, message="change"):
        (repo / name).write_text(text, encoding="utf-8")
        git(repo, "add", name)
        git(repo, "commit", "-qm", message)

    def push(self, *args, cfg=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = product_push.main([str(args[0] if args else self.wt), *args[1:]], cfg or self.cfg)
        lines = out.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 1, out.getvalue())
        return code, json.loads(lines[0])

    def origin_head(self):
        return git(self.origin, "rev-parse", "main")

    def gate_runs(self):
        return self.gate_log.read_text().split() if self.gate_log.exists() else []

    def test_main_clone_refused(self):
        self.commit(self.main, "product.txt", "main\n")
        before = self.origin_head()
        code, row = self.push(self.main)
        self.assertEqual((code, row["state"], row["step"]), (1, "failed", "worktree"))
        self.assertEqual(self.origin_head(), before)

    def test_dirty_refused(self):
        self.commit(self.wt, "product.txt", "two\n")
        (self.wt / "product.txt").write_text("dirty\n", encoding="utf-8")
        code, row = self.push()
        self.assertEqual((code, row["state"], row["step"]), (1, "failed", "clean"))
        self.assertEqual(self.gate_runs(), [])

    def test_conflict_aborted(self):
        self.commit(self.peer, "product.txt", "peer\n")
        git(self.peer, "push", "-q", "origin", "HEAD:main")
        self.commit(self.wt, "product.txt", "mine\n")
        mine = git(self.wt, "rev-parse", "HEAD")
        code, row = self.push()
        self.assertEqual((code, row["state"], row["step"]), (1, "failed", "rebase"))
        self.assertEqual(git(self.wt, "rev-parse", "HEAD"), mine)
        self.assertEqual(git(self.wt, "status", "--porcelain"), "")
        gitdir = Path(git(self.wt, "rev-parse", "--absolute-git-dir"))
        self.assertFalse((gitdir / "rebase-merge").exists() or (gitdir / "rebase-apply").exists())

    def test_mid_rebase_worktree_refused_and_its_rebase_kept(self):
        # Product-push aborted a rebase the caller had left open, with no word.
        self.commit(self.wt, "product.txt", "two\n")
        subprocess.run(["git", "rebase", "-x", "false", "HEAD~1"], cwd=str(self.wt), capture_output=True)
        gitdir = Path(git(self.wt, "rev-parse", "--absolute-git-dir"))
        self.assertTrue((gitdir / "rebase-merge").exists())
        before = self.origin_head()
        code, row = self.push()
        self.assertEqual((code, row["state"], row["step"]), (1, "failed", "rebase-in-progress"))
        self.assertIn("git rebase --abort", row["output"])
        self.assertTrue((gitdir / "rebase-merge").exists())
        self.assertEqual(self.origin_head(), before)
        self.assertEqual(self.gate_runs(), [])

    def test_steer_stop_during_retry_backoff_holds_the_retry(self):
        # A STOP set while pushretry waits after a remote-side fault must hold the retry.
        self.commit(self.wt, "product.txt", "two\n")
        real = product_push.git
        pushes = []

        def fake(root, *args):
            if args[:1] == ("push",):
                pushes.append(args)
                if len(pushes) == 1:
                    steer_check.stop(self.cfg, SESSION, "hold during backoff", "push")
                    return subprocess.CompletedProcess(args, 1, "", "error: RPC failed; HTTP 500\n")
            return real(root, *args)

        before = self.origin_head()
        with mock.patch.object(product_push, "git", fake), \
                mock.patch.object(product_push.pushretry, "BACKOFF_S", (0, 0, 0)):
            code, row = self.push()
        self.assertEqual((code, row["state"], row["step"]), (75, "need", "push"))
        self.assertIn("hold during backoff", row["why"])
        self.assertEqual(len(pushes), 1)
        self.assertEqual(self.origin_head(), before)

    def test_gate_runs_when_uncovered_then_pushes_and_rerun_is_already(self):
        self.commit(self.wt, "product.txt", "two\n")
        code, row = self.push()
        self.assertEqual((code, row["state"], row["gated"], row["rounds"]), (0, "pushed", True, 1))
        self.assertEqual(self.origin_head(), git(self.wt, "rev-parse", "HEAD"))
        self.assertEqual(len(self.gate_runs()), 1)
        code, row = self.push()
        self.assertEqual((code, row["state"]), (0, "already"))
        self.assertEqual(len(self.gate_runs()), 1)

    def test_covered_head_skips_the_gate(self):
        self.commit(self.wt, "product.txt", "two\n")
        (self.wt / ".gate-verdict").write_text(git(self.wt, "rev-parse", "HEAD^{tree}"), encoding="utf-8")
        code, row = self.push()
        self.assertEqual((code, row["state"], row["gated"]), (0, "pushed", False))
        self.assertEqual(self.gate_runs(), [])

    def test_steer_stop_gives_need_and_pushes_nothing(self):
        self.commit(self.wt, "product.txt", "two\n")
        steer_check.stop(self.cfg, SESSION, "hold the push", "push")
        before = self.origin_head()
        code, row = self.push()
        self.assertEqual((code, row["state"], row["step"]), (75, "need", "push"))
        self.assertEqual(self.origin_head(), before)
        self.assertEqual(self.gate_runs(), [])
        steer_check.go(self.cfg, SESSION)
        code, row = self.push()
        self.assertEqual((code, row["state"]), (0, "pushed"))

    def test_session_flag_names_the_steer_subject(self):
        self.commit(self.wt, "product.txt", "two\n")
        steer_check.stop(self.cfg, "deadbeef", "hold", "push")
        code, row = self.push(self.wt, "--session", "deadbeef")
        self.assertEqual((code, row["state"]), (75, "need"))

    def test_task_flag_under_a_task8_stop_gives_need_and_pushes_nothing(self):
        # Legs share one session id, so Flight holds one task's push by its task8.
        self.commit(self.wt, "product.txt", "two\n")
        steer_check.stop(self.cfg, "5a5a5a5a", "hold the task", "push")
        before = self.origin_head()
        code, row = self.push(self.wt, "--task", "5a5a5a5a")
        self.assertEqual((code, row["state"], row["step"]), (75, "need", "push"))
        self.assertIn("5a5a5a5a", row["why"])
        self.assertEqual(self.origin_head(), before)
        self.assertEqual(self.gate_runs(), [])
        steer_check.go(self.cfg, "5a5a5a5a")
        code, row = self.push(self.wt, "--task", "5a5a5a5a-5dcf-4a02-89de-488151981b27")
        self.assertEqual((code, row["state"]), (0, "pushed"))

    def test_task_flag_takes_a_task8_only(self):
        self.commit(self.wt, "product.txt", "two\n")
        with contextlib.redirect_stderr(io.StringIO()):
            code = product_push.main([str(self.wt), "--task", "nothex00"], cfg=self.cfg)
        self.assertEqual(code, 2)

    def test_non_fast_forward_rebased_and_pushed(self):
        self.commit(self.wt, "product.txt", "two\n")
        os.environ["PEER_PUSH_ONCE"] = str(self.peer)
        code, row = self.push()
        self.assertEqual((code, row["state"], row["rounds"], row["gated"]), (0, "pushed", 2, True))
        head = self.origin_head()
        self.assertEqual(head, git(self.wt, "rev-parse", "HEAD"))
        self.assertIn("peer", git(self.origin, "log", "--format=%s", "main"))
        self.assertEqual(len(self.gate_runs()), 2)

    def test_gate_set_and_hook_missing_refused(self):
        git(self.wt, "rm", "-q", ".githooks/pre-push")
        git(self.wt, "commit", "-qm", "drop hook")
        before = self.origin_head()
        code, row = self.push()
        self.assertEqual((code, row["state"], row["step"]), (1, "failed", "gate"))
        self.assertEqual(self.origin_head(), before)

    def test_red_gate_pushes_nothing(self):
        self.commit(self.wt, "product.txt", "two\n")
        cfg = {**self.cfg, "product": {"gate_command": "{python} -c \"raise SystemExit(1)\""}}
        before = self.origin_head()
        code, row = self.push(cfg=cfg)
        self.assertEqual((code, row["state"], row["step"], row["gate_exit"]), (1, "failed", "gate", 1))
        self.assertEqual(self.origin_head(), before)

    def test_gate_that_cannot_start_prints_a_failed_line(self):
        self.commit(self.wt, "product.txt", "two\n")
        cfg = {**self.cfg, "product": {"gate_command": "no-such-gate-binary-xyz"}}
        before = self.origin_head()
        code, row = self.push(cfg=cfg)
        self.assertEqual((code, row["state"], row["step"]), (1, "failed", "gate"))
        self.assertEqual(self.origin_head(), before)

    def test_no_session_refused(self):
        os.environ.pop("CLAUDE_CODE_SESSION_ID")
        self.commit(self.wt, "product.txt", "two\n")
        code, row = self.push()
        self.assertEqual((code, row["state"], row["step"]), (1, "failed", "steer"))

    def test_missing_folder_prints_a_failed_line(self):
        code, row = self.push(self.wt.parent / "absent")
        self.assertEqual((code, row["state"], row["step"]), (1, "failed", "worktree"))

    def reviewed(self):
        return product_push.review_diff.build(self.wt, self.cfg["project"]["state_root"])["hash"]

    def test_commit_added_after_review_gives_review_exit_3_and_pushes_nothing(self):
        self.commit(self.wt, "product.txt", "two\n")
        reviewed = self.reviewed()
        self.commit(self.wt, "late.txt", "unreviewed\n")
        before = self.origin_head()
        code, row = self.push(self.wt, "--reviewed", reviewed)
        self.assertEqual((code, row["state"], row["reviewed"]), (3, "review", reviewed))
        self.assertEqual(row["hash"], self.reviewed())
        self.assertEqual(row["args"]["files"], ["late.txt", "product.txt"])
        self.assertEqual(self.origin_head(), before)
        self.assertEqual(self.gate_runs(), [])
        code, row = self.push(self.wt, "--reviewed", row["hash"])
        self.assertEqual((code, row["state"]), (0, "pushed"))

    def test_reviewed_hash_holds_across_a_rebase_onto_an_unrelated_change(self):
        self.commit(self.wt, "product.txt", "two\n")
        self.commit(self.wt, "hero.png.import", "[remap]\n")
        reviewed = self.reviewed()
        self.commit(self.peer, "peer.txt", "peer\n")
        git(self.peer, "push", "-q", "origin", "HEAD:main")
        code, row = self.push(self.wt, "--session", SESSION, "--reviewed", reviewed.upper())
        self.assertEqual((code, row["state"]), (0, "pushed"), row)
        self.assertEqual(self.origin_head(), git(self.wt, "rev-parse", "HEAD"))

    def store_pass(self, found):
        review_pass.record(self.cfg["project"]["state_root"], found["hash"], found["head"], found["diffPath"], 1, "sonnet")

    def review_args(self, wt=None):
        return product_push.review_diff.build(wt or self.wt, self.cfg["project"]["state_root"])

    def test_risky_push_with_no_reviewed_is_refused_with_its_review_args(self):
        self.tags[TASK8] = ["risky"]
        self.commit(self.wt, "product.txt", "two\n")
        before = self.origin_head()
        code, row = self.push(self.wt, "--task", TASK8)
        self.assertEqual((code, row["state"], row["reviewed"]), (3, "review", None), row)
        self.assertEqual((row["hash"], row["args"]["hash"]), (self.reviewed(), self.reviewed()))
        self.assertIn(f"task {TASK8} is tagged risky", row["output"])
        self.assertEqual(self.origin_head(), before)
        self.assertEqual(self.gate_runs(), [])

    def test_risky_push_with_a_pass_for_another_diff_is_refused(self):
        self.tags[TASK8] = ["risky"]
        self.commit(self.wt, "product.txt", "two\n")
        first = self.review_args()
        self.store_pass(first)
        self.commit(self.wt, "late.txt", "late\n")
        now = self.reviewed()
        self.assertNotEqual(first["hash"], now)
        before = self.origin_head()
        code, row = self.push(self.wt, "--task", TASK8, "--reviewed", now)
        self.assertEqual((code, row["state"], row["reviewed"], row["hash"]), (3, "review", now, now), row)
        self.assertIn("no stored risky-review Pass", row["output"])
        code, row = self.push(self.wt, "--task", TASK8, "--reviewed", first["hash"])
        self.assertEqual((code, row["state"], row["hash"]), (3, "review", now), row)
        self.assertEqual(self.origin_head(), before)
        self.assertEqual(self.gate_runs(), [])

    def test_risky_push_with_a_stored_pass_pushes_with_its_reviewed_hash(self):
        self.tags[TASK8] = ["risky"]
        self.commit(self.wt, "product.txt", "two\n")
        found = self.review_args()
        self.store_pass(found)
        code, row = self.push(self.wt, "--task", TASK8, "--reviewed", found["hash"])
        self.assertEqual((code, row["state"], row["reviewed"]), (0, "pushed", found["hash"]), row)
        self.assertEqual(self.origin_head(), git(self.wt, "rev-parse", "HEAD"))

    def test_push_not_tagged_risky_needs_no_review(self):
        self.commit(self.wt, "product.txt", "two\n")
        code, row = self.push(self.wt, "--task", TASK8)
        self.assertEqual((code, row["state"]), (0, "pushed"), row)
        self.assertIn("reviewed", row)
        self.assertIsNone(row["reviewed"])
        self.assertEqual([TASK8], self.tag_calls)

    def test_the_task_is_the_product_worktree_name_when_no_task_is_given(self):
        # product_worktree.py names a task's product worktree <task8>.
        self.tags[TASK8] = ["risky"]
        wt = self.wt.parent / TASK8
        git(self.main, "worktree", "add", "-q", "-b", f"task/{TASK8}", str(wt), "origin/main")
        self.commit(wt, "product.txt", "two\n")
        before = self.origin_head()
        code, row = self.push(wt)
        self.assertEqual((code, row["state"], row["reviewed"]), (3, "review", None), row)
        self.assertEqual([TASK8], self.tag_calls)
        self.assertEqual(self.origin_head(), before)
        self.tags[TASK8] = review_pass.NoCard("no card matches")  # a folder name that names no card: not risky
        code, row = self.push(wt)
        self.assertEqual((code, row["state"]), (0, "pushed"), row)

    def test_a_mem_fault_for_the_product_worktree_name_fails_at_step_review(self):
        # Only NoCard may mean not risky for a task from the folder name; any other mem fault (database locked,
        # timeout) fails closed before the fetch.
        self.tags[TASK8] = review_pass.Refused("mem get: database is locked")
        wt = self.wt.parent / TASK8
        git(self.main, "worktree", "add", "-q", "-b", f"task/{TASK8}", str(wt), "origin/main")
        self.commit(wt, "product.txt", "two\n")
        before = self.origin_head()
        code, row = self.push(wt)
        self.assertEqual((code, row["state"], row["step"]), (1, "failed", "review"), row)
        self.assertIn(f"the tags of task {TASK8} are unreadable", row["output"])
        self.assertEqual(self.origin_head(), before)
        self.assertEqual(self.gate_runs(), [])

    def test_a_session_product_worktree_with_no_card_pushes_as_not_risky(self):
        # A product worktree <product repo>_wt/<session uuid8> names no card: with no --task there is no gate.
        session8 = SESSION[:8]
        self.tags[session8] = review_pass.NoCard("no card matches")
        wt = self.wt.parent / session8
        git(self.main, "worktree", "add", "-q", "-b", f"session/{session8}", str(wt), "origin/main")
        self.commit(wt, "product.txt", "two\n")
        code, row = self.push(wt)
        self.assertEqual((code, row["state"], row["reviewed"]), (0, "pushed", None), row)
        self.assertEqual([session8], self.tag_calls)

    def test_a_mem_fault_for_an_explicit_task_fails_at_step_review(self):
        self.commit(self.wt, "product.txt", "two\n")
        before = self.origin_head()
        for error in (review_pass.Refused("mem get: database is locked"), review_pass.NoCard("no card matches")):
            with self.subTest(error=str(error)):
                self.tags[TASK8] = error
                code, row = self.push(self.wt, "--task", TASK8)
                self.assertEqual((code, row["state"], row["step"]), (1, "failed", "review"), row)
                self.assertIn(str(error), row["output"])
        self.assertEqual(self.origin_head(), before)
        self.assertEqual(self.gate_runs(), [])

    def test_bad_arguments(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(product_push.main([], self.cfg), 2)
            self.assertEqual(product_push.main([str(self.wt), "--session"], self.cfg), 2)
            self.assertEqual(product_push.main([str(self.wt), "--reviewed", "abc"], self.cfg), 2)
            self.assertEqual(product_push.main([str(self.wt), "--force", "x"], self.cfg), 2)
            self.assertEqual(product_push.main([str(self.wt), "--session", "a", "--session", "b"], self.cfg), 2)
            self.assertEqual(product_push.main([str(self.wt), "--session", "--reviewed"], self.cfg), 2)


class HookArgvTests(unittest.TestCase):
    def hook(self, first):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "pre-push"
        path.write_text(first + "\n", encoding="utf-8")
        return path

    def test_the_runner_follows_the_shebang_through_git_shell(self):
        with mock.patch.object(product_push.oscompat, "git_shell", side_effect=lambda name: f"/git/{name}"):
            for first, runner in (("#!/bin/sh", "/git/sh"), ("#!/usr/bin/env bash", "/git/bash"), ("echo", "/git/sh")):
                hook = self.hook(first)
                self.assertEqual([runner, str(hook)], product_push.hook_argv(hook), first)
            hook = self.hook("#!/usr/bin/env python3")
            self.assertEqual([sys.executable, "-B", str(hook)], product_push.hook_argv(hook))


if __name__ == "__main__":
    unittest.main()
