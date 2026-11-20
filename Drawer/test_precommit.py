"""Tests for the pre-commit hook: the secret scanner and the name check block a commit.

Fake keys are built at run time, so this file holds no key-shaped string. Every name is invented.
"""
import json
import os
import random
import shutil
import string
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HUB = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HUB / "Drawer"))
import testgit  # noqa: E402

HAVE_GITLEAKS = shutil.which("gitleaks") is not None


def fake(prefix, n, alpha=string.ascii_letters + string.digits):
    rng = random.Random(prefix)
    return prefix + "".join(rng.choice(alpha) for _ in range(n))


FAKE_KEYS = {
    "openai-project": fake("sk-" + "proj-", 120, string.ascii_letters + string.digits + "_-"),
    "anthropic": fake("sk-" + "ant-" + "api03-", 93, string.ascii_letters + string.digits + "_-") + "AA",
    "github": fake("ghp" + "_", 36),
    "aws": fake("AKI" + "A", 16, string.ascii_uppercase + "234567"),
}


@unittest.skipUnless(HAVE_GITLEAKS, "gitleaks is not installed")
class Scanner(unittest.TestCase):
    def scan(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp, "scratch.txt")
            target.write_text(text)
            return subprocess.run(["gitleaks", "dir", "--no-banner", "--redact", "-l", "error",
                                   "-c", str(HUB / ".gitleaks.toml"), str(target)], capture_output=True).returncode

    def test_each_key_form_is_caught(self):
        for kind, key in FAKE_KEYS.items():
            self.assertEqual(self.scan(f"value = {key}\n"), 1, kind)

    def test_clean_text_passes(self):
        self.assertEqual(self.scan("value = nothing secret\n"), 0)


