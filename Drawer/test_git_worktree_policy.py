"""Task worktrees: worktree-add, worktree-land, the land verb and worktree-remove (skill git-flow §2).

Split from test_git_branch_policy.py: Drawer/smoke.py runs each hub test file in its
own process, in parallel, so two files halve the longest one. The fixture, its templates and the
policy module under test come from test_git_branch_policy.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_git_branch_policy as base  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent / "mem"))
import mem  # noqa: E402
from test_git_branch_policy import (  # noqa: E402
    CFG, NAME, NOTES, OWN_SESSION, _copy_template, _snapshot, policy, steer_check,
)

class NoEmbed:
    """An embedder with no key: mem stores and searches on keywords alone."""
    dim = 1536

    def embed(self, texts):
        return None


class TaskWorktreeTests(unittest.TestCase):
    """worktree-add / worktree-remove and publish on a task branch (skill task-worktree, OPS-10)."""

    setUp_policy = base.GitBranchPolicyTests.setUp
    tearDown = base.GitBranchPolicyTests.tearDown
    child_env = base.GitBranchPolicyTests.child_env
    git = base.GitBranchPolicyTests.git
    accepted = base.GitBranchPolicyTests.accepted
    install = base.GitBranchPolicyTests.install
    install_key = base.GitBranchPolicyTests.install_key

    def setUp(self) -> None:
        self.setUp_policy()
        base = Path(self.temporary.name)
        # The seat profile lives under the home folder; keep every profile write in the fixture.
        home = mock.patch.object(Path, "home", return_value=base / "home")
        home.start()
        self.addCleanup(home.stop)
        # A task worktree name carries the session uuid8.
        own = mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": OWN_SESSION})
        own.start()
        self.addCleanup(own.stop)
        self.venv_marker = self.repo / ".venv/marker.txt"
        # The installed hub is the same for every case: build it once, then copy it.
        if not _copy_template("task-worktree", base):
            self.install()
            # The hub ignores its prepared interpreter and bytecode, as the real hub does: a test that imports a
            # tree script writes Drawer/__pycache__, and hub-land refuses untracked work.
            (self.repo / ".gitignore").write_text(".venv/\n__pycache__/\n", encoding="utf-8")
            self.git("add", ".gitignore")
            self.git("commit", "-m", "ignore the interpreter")
            self.git("push", "origin", "HEAD:refs/heads/main")
            self.venv_marker.parent.mkdir()
            self.venv_marker.write_text("hub interpreter\n", encoding="utf-8")
            _snapshot("task-worktree", base)
        # Task worktrees stand at <hub parent>/<hub folder>_wt/<name> (the module contract).
        self.tree = base / "hub_wt" / NAME
        # Each removal logs one line under state_root; keep the log in the fixture.
        self.remove_log = base / "state" / "worktree-remove.log"
        log = mock.patch.object(policy, "REMOVE_LOG", self.remove_log)
        log.start()
        self.addCleanup(log.stop)
        # The steer state (Drawer/steer_check.py) lives under state_root; keep it in the fixture.
        self.steer_dir = base / "state" / "steer"
        steer = mock.patch.object(steer_check, "folder", lambda _cfg: self.steer_dir)
        steer.start()
        self.addCleanup(steer.stop)
        # The claim checks read this fixture mem store, never the live one.
        self.store = mem.Mem(base / "mem.db", NoEmbed())
        self.addCleanup(self.store.db.close)
        store = mock.patch.object(policy, "_open_mem", lambda: self.store)
        store.start()
        self.addCleanup(store.stop)

    def session_row(self, text: str) -> None:
        """A ~/.claude/sessions row of this live test process, as Claude Code writes one per session."""
        rows = Path.home() / ".claude" / "sessions"
        rows.mkdir(parents=True, exist_ok=True)
        (rows / f"{os.getpid()}.json").write_text(text, encoding="utf-8")

    def add(self) -> dict[str, object]:
        with mock.patch.dict(os.environ, self.env, clear=False):
            return self.accepted("worktree-add", policy.worktree_add, self.repo, NAME)

    def commit_in_tree(self) -> str:
        (self.tree / "task.txt").write_text("task work\n", encoding="utf-8")
        self.git("-C", str(self.tree), "add", "task.txt")
        self.git("-C", str(self.tree), "commit", "-m", "task work")
        return self.git("-C", str(self.tree), "rev-parse", "HEAD").stdout.strip()

    def land_on_origin_main(self, tip: str) -> None:
        """Stand-in for the merge and publish steps: accepted main now holds the branch tip."""
        subprocess.run(["git", "--git-dir", str(self.remote), "fetch", str(self.repo), f"{tip}:refs/heads/main"],
                       check=True, capture_output=True, env=self.child_env())

    def remove(self) -> dict[str, object]:
        with mock.patch.dict(os.environ, self.env, clear=False):
            return policy.worktree_remove(self.repo, NAME)

    def test_add_then_remove_deletes_branch_and_tree_and_keeps_the_hub_interpreter(self) -> None:
        added = self.add()
        self.assertEqual(("PASS", "task/0a1b2c3d-route", str(self.tree)),
                         (added.get("result"), added.get("branch"), added.get("path")))
        self.assertEqual("task/0a1b2c3d-route", self.git("-C", str(self.tree), "symbolic-ref", "--short", "HEAD").stdout.strip())
        self.assertEqual("", self.git("config", "--get-regexp", r"^branch\.task/0a1b2c3d-route\.", check=False).stdout)
        self.assertEqual("hub interpreter\n",
                         (self.tree / ".venv/marker.txt").read_text(encoding="utf-8"))
        self.land_on_origin_main(self.commit_in_tree())
        removed = self.remove()
        self.assertEqual(("PASS", "task/0a1b2c3d-route"), (removed.get("result"), removed.get("branch")))
        self.assertFalse(self.tree.exists())
        self.assertFalse(self.tree.parent.exists())  # the last tree gone: no empty <hub>_wt folder is left
        self.assertEqual("", self.git("branch", "--list", "task/0a1b2c3d-route").stdout)
        # The removal reached the worktree only: the main checkout's interpreter is whole.
        self.assertEqual("hub interpreter\n", self.venv_marker.read_text(encoding="utf-8"))

    def test_remove_keeps_the_worktree_parent_while_it_holds_another_entry(self) -> None:
        self.add()
        self.land_on_origin_main(self.commit_in_tree())
        other = self.tree.parent / "other-task"
        other.mkdir()
        self.assertEqual("PASS", self.remove().get("result"))
        self.assertFalse(self.tree.exists())
        self.assertTrue(other.is_dir())

    def test_remove_refuses_a_branch_not_on_origin_main_and_keeps_its_work(self) -> None:
        self.add()
        tip = self.commit_in_tree()
        with self.assertRaisesRegex(policy.PolicyError, re.escape(
                f"task/0a1b2c3d-route tip {tip[:12]} is not on origin/main; merge it and publish first")):
            self.remove()
        self.assertEqual(tip, self.git("rev-parse", "refs/heads/task/0a1b2c3d-route").stdout.strip())
        self.assertEqual("task work\n", (self.tree / "task.txt").read_text(encoding="utf-8"))
        self.assertEqual("hub interpreter\n", self.venv_marker.read_text(encoding="utf-8"))

    def test_remove_takes_a_rebased_landing_that_is_patch_equal_on_origin_main_and_logs_it(self) -> None:
        # A land that rebased the branch onto a newer main leaves accepted main with the same patch
        # under another sha: the branch tip is no ancestor, yet nothing on it is lost.
        self.add()
        tip = self.commit_in_tree()
        (self.repo / "other.txt").write_text("peer work\n", encoding="utf-8")
        self.git("add", "other.txt")
        self.git("commit", "-m", "peer work")
        self.git("cherry-pick", tip)
        self.git("push", "origin", "HEAD:refs/heads/main")
        removed = self.remove()
        self.assertEqual("PASS", removed.get("result"))
        self.assertFalse(self.tree.exists())
        self.assertEqual("", self.git("branch", "--list", "task/0a1b2c3d-route").stdout)
        log = self.remove_log.read_text(encoding="utf-8")
        self.assertRegex(log, rf"worktree-remove branch=task/0a1b2c3d-route path={re.escape(str(self.tree.resolve()))} "
                              rf"tip={tip} basis=patch-equivalent\n$")

    def test_remove_refuses_the_tree_of_a_live_peer_session_and_takes_its_own(self) -> None:
        # Any session could remove a landed tree a live peer still works in.
        self.add()
        tip = self.commit_in_tree()
        self.land_on_origin_main(tip)
        self.session_row(json.dumps({"sessionId": OWN_SESSION, "pid": os.getpid()}))
        peer = {**self.env, "CLAUDE_CODE_SESSION_ID": "ffffffff-0000-4000-8000-000000000000"}
        with mock.patch.dict(os.environ, peer, clear=False), \
                self.assertRaisesRegex(policy.PolicyError, "^0a1b2c3d-route is the worktree of live session 0a1b2c3d"):
            policy.worktree_remove(self.repo, NAME)
        self.assertTrue(self.tree.exists())
        self.assertEqual(tip, self.git("rev-parse", "refs/heads/task/0a1b2c3d-route").stdout.strip())
        self.assertFalse(self.remove_log.exists())
        # Its own session removes it: the live row is the caller's.
        self.assertEqual("PASS", self.remove().get("result"))
        self.assertFalse(self.tree.exists())
        self.assertIn("basis=ancestor", self.remove_log.read_text(encoding="utf-8"))

    def test_remove_reads_a_live_peer_by_its_row_name_and_keeps_the_callers_own_name(self) -> None:
        # A spawned session's row name ends in its uuid8; a /clear can give the same row a new sessionId.
        self.add()
        self.land_on_origin_main(self.commit_in_tree())
        self.session_row(json.dumps({"sessionId": "99999999-0000-4000-8000-000000000009",
                                     "name": "FIXTUREBOX-HARNESS-0a1b2c3d", "pid": os.getpid()}))
        peer = {**self.env, "CLAUDE_CODE_SESSION_ID": "ffffffff-0000-4000-8000-000000000000"}
        with mock.patch.dict(os.environ, peer, clear=False), \
                self.assertRaisesRegex(policy.PolicyError, "is the worktree of live session 0a1b2c3d"):
            policy.worktree_remove(self.repo, NAME)
        self.assertTrue(self.tree.exists())
        # The caller is that row (its sessionId): the old uuid8 in its name is its own.
        own = {**self.env, "CLAUDE_CODE_SESSION_ID": "99999999-0000-4000-8000-000000000009"}
        with mock.patch.dict(os.environ, own, clear=False):
            self.assertEqual("PASS", policy.worktree_remove(self.repo, NAME).get("result"))
        self.assertFalse(self.tree.exists())

    def test_remove_refuses_while_a_live_session_row_is_unreadable(self) -> None:
        # A half-written row of a live pid is no proof the tree is free: remove nothing.
        self.add()
        tip = self.commit_in_tree()
        self.land_on_origin_main(tip)
        self.session_row('{"pid": %d, "sessionId": "0a1b' % os.getpid())
        with self.assertRaisesRegex(policy.PolicyError, f"session row {os.getpid()}.json is unreadable"):
            self.remove()
        self.assertTrue(self.tree.exists())
        self.assertEqual(tip, self.git("rev-parse", "refs/heads/task/0a1b2c3d-route").stdout.strip())

    def test_remove_refuses_uncommitted_work_in_the_tree_and_keeps_it(self) -> None:
        self.add()
        tip = self.commit_in_tree()
        self.land_on_origin_main(tip)
        (self.tree / "unsaved.txt").write_text("unsaved\n", encoding="utf-8")
        with self.assertRaisesRegex(policy.PolicyError, "has uncommitted work; commit or discard it first:\n\\?\\? unsaved.txt$"):
            self.remove()
        self.assertEqual("unsaved\n", (self.tree / "unsaved.txt").read_text(encoding="utf-8"))
        self.assertEqual(tip, self.git("rev-parse", "refs/heads/task/0a1b2c3d-route").stdout.strip())
        self.assertEqual("hub interpreter\n",
                         (self.tree / ".venv/marker.txt").read_text(encoding="utf-8"))

    def test_a_failed_add_rolls_back_branch_and_tree_and_keeps_the_hub_interpreter(self) -> None:
        # Accepted main carries a render that fails, so the add fails after the interpreter link.
        (self.repo / "Drawer/settings-sync.py").write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
        self.git("commit", "-am", "failing render")
        self.land_on_origin_main(self.git("rev-parse", "HEAD").stdout.strip())
        with mock.patch.dict(os.environ, self.env, clear=False), \
                self.assertRaisesRegex(policy.PolicyError, "the worktree MCP render failed"):
            policy.worktree_add(self.repo, NAME)
        self.assertFalse(self.tree.exists())
        self.assertEqual("", self.git("branch", "--list", "task/0a1b2c3d-route").stdout)
        self.assertEqual("hub interpreter\n", self.venv_marker.read_text(encoding="utf-8"))

    def test_add_refuses_a_name_without_the_session_uuid8(self) -> None:
        with mock.patch.dict(os.environ, self.env, clear=False), \
                self.assertRaisesRegex(policy.PolicyError, "^a task worktree name carries your session uuid8 "
                                       "as one '-' part: <uuid8>-<slug>, as 0a1b2c3d-route; or starts with the task8 "
                                       "of a card your session holds a live claim on: <task8>-<slug>$"):
            policy.worktree_add(self.repo, "route")
        self.assertFalse((self.tree.parent / "route").exists())
        self.assertEqual("", self.git("branch", "--list", "task/route").stdout)

    def test_add_refuses_another_sessions_uuid8_and_an_unset_session(self) -> None:
        with mock.patch.dict(os.environ, self.env, clear=False), \
                self.assertRaisesRegex(policy.PolicyError, "carries your session uuid8"):
            policy.worktree_add(self.repo, "ffffffff-route")
        with mock.patch.dict(os.environ, {**self.env, "CLAUDE_CODE_SESSION_ID": ""}, clear=False), \
                self.assertRaisesRegex(policy.PolicyError, "^CLAUDE_CODE_SESSION_ID is not set"):
            policy.worktree_add(self.repo, NAME)
        self.assertEqual("", self.git("branch", "--list", "task/*").stdout)

    def test_publish_on_a_task_branch_names_worktree_land(self) -> None:
        self.add()
        self.commit_in_tree()
        with mock.patch.dict(os.environ, self.env, clear=False):
            result = policy.publish(self.tree)
        self.assertEqual(("blocked", "task_branch"), (result.get("state"), result.get("reason")))
        self.assertEqual("GIT PUBLISH BLOCKED — task/0a1b2c3d-route is never pushed. In the main checkout run "
                         "land --name 0a1b2c3d-route.", result.get("message"))
        self.assertEqual("", self.git("ls-remote", "origin", "refs/heads/task/*").stdout)

    # -- worktree-land: main fast-forwards to task/<name>, uncommitted work carried (OPS-10) ----
    advance_main = base.GitBranchPolicyTests.advance_main

    def seed_notes(self) -> None:
        """notes.txt on accepted main before the branch, so branch and checkout both change it."""
        (self.repo / "notes.txt").write_text(NOTES, encoding="utf-8")
        self.git("add", "notes.txt")
        self.git("commit", "-m", "notes")
        self.git("push", "origin", "HEAD:refs/heads/main")

    def commit_notes_in_tree(self, text: str) -> str:
        (self.tree / "notes.txt").write_text(text, encoding="utf-8")
        self.git("-C", str(self.tree), "commit", "-am", "task notes")
        return self.git("-C", str(self.tree), "rev-parse", "HEAD").stdout.strip()

    def land(self) -> dict[str, object]:
        with mock.patch.dict(os.environ, self.env, clear=False):
            return policy.worktree_land(self.repo, NAME)

    def head(self) -> str:
        return self.git("rev-parse", "HEAD").stdout.strip()

    def test_land_keeps_staged_and_unstaged_work_on_a_path_the_branch_also_changes(self) -> None:
        self.seed_notes()
        self.add()
        tip = self.commit_notes_in_tree(NOTES.replace("one", "ONE"))
        # Another session staged line three and left line six unstaged on top of it.
        staged = NOTES.replace("three", "THREE")
        (self.repo / "notes.txt").write_text(staged, encoding="utf-8")
        self.git("add", "notes.txt")
        (self.repo / "notes.txt").write_text(staged.replace("six", "SIX"), encoding="utf-8")
        result = self.land()
        self.assertEqual(("landed", tip), (result.get("state"), result.get("head_after")))
        self.assertEqual(tip, self.head())
        self.assertEqual(NOTES.replace("one", "ONE").replace("three", "THREE"),
                         self.git("show", ":notes.txt").stdout)
        self.assertEqual(NOTES.replace("one", "ONE").replace("three", "THREE").replace("six", "SIX"),
                         (self.repo / "notes.txt").read_text(encoding="utf-8"))

    def test_land_blocks_a_true_conflict_with_uncommitted_work_and_moves_nothing(self) -> None:
        self.seed_notes()
        self.add()
        tip = self.commit_notes_in_tree(NOTES.replace("one", "branch one"))
        before = self.head()
        (self.repo / "notes.txt").write_text(NOTES.replace("one", "local one"), encoding="utf-8")
        result = self.land()
        self.assertEqual(("blocked", "carry_conflict", before),
                         (result.get("state"), result.get("reason"), result.get("head_after")))
        self.assertEqual(
            f"GIT LAND BLOCKED — uncommitted edits in notes.txt conflict with task/0a1b2c3d-route; nothing moved, "
            f"main stays at {before[:12]}. Land again once that work is committed or reverted. Never stash, "
            "reset or check out another session's work.", result.get("message"))
        self.assertEqual(before, self.head())
        self.assertEqual(tip, self.git("rev-parse", "refs/heads/task/0a1b2c3d-route").stdout.strip())
        self.assertEqual(NOTES.replace("one", "local one"),
                         (self.repo / "notes.txt").read_text(encoding="utf-8"))
        self.assertEqual(NOTES, self.git("show", ":notes.txt").stdout)
        self.assertFalse((self.repo / "task.txt").exists())

    def test_land_failure_inside_the_carry_moves_nothing(self) -> None:
        self.add()
        self.commit_in_tree()
        (self.repo / "fixture.txt").write_text("unsaved\n", encoding="utf-8")
        before = self.head()
        git = policy._git

        def broken_merge_tree(repo, *arguments, **keywords):
            if arguments[0] == "merge-tree":
                raise policy.PolicyError("simulated merge-tree failure")
            return git(repo, *arguments, **keywords)

        with mock.patch.object(policy, "_git", broken_merge_tree):
            result = self.land()
        self.assertEqual(("blocked", "carry_failed", before),
                         (result.get("state"), result.get("reason"), result.get("head_after")))
        self.assertEqual("GIT LAND BLOCKED — carrying uncommitted work onto task/0a1b2c3d-route failed: "
                         "simulated merge-tree failure. Nothing moved; land again.", result.get("message"))
        self.assertEqual(before, self.head())
        self.assertEqual("unsaved\n", (self.repo / "fixture.txt").read_text(encoding="utf-8"))
        self.assertFalse((self.repo / "task.txt").exists())
        self.assertFalse((self.repo / ".git/hub-land.index").exists())

    def test_land_makes_no_commit_and_runs_no_hook(self) -> None:
        self.add()
        tip = self.commit_in_tree()
        (self.repo / "fixture.txt").write_text("unsaved\n", encoding="utf-8")
        self.env["REFRESH_LOG"] = str(Path(self.temporary.name) / "refresh.log")
        result = self.land()
        self.assertEqual(("landed", tip, True),
                         (result.get("state"), result.get("head_after"), result.get("renders")))
        self.assertEqual(tip, self.head())
        self.assertEqual(" M fixture.txt\n", self.git("status", "--porcelain", "--untracked-files=normal").stdout)
        self.assertEqual("unsaved\n", (self.repo / "fixture.txt").read_text(encoding="utf-8"))
        # One render, the land's own: no Git hook renders.
        self.assertEqual("--mcp\n", Path(self.env["REFRESH_LOG"]).read_text(encoding="utf-8"))

    def test_land_refuses_a_red_probe_or_tracked_edits_at_the_tip_and_moves_nothing(self) -> None:
        # worktree_land: the probe runs in the branch's worktree at the tip; red, or a tracked edit past the
        # tip, blocks the land and moves nothing (a hub commit landed on a red probe).
        self.add()
        tip = self.commit_in_tree()
        before = self.head()
        self.env["PROBE_EXIT"] = "1"
        red = self.land()
        self.env["PROBE_EXIT"] = "0"
        (self.tree / "task.txt").write_text("past the tip\n", encoding="utf-8")
        dirty = self.land()
        self.assertEqual([("blocked", "probe_red"), ("blocked", "worktree_dirty")],
                         [(r.get("state"), r.get("reason")) for r in (red, dirty)])
        self.assertIn("FAIL replay_x: fixture red", red.get("message"))
        self.assertEqual(before, self.head())
        self.git("-C", str(self.tree), "checkout", "--", "task.txt")
        self.assertEqual(("landed", tip), (self.land().get("state"), self.head()))

    def test_land_refuses_a_branch_that_lacks_accepted_main(self) -> None:
        self.add()
        tip = self.commit_in_tree()
        accepted = self.advance_main()
        result = self.land()
        self.assertEqual(("blocked", "branch_behind_main"), (result.get("state"), result.get("reason")))
        self.assertEqual("GIT LAND BLOCKED — task/0a1b2c3d-route lacks accepted main. In its worktree run "
                         "freshness --source resume, then land again; nothing moved.", result.get("message"))
        self.assertEqual(accepted, self.head())
        self.assertEqual(tip, self.git("rev-parse", "refs/heads/task/0a1b2c3d-route").stdout.strip())
        self.assertFalse((self.repo / "task.txt").exists())

    def test_land_refuses_when_main_holds_commits_the_branch_lacks(self) -> None:
        self.add()
        self.commit_in_tree()
        local = self.git("commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "main only").stdout.strip()
        self.git("update-ref", "refs/heads/main", local)
        result = self.land()
        self.assertEqual(("blocked", "not_fast_forward", local),
                         (result.get("state"), result.get("reason"), result.get("head_after")))
        self.assertEqual("GIT LAND BLOCKED — main holds commits task/0a1b2c3d-route lacks; publish main, rebase "
                         "the branch with freshness in its worktree, then land again.", result.get("message"))
        self.assertEqual(local, self.head())
        self.assertFalse((self.repo / "task.txt").exists())

    # -- land: one call, the tip to origin/main through pushretry, then worktree-land ----
    def land_verb(self) -> dict[str, object]:
        with mock.patch.dict(os.environ, self.env, clear=False):
            return policy.land(self.repo, NAME)

    def origin_main(self) -> str:
        return self.git("ls-remote", "origin", "refs/heads/main").stdout.split()[0]

    def on_origin(self, path: str) -> bool:
        return bool(self.git("ls-tree", "--name-only", self.origin_main(), "--", path).stdout.strip())

    def peer_pushes(self, rounds: int) -> list[str]:
        """Patch _push so a peer pushes to origin/main right before each of the first `rounds` pushes."""
        real, peers = policy._push, []

        def racing(repo, push, timeout):
            if len(peers) < rounds:
                peers.append(self.advance_main(f"peer {len(peers)}\n", path=f"peer{len(peers)}.txt"))
            return real(repo, push, timeout)

        patch = mock.patch.object(policy, "_push", racing)
        patch.start()
        self.addCleanup(patch.stop)
        return peers

    def test_land_verb_pushes_the_tip_reads_it_back_and_moves_the_main_checkout(self) -> None:
        self.add()
        tip = self.commit_in_tree()
        result = self.land_verb()
        self.assertEqual(("landed", tip, 1, True, "refs/heads/main"),
                         tuple(result.get(k) for k in ("state", "tip", "attempts", "pushed", "pushed_ref")))
        self.assertEqual(tip, self.origin_main())
        self.assertEqual(tip, self.head())
        self.assertTrue(result.get("renders"))
        self.assertEqual("PASS", self.remove()["result"])

    def test_land_verb_rebases_and_pushes_again_when_a_peer_pushes_mid_land(self) -> None:
        self.add()
        first = self.commit_in_tree()
        peers = self.peer_pushes(1)
        result = self.land_verb()
        self.assertEqual(("landed", 2), (result.get("state"), result.get("attempts")))
        tip = str(result.get("tip"))
        self.assertNotEqual(first, tip)
        self.assertEqual(tip, self.origin_main())
        self.assertEqual(peers[0], self.git("rev-parse", f"{tip}^").stdout.strip())
        self.assertEqual(tip, self.head())
        self.assertTrue((self.repo / "task.txt").exists() and (self.repo / "peer0.txt").exists())

    def test_land_verb_gives_up_after_three_rounds_of_origin_moving_and_lands_nothing(self) -> None:
        self.add()
        self.commit_in_tree()
        before = self.head()
        peers = self.peer_pushes(3)
        result = self.land_verb()
        self.assertEqual(("blocked", "origin_moved", 3, False),
                         tuple(result.get(k) for k in ("state", "reason", "attempts", "pushed")))
        self.assertEqual("GIT LAND BLOCKED — origin/main moved under the push 3 times; nothing landed. Land again.",
                         result.get("message"))
        self.assertEqual(peers[-1], self.origin_main())
        self.assertFalse(self.on_origin("task.txt"))
        self.assertEqual(before, self.head())

    def test_land_verb_with_an_untested_tip_pushes_nothing(self) -> None:
        # hub-land passes the tip its smoke judged; a rebase to another tip is tip_moved.
        self.add()
        tip = self.commit_in_tree()
        origin = self.origin_main()
        with mock.patch.dict(os.environ, self.env, clear=False):
            result = policy.land(self.repo, NAME, expect_tip="0" * 40)
        self.assertEqual(("blocked", "tip_moved", False, tip),
                         tuple(result.get(k) for k in ("state", "reason", "pushed", "tip")))
        self.assertEqual(origin, self.origin_main())
        with mock.patch.dict(os.environ, self.env, clear=False):
            self.assertEqual("landed", policy.land(self.repo, NAME, expect_tip=tip).get("state"))

    def test_land_verb_on_a_red_probe_pushes_nothing(self) -> None:
        self.add()
        self.commit_in_tree()
        before, origin = self.head(), self.origin_main()
        self.env["PROBE_EXIT"] = "1"
        result = self.land_verb()
        self.assertEqual(("blocked", "probe_red", False),
                         tuple(result.get(k) for k in ("state", "reason", "pushed")))
        self.assertIn("FAIL replay_x: fixture red", result.get("message"))
        self.assertTrue(str(result.get("message")).endswith("; nothing was pushed."))
        self.assertEqual((origin, before), (self.origin_main(), self.head()))

    def test_land_verb_under_a_steer_stop_pushes_nothing_exits_75_and_lands_after_go(self) -> None:
        self.add()
        tip = self.commit_in_tree()
        before, origin = self.head(), self.origin_main()
        steer_check.stop(CFG, OWN_SESSION, "hold for review", step="push")
        out = io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=False), contextlib.redirect_stdout(out):
            code = policy.main(["land", "--repo", str(self.repo), "--name", NAME])
        result = json.loads(out.getvalue())
        self.assertEqual((75, "need", "steer", "push", False),
                         (code, result.get("state"), result.get("reason"), result.get("step"), result.get("pushed")))
        self.assertIn("Flight holds a STOP for 0a1b2c3d at push: hold for review", result.get("why"))
        self.assertEqual((origin, before), (self.origin_main(), self.head()))
        self.assertEqual(["push"], [row.get("step") for row in steer_check.pauses(CFG)])
        steer_check.go(CFG, OWN_SESSION)
        self.assertEqual(("landed", tip), (self.land_verb().get("state"), self.origin_main()))

    def test_land_verb_stops_when_no_session_names_the_steer_subject(self) -> None:
        self.add()
        self.commit_in_tree()
        origin = self.origin_main()
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}), \
                mock.patch.object(steer_check, "uuid8", lambda _name: "route"):
            result = self.land_verb()
        self.assertEqual(("need", "steer"), (result.get("state"), result.get("reason")))
        self.assertIn("no session for the steer check", result.get("why"))
        self.assertEqual(origin, self.origin_main())

    def test_land_verb_names_a_refused_push_and_lands_nothing(self) -> None:
        self.add()
        self.commit_in_tree()
        before, origin = self.head(), self.origin_main()
        hook = self.remote / "hooks" / "pre-receive"
        hook.parent.mkdir(exist_ok=True)  # the fixture remote has no template hooks folder
        hook.write_text("#!/bin/sh\necho 'origin refuses this push' >&2\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        result = self.land_verb()
        self.assertEqual(("blocked", "push_failed", 1, False),
                         tuple(result.get(k) for k in ("state", "reason", "attempts", "pushed")))
        self.assertIn("origin refuses this push", result.get("message"))
        self.assertEqual((origin, before), (self.origin_main(), self.head()))

    def test_land_verb_reports_a_hook_refusal_in_the_first_round_while_origin_moves(self) -> None:
        # A review finding: a hook refusal beside a peer push read as an origin move for 3 rounds.
        self.add()
        self.commit_in_tree()
        before = self.head()
        hook = self.remote / "hooks" / "pre-receive"
        hook.parent.mkdir(exist_ok=True)  # the fixture remote has no template hooks folder
        hook.write_text("#!/bin/sh\nwhile read old new ref; do\n  if git cat-file -e \"$new:task.txt\" 2>/dev/null; then\n"
                        "    echo 'origin refuses this push' >&2; exit 1\n  fi\ndone\nexit 0\n", encoding="utf-8")
        hook.chmod(0o755)
        real, peers = policy._push, []

        def refused_then_peer(repo, push, timeout):
            # The hook refuses, then a peer push lands before land fetches: origin moved beside the refusal.
            answer = real(repo, push, timeout)
            peers.append(self.advance_main(f"peer {len(peers)}\n", path=f"peer{len(peers)}.txt"))
            return answer

        with mock.patch.object(policy, "_push", refused_then_peer):
            result = self.land_verb()
        self.assertEqual(("blocked", "push_failed", 1, False),
                         tuple(result.get(k) for k in ("state", "reason", "attempts", "pushed")))
        self.assertIn("origin refuses this push", result.get("message"))
        self.assertEqual((1, peers[0]), (len(peers), self.origin_main()))
        self.assertEqual(before, self.head())

    def test_land_verb_failed_push_and_failed_fetch_is_landing_unknown_not_nothing_landed(self) -> None:
        # A review finding: the push may have reached origin, so 'Nothing landed' is not known.
        self.add()
        tip = self.commit_in_tree()
        real_push, real_fetch, pushed = policy._push, policy._fetch_main, []

        def push(repo, args, timeout):
            pushed.append(real_push(repo, args, timeout))
            return subprocess.CompletedProcess(args, 124, stdout="", stderr="the push exceeded its bounded timeout")

        def fetch(repo, timeout):
            if pushed:
                raise policy.GitTimeout("fetch exceeded its bounded timeout", quiesced=True)
            return real_fetch(repo, timeout)

        with mock.patch.object(policy, "_push", push), mock.patch.object(policy, "_fetch_main", fetch):
            result = self.land_verb()
        self.assertEqual(("partial", "landing_unknown", None, 1),
                         tuple(result.get(k) for k in ("state", "reason", "pushed", "attempts")))
        message = str(result.get("message"))
        self.assertIn("landing state unknown; land again", message)
        self.assertNotIn("Nothing landed", message)
        self.assertEqual(tip, self.origin_main())
        self.assertEqual(("landed", tip), (self.land_verb().get("state"), self.head()))

    def test_land_verb_pushes_the_probed_sha_when_the_branch_moves_during_the_land(self) -> None:
        # A review finding: the push named the branch, so a commit made after the probe went out.
        self.add()
        tip = self.commit_in_tree()
        real = policy._push

        def late_commit(repo, push, timeout):
            (self.tree / "late.txt").write_text("unprobed\n", encoding="utf-8")
            self.git("-C", str(self.tree), "add", "late.txt")
            self.git("-C", str(self.tree), "commit", "-m", "after the probe")
            return real(repo, push, timeout)

        with mock.patch.object(policy, "_push", late_commit):
            result = self.land_verb()
        self.assertEqual(("partial", "branch_moved", True),
                         tuple(result.get(k) for k in ("state", "reason", "pushed")))
        self.assertEqual(tip, self.origin_main())
        self.assertFalse(self.on_origin("late.txt"))
        self.assertNotEqual(tip, self.head())

    def test_land_verb_takes_a_push_that_landed_but_lost_its_answer(self) -> None:
        self.add()
        tip = self.commit_in_tree()
        real = policy._push

        def lost_answer(repo, push, timeout):
            real(repo, push, timeout)
            return subprocess.CompletedProcess(push, 124, stdout="", stderr="the push exceeded its bounded timeout")

        with mock.patch.object(policy, "_push", lost_answer):
            result = self.land_verb()
        self.assertEqual(("landed", 1, True), tuple(result.get(k) for k in ("state", "attempts", "pushed")))
        self.assertEqual((tip, tip), (self.origin_main(), self.head()))

    def test_land_verb_readback_timeout_after_the_push_is_partial_not_a_crash(self) -> None:
        self.add()
        tip = self.commit_in_tree()
        real_push, real_fetch, pushed = policy._push, policy._fetch_main, []

        def push(repo, args, timeout):
            pushed.append(args)
            return real_push(repo, args, timeout)

        def fetch(repo, timeout):
            if pushed:
                raise policy.GitTimeout("fetch exceeded its bounded timeout", quiesced=True)
            return real_fetch(repo, timeout)

        with mock.patch.object(policy, "_push", push), mock.patch.object(policy, "_fetch_main", fetch):
            result = self.land_verb()
        self.assertEqual(("partial", "readback_failed"), (result.get("state"), result.get("reason")))
        self.assertIn("fetch exceeded its bounded timeout", result.get("message"))
        self.assertEqual(tip, self.origin_main())

    def test_land_verb_refuses_before_the_push_when_main_holds_commits_the_branch_lacks(self) -> None:
        self.add()
        self.commit_in_tree()
        origin = self.origin_main()
        local = self.git("commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "main only").stdout.strip()
        self.git("update-ref", "refs/heads/main", local)
        result = self.land_verb()
        self.assertEqual(("blocked", "not_fast_forward", False),
                         tuple(result.get(k) for k in ("state", "reason", "pushed")))
        self.assertEqual((origin, local), (self.origin_main(), self.head()))

    def test_land_verb_carry_conflict_after_the_push_is_partial_and_a_rerun_lands(self) -> None:
        self.seed_notes()
        self.add()
        tip = self.commit_notes_in_tree(NOTES.replace("one", "branch one"))
        before = self.head()
        (self.repo / "notes.txt").write_text(NOTES.replace("one", "local one"), encoding="utf-8")
        result = self.land_verb()
        self.assertEqual(("partial", "carry_conflict", True),
                         tuple(result.get(k) for k in ("state", "reason", "pushed")))
        self.assertTrue(str(result.get("message")).startswith(
            f"GIT LAND PARTIAL — origin/main holds task/0a1b2c3d-route tip {tip[:12]}, but the main checkout"))
        self.assertEqual((tip, before), (self.origin_main(), self.head()))
        self.assertEqual(NOTES.replace("one", "local one"), (self.repo / "notes.txt").read_text(encoding="utf-8"))
        self.git("checkout", "--", "notes.txt")
        self.assertEqual(("landed", tip), (self.land_verb().get("state"), self.head()))

    def race(self, after: tuple[str, ...], peer) -> tuple[dict[str, object], str]:
        """Run the land with peer() fired once, right after the first _git call starting with `after`."""
        self.add()
        self.commit_in_tree()
        (self.repo / "fixture.txt").write_text("unsaved\n", encoding="utf-8")
        before = self.head()
        git, fired = policy._git, []

        def racing(repo, *arguments, **keywords):
            result = git(repo, *arguments, **keywords)
            if not fired and arguments[:len(after)] == after:
                fired.append(peer())
            return result

        with mock.patch.object(policy, "_git", racing):
            result = self.land()
        self.assertEqual(1, len(fired))
        return result, before

    def test_land_moves_back_when_a_peer_commit_lands_during_the_move(self) -> None:
        def peer_commit() -> str:
            peer = self.git("commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "peer").stdout.strip()
            self.git("update-ref", "refs/heads/main", peer)
            return peer

        result, _ = self.race(("read-tree", "-m", "-u"), peer_commit)
        peer = self.git("rev-parse", "refs/heads/main").stdout.strip()
        self.assertEqual(("blocked", "peer_commit_race", peer),
                         (result.get("state"), result.get("reason"), result.get("head_after")))
        self.assertEqual("GIT LAND BLOCKED — a commit landed on main while the land ran. The worktree was "
                         "moved back; land again.", result.get("message"))
        self.assertEqual("peer", self.git("log", "-1", "--format=%s").stdout.strip())
        self.assertFalse((self.repo / "task.txt").exists())
        self.assertEqual("unsaved\n", (self.repo / "fixture.txt").read_text(encoding="utf-8"))

    def test_land_moves_back_when_a_peer_stages_work_during_the_move(self) -> None:
        def peer_stages() -> None:
            (self.repo / "peer.txt").write_text("peer staged\n", encoding="utf-8")
            self.git("add", "peer.txt")

        result, before = self.race(("update-ref", "-m"), peer_stages)
        self.assertEqual(("blocked", "peer_index_race", before),
                         (result.get("state"), result.get("reason"), result.get("head_after")))
        self.assertEqual("GIT LAND BLOCKED — another session staged work while the land ran. HEAD and the "
                         "worktree were moved back; land again.", result.get("message"))
        self.assertEqual(before, self.head())
        self.assertFalse((self.repo / "task.txt").exists())
        self.assertEqual("unsaved\n", (self.repo / "fixture.txt").read_text(encoding="utf-8"))
        self.assertEqual("peer staged\n", self.git("show", ":peer.txt").stdout)



PEER_SESSION = "ffffffff-0000-4000-8000-000000000000"


class OwnCleanupTests(unittest.TestCase):
    """own-cleanup and its SessionEnd hook: own landed hub trees and own temp folders go;
    unlanded, peer and product session/ trees stay."""

    setUp_tree = TaskWorktreeTests.setUp
    setUp_policy = TaskWorktreeTests.setUp_policy
    tearDown = TaskWorktreeTests.tearDown
    child_env = TaskWorktreeTests.child_env
    git = TaskWorktreeTests.git
    accepted = TaskWorktreeTests.accepted
    install = TaskWorktreeTests.install
    install_key = TaskWorktreeTests.install_key
    add = TaskWorktreeTests.add
    commit_in_tree = TaskWorktreeTests.commit_in_tree
    land_on_origin_main = TaskWorktreeTests.land_on_origin_main

    def setUp(self) -> None:
        self.setUp_tree()
        base = Path(self.temporary.name)
        self.tmp = base / "tmp"
        self.tmp.mkdir()
        self.trashed: list[Path] = []
        self.real_trash = policy._trash
        trash_bin = base / "trash"
        trash_bin.mkdir()

        def fake_trash(path: Path) -> None:
            self.trashed.append(path)
            path.rename(trash_bin / path.name)

        for name, value in (("_temp_roots", lambda: [self.tmp]), ("_trash", fake_trash),
                            ("OWN_CLEANUP_LOG", base / "state" / "own-cleanup.log")):
            patcher = mock.patch.object(policy, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.log = base / "state" / "own-cleanup.log"

    def test_own_landed_goes_and_unlanded_peer_and_product_session_trees_stay(self) -> None:
        base = Path(self.temporary.name)
        self.add()
        self.land_on_origin_main(self.commit_in_tree())
        # An own tree with work not on origin/main.
        wip = base / "hub_wt" / "0a1b2c3d-wip"
        with mock.patch.dict(os.environ, self.env, clear=False):
            policy.worktree_add(self.repo, "0a1b2c3d-wip")
        (wip / "wip.txt").write_text("unlanded\n", encoding="utf-8")
        self.git("-C", str(wip), "add", "wip.txt")
        self.git("-C", str(wip), "commit", "-m", "unlanded")
        # A peer's tree, landed (it holds nothing past origin/main).
        peer = base / "hub_wt" / "ffffffff-peer"
        self.git("worktree", "add", "--no-track", "-b", "task/ffffffff-peer", str(peer), "refs/remotes/origin/main")
        # Temp: an own folder, a peer folder, an own file and a product session/<uuid8> tree.
        (self.tmp / "0a1b2c3d-scratch").mkdir()
        (self.tmp / "0a1b2c3d-scratch" / "x.txt").write_text("scratch\n", encoding="utf-8")
        (self.tmp / "ffffffff-scratch").mkdir()
        (self.tmp / "0a1b2c3d.txt").write_text("own file\n", encoding="utf-8")
        product = base / "product"
        product.mkdir()
        for argv in (["init", "-q", "-b", "main"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit",
                                                    "-q", "--allow-empty", "-m", "product"]):
            subprocess.run(["git", "-C", str(product), *argv], check=True, capture_output=True, env=self.child_env())
        # A scratch clone (a .git folder) may hold unpushed work: it stays too.
        (self.tmp / "0a1b2c3d-clone" / ".git").mkdir(parents=True)
        product_tree = self.tmp / "0a1b2c3d"
        subprocess.run(["git", "-C", str(product), "worktree", "add", "-q", "-b", "session/0a1b2c3d", str(product_tree)],
                       check=True, capture_output=True, env=self.child_env())

        with mock.patch.dict(os.environ, self.env, clear=False):
            result = policy.own_cleanup(self.repo, OWN_SESSION)

        self.assertEqual(["task/0a1b2c3d-route", str(self.tmp / "0a1b2c3d-scratch")], result["removed"])
        self.assertEqual(("LEFTOVER", 1), (result["result"], result["leftover"]))
        self.assertEqual("task/0a1b2c3d-wip", result["kept"][0]["item"])
        self.assertIn("is not on origin/main", result["kept"][0]["reason"])
        self.assertEqual([str(product_tree), str(self.tmp / "0a1b2c3d-clone")], [i["item"] for i in result["left"]])
        self.assertTrue((self.tmp / "0a1b2c3d-clone" / ".git").is_dir())
        self.assertFalse(self.tree.exists())
        self.assertEqual("", self.git("branch", "--list", "task/0a1b2c3d-route").stdout)
        self.assertEqual("unlanded\n", (wip / "wip.txt").read_text(encoding="utf-8"))
        self.assertNotEqual("", self.git("branch", "--list", "task/0a1b2c3d-wip").stdout)
        self.assertTrue(peer.is_dir())
        self.assertNotEqual("", self.git("branch", "--list", "task/ffffffff-peer").stdout)
        self.assertEqual([self.tmp / "0a1b2c3d-scratch"], self.trashed)
        self.assertTrue((self.tmp / "ffffffff-scratch").is_dir())
        self.assertTrue((self.tmp / "0a1b2c3d.txt").is_file())
        self.assertTrue((product_tree / ".git").is_file())
        self.assertIn("session/0a1b2c3d", subprocess.run(
            ["git", "-C", str(product), "branch", "--list", "session/*"], capture_output=True, text=True).stdout)
        self.assertEqual("hub interpreter\n", self.venv_marker.read_text(encoding="utf-8"))

    def run_hook(self, session_env: str, payload: str) -> int:
        env = {**self.env, "CLAUDE_CODE_SESSION_ID": session_env}
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(sys, "stdin", io.StringIO(payload)), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = policy.main(["own-cleanup", "--session-end", "--repo", str(self.repo)])
        self.assertEqual("", out.getvalue())
        return code

    def test_session_end_hook_cleans_the_payload_session_logs_one_line_and_refuses_a_forged_id(self) -> None:
        self.add()
        self.land_on_origin_main(self.commit_in_tree())
        payload = json.dumps({"session_id": OWN_SESSION, "hook_event_name": "SessionEnd", "reason": "exit"})
        # A shell of another session that sends this payload is a forged call: nothing goes, the close goes on.
        self.assertEqual(0, self.run_hook(PEER_SESSION, payload))
        self.assertTrue(self.tree.exists())
        self.assertRegex(self.log.read_text(encoding="utf-8"),
                         r"^\S+ own-cleanup session=\? REFUSED the payload session id is not this environment's")
        # A payload with no session id removes nothing either.
        self.assertEqual(0, self.run_hook("", "{}"))
        self.assertTrue(self.tree.exists())
        self.assertEqual(0, self.run_hook(OWN_SESSION, payload))
        self.assertFalse(self.tree.exists())
        lines = self.log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(3, len(lines))
        self.assertIn("REFUSED own-cleanup needs the session id", lines[1])
        self.assertRegex(lines[2], r"own-cleanup session=0a1b2c3d PASS removed=\['task/0a1b2c3d-route'\] "
                                   r"leftover=0 kept=\[\] left=\[\]$")

    def test_the_closing_sessions_own_live_row_does_not_block_its_trees_with_no_session_variable(self) -> None:
        # At SessionEnd the closing session's row still names a live pid; a hook shell may carry no
        # CLAUDE_CODE_SESSION_ID, so the payload id must reach the live check as the caller's own.
        self.add()
        self.land_on_origin_main(self.commit_in_tree())
        rows = Path.home() / ".claude" / "sessions"
        rows.mkdir(parents=True, exist_ok=True)
        (rows / f"{os.getpid()}.json").write_text(json.dumps({"sessionId": OWN_SESSION, "pid": os.getpid()}),
                                                 encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
        with mock.patch.dict(os.environ, env, clear=True):
            result = policy.own_cleanup(self.repo, OWN_SESSION)
        self.assertEqual(["task/0a1b2c3d-route"], result["removed"])
        self.assertFalse(self.tree.exists())

    def test_session_end_on_clear_or_resume_removes_nothing(self) -> None:
        self.add()
        self.land_on_origin_main(self.commit_in_tree())
        (self.tmp / "0a1b2c3d-scratch").mkdir()
        for reason in ("clear", "resume"):
            self.assertEqual(0, self.run_hook(OWN_SESSION, json.dumps({"session_id": OWN_SESSION, "reason": reason})))
        self.assertTrue(self.tree.exists())
        self.assertTrue((self.tmp / "0a1b2c3d-scratch").is_dir())
        self.assertEqual(["SKIPPED reason=clear", "SKIPPED reason=resume"],
                         re.findall(r"SKIPPED reason=\w+", self.log.read_text(encoding="utf-8")))

    def test_session_end_hook_exits_0_on_a_bad_repo_or_bytes_and_logs_why(self) -> None:
        payload = json.dumps({"session_id": OWN_SESSION, "reason": "exit"})
        env = {**self.env, "CLAUDE_CODE_SESSION_ID": OWN_SESSION}
        stdin = mock.Mock(buffer=io.BytesIO(b"\xff\xfe not json"))
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(sys, "stdin", stdin):
            self.assertEqual(0, policy.main(["own-cleanup", "--session-end", "--repo", str(self.repo)]))
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(sys, "stdin", io.StringIO(payload)):
            self.assertEqual(0, policy.main(["own-cleanup", "--session-end", "--repo", str(self.tmp)]))
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(sys, "stdin", None):
            self.assertEqual(0, policy.main(["own-cleanup", "--session-end", "--repo", str(self.repo)]))
        lines = self.log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(3, len(lines))
        self.assertIn("REFUSED JSONDecodeError", lines[0])
        self.assertIn("session=? REFUSED", lines[1])
        self.assertIn("REFUSED the payload session id is not this environment's", lines[2])

    def test_past_the_time_budget_every_own_item_is_kept(self) -> None:
        self.add()
        self.land_on_origin_main(self.commit_in_tree())
        (self.tmp / "0a1b2c3d-scratch").mkdir()
        with mock.patch.object(policy, "CLEANUP_BUDGET_SECONDS", -1), \
                mock.patch.dict(os.environ, self.env, clear=False):
            result = policy.own_cleanup(self.repo, OWN_SESSION)
        self.assertEqual([], result["removed"])
        self.assertEqual({"out of the own-cleanup time budget"}, {k["reason"] for k in result["kept"]})
        self.assertTrue(self.tree.exists())
        self.assertTrue((self.tmp / "0a1b2c3d-scratch").is_dir())

    def test_trash_with_no_trash_command_keeps_the_folder(self) -> None:
        folder = self.tmp / "0a1b2c3d-keep"
        folder.mkdir()
        with mock.patch.object(policy.shutil, "which", return_value=None), \
                mock.patch.object(policy.os, "access", return_value=False), \
                self.assertRaisesRegex(policy.PolicyError, "no trash command"):
            self.real_trash(folder)
        self.assertTrue(folder.is_dir())


class ClaimTreeTests(unittest.TestCase):
    """<task8>-* hub trees: legs share one session id, so a live mem claim names the tree."""

    setUp = TaskWorktreeTests.setUp
    setUp_policy = TaskWorktreeTests.setUp_policy
    tearDown = TaskWorktreeTests.tearDown
    child_env = TaskWorktreeTests.child_env
    git = TaskWorktreeTests.git
    accepted = TaskWorktreeTests.accepted
    install = TaskWorktreeTests.install
    install_key = TaskWorktreeTests.install_key
    session_row = TaskWorktreeTests.session_row
    land_on_origin_main = TaskWorktreeTests.land_on_origin_main

    def card(self, session: str | None = OWN_SESSION) -> str:
        """A task card's task8, claimed by session (None: no claim)."""
        row = self.store.add("task", "Claim tree fixture: a task tree. Why: a test. Done when: it passes.",
                             new=True, status="todo")
        if session:
            self.store.claim(row["id"], session)
        return row["id"][:8]

    def live(self) -> None:
        """This test process is the live session OWN_SESSION."""
        self.session_row(json.dumps({"sessionId": OWN_SESSION, "pid": os.getpid()}))

    def add_landed(self, name: str) -> Path:
        """Add the tree name, commit one file there and land it on origin/main."""
        with mock.patch.dict(os.environ, self.env, clear=False):
            path = Path(policy.worktree_add(self.repo, name)["path"])
        (path / f"{name}.txt").write_text("task work\n", encoding="utf-8")
        self.git("-C", str(path), "add", f"{name}.txt")
        self.git("-C", str(path), "commit", "-m", f"work {name}")
        self.land_on_origin_main(self.git("-C", str(path), "rev-parse", "HEAD").stdout.strip())
        return path

    def test_add_accepts_the_task8_of_a_card_this_session_holds_a_live_claim_on(self) -> None:
        self.live()
        task8 = self.card()
        with mock.patch.dict(os.environ, self.env, clear=False):
            added = policy.worktree_add(self.repo, f"{task8}-fix")
        self.assertEqual(("PASS", f"task/{task8}-fix"), (added["result"], added["branch"]))
        self.assertTrue(Path(added["path"]).is_dir())

    def test_add_refuses_a_task8_with_no_claim_a_peer_claim_or_a_dead_claim(self) -> None:
        unclaimed, peer, dead = self.card(None), self.card(PEER_SESSION), self.card()
        for task8 in (unclaimed, peer, dead):  # no session row yet: OWN_SESSION's claim is dead
            with self.subTest(task8=task8), mock.patch.dict(os.environ, self.env, clear=False), \
                    self.assertRaisesRegex(policy.PolicyError, "or starts with the task8 of a card your session "
                                                               "holds a live claim on"):
                policy.worktree_add(self.repo, f"{task8}-fix")
        self.assertEqual("", self.git("branch", "--list", "task/*").stdout)

    def test_remove_refuses_a_task8_tree_while_its_claim_is_live_then_takes_it(self) -> None:
        self.live()
        task8 = self.card()
        path = self.add_landed(f"{task8}-fix")
        with mock.patch.dict(os.environ, self.env, clear=False), \
                self.assertRaisesRegex(policy.PolicyError, rf"^{task8}-fix: task {task8} claim \w{{8}} is live"):
            policy.worktree_remove(self.repo, f"{task8}-fix")
        self.assertTrue(path.is_dir())
        self.store.release(task8, self.store.get(task8)["claim_id"])
        with mock.patch.dict(os.environ, self.env, clear=False):
            self.assertEqual("PASS", policy.worktree_remove(self.repo, f"{task8}-fix")["result"])
        self.assertFalse(path.exists())

    def test_remove_refuses_a_task8_tree_when_mem_is_unreadable(self) -> None:
        self.live()
        task8 = self.card()
        path = self.add_landed(f"{task8}-fix")

        def down():
            raise OSError("disk gone")
        with mock.patch.object(policy, "_open_mem", down), mock.patch.dict(os.environ, self.env, clear=False), \
                self.assertRaisesRegex(policy.PolicyError, "^mem unreadable \\(disk gone\\)"):
            policy.worktree_remove(self.repo, f"{task8}-fix")
        self.assertTrue(path.is_dir())

    def test_own_cleanup_removes_a_done_tasks_task8_tree_and_keeps_a_claimed_or_open_one(self) -> None:
        self.live()
        done, claimed, column = self.card(), self.card(), self.card()
        trees = {t: self.add_landed(f"{t}-fix") for t in (done, claimed, column)}
        for task8 in (done, column):
            self.store.release(task8, self.store.get(task8)["claim_id"])
            self.store.set_status(task8, "done")
        # A card whose worktree column still names a tree stays, done or not.
        self.store.db.execute("UPDATE cards SET worktree=? WHERE id LIKE ?", (str(trees[column]), column + "%"))
        self.store.db.commit()
        with mock.patch.object(policy, "OWN_CLEANUP_LOG", Path(self.temporary.name) / "state" / "own.log"), \
                mock.patch.object(policy, "_temp_roots", lambda: []), \
                mock.patch.dict(os.environ, self.env, clear=False):
            result = policy.own_cleanup(self.repo, OWN_SESSION)
        self.assertEqual([f"task/{done}-fix"], result["removed"])
        self.assertEqual(("PASS", 0), (result["result"], result["leftover"]))
        self.assertEqual({f"task/{claimed}-fix": "task doing", f"task/{column}-fix": f"card names worktree {trees[column]}"},
                         {i["item"]: i["reason"] for i in result["left"]})
        self.assertFalse(trees[done].exists())
        self.assertEqual("", self.git("branch", "--list", f"task/{done}-fix").stdout)
        self.assertTrue(trees[claimed].is_dir() and trees[column].is_dir())

    def test_task_sweep_keeps_a_dropped_tasks_tree_with_unpushed_work(self) -> None:
        # Review finding (medium): a done or dropped card is no proof its tree's work landed.
        self.live()
        task8 = self.card()
        with mock.patch.dict(os.environ, self.env, clear=False):
            path = Path(policy.worktree_add(self.repo, f"{task8}-fix")["path"])
        (path / "unpushed.txt").write_text("unpushed\n", encoding="utf-8")
        self.git("-C", str(path), "add", "unpushed.txt")
        self.git("-C", str(path), "commit", "-m", "unpushed")
        self.store.release(task8, self.store.get(task8)["claim_id"])
        self.store.set_status(task8, "dropped")
        with mock.patch.dict(os.environ, self.env, clear=False):
            removed, held = policy.task_sweep(self.repo)
        self.assertEqual([], removed)
        self.assertEqual(f"task/{task8}-fix", held[0]["item"])
        self.assertIn("is not on origin/main", held[0]["reason"])
        self.assertEqual("unpushed\n", (path / "unpushed.txt").read_text(encoding="utf-8"))

    def test_task_sweep_holds_every_tree_when_mem_is_unreadable(self) -> None:
        self.live()
        task8 = self.card()
        path = self.add_landed(f"{task8}-fix")
        self.store.release(task8, self.store.get(task8)["claim_id"])
        self.store.set_status(task8, "done")

        def down():
            raise OSError("disk gone")
        with mock.patch.object(policy, "_open_mem", down), mock.patch.dict(os.environ, self.env, clear=False):
            removed, held = policy.task_sweep(self.repo)
        self.assertEqual([], removed)
        self.assertIn("mem unreadable", held[0]["reason"])
        self.assertTrue(path.is_dir())


if __name__ == "__main__":
    unittest.main()
