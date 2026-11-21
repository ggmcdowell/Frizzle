"""Tests for examples/godot-pack: the gate, the push gate and the pre-commit hook, run with a stub godot on PATH.

The stub godot prints what STUB_MODE says, as the real one does: a parse error exits 0 with a SCRIPT ERROR line.
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

HUB = Path(__file__).resolve().parent.parent
PACK = HUB / "examples" / "godot-pack"
sys.path.insert(0, str(HUB / "Drawer"))
import review_pass  # noqa: E402
import testgit  # noqa: E402

testgit.arm()  # no git template in any fixture repo
BLOCKED = tomllib.loads((HUB / "harness.toml").read_text(encoding="utf-8"))["smoke"]["blocked_exit"]
GIT = shutil.which("git")
SYSTEM_PATH = testgit.system_path()
STUB = """#!/bin/sh
echo "$*" >> "$STUB_LOG"
case "$*" in
  *--import*) echo "imported"; exit "${STUB_IMPORT_EXIT:-0}" ;;
  *--script*)
    case "${STUB_MODE:-pass}" in
      parse) echo "SCRIPT ERROR: Parse Error: Identifier undefined_call not declared"; echo "ERROR: Failed to load script"; exit 0 ;;
      silent) echo "ran zero checks"; exit 0 ;;
      fail) echo "SMOKE FAIL: arithmetic"; exit 1 ;;
      *) echo "SMOKE PASS"; exit 0 ;;
    esac ;;
esac
exit 0
"""
SPEC = importlib.util.spec_from_file_location("product_push", HUB / "Drawer" / "product-push.py")
product_push = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(product_push)


def git(cwd, *args):
    return subprocess.run([GIT, *args], cwd=str(cwd), check=True, capture_output=True, text=True).stdout.strip()


class PackCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        empty = self.base / "empty.gitconfig"
        empty.write_text("", encoding="utf-8")
        self.bin = self.base / "bin"
        self.bin.mkdir()
        stub = self.bin / "godot"
        stub.write_text(STUB, encoding="utf-8")
        stub.chmod(0o755)
        self.log = self.base / "stub.log"
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k not in ("GODOT", "STUB_MODE")}
        env.update(PATH=f"{self.bin}{os.pathsep}{SYSTEM_PATH}", HOME=str(self.base), GIT_CONFIG_GLOBAL=str(empty),
                   GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid", STUB_LOG=str(self.log),
                   GODOT_APP=str(self.base / "no-such-app"), **testgit.ENV)
        self.env = env
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def make_repo(self, name="product"):
        """A product repo with the pack's gate, hooks, a smoke test and a first commit."""
        repo = self.base / name
        (repo / "tools").mkdir(parents=True)
        (repo / "tests").mkdir()
        (repo / ".githooks").mkdir()
        shutil.copy2(PACK / "gate.sh", repo / "tools" / "gate.sh")
        shutil.copy2(PACK / "hooks" / "pre-push", repo / ".githooks" / "pre-push")
        shutil.copy2(PACK / "hooks" / "pre-commit", repo / ".githooks" / "pre-commit")
        (repo / "tests" / "smoke_test.gd").write_text("extends SceneTree\n", encoding="utf-8")
        (repo / "project.godot").write_text("config_version=5\n", encoding="utf-8")
        (repo / ".gitignore").write_text(".godot/\n.gate-verdict\n", encoding="utf-8")
        git(self.base, "init", "-q", "-b", "main", str(repo))
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "base")
        return repo

    def gate(self, repo, mode="pass", env=None, project="."):
        run_env = {**self.env, "STUB_MODE": mode, **(env or {})}
        return subprocess.run(["sh", "tools/gate.sh", project], cwd=str(repo), env=run_env, capture_output=True,
                              text=True, timeout=60)

    def verdict(self, repo):
        path = repo / ".gate-verdict"
        return path.read_text(encoding="utf-8").strip() if path.exists() else None