@unittest.skipUnless(HAVE_GITLEAKS, "gitleaks is not installed")
class Hook(unittest.TestCase):
    """A scratch repo with this hub's hook, config and checker, and an invented deny list."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name, "hub")
        (self.repo / "Drawer" / "git-hooks").mkdir(parents=True)
        for rel in ("harness.toml", ".gitleaks.toml", "Drawer/harness_config.py", "Drawer/name-check.py", "Drawer/rules_check.py", "Drawer/rules_audit.py",
                    "Drawer/git-hooks/pre-commit", "Drawer/git-hooks/bound-gate.sh"):
            shutil.copy2(HUB / rel, self.repo / rel)
        marker = subprocess.run([sys.executable, "-B", str(HUB / "Drawer/harness_config.py"),
                                 "--get", "project.marker_file"], capture_output=True, text=True, cwd=HUB).stdout.strip()
        (self.repo / marker).touch()
        self.deny = Path(self.tmp.name, "deny.txt")
        self.deny.write_text("zorblax corp\n")
        (self.repo / "harness.local.toml").write_text(f'[names]\ndenylist = "{self.deny}"\n')
        (self.repo / ".gitignore").write_text("harness.local.toml\n")
        self.git("init", "-q", "-b", "main")
        self.git("config", "core.hooksPath", "Drawer/git-hooks")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        # Only what git and the hook read.
        env = {name: os.environ[name] for name in ("PATH", "HOME", "SYSTEMROOT") if name in os.environ}
        return subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True,
                              env={**env, **testgit.ENV})

    def commit(self, text):
        Path(self.repo, "note.txt").write_text(text)
        self.git("add", "-A")
        return self.git("commit", "-q", "-m", "test")

    def test_clean_commit_passes(self):
        self.assertEqual(self.commit("plain words\n").returncode, 0)

    def test_secret_blocks_commit(self):
        result = self.commit(f"key = {FAKE_KEYS['github']}\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("gitleaks found a secret", result.stderr)

    def test_denied_name_blocks_commit(self):
        result = self.commit("made by Zorblax Corp\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("name-check", result.stderr)

    def old_hit(self):
        """A file that landed with a name before its rule existed."""
        self.deny.write_text("quuxware\n")
        Path(self.repo, "old.txt").write_text("line one\nmade by Zorblax Corp\nline three\n")
        self.git("add", "-A")
        self.assertEqual(self.git("commit", "-q", "-m", "old").returncode, 0)
        self.deny.write_text("zorblax corp\n")

    def test_an_unrelated_commit_passes_beside_an_old_hit(self):
        self.old_hit()
        result = self.commit("plain words\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        Path(self.repo, "old.txt").write_text("line one\nmade by Zorblax Corp\nline three, edited\n")
        self.git("add", "-A")
        result = self.git("commit", "-q", "-m", "edit beside the old hit")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_new_name_beside_an_old_hit_is_refused(self):
        self.old_hit()
        Path(self.repo, "old.txt").write_text("line one\nmade by Zorblax Corp\nline three\nalso Zorblax-Corp\n")
        self.git("add", "-A")
        result = self.git("commit", "-q", "-m", "new name")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("old.txt:4: matches", result.stderr)
        self.assertNotIn("old.txt:2", result.stderr)

    def test_a_new_file_named_with_a_denied_name_is_refused(self):
        Path(self.repo, "zorblax_corp_notes.txt").write_text("plain words\n")
        self.git("add", "-A")
        result = self.git("commit", "-q", "-m", "named file")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("zorblax_corp_notes.txt: path matches", result.stderr)

    def test_the_audit_still_finds_an_old_hit(self):
        self.old_hit()
        env = {k: v for k, v in os.environ.items() if k != "HARNESS_NO_LOCAL_TOML"}  # it reads the local denylist
        result = subprocess.run([sys.executable, "-B", "Drawer/name-check.py", "--all"], cwd=self.repo,
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("old.txt:2: matches", result.stderr)

    def test_a_claude_instruction_file_blocks_commit(self):
        Path(self.repo, "CLAUDE.local.md").write_text("@AGENTS.md\n")
        result = self.commit("plain words\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pre-commit: CLAUDE.local.md is a Claude instruction file.", result.stderr)

    def failing_conform(self, bind):
        """A copied commit gate that always fails, and the [gates] bind switch."""
        Path(self.repo, "Drawer", "harness-conform.py").write_text("import sys\nsys.exit(3)\n")
        local = self.repo / "harness.local.toml"
        local.write_text(local.read_text() + f"[gates]\nbind = {'true' if bind else 'false'}\n")

    def test_unbound_gate_failure_reports_only(self):
        self.failing_conform(bind=False)
        result = self.commit("plain words\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("harness-conform.py failed (rc 3); report only", result.stderr)

    def test_bound_gate_failure_blocks_commit(self):
        self.failing_conform(bind=True)
        result = self.commit("plain words\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("report only", result.stderr)

    def test_empty_deny_list_blocks_commit(self):
        self.deny.write_text("# nothing\n")
        result = self.commit("plain words\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("deny list is empty", result.stderr)


@unittest.skipUnless(HAVE_GITLEAKS, "gitleaks is not installed")
class FullHub(unittest.TestCase):
    """A scratch copy of every tracked hub file with the real hook and commit gate:
    a form finding warns and commits, a settings drift commits rendered, and the hard checks refuse."""

    @classmethod
    def setUpClass(cls):
        # Each tracked file's blob, packed once: a case's git add finds it there and writes no loose object.
        cls.seed_tmp = tempfile.TemporaryDirectory()
        cls.tracked = [rel for rel in subprocess.run(["git", "ls-files", "-z"], cwd=HUB, capture_output=True, text=True,
                                                     check=True).stdout.split("\0") if rel and (HUB / rel).is_file()]
        cls.seed = testgit.seed(Path(cls.seed_tmp.name), [HUB / rel for rel in cls.tracked])

    @classmethod
    def tearDownClass(cls):
        cls.seed_tmp.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name, "hub")
        for rel in self.tracked:
            if (HUB / rel).is_file():
                (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(HUB / rel, self.repo / rel)
        venv = (HUB / ".venv").resolve()
        if venv.is_dir():
            (self.repo / ".venv").symlink_to(venv, target_is_directory=True)
        marker = subprocess.run([sys.executable, "-B", str(HUB / "Drawer/harness_config.py"),
                                 "--get", "project.marker_file"], capture_output=True, text=True, cwd=HUB).stdout.strip()
        (self.repo / marker).touch()
        deny = Path(self.tmp.name, "deny.txt")
        deny.write_text("zorblax corp\n")
        (self.repo / "harness.local.toml").write_text(f'[names]\ndenylist = "{deny}"\n')
        self.git("init", "-q", "-b", "main")
        testgit.borrow(self.repo / ".git", self.seed)
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("add", "-A")
        self.assertEqual(0, self.git("commit", "-q", "-m", "fixture").returncode)
        self.git("config", "core.hooksPath", "Drawer/git-hooks")
        # A bound checkout, so the branch policy passes on main.
        self.git("config", "hub.bound", "true")

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        env = {name: os.environ[name] for name in ("PATH", "HOME", "SYSTEMROOT", "TMPDIR") if name in os.environ}
        return subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True,
                              env={**env, **testgit.ENV})

    def commit(self, *paths):
        self.git("add", "--", *paths)
        return self.git("commit", "-q", "-m", "test")

    def test_a_staged_dead_pointer_commits_with_a_warning(self):
        manual = self.repo / ".claude/agents/reviewer.md"
        manual.write_text(manual.read_text(encoding="utf-8") + "\nSee `Drawer/no-such-tool.py`.\n", encoding="utf-8")
        result = self.commit(".claude/agents/reviewer.md")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        out = result.stdout + result.stderr
        self.assertIn("[WARN] pointers", out)
        self.assertIn(".claude/agents/reviewer.md", out)
        self.assertIn("-> Drawer/no-such-tool.py", out)
        self.assertEqual("", self.git("status", "--porcelain", "--", ".claude/agents/reviewer.md").stdout)

    def test_a_manifest_edit_with_no_render_commits_with_settings_staged(self):
        manifest = self.repo / "Drawer/settings-manifest.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["common"]["permissions"].setdefault("deny", []).append("mcp__zz_test_only")
        manifest.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        result = self.commit("Drawer/settings-manifest.json")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("settings drift fixed", result.stdout + result.stderr)
        shown = self.git("show", "--name-only", "--format=", "HEAD").stdout.split()
        self.assertIn(".claude/settings.json", shown)
        self.assertIn("mcp__zz_test_only", self.git("show", "HEAD:.claude/settings.json").stdout)
        # the working tree and the index hold the render too: nothing left to commit or revert
        self.assertEqual("", self.git("status", "--porcelain", "--", ".claude/settings.json").stdout)

    def test_a_path_commit_of_a_manifest_edit_stages_the_render_in_the_real_index(self):
        """git commit -- <paths> builds a temporary index; the real one Git writes back takes the render too."""
        manifest = self.repo / "Drawer/settings-manifest.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["common"]["permissions"].setdefault("deny", []).append("mcp__zz_path_only")
        manifest.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        result = self.git("commit", "-q", "-m", "path commit", "--", "Drawer/settings-manifest.json")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn(".claude/settings.json", self.git("show", "--name-only", "--format=", "HEAD").stdout.split())
        self.assertIn("mcp__zz_path_only", self.git("show", "HEAD:.claude/settings.json").stdout)
        self.assertEqual("", self.git("status", "--porcelain").stdout)

    def test_a_planted_secret_still_refuses(self):
        Path(self.repo, "note.txt").write_text(f"key = {FAKE_KEYS['github']}\n")
        result = self.commit("note.txt")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("gitleaks found a secret", result.stderr)

    def test_a_101_line_agents_md_still_refuses(self):
        agents = self.repo / "AGENTS.md"
        lines = agents.read_text(encoding="utf-8").splitlines()
        lines += [f"- filler line {n}" for n in range(101 - len(lines))]
        agents.write_text("\n".join(lines) + "\n", encoding="utf-8")
        result = self.commit("AGENTS.md")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("101", result.stdout + result.stderr)
        self.assertEqual("1", self.git("rev-list", "--count", "HEAD").stdout.strip())


if __name__ == "__main__":
    unittest.main()
