"""Tests for the product-repo hooks in Drawer/product-hooks: the pre-push gate fails closed.

Each test binds a temp product clone (with a bare origin) to a temp hub that holds this checkout's hooks, harness.toml,
its marker file and Drawer/harness_config.py, with [product] gate_command set in a harness.local.toml.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

HUB = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HUB / "Drawer"))
import oscompat  # noqa: E402
import testgit  # noqa: E402

testgit.arm()  # no git template in any fixture repo
GIT = shutil.which("git")
BASE_PATH = testgit.system_path()


class ProductHooks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.hub = root / "hub"
        (self.hub / "Drawer").mkdir(parents=True)
        shutil.copy2(HUB / "harness.toml", self.hub / "harness.toml")
        shutil.copy2(HUB / "Drawer/harness_config.py", self.hub / "Drawer/harness_config.py")
        shutil.copy2(HUB / "Drawer/oscompat.py", self.hub / "Drawer/oscompat.py")
        shutil.copytree(HUB / "Drawer/product-hooks", self.hub / "Drawer/product-hooks")
        marker = tomllib.loads((HUB / "harness.toml").read_text())["project"]["marker_file"]
        (self.hub / marker).write_text("")
        py = self.hub / ".venv/bin/python"
        py.parent.mkdir(parents=True)
        py.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        py.chmod(0o755)
        self.bin = root / "bin"
        self.bin.mkdir()
        # The hooks read the temp hub's harness.local.toml on purpose: drop the runners' skip.
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k != "HARNESS_NO_LOCAL_TOML"}
        self.env.update(PATH=BASE_PATH, HOME=str(root), GIT_CONFIG_NOSYSTEM="1",
                        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid", **testgit.ENV)
        self.origin = root / "origin.git"
        self.repo = root / "product"
        self.git("init", "-q", "--bare", "-b", "main", str(self.origin), cwd=root)
        self.git("init", "-q", "-b", "main", str(self.repo), cwd=root)
        self.git("config", "core.hooksPath", str(self.hub / "Drawer/product-hooks"))
        self.git("remote", "add", "origin", str(self.origin))
        (self.repo / "README").write_text("product\n")
        self.git("add", "README")
        self.git("commit", "-q", "-m", "first")

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args, cwd=None, env=None, check=True):
        out = subprocess.run([GIT, *args], cwd=cwd or self.repo, env=env or self.env, capture_output=True,
                             text=True, timeout=60)
        if check and out.returncode:
            self.fail(f"git {' '.join(args)} exit {out.returncode}: {out.stdout}{out.stderr}")
        return out

    def path_seen(self):
        """The folders of the PATH a hook echoed. Git's sh writes them as POSIX paths joined by colons: on Windows
        cygpath (beside sh.exe) turns them into the Windows paths the fixture folders have."""
        text = (self.repo / ".git/path-seen").read_text().strip()
        if oscompat.WINDOWS:
            cygpath = Path(oscompat.git_shell("sh")).with_name("cygpath.exe")
            text = subprocess.run([str(cygpath), "-w", "-p", text], capture_output=True, text=True, check=True).stdout.strip()
        return text.split(os.pathsep)

    def gate_command(self, value):
        (self.hub / "harness.local.toml").write_text(f'[product]\ngate_command = "{value}"\n')

    def gate(self, body):
        hook = self.repo / ".githooks/pre-push"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text(body)
        hook.chmod(0o755)
        self.git("add", ".githooks/pre-push")
        self.git("commit", "-q", "-m", "gate")

    def push(self):
        return self.git("push", "-q", "origin", "HEAD:main", check=False)

    def origin_head(self):
        return self.git("rev-parse", "--verify", "-q", "main", cwd=self.origin, check=False).stdout.strip()

    # pre-push

    def test_push_refused_when_gate_command_is_set_and_the_gate_is_missing(self):
        self.gate_command("tools/gate.sh")
        out = self.push()
        self.assertEqual(out.returncode, 1, out.stderr)
        self.assertIn(".githooks/pre-push is missing", out.stderr)
        self.assertEqual(self.origin_head(), "")

    def test_push_passes_through_a_green_gate_that_gets_the_push_input(self):
        self.gate_command("tools/gate.sh")
        self.gate('#!/bin/sh\ncat > "$(git rev-parse --show-toplevel)/.git/gate-input"\necho "$1" >> "$(git rev-parse --show-toplevel)/.git/gate-input"\n')
        out = self.push()
        self.assertEqual(out.returncode, 0, out.stderr)
        head = self.git("rev-parse", "HEAD").stdout.strip()
        self.assertEqual(self.origin_head(), head)
        seen = (self.repo / ".git/gate-input").read_text()
        self.assertIn(head, seen)
        self.assertIn("refs/heads/main", seen)
        self.assertTrue(seen.rstrip().endswith("origin"), seen)

    def test_push_refused_by_a_red_gate(self):
        self.gate_command("tools/gate.sh")
        self.gate("#!/bin/sh\necho red leg >&2\nexit 1\n")
        out = self.push()
        self.assertEqual(out.returncode, 1)
        self.assertIn("red leg", out.stderr)
        self.assertIn("product push gate", out.stderr)
        self.assertEqual(self.origin_head(), "")

    def test_a_python_gate_runs_under_the_hub_python(self):
        self.gate_command("")
        self.gate("#!/usr/bin/env python3-not-here\nimport sys\nsys.exit(0 if sys.flags.dont_write_bytecode and 'refs/heads/main' in sys.stdin.read() else 1)\n")
        out = self.push()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotEqual(self.origin_head(), "")

    def test_push_refused_when_the_config_cannot_be_read(self):
        (self.hub / "harness.local.toml").write_text("[smoke\n")
        out = self.push()
        self.assertEqual(out.returncode, 1, out.stderr)
        self.assertIn("cannot read [product] gate_command", out.stderr)
        self.assertEqual(self.origin_head(), "")

    def test_push_passes_with_one_line_when_no_gate_is_set_up(self):
        self.gate_command("")
        out = self.push()
        self.assertEqual(out.returncode, 0, out.stderr)
        lines = [line for line in out.stderr.splitlines() if line.startswith("pre-push:")]
        self.assertEqual(len(lines), 1, out.stderr)
        self.assertIn("no product push gate set up", lines[0])
        self.assertNotEqual(self.origin_head(), "")

    # pre-commit

    def test_the_repo_pre_commit_finds_the_hub_environment_first_on_path(self):
        hook = self.repo / ".githooks/pre-commit"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text('#!/bin/sh\necho "$PATH" > "$(git rev-parse --show-toplevel)/.git/path-seen"\n')
        hook.chmod(0o755)
        (self.repo / "a.txt").write_text("a\n")
        self.git("add", "a.txt")
        self.assertEqual(self.git("commit", "-q", "-m", "a", check=False).returncode, 0)
        first = self.path_seen()[0]
        self.assertEqual(os.path.realpath(self.hub / ".venv/bin"), os.path.realpath(first))

    def test_the_hub_scripts_folder_follows_the_bin_folder_on_path(self):
        # Windows holds the pinned product tools (gdformat) in .venv/Scripts.
        hook = self.repo / ".githooks/pre-commit"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text('#!/bin/sh\necho "$PATH" > "$(git rev-parse --show-toplevel)/.git/path-seen"\n')
        (self.repo / "a.txt").write_text("a\n")
        self.git("add", "a.txt")
        self.assertEqual(self.git("commit", "-q", "-m", "a", check=False).returncode, 0)
        seen = [os.path.realpath(p) for p in self.path_seen()[:2]]
        self.assertEqual([os.path.realpath(self.hub / ".venv/bin"), os.path.realpath(self.hub / ".venv/Scripts")], seen)

    # Defect: a Windows checkout has no executable bit, so test -x skipped the repo hook with no word.
    # Injection: dispatch.sh execs the hook file itself, or the hooks test -x again.
    def test_a_repo_hook_with_no_executable_bit_runs_under_the_shell_its_shebang_names(self):
        for name, shebang in (("pre-commit", "#!/bin/sh"), ("post-merge", "#!/usr/bin/env bash")):
            hook = self.repo / ".githooks" / name
            hook.parent.mkdir(exist_ok=True)
            hook.write_text(shebang + f'\necho ran > "$(git rev-parse --show-toplevel)/.git/{name}-ran"\n')
            hook.chmod(0o644)
        (self.repo / "a.txt").write_text("a\n")
        self.git("add", "a.txt")
        self.assertEqual(self.git("commit", "-q", "-m", "a", check=False).returncode, 0)
        self.assertEqual("ran\n", (self.repo / ".git/pre-commit-ran").read_text())
        # Windows runs no shebang: Git for Windows' sh runs the hook, as git does.
        shell = [oscompat.git_shell("sh")] if oscompat.WINDOWS else []
        subprocess.run([*shell, str(self.hub / "Drawer/product-hooks/post-merge")], cwd=self.repo, env=self.env, check=True)
        self.assertEqual("ran\n", (self.repo / ".git/post-merge-ran").read_text())

    def test_a_python_shebang_repo_hook_runs_under_the_hub_python(self):
        hook = self.repo / ".githooks/pre-commit"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text('#!/usr/bin/env python3\nimport pathlib, sys\n'
                        'pathlib.Path(".git/py-ran").write_text(sys.executable)\n')
        hook.chmod(0o644)
        (self.repo / "a.txt").write_text("a\n")
        self.git("add", "a.txt")
        self.assertEqual(self.git("commit", "-q", "-m", "a", check=False).returncode, 0)
        self.assertEqual(os.path.realpath(sys.executable), os.path.realpath((self.repo / ".git/py-ran").read_text()))

    def test_commit_runs_the_product_repos_tracked_pre_commit_and_obeys_its_refusal(self):
        # Defect: the pack's pre-commit had no home but an untracked file in the hub's Drawer/product-hooks.
        hook = self.repo / ".githooks/pre-commit"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text('#!/bin/sh\necho "$0" > "$(git rev-parse --show-toplevel)/.git/pre-commit-ran"\ntest ! -e refuse\n')
        hook.chmod(0o755)
        (self.repo / "a.txt").write_text("a\n")
        self.git("add", "a.txt")
        self.assertEqual(self.git("commit", "-q", "-m", "a", check=False).returncode, 0)
        self.assertIn(".githooks/pre-commit", (self.repo / ".git/pre-commit-ran").read_text())
        (self.repo / "refuse").write_text("x\n")
        self.git("add", "refuse")
        self.assertEqual(self.git("commit", "-q", "-m", "b", check=False).returncode, 1)

    def test_commit_passes_when_the_product_repo_has_no_pre_commit(self):
        (self.repo / "a.txt").write_text("a\n")
        self.git("add", "a.txt")
        self.assertEqual(self.git("commit", "-q", "-m", "a", check=False).returncode, 0)


if __name__ == "__main__":
    unittest.main()
