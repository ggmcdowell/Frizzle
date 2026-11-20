"""Tests for Drawer/new-hub.py, the product hub stamp.

Run: .venv/bin/python -B -m unittest Drawer/test_new_hub.py. Hermetic: a temp HOME, a throwaway template
cloned from this checkout's head, and the stamp in the temp folder.
"""
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HUB = HERE.parent
CLI = HERE / "new-hub.py"
PY = sys.executable
sys.path.insert(0, str(HERE))
import testgit  # noqa: E402

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.invalid", "GIT_CONFIG_GLOBAL": os.devnull, **testgit.ENV}
SLUG = "zq" + "stamp"  # split: the stamp's slug grep test must not find it in this file


def run(args, cwd, env, check=True):
    out = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=300)
    if check and out.returncode:
        raise AssertionError(f"{args}: exit {out.returncode}\n{out.stdout}{out.stderr}")
    return out


class NewHubTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.home = cls.tmp / "home"
        cls.home.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        cls.env = {**env, **GIT_ENV, "HOME": str(cls.home)}
        # The template: this checkout's head plus its uncommitted files, so the test runs the code under test.
        # --shared: the template reads the hub's objects by alternates and copies none.
        cls.template = cls.tmp / "template"
        run(["git", "clone", "-q", "--shared", str(HUB), str(cls.template)], cls.tmp, cls.env)
        changed = (run(["git", "diff", "--name-only", "HEAD"], HUB, cls.env).stdout.split()
                   + run(["git", "ls-files", "-o", "--exclude-standard"], HUB, cls.env).stdout.split())
        for rel in changed:
            if (HUB / rel).is_file():
                (cls.template / rel).parent.mkdir(parents=True, exist_ok=True)  # a new folder holds no file yet
                shutil.copy2(HUB / rel, cls.template / rel)
        run(["git", "add", "-A"], cls.template, cls.env)
        run(["git", "commit", "-q", "--allow-empty", "-m", "fixture head"], cls.template, cls.env)
        cls.old = tomllib.loads((cls.template / "harness.toml").read_text())["project"]
        cfg_root = cls.home / cls.old["config_root"].removeprefix("~/")
        cfg_root.mkdir(parents=True)
        (cfg_root / "name-denylist.txt").write_text("secretname\n")
        (cfg_root / "mem.env").write_text("OPENAI_API_KEY=sk-test\n")
        cls.status_before = run(["git", "status", "--porcelain"], cls.template, cls.env).stdout
        cls.dest = cls.tmp / "products" / "Zq_Stamp"
        cls.out = run([PY, "-B", str(cls.template / "Drawer/new-hub.py"), "--dest", str(cls.dest), "--name", SLUG,
                       "--display", "Zq Stamp", "--origin", "https://example.invalid/zq.git"], cls.template, cls.env)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, True)

    def cfg(self):
        return tomllib.loads((self.dest / "harness.toml").read_text())

    def test_harness_toml_carries_the_new_slug(self):
        cfg = self.cfg()
        p, m = cfg["project"], cfg["mem"]
        self.assertEqual((SLUG, "Zq Stamp", SLUG.upper(), f".{SLUG}-hub"),
                         (p["name"], p["display_name"], p["env_prefix"], p["marker_file"]))
        self.assertEqual((f"~/.config/{SLUG}-hub", f"~/.local/share/{SLUG}-hub"), (p["config_root"], p["state_root"]))
        self.assertEqual(f"~/.local/share/{SLUG}-hub/hub.db", m["db"])
        self.assertEqual(f"~/.config/{SLUG}-hub/mem.env", m["env_file"])
        self.assertEqual(f"~/.local/share/{SLUG}-hub/mem-export", m["export_dir"])
        self.assertNotIn("remote_hub", m)  # mem finds the live hub from the checkout, not from a folder name
        self.assertEqual(f"~/.config/{SLUG}-hub/name-denylist.txt", cfg["names"]["denylist"])

    def test_no_tracked_file_names_the_old_slug_or_prefix(self):
        out = run(["git", "grep", "-l", "-i", "-I", "-F", self.old["name"]], self.dest, self.env, check=False)
        self.assertEqual("", out.stdout)

    def test_marker_is_renamed_and_tracked(self):
        files = run(["git", "ls-files"], self.dest, self.env).stdout.split()
        self.assertIn(f".{SLUG}-hub", files)
        self.assertNotIn(self.old["marker_file"], files)
        self.assertFalse((self.dest / self.old["marker_file"]).exists())

    def test_pyproject_and_lock_carry_the_hub_name_and_the_lock_checks(self):
        self.assertIn(f'name = "{SLUG}-hub"', (self.dest / "pyproject.toml").read_text())
        self.assertIn(f'name = "{SLUG}-hub"', (self.dest / "uv.lock").read_text())
        run(["uv", "lock", "--check", "--offline"], self.dest, self.env)

    def test_settings_render_the_new_prefix(self):
        text = (self.dest / ".claude/settings.json").read_text()
        self.assertIn(f"{SLUG.upper()}_FLIGHT", text)
        self.assertNotIn(f"{self.old['env_prefix']}_", text)

    def test_the_stamp_is_one_clean_commit_on_template_history_with_remotes(self):
        self.assertEqual("", run(["git", "status", "--porcelain"], self.dest, self.env).stdout)
        head = run(["git", "rev-parse", "HEAD~1"], self.dest, self.env).stdout.strip()
        self.assertEqual(run(["git", "rev-parse", "HEAD"], self.template, self.env).stdout.strip(), head)
        remotes = run(["git", "remote", "-v"], self.dest, self.env).stdout
        self.assertIn("template\t", remotes)
        self.assertIn("origin\thttps://example.invalid/zq.git", remotes)

    def test_slug_grep_test_passes_in_the_stamp(self):
        run([PY, "-B", "-m", "unittest", "Drawer/test_project_slug.py", "-k", "test_only_the_config_files"],
            self.dest, self.env)

    def test_settings_sync_check_passes_in_the_stamp(self):
        run([PY, "-B", "Drawer/settings-sync.py", "--tracked"], self.dest, self.env)
        run([PY, "-B", "-m", "unittest", "Drawer/test_settings_sync.py", "-k", "flight_guard"], self.dest, self.env)

    def test_template_status_is_unchanged(self):
        self.assertEqual(self.status_before, run(["git", "status", "--porcelain"], self.template, self.env).stdout)

    def test_the_default_stamp_writes_nothing_in_the_home_and_names_each_skipped_copy(self):
        self.assertFalse((self.home / f".config/{SLUG}-hub").exists())
        self.assertFalse((self.home / f".local/share/{SLUG}-hub").exists())
        self.assertIn("name-denylist.txt: pass --copy-secrets to copy it at mode 600", self.out.stdout)
        self.assertIn("mem.env: pass --copy-secrets to copy it at mode 600", self.out.stdout)

    def test_the_roots_come_from_options_and_secrets_copy_only_on_request(self):
        state, config = self.tmp / "roots" / "state", self.tmp / "roots" / "config"
        dest = self.tmp / "products" / "Zq_Roots"
        out = run([PY, "-B", str(self.template / "Drawer/new-hub.py"), "--dest", str(dest), "--name", "zqroots",
                   "--display", "Zq Roots", "--state-root", str(state), "--config-root", f"{config}/",
                   "--copy-secrets"], self.template, self.env)
        cfg = tomllib.loads((dest / "harness.toml").read_text())
        self.assertEqual((str(config), str(state)), (cfg["project"]["config_root"], cfg["project"]["state_root"]))
        self.assertEqual(f"{state}/hub.db", cfg["mem"]["db"])
        self.assertEqual(f"{state}/mem-export", cfg["mem"]["export_dir"])
        self.assertEqual(f"{config}/mem.env", cfg["mem"]["env_file"])
        self.assertEqual(f"{config}/name-denylist.txt", cfg["names"]["denylist"])
        for name in ("name-denylist.txt", "mem.env"):
            self.assertEqual(0o600, stat.S_IMODE((config / name).stat().st_mode), name)
        self.assertEqual("secretname\n", (config / "name-denylist.txt").read_text())
        self.assertNotIn("secretname", out.stdout + out.stderr)
        self.assertNotIn("sk-test", out.stdout + out.stderr)
        self.assertFalse((self.home / ".config/zqroots-hub").exists())

    def refuse(self, dest, name, pre=None):
        snap = sorted(str(p) for p in self.tmp.rglob("*") if ".git" not in p.parts and "Zq_Stamp" not in p.parts)
        out = run([PY, "-B", str(self.template / "Drawer/new-hub.py"), "--dest", str(dest), "--name", name,
                   "--display", "X"], self.template, self.env, check=False)
        self.assertNotEqual(0, out.returncode, out.stdout)
        self.assertIn("REFUSED", out.stdout)
        after = sorted(str(p) for p in self.tmp.rglob("*") if ".git" not in p.parts and "Zq_Stamp" not in p.parts)
        self.assertEqual(snap, after, "a refusal wrote")

    def refuse_with(self, dest, name, *extra):
        out = run([PY, "-B", str(self.template / "Drawer/new-hub.py"), "--dest", str(dest), "--name", name,
                   "--display", "X", *extra], self.template, self.env, check=False)
        self.assertEqual(2, out.returncode, out.stdout)
        self.assertIn("REFUSED", out.stdout)
        self.assertFalse(Path(dest).exists())

    def test_a_template_commit_upgrades_a_stamped_hub_and_a_conflict_is_refused(self):
        up = self.tmp / "products" / "Zq_Up"
        run([PY, "-B", str(self.template / "Drawer/new-hub.py"), "--dest", str(up), "--name", "zqup", "--display",
             "Zq Up"], self.template, self.env)
        newer = self.tmp / "template-newer"
        run(["git", "clone", "-q", str(self.template), str(newer)], self.tmp, self.env)
        run(["git", "switch", "-q", "-C", "main"], newer, self.env)  # the fixture template sits on a task branch or on main
        manifest = newer / "Drawer/settings-manifest.json"
        # The timeout line right below a command that names {ENV_PREFIX}: the stamp changed that command line in
        # settings.json, so both sides touch neighbouring lines there and git reports a conflict in the generated file.
        bumped, hits = re.subn(r'(\{ENV_PREFIX\}_FLIGHT[^\n]*\n\s+"timeout": )60', r"\g<1>61", manifest.read_text(), count=1)
        self.assertEqual(1, hits)
        manifest.write_text(bumped)
        (newer / "Drawer/upgrade_probe.py").write_text("# a file the template added after the stamp\n")
        readme = newer / "README.md"
        readme.write_text("template line\n" + readme.read_text())
        run([PY, "-B", "Drawer/settings-sync.py", "--apply"], newer, self.env)
        run(["git", "add", "-A"], newer, self.env)
        run(["git", "commit", "-q", "-m", "template moves on"], newer, self.env)
        run(["git", "remote", "set-url", "template", str(newer)], up, self.env)
        upgrade = [PY, "-B", "Drawer/hub-upgrade.py"]

        main = run(upgrade, up, self.env, check=False)  # the live main checkout of the stamp
        self.assertEqual((1, True), (main.returncode, "REFUSED this is the live main checkout" in main.stdout), main.stdout)

        branch = self.tmp / "products" / "Zq_Up_wt"
        run(["git", "worktree", "add", "-q", "-b", "task/upgrade", str(branch)], up, self.env)
        mine = branch / "README.md"
        mine.write_text("stamp line\n" + mine.read_text())
        run(["git", "commit", "-qam", "stamp edit"], branch, self.env)
        before = run(["git", "rev-parse", "HEAD"], branch, self.env).stdout
        clash = run(upgrade, branch, self.env, check=False)  # both sides edited README.md line 1
        self.assertEqual((1, True), (clash.returncode, "REFUSED conflict: README.md" in clash.stdout), clash.stdout)
        self.assertEqual((before, ""), (run(["git", "rev-parse", "HEAD"], branch, self.env).stdout,
                                        run(["git", "status", "--porcelain"], branch, self.env).stdout))
        self.assertNotEqual(0, run(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"], branch, self.env, check=False).returncode)

        run(["git", "reset", "-q", "--hard", "HEAD~1"], branch, self.env)  # drop the clashing stamp edit
        ok = run(upgrade, branch, self.env)
        self.assertRegex(ok.stdout, r"UPGRADED [0-9a-f]{40}")
        self.assertTrue((branch / "Drawer/upgrade_probe.py").is_file())
        cfg = tomllib.loads((branch / "harness.toml").read_text())["project"]
        self.assertEqual(("zqup", "ZQUP", ".zqup-hub"), (cfg["name"], cfg["env_prefix"], cfg["marker_file"]))
        self.assertTrue((branch / ".zqup-hub").is_file())
        self.assertEqual("", run(["git", "grep", "-l", "-i", "-I", "-F", self.old["name"]], branch, self.env,
                                 check=False).stdout)
        settings = (branch / ".claude/settings.json").read_text()
        self.assertIn('"timeout": 61', settings)
        self.assertIn("re-rendered .claude/settings.json", ok.stdout)
        self.assertIn("ZQUP_FLIGHT", settings)
        self.assertNotIn(self.old["env_prefix"] + "_", settings)
        run([PY, "-B", "Drawer/settings-sync.py", "--tracked"], branch, self.env)
        self.assertEqual("", run(["git", "status", "--porcelain"], branch, self.env).stdout)
        again = run(upgrade, branch, self.env, check=False)
        self.assertEqual((1, True), (again.returncode, "REFUSED already up to date" in again.stdout), again.stdout)

    def test_refuses_the_template_slug(self):
        self.refuse(self.tmp / "products" / "other", self.old["name"])

    def test_refuses_a_bad_slug(self):
        self.refuse(self.tmp / "products" / "other", "Bad-Slug")

    def test_refuses_a_dest_inside_the_template(self):
        self.refuse(self.template / "nested", "zqother")

    def test_refuses_a_used_dest(self):
        self.refuse(self.dest, "zqother")

    def test_refuses_a_used_state_root(self):
        state = self.home / ".local/share/zqused-hub"
        state.mkdir(parents=True)
        try:
            self.refuse(self.tmp / "products" / "fresh", "zqused")
        finally:
            state.rmdir()

    def test_refuses_the_stamp_slug_again(self):
        # Setup makes the roots: a second hub on the same slug would share them.
        state = self.home / f".local/share/{SLUG}-hub"
        state.mkdir(parents=True)
        try:
            self.refuse(self.tmp / "products" / "again", SLUG)
        finally:
            state.rmdir()

    def test_refuses_a_used_given_root(self):
        used = self.tmp / "used-root"
        used.mkdir()
        self.refuse_with(self.tmp / "products" / "fresh2", "zqgiven", "--state-root", str(used))


class WindowsBranchTest(unittest.TestCase):
    """The Windows branches of new-hub.py, run with oscompat.WINDOWS set on any host."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location("new_hub_under_test", CLI)
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    def test_a_given_windows_root_keeps_its_slashes_forward_so_the_toml_string_parses(self):
        from unittest import mock
        with mock.patch.object(self.mod.oscompat, "WINDOWS", True):
            root = self.mod._slashes("C:\\work\\me\\state")
        self.assertEqual("C:/work/me/state", root)
        text = self.mod.set_key('[project]\nstate_root = "x"\n', "project", "state_root", root)
        self.assertEqual(root, tomllib.loads(text)["project"]["state_root"])
        self.assertEqual("a\\b", self.mod._slashes("a\\b"))  # POSIX: a backslash is a name character

    def test_a_copied_secret_is_byte_identical_and_names_the_windows_access_list(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = Path(tmp, "src.env"), Path(tmp, "out", "dst.env")
            src.write_bytes(b"KEY=1\nB=2\n")
            with mock.patch.object(self.mod.oscompat, "WINDOWS", True):
                note = self.mod.copy_secret(str(src), str(dst))
            self.assertEqual(b"KEY=1\nB=2\n", dst.read_bytes())
            self.assertIn("user-profile ACL", note)
            self.assertNotIn("mode 600", note.replace("no mode 600", ""))


if __name__ == "__main__":
    unittest.main()
