"""Drawer/product_worktree.py: per-task product worktrees on a temp product repo with a scratch mem DB."""

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Drawer"))
sys.path.insert(0, str(ROOT / "Drawer" / "mem"))
import product_worktree as gwt  # noqa: E402
import harness_config  # noqa: E402
import mem  # noqa: E402
import testgit  # noqa: E402

MEM_DB, MEM_EMBED = harness_config.env_name("MEM_DB"), harness_config.env_name("MEM_EMBED")  # the hub's prefix
testgit.arm()  # no git template in any fixture repo

LIVE = "11111111-0000-4000-8000-000000000001"
DEAD = "22222222-0000-4000-8000-000000000002"
OTHER = "33333333-0000-4000-8000-000000000003"


class NoEmbed:
    dim = 1536

    def embed(self, texts):
        return None


class ProductWorktreeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="product-wt-")
        self.addCleanup(self._tmp.cleanup)
        base = self.base = Path(self._tmp.name).resolve()
        self.home = base / "home"
        (self.home / ".claude" / "sessions").mkdir(parents=True)
        self.alive(LIVE)
        origin, self.seed, self.main = base / "origin.git", base / "seed", base / "product"
        self.git(base, "init", "-q", "--bare", "-b", "main", str(origin))
        self.git(base, "clone", "-q", str(origin), str(self.seed))
        (self.seed / "project").mkdir()
        (self.seed / "project" / "project.cfg").write_text("x\n", encoding="utf-8")
        self.commit(self.seed, "product")
        self.git(self.seed, "push", "-q", "origin", "HEAD:main")
        self.git(base, "clone", "-q", str(origin), str(self.main))
        self.cfg = {"product": {"path": str(self.main / "project")}}
        self.store = mem.Mem(base / "mem.db", NoEmbed())
        self.addCleanup(self.store.db.close)
        env = unittest.mock.patch.dict(os.environ, {**harness_config.home_vars(self.home), MEM_DB: str(base / "mem.db"),
                                                    MEM_EMBED: "off"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)  # a profile's config dir would move the sessions folder off self.home; env.stop restores it

    def git(self, repo, *args):
        r = subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                           capture_output=True, text=True)
        self.assertEqual(0, r.returncode, r.stderr)
        return r.stdout.strip()

    def commit(self, repo, msg):
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "-m", msg)

    def alive(self, session):
        (self.home / ".claude" / "sessions" / f"{session[:8]}.json").write_text(
            json.dumps({"sessionId": session, "pid": os.getpid()}), encoding="utf-8")

    def kill(self, session):
        (self.home / ".claude" / "sessions" / f"{session[:8]}.json").unlink()

    def task(self, session=None, status="todo"):
        """A new task card, claimed by session when given; its uuid8."""
        card = self.store.add("task", f"Fixture {len(self.store.list(kind='task', all_rows=True))}: a product tree. "
                              "Why: a test. Done when: it passes.", new=True, status="todo")
        if session:
            self.store.claim(card["id"], session)
        if status != "todo":
            self.store.db.execute("UPDATE cards SET status=? WHERE id=?", (status, card["id"]))
        return card["id"][:8]

    def lock_of(self, root):
        for block in self.git(self.main, "worktree", "list", "--porcelain").split("\n\n"):
            rows = dict(line.partition(" ")[::2] for line in block.splitlines())
            if Path(rows.get("worktree", "")) == Path(root):  # git prints forward slashes on Windows
                return rows.get("locked")
        return None

    def add(self, task8, session=None):
        return gwt.add_task(self.cfg, self.store, task8, session)

    def test_add_turns_on_long_paths_and_reports_a_checkout_that_git_refuses(self):
        task8 = self.task(LIVE)
        real = gwt.git
        seen = []

        def failing(repo, *args, **kw):
            if "worktree" in args and "add" in args:
                seen.append(args)
                return subprocess.CompletedProcess(args, 128, "", "error: unable to create file: Filename too long\n")
            return real(repo, *args, **kw)
        with unittest.mock.patch.object(gwt, "git", failing):
            with self.assertRaisesRegex(gwt.Refused, "not added: error: unable to create file: Filename too long"):
                self.add(task8)
        self.assertEqual(("-c", "core.longpaths=true"), seen[0][:2])

    def test_a_second_add_returns_the_same_tree_and_lock(self):
        task8 = self.task(LIVE)
        first = self.add(task8)
        lock = self.lock_of(Path(first["path"]))
        second = self.add(task8, LIVE)

        root = self.base / "product_wt" / task8
        self.assertEqual(("added", "kept"), (first["state"], second["state"]))
        self.assertEqual(str(root), first["path"])
        self.assertEqual(first["path"], second["path"])
        self.assertEqual(str(root / "project"), first["project"])
        self.assertEqual(lock, self.lock_of(root))
        claim8 = self.store.get(task8)["claim_id"][:8]
        self.assertEqual(["task", task8, "claim", claim8, "session", LIVE], lock.split()[:6])
        self.assertEqual(f"task/{task8}", self.git(root, "branch", "--show-current"))
        self.assertEqual(self.git(self.main, "rev-parse", "origin/main"), self.git(root, "rev-parse", "HEAD"))

    def test_add_refuses_an_unclaimed_task_and_another_session(self):
        unclaimed, claimed = self.task(), self.task(LIVE)
        with self.assertRaisesRegex(gwt.Refused, "holds no claim"):
            self.add(unclaimed)
        with self.assertRaisesRegex(gwt.Refused, "claimed by session"):
            self.add(claimed, OTHER)
        self.assertFalse((self.base / "product_wt").exists())

    def test_a_dead_claim_tree_relocks_to_the_new_claim_and_keeps_its_work(self):
        self.alive(DEAD)
        task8 = self.task(DEAD)
        root = Path(self.add(task8)["path"])
        (root / "project" / "half.gd").write_text("x\n", encoding="utf-8")
        self.commit(root, "half done")
        tip = self.git(root, "rev-parse", "HEAD")
        (root / "project" / "wip.gd").write_text("y\n", encoding="utf-8")
        self.kill(DEAD)
        self.store.interrupt(task8)  # the dead leg's claim clears; the task parks
        self.store.claim(task8, LIVE)
        again = self.add(task8, LIVE)

        claim8 = self.store.get(task8)["claim_id"][:8]
        self.assertEqual(("relocked", str(root)), (again["state"], again["path"]))
        self.assertEqual(["task", task8, "claim", claim8, "session", LIVE], self.lock_of(root).split()[:6])
        self.assertEqual(tip, self.git(root, "rev-parse", "HEAD"))
        self.assertEqual("y\n", (root / "project" / "wip.gd").read_text(encoding="utf-8"))

    def test_a_kept_branch_with_no_tree_takes_a_new_tree_on_that_branch(self):
        task8 = self.task(LIVE)
        root = Path(self.add(task8)["path"])
        (root / "project" / "a.gd").write_text("x\n", encoding="utf-8")
        self.commit(root, "unlanded")
        tip = self.git(root, "rev-parse", "HEAD")
        self.git(self.main, "worktree", "unlock", str(root))
        self.git(self.main, "worktree", "remove", str(root))
        again = self.add(task8)
        self.assertEqual(("re-entered", tip), (again["state"], self.git(root, "rev-parse", "HEAD")))

    def test_a_tree_deleted_by_hand_is_pruned_and_re_entered(self):
        task8 = self.task(LIVE)
        root = Path(self.add(task8)["path"])
        (root / "project" / "a.gd").write_text("x\n", encoding="utf-8")
        self.commit(root, "unlanded")
        tip = self.git(root, "rev-parse", "HEAD")
        subprocess.run(["chmod", "-R", "u+w", str(root)], check=True)
        import shutil
        shutil.rmtree(root)
        again = self.add(task8)
        self.assertEqual(("re-entered", tip), (again["state"], self.git(root, "rev-parse", "HEAD")))

    def landed(self, task8):
        """Commit one file on the task tree and land it on origin/main as a rebased (non-ancestor) commit."""
        root = self.base / "product_wt" / task8
        (root / "project" / f"{task8}.gd").write_text("x\n", encoding="utf-8")
        self.commit(root, f"work {task8}")
        self.git(self.seed, "pull", "-q", "--rebase", "origin", "main")
        (self.seed / "project" / f"other-{task8}.gd").write_text("o\n", encoding="utf-8")
        self.commit(self.seed, "someone else")
        self.git(self.seed, "fetch", "-q", str(self.main), f"task/{task8}")
        self.git(self.seed, "cherry-pick", "FETCH_HEAD")
        self.git(self.seed, "push", "-q", "origin", "HEAD:main")
        self.git(self.main, "fetch", "-q")
        return root

    def sweep(self):
        return gwt.sweep(self.cfg, lambda: self.store)

    def test_sweep_refuses_a_live_claim_and_an_unlanded_branch_and_removes_a_landed_done_tree(self):
        live = self.task(LIVE)
        self.add(live)
        live_root = self.landed(live)
        self.store.db.execute("UPDATE cards SET status='dropped' WHERE id LIKE ?", (live + "%",))

        unlanded = self.task(LIVE)
        self.add(unlanded)
        un_root = self.base / "product_wt" / unlanded
        (un_root / "project" / "u.gd").write_text("x\n", encoding="utf-8")
        self.commit(un_root, "unlanded")

        done = self.task(LIVE)
        self.add(done)
        done_root = self.landed(done)

        open_task = self.task(LIVE)
        self.add(open_task)
        open_root = self.landed(open_task)

        for task8 in (unlanded, done, open_task):
            claim = self.store.get(task8)["claim_id"]
            self.store.release(task8, claim)
        for task8 in (unlanded, done):
            self.store.set_status(task8, "done")

        reports = self.sweep()
        self.assertEqual({"live": True, "unlanded": True, "done": False, "open": True},
                         {"live": live_root.exists(), "unlanded": un_root.exists(), "done": done_root.exists(),
                          "open": open_root.exists()}, reports)
        branches = self.git(self.main, "branch", "--list", "task/*", "--format=%(refname:short)").split()
        self.assertEqual(sorted(f"task/{t}" for t in (live, unlanded, open_task)), sorted(branches))
        text = "; ".join(reports)
        self.assertIn("claim live", text)
        self.assertIn("not on origin/main", text)
        self.assertIn("task todo", text)

        # The claim's session dies: the dropped, landed tree goes; the unlanded one still stays.
        self.kill(LIVE)
        self.sweep()
        self.assertEqual((False, True), (live_root.exists(), un_root.exists()))

    def test_sweep_keeps_a_done_tree_with_uncommitted_work_and_holds_on_a_mem_fault(self):
        task8 = self.task(LIVE)
        root = Path(self.add(task8)["path"])
        self.store.release(task8, self.store.get(task8)["claim_id"])
        self.store.set_status(task8, "done")
        (root / "project" / "wip.gd").write_text("x\n", encoding="utf-8")
        self.assertIn("uncommitted", "; ".join(self.sweep()))

        def broken():
            raise mem.MemError("db locked")
        self.assertEqual(["task trees held: mem unreadable (db locked)"], gwt.sweep(self.cfg, broken))
        self.assertTrue(root.exists())

    def test_remove_refuses_a_live_claim_and_takes_a_released_clean_tree(self):
        task8 = self.task(LIVE)
        root = Path(self.add(task8)["path"])
        with self.assertRaisesRegex(gwt.Refused, "is live"):
            gwt.remove_task(self.cfg, self.store, task8)
        self.assertTrue(root.exists())
        self.store.release(task8, self.store.get(task8)["claim_id"])
        out = gwt.remove_task(self.cfg, self.store, task8)
        self.assertFalse(root.exists())
        self.assertIn("branch task/" + task8 + " deleted", out["report"])  # no commits: patch-equal

    def test_remove_refuses_an_unknown_claim(self):
        task8 = self.task(LIVE)
        root = Path(self.add(task8)["path"])
        lock = self.lock_of(root)
        import shutil
        shutil.rmtree(self.home / ".claude" / "sessions")  # no sessions folder: liveness unknown
        with self.assertRaisesRegex(gwt.Refused, "is unknown"):
            gwt.remove_task(self.cfg, self.store, task8)
        self.assertEqual((True, lock), (root.exists(), self.lock_of(root)))

    def test_remove_refuses_uncommitted_work_under_a_dead_claim_and_takes_the_clean_tree(self):
        self.alive(DEAD)
        task8 = self.task(DEAD)
        root = Path(self.add(task8)["path"])
        lock = self.lock_of(root)
        (root / "project" / "wip.gd").write_text("x\n", encoding="utf-8")
        self.kill(DEAD)
        self.assertEqual("dead", gwt.claim_live(self.store, self.store.get(task8)))
        with self.assertRaisesRegex(gwt.Refused, "kept: uncommitted project/wip.gd"):
            gwt.remove_task(self.cfg, self.store, task8)
        self.assertEqual("x\n", (root / "project" / "wip.gd").read_text(encoding="utf-8"))
        self.assertEqual(lock, self.lock_of(root))
        (root / "project" / "wip.gd").unlink()
        gwt.remove_task(self.cfg, self.store, task8)  # a dead claim on a clean tree is no guard
        self.assertFalse(root.exists())

    def test_remove_refuses_uncommitted_work_with_no_claim(self):
        task8 = self.task(LIVE)
        root = Path(self.add(task8)["path"])
        self.store.release(task8, self.store.get(task8)["claim_id"])
        (root / "project" / "project.cfg").write_text("changed\n", encoding="utf-8")
        with self.assertRaisesRegex(gwt.Refused, "uncommitted project/project.cfg"):
            gwt.remove_task(self.cfg, self.store, task8)
        self.assertEqual("changed\n", (root / "project" / "project.cfg").read_text(encoding="utf-8"))

    def test_a_failed_remove_relocks_the_tree(self):
        task8 = self.task(LIVE)
        root = Path(self.add(task8)["path"])
        lock = self.lock_of(root)
        self.store.release(task8, self.store.get(task8)["claim_id"])
        real = gwt.git

        def failing(repo, *args, **kw):
            if args[:2] == ("worktree", "remove"):
                return subprocess.CompletedProcess(args, 1, "", "fatal: cannot remove: busy\n")
            return real(repo, *args, **kw)
        with unittest.mock.patch.object(gwt, "git", failing):
            with self.assertRaisesRegex(gwt.Refused, "kept: fatal: cannot remove: busy"):
                gwt.remove_task(self.cfg, self.store, task8)
        self.assertTrue(root.exists())
        self.assertEqual(lock, self.lock_of(root))

    def test_the_cli_add_prints_one_json_line(self):
        task8 = self.task(LIVE)
        with unittest.mock.patch.object(gwt.harness_config, "load", lambda root: self.cfg), \
                unittest.mock.patch.object(gwt, "open_mem", lambda: self.store), \
                unittest.mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": OTHER}), \
                unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(1, gwt.main(["add", "--task", task8]))
            self.assertEqual(0, gwt.main(["add", "--task", task8, "--session", LIVE]))
        lines = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(["REFUSED", "PASS"], [line["result"] for line in lines])


if __name__ == "__main__":
    unittest.main()