class GateTests(PackCase):
    def test_a_green_run_exits_0_and_writes_the_verdict_naming_the_head_tree(self):
        repo = self.make_repo()
        done = self.gate(repo)
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertEqual(git(repo, "rev-parse", "HEAD^{tree}"), self.verdict(repo))

    def test_a_parse_error_that_exits_0_is_red(self):
        # Defect: gate.sh exec'd godot, so a script parse error (exit 0) passed the gate.
        repo = self.make_repo()
        done = self.gate(repo, "parse")
        self.assertEqual(1, done.returncode, done.stdout + done.stderr)
        self.assertIn("SCRIPT ERROR", done.stderr)
        self.assertEqual("", self.verdict(repo))

    def test_a_test_that_prints_no_smoke_pass_line_is_red(self):
        repo = self.make_repo()
        done = self.gate(repo, "silent")
        self.assertEqual(1, done.returncode, done.stdout + done.stderr)
        self.assertIn("SMOKE PASS", done.stderr)
        self.assertEqual("", self.verdict(repo))

    def test_a_failing_test_is_red(self):
        done = self.gate(self.make_repo(), "fail")
        self.assertEqual(1, done.returncode)

    def test_a_missing_test_file_is_red_and_runs_no_godot(self):
        repo = self.make_repo()
        (repo / "tests" / "smoke_test.gd").unlink()
        done = self.gate(repo)
        self.assertEqual(1, done.returncode, done.stdout + done.stderr)
        self.assertIn("smoke_test.gd is missing", done.stderr)
        self.assertFalse(self.log.exists())

    def test_a_failed_import_is_red(self):
        done = self.gate(self.make_repo(), env={"STUB_IMPORT_EXIT": "3"})
        self.assertEqual(1, done.returncode)

    def test_no_godot_exits_with_the_blocked_code_not_red(self):
        # Defect: the missing-Godot exit was 2, which Drawer/smoke.py reads as red.
        repo = self.make_repo()
        done = self.gate(repo, env={"PATH": SYSTEM_PATH})
        self.assertEqual(BLOCKED, done.returncode, done.stdout + done.stderr)
        self.assertIn("brew install --cask godot", done.stderr)

    def test_godot_names_the_binary_before_path(self):
        repo = self.make_repo()
        other = self.base / "other-godot"
        other.write_text("#!/bin/sh\necho OTHER >> \"$STUB_LOG\"\necho SMOKE PASS\n", encoding="utf-8")
        other.chmod(0o755)
        self.assertEqual(0, self.gate(repo, env={"GODOT": str(other)}).returncode)
        self.assertIn("OTHER", self.log.read_text(encoding="utf-8"))

    def test_tracked_changes_get_no_verdict_and_a_red_run_clears_the_old_one(self):
        repo = self.make_repo()
        self.assertEqual(0, self.gate(repo).returncode)
        (repo / "project.godot").write_text("config_version=5\n; changed\n", encoding="utf-8")
        self.assertEqual(0, self.gate(repo).returncode)  # green, but the tree is not HEAD's
        self.assertEqual("", self.verdict(repo))
        git(repo, "commit", "-qam", "change")
        self.assertEqual(0, self.gate(repo).returncode)
        self.assertEqual(git(repo, "rev-parse", "HEAD^{tree}"), self.verdict(repo))
        self.assertEqual(1, self.gate(repo, "parse").returncode)
        self.assertEqual("", self.verdict(repo))


class PushHookTests(PackCase):
    def hook(self, repo, line):
        return subprocess.run(["sh", ".githooks/pre-push", "origin"], cwd=str(repo), env=self.env, input=line,
                              capture_output=True, text=True, timeout=60)

    def test_the_hook_passes_a_push_the_verdict_covers_and_refuses_any_other(self):
        repo = self.make_repo()
        head = git(repo, "rev-parse", "HEAD")
        line = f"refs/heads/main {head} refs/heads/main {'0' * 40}\n"
        refused = self.hook(repo, line)
        self.assertEqual(1, refused.returncode)
        self.assertIn("no green gate verdict", refused.stderr)
        self.assertEqual(0, self.gate(repo).returncode)
        self.assertEqual(0, self.hook(repo, line).returncode)
        (repo / "a.txt").write_text("a\n", encoding="utf-8")
        git(repo, "add", "a.txt")
        git(repo, "commit", "-qm", "later")
        newer = git(repo, "rev-parse", "HEAD")
        self.assertEqual(1, self.hook(repo, f"refs/heads/main {newer} refs/heads/main {head}\n").returncode)

    def test_the_hook_passes_a_ref_delete_and_an_empty_push(self):
        repo = self.make_repo()
        self.assertEqual(0, self.hook(repo, f"(delete) {'0' * 40} refs/heads/x {'1' * 40}\n").returncode)
        self.assertEqual(0, self.hook(repo, "").returncode)

    def test_product_push_runs_the_gate_and_pushes_a_pack_repo(self):
        # The verdict format: gate.sh writes it, the hook reads it, product-push lands the change.
        repo = self.make_repo()
        origin = self.base / "origin.git"
        git(self.base, "init", "-q", "--bare", "-b", "main", str(origin))
        git(repo, "remote", "add", "origin", str(origin))
        git(repo, "push", "-q", "origin", "main")
        wt = self.base / "product_wt" / "t1"
        git(repo, "worktree", "add", "-q", "-b", "task/t1", str(wt), "origin/main")
        cfg = {"project": {"state_root": str(self.base / "state")}, "product": {"gate_command": "sh tools/gate.sh ."}}

        def push(*args):
            out = io.StringIO()
            with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "0a1b2c3d-0000-4000-8000-000000000000"}), \
                    mock.patch.object(review_pass, "task_tags", lambda *_: []), \
                    contextlib.redirect_stdout(out):
                code = product_push.main([str(wt), *args], cfg)
            return code, json.loads(out.getvalue().strip().splitlines()[-1])

        (wt / "a.txt").write_text("a\n", encoding="utf-8")
        git(wt, "add", "a.txt")
        git(wt, "commit", "-qm", "a")
        code, row = push()
        self.assertEqual((0, "pushed"), (code, row["state"]), row)
        self.assertTrue(row["gated"])
        self.assertEqual(git(origin, "rev-parse", "main"), git(wt, "rev-parse", "HEAD"))
        (wt / "tests" / "smoke_test.gd").write_text("extends SceneTree\n# broken\n", encoding="utf-8")
        git(wt, "commit", "-qam", "b")
        with mock.patch.dict(os.environ, {"STUB_MODE": "parse"}):
            code, row = push()
        self.assertEqual((1, "failed", "gate"), (code, row["state"], row["step"]), row)
        self.assertNotEqual(git(origin, "rev-parse", "main"), git(wt, "rev-parse", "HEAD"))


