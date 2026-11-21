"""Tests for Drawer/review-diff.py on a bare-origin fixture: a stable hash over every changed file."""
import importlib.util
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
import testgit  # noqa: E402

testgit.arm()  # no git template in any fixture repo
SPEC = importlib.util.spec_from_file_location("review_diff", HERE / "review-diff.py")
review_diff = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review_diff)


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True).stdout.strip()


class ProductReviewDiffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.config = base / "user.gitconfig"
        self.config.write_text("", encoding="utf-8")
        env = {"GIT_CONFIG_GLOBAL": str(self.config), "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
               "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@example.invalid"}
        patch = mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)
        self.state = base / "state"
        origin = base / "origin.git"
        git(base, "init", "-q", "--bare", "-b", "main", str(origin))
        self.repo = base / "product"
        git(base, "clone", "-q", str(origin), str(self.repo))
        git(self.repo, "checkout", "-q", "-b", "main")
        self.commit("unit.gd", "var hp = 10\n", "base")
        git(self.repo, "push", "-q", "origin", "main")

    def commit(self, name, text, message="change"):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        git(self.repo, "add", name)
        git(self.repo, "commit", "-qm", message)

    def build(self):
        return review_diff.build(self.repo, self.state)

    def test_no_change_gives_none_and_each_former_churn_file_is_in_the_diff(self):
        self.assertIsNone(self.build())
        names = ["art/hero.png.import", ".cache/editor/cache.cfg", "README.md", ".claude/notes.txt.md.import",
                 "docs/plan.txt"]
        for name in names:
            self.commit(name, "x %s\n" % name)
        found = self.build()
        self.assertEqual(found["files"], sorted(names))
        diff = Path(found["diffPath"]).read_text(encoding="utf-8")
        for name in names:
            self.assertIn("+x %s" % name, diff)

    def test_line_names_the_change_and_writes_the_diff_under_state_root(self):
        self.commit("unit.gd", "var hp = 12\n")
        found = self.build()
        head = git(self.repo, "rev-parse", "HEAD")
        self.assertEqual((Path(found["repo"]), found["head"], found["base"], found["files"]),
                         (self.repo, head, git(self.repo, "rev-parse", "origin/main"), ["unit.gd"]))
        diff = Path(found["diffPath"])
        self.assertEqual(diff, self.state / "review" / (head[:12] + ".diff"))
        self.assertIn("+var hp = 12", diff.read_text(encoding="utf-8"))
        self.assertRegex(found["hash"], r"^[0-9a-f]{64}$")
        json.dumps(found)

    def test_hash_is_stable_under_user_diff_settings(self):
        lines = ["line %d\n" % n for n in range(40)]
        self.commit("long.gd", "".join(lines))
        self.commit("zz.gd", "a\n")
        git(self.repo, "push", "-q", "origin", "main")
        lines[10], lines[25] = "ten\n", "twenty-five\n"
        self.commit("long.gd", "".join(lines))
        self.commit("zz.gd", "b\n")
        self.commit("unit.gd", "var hp = 12\n")
        plain = self.build()["hash"]
        order = self.repo.parent / "order.txt"
        order.write_text("zz.gd\n", encoding="utf-8")
        self.config.write_text("[diff]\n\tnoprefix = true\n\tmnemonicPrefix = true\n\trenames = copies\n"
                               "\tcontext = 9\n\tinterHunkContext = 20\n\talgorithm = patience\n"
                               "\tindentHeuristic = false\n\torderFile = %s\n[color]\n\tui = always\n"
                               "[core]\n\tquotePath = false\n" % order.as_posix(), encoding="utf-8")
        self.assertNotEqual(git(self.repo, "diff", "origin/main", "HEAD").split("\n", 1)[0],
                            "diff --git a/long.gd b/long.gd")
        self.assertIn("noprefix", git(self.repo, "config", "--list"))
        self.assertEqual(self.build()["hash"], plain)

    def test_each_file_added_after_review_moves_the_hash(self):
        self.commit("unit.gd", "var hp = 12\n")
        reviewed = self.build()["hash"]
        for name in ("art/hero.png.import", ".cache/imported/hero.ctex", "notes/plan.md", "docs/plan.txt",
                     "enemy.gd"):
            self.commit(name, "late\n")
            found = self.build()
            self.assertIn(name, found["files"])
            self.assertNotEqual(found["hash"], reviewed, name)
            reviewed = found["hash"]

    def test_art_markdown_is_in_the_review_diff(self):
        # A review diff left out Art/MANUAL.md and two more Art/*.md files.
        self.commit("unit.gd", "var hp = 12\n")
        names = ("Art/MANUAL.md", "Art/flux/MANUAL.md", "Art/openai/MANUAL.md")
        for name in names:
            self.commit(name, "art rule %s\n" % name)
        found = self.build()
        self.assertEqual(found["files"], sorted(("unit.gd",) + names))
        diff = Path(found["diffPath"]).read_text(encoding="utf-8")
        for name in names:
            self.assertIn("+art rule %s" % name, diff)

    def test_law_files_and_claude_markdown_stay_in_the_hash(self):
        self.commit("unit.gd", "var hp = 12\n")
        reviewed = self.build()["hash"]
        for name in ("AGENTS.md", ".claude/skills/x/SKILL.md", ".claude/skills/x/ref.md", "product/PRODUCT-CONVENTIONS.md"):
            self.commit(name, "a rule\n")
            found = self.build()
            self.assertIn(name, found["files"])
            self.assertNotEqual(found["hash"], reviewed, name)
            reviewed = found["hash"]

    def test_a_markdown_file_turned_folder_holds_both_paths(self):
        self.commit("a.md", "notes\n")
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "rm", "-q", "a.md")
        self.commit("a.md/b.gd", "var x = 1\n")
        found = self.build()
        self.assertEqual(found["files"], ["a.md", "a.md/b.gd"])
        diff = Path(found["diffPath"]).read_text(encoding="utf-8")
        self.assertIn("-notes", diff)
        self.assertIn("+var x = 1", diff)

    def test_a_lone_cr_stays_in_the_diff(self):
        self.commit("unit.gd", "x\r@@ -1 +1 @@ hidden\n")
        self.assertIn(b"x\r@@ -1 +1 @@ hidden", Path(self.build()["diffPath"]).read_bytes())

    def test_hash_is_stable_under_user_attributes_and_from_a_subfolder(self):
        self.commit("src/unit.gd", "\tvar z = 0\n\tvar a = 1\n\tvar b = 2\n\tvar c = 3\n\tvar d = 4\n")
        git(self.repo, "push", "-q", "origin", "main")
        self.commit("src/unit.gd", "\tvar z = 0\n\tvar a = 1\n\tvar b = 2\n\tvar c = 3\n\tvar d = 5\n")
        plain = self.build()["hash"]
        default = git(self.repo, "diff", "origin/main", "HEAD")
        attrs = self.repo.parent / "attributes"
        attrs.write_text("*.gd diff=z\n", encoding="utf-8")
        self.config.write_text("[core]\n\tattributesFile = %s\n[diff \"z\"]\n\txfuncname = ^(.*)$\n"
                               "[diff]\n\tsubmodule = log\n" % attrs.as_posix(), encoding="utf-8")
        self.assertNotEqual(git(self.repo, "diff", "origin/main", "HEAD"), default)
        found = review_diff.build(self.repo / "src", self.state)
        self.assertEqual((found["hash"], Path(found["repo"])), (plain, self.repo))
        (self.repo / ".git" / "info").mkdir(exist_ok=True)
        (self.repo / ".git" / "info" / "attributes").write_text("*.gd diff=z\n", encoding="utf-8")
        self.config.write_text("[diff \"z\"]\n\txfuncname = ^(.*)$\n", encoding="utf-8")
        self.assertNotEqual(git(self.repo, "diff", "origin/main", "HEAD"), default)
        self.assertEqual(self.build()["hash"], plain)

    def test_cli_help_and_bad_arguments(self):
        run = subprocess.run([sys.executable, "-B", str(HERE / "review-diff.py"), "--help"],
                             capture_output=True, text=True)
        self.assertEqual(run.returncode, 0)
        self.assertIn("--reviewed", run.stdout)
        run = subprocess.run([sys.executable, "-B", str(HERE / "review-diff.py")], capture_output=True, text=True)
        self.assertEqual(run.returncode, 2)


if __name__ == "__main__":
    unittest.main()
