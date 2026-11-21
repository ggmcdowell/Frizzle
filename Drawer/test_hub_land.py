"""hub-land.py on a fixture hub: one call commits, smokes, lands and removes a task worktree.

The fixture is test_git_branch_policy's operator-box hub with a bare origin and a task worktree. Its harness.toml
points state_root into the fixture and [smoke] command at a stub that exits SMOKE_EXIT and logs where it ran.
hub-land.py runs as a child process, as a session runs it.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import tempfile
import subprocess
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Drawer"))
import test_git_branch_policy as gbp  # noqa: E402
import test_git_worktree_policy as gwp  # noqa: E402
import run_tmp  # noqa: E402

steer_check = gbp.steer_check
NAME = gbp.NAME
BRANCH = f"task/{NAME}"
TASK8 = "1234abcd"  # a task id the fixture mem stub answers
# The fixture's Drawer/mem/mem.py, never the live mem: mem get answers an id FAKE_MEM_IDS (a comma list, TASK8 by
# default) starts with by FAKE_MEM_TAGS, or by a mem error FAKE_MEM_ERROR; any other id has no card (the
# fixture session uuid8 0a1b2c3d of NAME is no card, as a live session uuid8 is not).
STUB_MEM = ("import json, os, sys\n"
            f"known = any(sys.argv[2].startswith(i) for i in os.environ.get('FAKE_MEM_IDS', '{TASK8}').split(','))\n"
            "error = os.environ.get('FAKE_MEM_ERROR') if known else f\"no card matches '{sys.argv[2]}'\"\n"
            "print(json.dumps({'error': error} if error else\n"
            "                 {'id': sys.argv[2], 'tags': json.loads(os.environ.get('FAKE_MEM_TAGS', '[]'))}))\n"
            "sys.exit(2 if error else 0)\n")
# PEER_CLONE: on the first run only, a peer pushes a commit to origin/main mid-smoke.
STUB_SMOKE = ("import os, subprocess, sys\nlog = os.environ.get('SMOKE_LOG')\n"
              "head = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()\n"
              "log and open(log, 'a').write(' '.join([os.getcwd(), head, *sys.argv[1:]]) + '\\n')\n"
              "peer = os.environ.get('PEER_CLONE')\n"
              "if peer and not os.path.exists(peer + '.done'):\n"
              "    open(peer + '.done', 'w').close()\n"
              "    open(os.path.join(peer, 'peer.txt'), 'w').write('peer\\n')\n"
              "    subprocess.run(['git', '-C', peer, 'add', 'peer.txt'], check=True)\n"
              "    subprocess.run(['git', '-C', peer, 'commit', '-q', '-m', 'peer mid-smoke'], check=True)\n"
              "    subprocess.run(['git', '-C', peer, 'push', '-q', 'origin', 'HEAD:main'], check=True)\n"
              "tmp_log = os.environ.get('SMOKE_TMP_LOG')\n"
              "tmp_log and open(tmp_log, 'a').write(os.environ.get('TMPDIR', '') + '\\n')\n"
              "tmp_log and open(os.path.join(os.environ['TMPDIR'], 'scratch.txt'), 'w').write('x')\n"
              "print('SMOKE stub', os.environ.get('SMOKE_EXIT', '0'))\n"
              "sys.exit(int(os.environ.get('SMOKE_EXIT', '0')))\n")


class HubLandTests(unittest.TestCase):
    # Borrowed by name, never bound at module level: a TestCase name here would run its tests twice.
    setUp_policy = gbp.GitBranchPolicyTests.setUp
    setUp_tree = gwp.TaskWorktreeTests.setUp
    tearDown = gwp.TaskWorktreeTests.tearDown
    child_env = gwp.TaskWorktreeTests.child_env
    git = gwp.TaskWorktreeTests.git
    accepted = gwp.TaskWorktreeTests.accepted
    install = gwp.TaskWorktreeTests.install
    install_key = gwp.TaskWorktreeTests.install_key
    add = gwp.TaskWorktreeTests.add
    commit_in_tree = gwp.TaskWorktreeTests.commit_in_tree
    origin_main = gwp.TaskWorktreeTests.origin_main
    on_origin = gwp.TaskWorktreeTests.on_origin
    land_verb = gwp.TaskWorktreeTests.land_verb
    land_on_origin_main = gwp.TaskWorktreeTests.land_on_origin_main

    def setUp(self) -> None:
        self.setUp_tree()
        base = Path(self.temporary.name)
        drawer = self.repo / "Drawer"
        shutil.copy2(ROOT / "Drawer/hub-land.py", drawer / "hub-land.py")
        shutil.copy2(ROOT / "Drawer/review-diff.py", drawer / "review-diff.py")
        shutil.copy2(ROOT / "Drawer/steer_check.py", drawer / "steer_check.py")
        shutil.copy2(ROOT / "Drawer/pushretry.py", drawer / "pushretry.py")
        shutil.copy2(ROOT / "Drawer/run_tmp.py", drawer / "run_tmp.py")
        shutil.copy2(ROOT / "Drawer/oscompat.py", drawer / "oscompat.py")  # runtime/profile.py imports it
        shutil.copy2(ROOT / "Drawer/review_pass.py", drawer / "review_pass.py")
        (drawer / "mem").mkdir()
        (drawer / "mem" / "mem.py").write_text(STUB_MEM, encoding="utf-8")
        (drawer / "runtime").mkdir()
        for name in ("__init__.py", "profile.py"):
            shutil.copy2(ROOT / "Drawer/runtime" / name, drawer / "runtime" / name)
        (drawer / "stub_smoke.py").write_text(STUB_SMOKE, encoding="utf-8")
        toml = self.repo / "harness.toml"
        text = re.sub(r"(?m)^state_root = .*$", lambda _m: f"state_root = {json.dumps(str(base / 'state'))}",
                      toml.read_text(encoding="utf-8"))
        text, smoked = re.subn(r"(?m)^(\[smoke\]\ncommand = ).*$", r'\1"{python} Drawer/stub_smoke.py"', text)
        text, landed = re.subn(r"(?m)^(land_command = ).*$", r'\1"{python} Drawer/stub_smoke.py --impact origin/main"',
                               text)
        self.assertEqual((1, 1), (smoked, landed))
        toml.write_text(text, encoding="utf-8")
        self.git("add", "Drawer", "harness.toml")
        self.git("commit", "-m", "hub-land fixture")
        self.git("push", "origin", "HEAD:refs/heads/main")
        self.smoke_log = base / "smoke.log"
        self.home = base / "home"
        # The run folder goes to a fixture trash bin, never the box trash: a trash command first on the PATH.
        self.trash_bin = base / "trash-bin"
        self.trash_bin.mkdir()
        self.fake_bin.mkdir(exist_ok=True)
        fake_trash = self.fake_bin / "trash"
        fake_trash.write_text(f'#!/bin/sh\nmv "$1" {shlex.quote(str(self.trash_bin))}/\n', encoding="utf-8")
        fake_trash.chmod(0o755)
        self.add()

    def hub_land(self, *args: str, cwd: Path | None = None, **env: str) -> tuple[int, dict]:
        """hub-land.py of the task worktree, run as a child, as a session runs it."""
        # Windows names its temp folder in TEMP and TMP, which child_env does not pass: without them the child's
        # temp folder is the C:/temp fallback, not the one the test reads. Without SYSTEMROOT the Recycle Bin call
        # of the run folder's trash fails (code 126). POSIX passes TMPDIR already.
        temp = {name: os.environ[name] for name in ("TEMP", "TMP", "SYSTEMROOT") if name in os.environ}
        child = {**self.child_env(), **temp, "CLAUDE_CODE_SESSION_ID": gbp.OWN_SESSION, "HOME": str(self.home),
                 "SMOKE_LOG": str(self.smoke_log), **env}
        script = (self.tree if self.tree.exists() else self.repo) / "Drawer/hub-land.py"
        run = subprocess.run([sys.executable, "-B", str(script), "--name", NAME, *args],
                             cwd=str(cwd or self.repo), capture_output=True, text=True, env=child, check=False)
        lines = run.stdout.strip().splitlines()
        self.assertEqual(1, len(lines), f"one JSON line, got stdout {run.stdout!r} stderr {run.stderr!r}")
        return run.returncode, json.loads(lines[0])

    def branch_tip(self) -> str:
        return self.git("rev-parse", "--verify", "--quiet", f"refs/heads/{BRANCH}", check=False).stdout.strip()

    def smoke_runs(self) -> list[str]:
        try:
            return [line.split(" ")[0] for line in self.smoke_log.read_text(encoding="utf-8").splitlines()]
        except OSError:
            return []

    def test_the_branch_copy_runs_the_main_copy_for_the_land(self) -> None:
        # A task smoked with its own stale hub-land copy and went red on a fault main had fixed.
        script = self.tree / "Drawer/hub-land.py"
        old = 'print(json.dumps({"state": state, **fields}))'
        text = script.read_text(encoding="utf-8")
        self.assertEqual(1, text.count(old))
        script.write_text(text.replace(old, 'print(json.dumps({"state": state, "runner": "branch", **fields}))'),
                          encoding="utf-8")
        code, out = self.hub_land("-m", "stale lander", "--", "Drawer/hub-land.py", cwd=self.tree)
        self.assertNotIn("runner", out, "the branch copy ran the land")
        self.assertEqual((0, "landed", "green", True),
                         (code, out.get("state"), out.get("smoke"), out.get("removed")), out)
        self.assertEqual([str(self.tree.resolve())], [str(Path(p).resolve()) for p in self.smoke_runs()])
        self.assertEqual("stale lander", self.git("log", "-1", "--format=%s", self.origin_main()).stdout.strip())

    def test_the_smoke_runs_in_its_own_run_folder_that_is_trashed_after(self) -> None:
        # The smoke's temp folders stay out of the box TMPDIR, in <cache>/<state name>-<hash>-test-tmp.
        tmp_log = Path(self.temporary.name) / "tmp.log"
        self.commit_in_tree()
        code, out = self.hub_land(SMOKE_TMP_LOG=str(tmp_log))
        self.assertEqual((0, "landed", "green"), (code, out.get("state"), out.get("smoke")), out)
        folder = Path(tmp_log.read_text(encoding="utf-8").strip())
        parent = run_tmp.base({"project": {"state_root": str(Path(self.temporary.name) / "state")}})
        self.assertEqual(parent.resolve(), folder.parent.resolve())
        self.assertFalse(parent.exists())  # the last run leaves no empty parent behind
        if sys.platform == "darwin":  # only macOS has a cache folder apart from the temp folder (run_tmp.temp_root)
            self.assertNotIn(str(Path(tempfile.gettempdir()).resolve()), str(folder.resolve()))  # out of the box TMPDIR
        self.assertFalse(folder.exists())
        if os.name != "nt":  # Windows trashes to the Recycle Bin, never the fixture's trash command
            self.assertTrue((self.trash_bin / folder.name / "scratch.txt").is_file())

    def test_the_land_smoke_is_land_command(self) -> None:
        # Hub-land smokes with [smoke] land_command, the impact smoke.
        self.commit_in_tree()
        code, out = self.hub_land()
        self.assertEqual((0, "landed", "green"), (code, out.get("state"), out.get("smoke")), out)
        self.assertEqual(["--impact", "origin/main"], self.smoke_log.read_text(encoding="utf-8").split()[2:])

    def test_an_empty_land_command_smokes_with_command(self) -> None:
        toml = self.tree / "harness.toml"
        toml.write_text(re.sub(r"(?m)^land_command = .*$", 'land_command = ""', toml.read_text(encoding="utf-8")),
                        encoding="utf-8")
        self.git("-C", str(self.tree), "commit", "-qam", "empty land_command")
        code, out = self.hub_land()
        self.assertEqual((0, "landed", "green"), (code, out.get("state"), out.get("smoke")), out)
        self.assertEqual([], self.smoke_log.read_text(encoding="utf-8").split()[2:])

    def test_bytecode_is_ignored_in_the_hub_and_a_pycache_folder_does_not_trip_the_land(self) -> None:
        # A tree that ran Python once holds Drawer/__pycache__; hub-land read it as uncommitted work.
        ignored = subprocess.run(["git", "check-ignore", "-q", "Drawer/__pycache__/x.pyc"], cwd=str(ROOT),
                                 capture_output=True, check=False)
        self.assertEqual(0, ignored.returncode, "the hub .gitignore must ignore __pycache__")
        (self.tree / "Drawer/__pycache__").mkdir(exist_ok=True)
        (self.tree / "Drawer/__pycache__/x.cpython-312.pyc").write_bytes(b"\0")
        self.commit_in_tree()
        code, out = self.hub_land()
        self.assertEqual((0, "landed", "green"), (code, out.get("state"), out.get("smoke")), out)

    def test_one_call_commits_smokes_lands_and_removes_then_a_rerun_prints_already(self) -> None:
        (self.tree / "task.txt").write_text("task work\n", encoding="utf-8")
        code, out = self.hub_land("-m", "task work", "--", "task.txt", cwd=self.tree)
        self.assertEqual((0, "landed", True, "green"),
                         (code, out.get("state"), out.get("removed"), out.get("smoke")), out)
        self.assertEqual(out.get("tip"), self.origin_main())
        self.assertEqual("task work", self.git("log", "-1", "--format=%s", self.origin_main()).stdout.strip())
        self.assertTrue(self.on_origin("task.txt"))
        self.assertEqual(self.origin_main(), self.git("rev-parse", "HEAD").stdout.strip())
        self.assertFalse(self.tree.exists())
        self.assertEqual("", self.branch_tip())
        self.assertEqual([str(self.tree.resolve())], [str(Path(p).resolve()) for p in self.smoke_runs()])
        code, out = self.hub_land("-m", "task work", "--", "task.txt")
        self.assertEqual((0, "already", True), (code, out.get("state"), out.get("removed")), out)
        self.assertEqual(1, len(self.smoke_runs()))

    def test_origin_moving_during_the_smoke_smokes_the_rebased_tip_before_its_push(self) -> None:
        # The smoke judged the pre-rebase tip and land pushed the rebased tip untested.
        peer = Path(self.temporary.name) / "peer"
        subprocess.run(["git", "clone", "-q", "-b", "main", str(self.remote), str(peer)], check=True,
                       capture_output=True, env=self.child_env())
        for key, value in (("user.name", "Peer"), ("user.email", "peer@example.invalid")):
            subprocess.run(["git", "-C", str(peer), "config", key, value], check=True, env=self.child_env())
        self.commit_in_tree()
        code, out = self.hub_land(PEER_CLONE=str(peer))
        self.assertEqual((0, "landed", "green"), (code, out.get("state"), out.get("smoke")), out)
        heads = [line.split(" ")[1] for line in self.smoke_log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(2, len(heads), heads)
        self.assertEqual(out.get("tip"), heads[-1])
        self.assertEqual(out.get("tip"), self.origin_main())
        self.assertTrue(self.on_origin("peer.txt") and self.on_origin("task.txt"))

    def reviewed(self) -> str:
        spec = importlib.util.spec_from_file_location("review_diff", self.tree / "Drawer/review-diff.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.build(self.tree, Path(self.temporary.name) / "state")["hash"]

    def test_a_commit_added_after_review_gives_review_exit_3_with_nothing_smoked_or_pushed(self) -> None:
        # A rebase or a late commit after review could reach main unreviewed.
        self.commit_in_tree()
        reviewed = self.reviewed()
        origin = self.origin_main()
        (self.tree / "late.txt").write_text("unreviewed\n", encoding="utf-8")
        code, out = self.hub_land("--reviewed", reviewed, "-m", "late", "--", "late.txt")
        self.assertEqual((3, "review", reviewed), (code, out.get("state"), out.get("reviewed")), out)
        self.assertEqual(self.reviewed(), out.get("hash"))
        self.assertIn("late.txt", out["args"]["files"])
        self.assertEqual((origin, []), (self.origin_main(), self.smoke_runs()))
        self.assertTrue(self.tree.exists())
        code, out = self.hub_land("--reviewed", out["hash"])
        self.assertEqual((0, "landed"), (code, out.get("state")), out)
        self.assertTrue(self.on_origin("late.txt"))

    def test_markdown_added_after_review_gives_review_exit_3(self) -> None:
        # A Markdown file left out of the review diff reached main unreviewed.
        self.commit_in_tree()
        reviewed = self.reviewed()
        origin = self.origin_main()
        (self.tree / "notes.md").write_text("notes\n", encoding="utf-8")
        code, out = self.hub_land("--reviewed", reviewed, "-m", "notes", "--", "notes.md")
        self.assertEqual((3, "review", reviewed), (code, out.get("state"), out.get("reviewed")), out)
        self.assertIn("notes.md", out["args"]["files"])
        self.assertEqual((origin, []), (self.origin_main(), self.smoke_runs()))

    # A task tagged risky landed with no review. The fixture mem stub answers mem get with
    # FAKE_MEM_TAGS (JSON), or a mem error with FAKE_MEM_ERROR.
    def review_args(self) -> dict:
        spec = importlib.util.spec_from_file_location("review_diff", self.tree / "Drawer/review-diff.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.build(self.tree, Path(self.temporary.name) / "state")

    def store_pass(self, found: dict) -> None:
        import review_pass
        review_pass.record(Path(self.temporary.name) / "state", found["hash"], found["head"], found["diffPath"], 1,
                           "claude-sonnet-5-5")

    def test_a_risky_land_with_no_reviewed_is_refused_with_its_review_args(self) -> None:
        self.commit_in_tree()
        origin = self.origin_main()
        code, out = self.hub_land("--task", TASK8, FAKE_MEM_TAGS='["risky"]')
        self.assertEqual((3, "review", None), (code, out.get("state"), out.get("reviewed")), out)
        found = self.review_args()
        self.assertEqual((found["hash"], found["hash"]), (out.get("hash"), out["args"]["hash"]))
        self.assertIn(f"task {TASK8} is tagged risky", out.get("output"))
        self.assertEqual((origin, []), (self.origin_main(), self.smoke_runs()))
        self.assertTrue(self.tree.exists())

    def test_a_risky_land_with_a_pass_for_another_diff_is_refused(self) -> None:
        self.commit_in_tree()
        first = self.review_args()
        self.store_pass(first)
        (self.tree / "late.txt").write_text("late\n", encoding="utf-8")
        self.git("-C", str(self.tree), "add", "late.txt")
        self.git("-C", str(self.tree), "commit", "-m", "late")
        now = self.review_args()["hash"]
        self.assertNotEqual(first["hash"], now)
        origin = self.origin_main()
        code, out = self.hub_land("--task", TASK8, "--reviewed", now, FAKE_MEM_TAGS='["risky"]')
        self.assertEqual((3, "review", now, now), (code, out.get("state"), out.get("reviewed"), out.get("hash")), out)
        self.assertIn("no stored risky-review Pass", out.get("output"))
        code, out = self.hub_land("--task", TASK8, "--reviewed", first["hash"], FAKE_MEM_TAGS='["risky"]')
        self.assertEqual((3, "review", now), (code, out.get("state"), out.get("hash")), out)
        self.assertEqual((origin, []), (self.origin_main(), self.smoke_runs()))

    def test_a_risky_land_with_a_stored_pass_lands_with_its_reviewed_hash(self) -> None:
        self.commit_in_tree()
        found = self.review_args()
        self.store_pass(found)
        code, out = self.hub_land("--task", TASK8, "--reviewed", found["hash"], FAKE_MEM_TAGS='["flight-backlog", "risky"]')
        self.assertEqual((0, "landed", found["hash"]), (code, out.get("state"), out.get("reviewed")), out)
        self.assertTrue(self.on_origin("task.txt"))

    def test_a_land_not_tagged_risky_needs_no_review(self) -> None:
        self.commit_in_tree()
        code, out = self.hub_land("--task", TASK8, FAKE_MEM_TAGS='["flight-backlog"]')
        self.assertEqual((0, "landed", "green"), (code, out.get("state"), out.get("smoke")), out)
        self.assertIn("reviewed", out)
        self.assertIsNone(out["reviewed"])

    def test_a_mem_fault_for_an_explicit_task_fails_at_step_review(self) -> None:
        self.commit_in_tree()
        origin = self.origin_main()
        for error in ("database is locked", f"no card matches '{TASK8}'"):
            with self.subTest(error=error):
                code, out = self.hub_land("--task", TASK8, FAKE_MEM_ERROR=error)
                self.assertEqual((1, "failed", "review"), (code, out.get("state"), out.get("step")), out)
                self.assertIn(error, out.get("output"))
        self.assertEqual((origin, []), (self.origin_main(), self.smoke_runs()))

    # A session tree <session uuid8>-<slug> (NAME) names no task mem knows: with no --task there is no gate.
    def test_a_tree_with_no_known_task_lands_as_not_risky(self) -> None:
        self.commit_in_tree()
        code, out = self.hub_land(FAKE_MEM_TAGS='["risky"]')
        self.assertEqual((0, "landed", None), (code, out.get("state"), out.get("reviewed")), out)

    def test_a_mem_fault_for_a_task_from_the_name_fails_at_step_review(self) -> None:
        # A tree named <uuid8>-<slug>-<task8> gives the task by its name. Only NoCard (mem has no such
        # card) may mean not risky; any other mem fault (database locked, timeout) fails closed.
        name = f"{NAME[:8]}-gate-{TASK8}"
        with mock.patch.dict(os.environ, self.env, clear=False):
            self.accepted("worktree-add", gwp.policy.worktree_add, self.repo, name)
        tree = self.tree.parent / name
        (tree / "named.txt").write_text("named\n", encoding="utf-8")
        self.git("-C", str(tree), "add", "named.txt")
        self.git("-C", str(tree), "commit", "-m", "named work")
        origin = self.origin_main()
        code, out = self.hub_land("--name", name, FAKE_MEM_ERROR="database is locked")
        self.assertEqual((1, "failed", "review"), (code, out.get("state"), out.get("step")), out)
        self.assertIn(f"the tags of task {TASK8} are unreadable", out.get("output"))
        self.assertEqual((origin, []), (self.origin_main(), self.smoke_runs()))
        code, out = self.hub_land("--name", name, FAKE_MEM_ERROR=f"no card matches '{TASK8}'")
        self.assertEqual((0, "landed", None), (code, out.get("state"), out.get("reviewed")), out)
        self.assertTrue(self.on_origin("named.txt"))

    # The git-flow task form names a tree <task8>-<slug>, the task id first. The gate reads a leading
    # 8-hex part as well as a trailing one, when mem has a card for it.
    def plain_tree(self, name: str) -> None:
        """A task tree made by plain git: worktree-add takes <task8>-<slug> only with a live claim in the policy mem."""
        tree = self.tree.parent / name
        self.git("worktree", "add", "-q", "-b", f"task/{name}", str(tree))
        (tree / "named.txt").write_text("named\n", encoding="utf-8")
        self.git("-C", str(tree), "add", "named.txt")
        self.git("-C", str(tree), "commit", "-m", "named work")

    def test_a_risky_task8_slug_land_with_no_task_is_refused(self) -> None:
        name = f"{TASK8}-gate"
        self.plain_tree(name)
        origin = self.origin_main()
        code, out = self.hub_land("--name", name, FAKE_MEM_TAGS='["risky"]')
        self.assertEqual((3, "review", None), (code, out.get("state"), out.get("reviewed")), out)
        self.assertIn(f"task {TASK8} is tagged risky", out.get("output"))
        self.assertEqual((origin, []), (self.origin_main(), self.smoke_runs()))
        code, out = self.hub_land("--name", name, FAKE_MEM_ERROR="database is locked")
        self.assertEqual((1, "failed", "review"), (code, out.get("state"), out.get("step")), out)
        self.assertIn(f"the tags of task {TASK8} are unreadable", out.get("output"))
        self.assertEqual((origin, []), (self.origin_main(), self.smoke_runs()))

    def test_a_leading_hex_word_with_no_card_is_not_read_as_a_task(self) -> None:
        name = "deadbeef-gate"
        self.plain_tree(name)
        code, out = self.hub_land("--name", name, FAKE_MEM_TAGS='["risky"]')
        self.assertEqual((0, "landed", None), (code, out.get("state"), out.get("reviewed")), out)
        self.assertTrue(self.on_origin("named.txt"))

    def test_both_hex_parts_prefer_the_one_mem_knows(self) -> None:
        for name in (f"deadbeef-gate-{TASK8}", f"{TASK8}-gate-deadbeef"):
            with self.subTest(name=name):
                self.plain_tree(name)
                code, out = self.hub_land("--name", name, FAKE_MEM_TAGS='["risky"]')
                self.assertEqual((3, "review"), (code, out.get("state")), out)
                self.assertIn(f"task {TASK8} is tagged risky", out.get("output"))

    def test_a_bad_reviewed_hash_is_bad_arguments(self) -> None:
        run = subprocess.run([sys.executable, "-B", str(self.repo / "Drawer/hub-land.py"), "--name", NAME,
                              "--reviewed", "xyz"], cwd=str(self.repo), capture_output=True, text=True,
                             env=self.child_env(), check=False)
        self.assertEqual(2, run.returncode)

    def test_a_red_smoke_fails_at_step_smoke_with_nothing_pushed_and_a_rerun_lands(self) -> None:
        origin = self.origin_main()
        (self.tree / "task.txt").write_text("task work\n", encoding="utf-8")
        code, out = self.hub_land("-m", "task work", "--", "task.txt", SMOKE_EXIT="1")
        self.assertEqual((1, "failed", "smoke", 1, False),
                         (code, out.get("state"), out.get("step"), out.get("exit"), out.get("blocked")), out)
        self.assertIn("SMOKE stub 1", out.get("output"))
        self.assertEqual(origin, self.origin_main())
        self.assertFalse(self.on_origin("task.txt"))
        self.assertEqual(out.get("tip"), self.branch_tip())
        self.assertTrue(self.tree.exists())
        code, out = self.hub_land()
        self.assertEqual((0, "landed", True), (code, out.get("state"), out.get("removed")), out)
        self.assertTrue(self.on_origin("task.txt"))

    def test_a_blocked_smoke_says_blocked_and_pushes_nothing(self) -> None:
        origin = self.commit_in_tree() and self.origin_main()
        code, out = self.hub_land(SMOKE_EXIT="75")
        self.assertEqual((1, "failed", "smoke", True), (code, out.get("state"), out.get("step"), out.get("blocked")))
        self.assertEqual(origin, self.origin_main())

    def test_a_steer_stop_gives_need_exit_75_with_nothing_pushed_and_lands_after_go(self) -> None:
        tip = self.commit_in_tree()
        origin = self.origin_main()
        steer_check.stop(gbp.CFG, gbp.OWN_SESSION, "hold for review", step="push")
        code, out = self.hub_land()
        self.assertEqual((75, "need", "steer"), (code, out.get("state"), out.get("reason")), out)
        self.assertIn("Flight holds a STOP for 0a1b2c3d at push: hold for review", out.get("why"))
        self.assertEqual(origin, self.origin_main())
        self.assertTrue(self.tree.exists())
        steer_check.go(gbp.CFG, gbp.OWN_SESSION)
        code, out = self.hub_land()
        self.assertEqual((0, "landed", tip, True), (code, out.get("state"), self.origin_main(), out.get("removed")))

    def test_a_task8_stop_holds_a_task_push_with_nothing_pushed(self) -> None:
        # Legs share one session id, so Flight holds one task's push by its task8.
        tip = self.commit_in_tree()
        origin = self.origin_main()
        steer_check.stop(gbp.CFG, "5a5a5a5a", "hold this task", step="push")
        code, out = self.hub_land("--task", "5a5a5a5a", FAKE_MEM_IDS="5a5a5a5a")
        self.assertEqual((75, "need", "steer"), (code, out.get("state"), out.get("reason")), out)
        self.assertIn("Flight holds a STOP for 5a5a5a5a at push: hold this task", out.get("why"))
        self.assertEqual(origin, self.origin_main())
        self.assertTrue(self.tree.exists())
        steer_check.go(gbp.CFG, "5a5a5a5a")
        code, out = self.hub_land("--task", "5a5a5a5a-5dcf-4a02-89de-488151981b27", FAKE_MEM_IDS="5a5a5a5a")
        self.assertEqual((0, "landed", tip), (code, out.get("state"), self.origin_main()), out)

    def test_a_deleted_path_is_staged_as_a_removal(self) -> None:
        self.commit_in_tree()
        self.assertEqual("landed", self.land_verb().get("state"))
        self.assertTrue(self.on_origin("task.txt"))
        (self.tree / "task.txt").unlink()
        code, out = self.hub_land("-m", "drop task.txt", "--", "task.txt")
        self.assertEqual((0, "landed", True), (code, out.get("state"), out.get("removed")), out)
        self.assertFalse(self.on_origin("task.txt"))

    def test_a_tip_already_on_origin_main_prints_already_and_removes_the_tree(self) -> None:
        tip = self.commit_in_tree()
        self.assertEqual("landed", self.land_verb().get("state"))
        code, out = self.hub_land()
        self.assertEqual((0, "already", True, tip), (code, out.get("state"), out.get("removed"), out.get("tip")))
        self.assertFalse(self.tree.exists())
        self.assertEqual([], self.smoke_runs())

    def test_a_rerun_after_a_partial_land_moves_the_main_checkout_before_already(self) -> None:
        # Review finding: a tip on origin/main whose main checkout did not move printed
        # already and removed the tree; main never moved.
        tip = self.commit_in_tree()
        self.land_on_origin_main(tip)
        self.assertNotEqual(tip, self.git("rev-parse", "HEAD").stdout.strip())
        code, out = self.hub_land()
        self.assertEqual((0, "landed", True, "pushed before"),
                         (code, out.get("state"), out.get("removed"), out.get("smoke")), out)
        self.assertEqual(tip, self.git("rev-parse", "HEAD").stdout.strip())
        self.assertEqual([], self.smoke_runs())

    def test_untracked_work_fails_at_step_stage_before_smoke_or_push(self) -> None:
        # Review finding: an untracked file left out of <paths> fed the smoke and blocked the remove.
        origin = self.origin_main()
        self.commit_in_tree()
        (self.tree / "forgotten.txt").write_text("not named\n", encoding="utf-8")
        code, out = self.hub_land()
        self.assertEqual((1, "failed", "stage"), (code, out.get("state"), out.get("step")), out)
        self.assertIn("forgotten.txt", out.get("output"))
        self.assertEqual(([], origin), (self.smoke_runs(), self.origin_main()))

    def test_an_already_landed_tree_with_untracked_work_fails_at_step_remove(self) -> None:
        # Review finding: already with removed false hid work that never landed.
        self.commit_in_tree()
        self.assertEqual("landed", self.land_verb().get("state"))
        (self.tree / "forgotten.txt").write_text("not named\n", encoding="utf-8")
        code, out = self.hub_land()
        self.assertEqual((1, "failed", "remove"), (code, out.get("state"), out.get("step")), out)
        self.assertTrue((self.tree / "forgotten.txt").exists())

    def test_a_rerun_with_the_same_deleted_path_lands_after_a_red_smoke(self) -> None:
        # Re-review finding: the rerun failed at stage, the path gone from the index.
        self.commit_in_tree()
        self.assertEqual("landed", self.land_verb().get("state"))
        (self.tree / "task.txt").unlink()
        code, out = self.hub_land("-m", "drop task.txt", "--", "task.txt", SMOKE_EXIT="1")
        self.assertEqual((1, "smoke"), (code, out.get("step")), out)
        code, out = self.hub_land("-m", "drop task.txt", "--", "task.txt")
        self.assertEqual((0, "landed"), (code, out.get("state")), out)
        self.assertFalse(self.on_origin("task.txt"))

    def test_an_unknown_path_fails_at_step_stage(self) -> None:
        origin = self.origin_main()
        code, out = self.hub_land("-m", "typo", "--", "no-such.txt")
        self.assertEqual((1, "failed", "stage"), (code, out.get("state"), out.get("step")), out)
        self.assertEqual(origin, self.origin_main())

    def test_a_deleted_folder_is_staged_as_a_removal(self) -> None:
        (self.tree / "dir").mkdir()
        (self.tree / "dir" / "a.txt").write_text("a\n", encoding="utf-8")
        self.git("-C", str(self.tree), "add", "dir")
        self.git("-C", str(self.tree), "commit", "-m", "dir")
        self.assertEqual("landed", self.land_verb().get("state"))
        shutil.rmtree(self.tree / "dir")
        code, out = self.hub_land("-m", "drop dir", "--", "dir")
        self.assertEqual((0, "landed"), (code, out.get("state")), out)
        self.assertFalse(self.on_origin("dir/a.txt"))

    def test_an_unknown_task_fails_at_step_worktree(self) -> None:
        child = {**self.child_env(), "HOME": str(self.home)}
        run = subprocess.run([sys.executable, "-B", str(self.repo / "Drawer/hub-land.py"), "--name", "0a1b2c3d-none"],
                             cwd=str(self.repo), capture_output=True, text=True, env=child, check=False)
        out = json.loads(run.stdout)
        self.assertEqual((1, "failed", "worktree"), (run.returncode, out.get("state"), out.get("step")))

    def test_paths_without_a_message_are_bad_arguments(self) -> None:
        run = subprocess.run([sys.executable, "-B", str(self.repo / "Drawer/hub-land.py"), "--name", NAME, "--",
                              "task.txt"], cwd=str(self.repo), capture_output=True, text=True,
                             env=self.child_env(), check=False)
        self.assertEqual(2, run.returncode)


if __name__ == "__main__":
    unittest.main()