class PreCommitTests(PackCase):
    def commit_gd(self, env):
        repo = self.make_repo()
        git(repo, "config", "core.hooksPath", ".githooks")
        (repo / "a.gd").write_text("extends Node\n", encoding="utf-8")
        git(repo, "add", "a.gd")
        return subprocess.run([GIT, "commit", "-qm", "a"], cwd=str(repo), env=env, capture_output=True, text=True,
                              timeout=60)

    def test_a_missing_gdformat_is_said_with_the_install_command_and_the_commit_goes_on(self):
        done = self.commit_gd(self.env)
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn("gdformat is not installed", done.stderr)
        self.assertIn("run uv sync in the hub (uv.lock pins gdtoolkit)", done.stderr)

    def test_gdformat_on_path_formats_the_staged_file(self):
        fmt = self.bin / "gdformat"
        fmt.write_text("#!/bin/sh\nfor f in \"$@\"; do echo '# formatted' >> \"$f\"; done\n", encoding="utf-8")
        fmt.chmod(0o755)
        done = self.commit_gd(self.env)
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertNotIn("not installed", done.stderr)


class PackFilesTests(unittest.TestCase):
    def test_the_conventions_name_no_removed_editor_tool_and_no_ship_rule(self):
        # Defect: ENG-2..6 and ENG-24/25 named editor tools and a ship path the pack does not ship.
        text = (PACK / "GODOT-CONVENTIONS.md").read_text(encoding="utf-8")
        for removed in ("gdscript_eval", "asset_move", "asset_reimport", "ENG-24", "ENG-25", "export_presets"):
            self.assertNotIn(removed, text)

    def test_the_readme_points_to_no_unpublished_tag(self):
        # Defect: godot-v0 is local only and holds the pre-squash history; a clone of origin has no such tag.
        for name in ("README.md", "GODOT-CONVENTIONS.md", "harness.pack.toml"):
            self.assertNotIn("godot-v0", (PACK / name).read_text(encoding="utf-8"))

    def test_the_readmes_name_the_one_command_in_place_of_manual_copy_steps(self):
        for text in ((PACK / "README.md").read_text(encoding="utf-8"), (HUB / "README.md").read_text(encoding="utf-8")):
            self.assertIn("Drawer/product-init.py", text)
        self.assertIn("--pack godot", (PACK / "README.md").read_text(encoding="utf-8"))
        self.assertIn("--pack godot", (HUB / "README.md").read_text(encoding="utf-8"))
        self.assertNotIn("Copy `gate.sh`", (PACK / "README.md").read_text(encoding="utf-8"))

    def test_the_pack_lines_run_the_pack_gate_and_the_core_documents_the_verdict(self):
        lines = tomllib.loads((PACK / "harness.pack.toml").read_text(encoding="utf-8"))["product"]
        self.assertEqual("sh tools/gate.sh .", lines["gate_command"])
        self.assertNotIn("mcp_url", lines)
        self.assertIn(".gate-verdict", (HUB / "PRODUCT-CONVENTIONS.md").read_text(encoding="utf-8"))
        self.assertTrue(os.access(PACK / "hooks" / "pre-push", os.X_OK))

    def test_the_hub_chains_a_product_pre_commit(self):
        self.assertTrue(os.access(HUB / "Drawer" / "product-hooks" / "pre-commit", os.X_OK))


if __name__ == "__main__":
    unittest.main()
