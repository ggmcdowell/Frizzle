"""Tests for Drawer/product-init.py: the product bootstrap, on a temp hub, a temp product and a stub godot.

The temp hub holds this checkout's harness.toml, harness_config.py and product hooks, a hub Python shim and a stub
settings-sync.py, so a real git push in the product runs the real hub pre-push hook with the harness.local.toml
that product-init wrote.
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
sys.path.insert(0, str(HUB / "Drawer"))
import oscompat  # noqa: E402
import testgit  # noqa: E402

testgit.arm()  # no git template in any fixture repo
SPEC = importlib.util.spec_from_file_location("product_init", HUB / "Drawer" / "product-init.py")
product_init = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(product_init)
GIT = shutil.which("git")
SYSTEM_PATH = testgit.system_path()
BLOCKED = tomllib.loads((HUB / "harness.toml").read_text(encoding="utf-8"))["smoke"]["blocked_exit"]
STUB = """#!/bin/sh
case "$*" in
  *--version*) echo "4.9.stub.test"; exit 0 ;;
  *--script*) echo "${STUB_SCRIPT_OUT:-SMOKE PASS}"; exit 0 ;;
esac
exit 0
"""
# Windows: Python finds and starts a godot only as a PATHEXT file (godot.cmd here), while Git Bash's sh, which runs the
# gate, finds only a script that starts with #! (the extensionless godot beside it). The stub is both.
CMD_STUB = "@echo 4.9.stub.test\r\n"
SYNC_STUB = "import sys\nprint('wrote: .mcp.json' if '--mcp' in sys.argv and not __import__('os').path.exists('.synced') else 'in sync')\n"


def write_godot(path):
    """A stub godot at path: an executable sh script; on Windows also the .cmd beside it that Python can start."""
    path.write_text(STUB, encoding="utf-8", newline="\n")
    path.chmod(0o755)
    if oscompat.WINDOWS:
        path.with_name(path.name + ".cmd").write_text(CMD_STUB, encoding="utf-8", newline="")


def run_gate(argv, **kwargs):
    """subprocess.run; on Windows the GODOT that find_godot found is the .cmd, and the gate's sh wants the script."""
    env = kwargs.get("env") or {}
    if env.get("GODOT", "").lower().endswith(".cmd"):
        kwargs["env"] = {**env, "GODOT": env["GODOT"][:-len(".cmd")]}
    return subprocess.run(argv, **kwargs)


def git(cwd, *args):
    return subprocess.run([GIT, *args], cwd=str(cwd), check=True, capture_output=True, text=True).stdout.strip()


class ProductInitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.hub = self.base / "hub"
        (self.hub / "Drawer").mkdir(parents=True)
        shutil.copy2(HUB / "harness.toml", self.hub / "harness.toml")
        shutil.copy2(HUB / "Drawer" / "harness_config.py", self.hub / "Drawer" / "harness_config.py")
        shutil.copy2(HUB / "Drawer" / "oscompat.py", self.hub / "Drawer" / "oscompat.py")
        shutil.copytree(HUB / "Drawer" / "product-hooks", self.hub / "Drawer" / "product-hooks")
        (self.hub / "Drawer" / "settings-sync.py").write_text(SYNC_STUB, encoding="utf-8")
        marker = tomllib.loads((HUB / "harness.toml").read_text(encoding="utf-8"))["project"]["marker_file"]
        (self.hub / marker).write_text("")
        py = self.hub / ".venv" / "bin" / "python"
        py.parent.mkdir(parents=True)
        py.write_text(f'#!/bin/sh\nexec "{Path(sys.executable).as_posix()}" "$@"\n', encoding="utf-8", newline="\n")
        py.chmod(0o755)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        write_godot(self.bin / "godot")
        empty = self.base / "empty.gitconfig"
        empty.write_text("", encoding="utf-8")
        # No git identity at all: init commits with its own.
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k not in ("GODOT", "HARNESS_NO_LOCAL_TOML")}
        env.update(PATH=f"{self.bin}{os.pathsep}{SYSTEM_PATH}", HOME=str(self.base), GIT_CONFIG_GLOBAL=str(empty),
                   GIT_CONFIG_NOSYSTEM="1", CLAUDE_CODE_SESSION_ID="0a1b2c3d-0000-4000-8000-000000000000", **testgit.ENV)
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        app = mock.patch.object(product_init, "MAC_APP", self.base / "no-such-app")
        app.start()
        self.addCleanup(app.stop)
        self.product = self.base / "game"
        self.local = self.hub / "harness.local.toml"

    def init(self, *args, path=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = product_init.main(["--path", str(path or self.product), *args], hub=self.hub, run=run_gate)
        lines = out.getvalue().strip().splitlines()
        self.assertEqual(1, len(lines), out.getvalue() + err.getvalue())
        return code, json.loads(lines[0]), err.getvalue()

    def product_table(self):
        return tomllib.loads(self.local.read_text(encoding="utf-8")).get("product", {})

    def test_gdformat_in_the_hub_environment_counts_with_no_gdformat_on_path(self):
        # The hub environment's script folder, and on Windows a name PATHEXT finds.
        fmt = oscompat.venv_bin(self.hub) / ("gdformat.cmd" if oscompat.WINDOWS else "gdformat")
        fmt.parent.mkdir(parents=True, exist_ok=True)
        fmt.write_text("@exit /b 0\r\n" if oscompat.WINDOWS else "#!/bin/sh\nexit 0\n", encoding="utf-8", newline="")
        fmt.chmod(0o755)
        code, row, err = self.init("--pack", "godot", "--local-origin")
        self.assertEqual((0, True), (code, row["gdformat"]), err)
        self.assertNotIn("gdformat is not", err)

    def test_one_run_makes_the_repo_binds_the_hooks_and_runs_a_green_first_gate(self):
        code, row, err = self.init("--pack", "godot", "--local-origin")
        self.assertEqual((0, "ready", 0), (code, row["state"], row["gate_exit"]), row)
        self.assertEqual("4.9.stub.test", row["version"])
        self.assertIn("4.9.stub.test", err)
        self.assertFalse(row["gdformat"])
        self.assertIn("uv sync (in the hub; uv.lock pins gdtoolkit)", err)
        for rel in ("tools/gate.sh", ".githooks/pre-commit", ".githooks/pre-push", "GODOT-CONVENTIONS.md", "project.godot",
                    "tests/smoke_test.gd", ".gitignore"):
            self.assertTrue((self.product / rel).is_file(), rel)
        self.assertEqual("main", git(self.product, "branch", "--show-current"))
        origin = self.base / "game-origin.git"
        self.assertEqual(str(origin), git(self.product, "remote", "get-url", "origin"))
        self.assertEqual(git(self.product, "rev-parse", "HEAD"), git(origin, "rev-parse", "main"))
        self.assertEqual("", git(self.product, "status", "--porcelain"))
        table = self.product_table()
        self.assertEqual(str(self.product), table["path"])
        self.assertEqual("sh tools/gate.sh .", table["gate_command"])
        self.assertEqual([".godot", ".import"], table["broad_roots"])
        self.assertEqual("godot-editor", table["mcp_server"])
        self.assertNotIn("mcp_url", table)
        self.assertEqual((self.hub / "Drawer" / "product-hooks").as_posix(), git(self.product, "config", "core.hooksPath"))
        self.assertEqual(git(self.product, "rev-parse", "HEAD^{tree}"),
                         (self.product / ".gate-verdict").read_text(encoding="utf-8").strip())
        self.assertIn("commit", row["changed"])
        self.assertIn("harness.local.toml", row["changed"])

    def test_the_pack_ships_lf_rules_and_commits_its_scripts_executable(self):
        self.init("--pack", "godot", "--local-origin")
        attrs = (self.product / ".gitattributes").read_text(encoding="utf-8")
        self.assertIn("* text=auto eol=lf", attrs)
        self.assertIn(".githooks/* text eol=lf", attrs)
        modes = {line.split("\t")[1]: line.split()[0] for line in git(self.product, "ls-files", "-s").splitlines()}
        for rel in ("tools/gate.sh", ".githooks/pre-commit", ".githooks/pre-push"):
            self.assertEqual("100755", modes[rel], rel)
        self.assertEqual("100644", modes[".gitattributes"])

    def test_local_files_the_run_writes_are_lf_on_every_platform(self):
        self.init("--pack", "godot", "--local-origin")
        for path in (self.local, self.product / ".gitignore"):
            self.assertNotIn(b"\r", path.read_bytes(), path.name)

    def test_a_bom_in_the_local_toml_does_not_stop_the_run(self):
        self.local.write_bytes(b"\xef\xbb\xbf[mem]\ndb = \"/db\"\n")
        code, row, _ = self.init("--local-origin")
        self.assertEqual((0, "ready"), (code, row["state"]), row)
        self.assertEqual(str(self.product), self.product_table()["path"])

    def test_windows_names_one_place_in_either_slash_style_and_case(self):
        same = product_init.same_place
        with mock.patch.object(product_init.oscompat, "WINDOWS", False):
            self.assertFalse(same("C:\\Work\\x-origin.git", "c:/work/x-origin.git"))
        with mock.patch.object(product_init.oscompat, "WINDOWS", True):
            self.assertTrue(same("C:\\Work\\x-origin.git", "c:/work/x-origin.git"))
            self.assertFalse(same("C:\\Work\\x-origin.git", "C:/Work/y-origin.git"))

    def test_a_gate_command_that_cannot_start_is_failed_at_the_gate_step(self):
        self.local.write_text('[product]\ngate_command = "sh tools/gate.sh ."\n', encoding="utf-8")
        with mock.patch.object(product_init.harness_config.oscompat, "WINDOWS", True), \
                mock.patch.object(product_init.harness_config.oscompat, "git_shell", side_effect=OSError("no Git sh")):
            code, row, _ = self.init("--local-origin", "--pack", "godot")
        self.assertEqual((1, "failed", "gate"), (code, row["state"], row["step"]), row)

    def test_a_second_run_changes_nothing(self):
        self.init("--pack", "godot", "--local-origin")
        head = git(self.product, "rev-parse", "HEAD")
        toml = self.local.read_bytes()
        (self.hub / ".synced").write_text("")
        code, row, _ = self.init("--pack", "godot", "--local-origin")
        self.assertEqual((0, "ready", []), (code, row["state"], row["changed"]), row)
        self.assertEqual(head, git(self.product, "rev-parse", "HEAD"))
        self.assertEqual(toml, self.local.read_bytes())
        self.assertEqual("", git(self.product, "status", "--porcelain"))

    def test_no_godot_prints_the_install_command_exits_blocked_and_changes_nothing(self):
        with mock.patch.dict(os.environ, {"PATH": SYSTEM_PATH}):
            code, row, err = self.init("--pack", "godot", "--local-origin")
        self.assertEqual((BLOCKED, "blocked"), (code, row["state"]), row)
        self.assertEqual("brew install --cask godot", row["install"])
        self.assertIn("brew install --cask godot", err)
        self.assertFalse(self.product.exists())
        self.assertFalse(self.local.exists())

    def test_godot_is_found_by_env_then_path_then_the_mac_app(self):
        ext = ".cmd" if oscompat.WINDOWS else ""  # Windows finds a file only by an extension in PATHEXT
        other = self.base / ("other-godot" + ext)
        write_godot(self.base / "other-godot")
        app = self.base / "Godot"
        write_godot(app)
        self.assertEqual(str(other), product_init.find_godot({"GODOT": str(other), "PATH": str(self.bin)}, app))
        # PATHEXT may spell the extension in upper case
        self.assertEqual(os.path.normcase(self.bin / ("godot" + ext)),
                         os.path.normcase(product_init.find_godot({"PATH": str(self.bin)}, app)))
        self.assertEqual(str(app), product_init.find_godot({"PATH": "/nonexistent"}, app))
        self.assertIsNone(product_init.find_godot({"GODOT": "/nonexistent/godot", "PATH": str(self.bin)}, app))
        self.assertIsNone(product_init.find_godot({"PATH": "/nonexistent"}, self.base / "absent"))

    def test_a_red_first_gate_is_state_red_with_its_exit(self):
        with mock.patch.dict(os.environ, {"STUB_SCRIPT_OUT": "ran zero checks"}):
            code, row, _ = self.init("--pack", "godot", "--local-origin")
        self.assertEqual((1, "red", 1), (code, row["state"], row["gate_exit"]), row)

    def test_a_first_gate_that_exits_blocked_is_state_blocked_with_the_blocked_exit(self):
        self.local.write_text('[product]\ngate_command = "sh -c \'exit %d\'"\n' % BLOCKED, encoding="utf-8")
        code, row, _ = self.init("--local-origin", "--pack", "godot")
        self.assertEqual((BLOCKED, "blocked", BLOCKED), (code, row["state"], row["gate_exit"]), row)

    def test_origin_url_and_mcp_url_are_written_and_a_different_origin_is_refused(self):
        bare = self.base / "named-origin.git"
        git(self.base, "init", "-q", "--bare", "-b", "main", str(bare))
        code, row, _ = self.init("--origin", str(bare), "--pack", "godot", "--mcp-url", "http://127.0.0.1:6000/mcp")
        self.assertEqual((0, "ready"), (code, row["state"]), row)
        self.assertEqual(str(bare), git(self.product, "remote", "get-url", "origin"))
        self.assertEqual("http://127.0.0.1:6000/mcp", self.product_table()["mcp_url"])
        code, row, _ = self.init("--local-origin", "--pack", "godot")
        self.assertEqual((1, "failed", "origin"), (code, row["state"], row["step"]), row)

    def test_no_origin_named_is_failed_not_a_guess(self):
        code, row, _ = self.init()
        self.assertEqual((1, "failed", "origin"), (code, row["state"], row["step"]), row)

    def test_another_product_path_in_the_local_toml_is_refused_and_other_keys_stay(self):
        self.local.write_text('[project]\nstate_root = "/x"\n\n[product]\npath = "/somewhere/else"\n', encoding="utf-8")
        before = self.local.read_text(encoding="utf-8")
        code, row, _ = self.init("--local-origin", "--pack", "godot")
        self.assertEqual((1, "failed", "toml"), (code, row["state"], row["step"]), row)
        self.assertEqual(before, self.local.read_text(encoding="utf-8"))

    def test_an_empty_product_table_is_filled_and_the_other_tables_stay(self):
        self.local.write_text('[project]\nstate_root = "/x"\n\n[product]\npath = ""\ngate_command = ""\n\n[mem]\ndb = "/db"\n',
                              encoding="utf-8")
        self.init("--local-origin", "--pack", "godot")
        cfg = tomllib.loads(self.local.read_text(encoding="utf-8"))
        self.assertEqual(("/x", "/db"), (cfg["project"]["state_root"], cfg["mem"]["db"]))
        self.assertEqual((str(self.product), "sh tools/gate.sh ."), (cfg["product"]["path"], cfg["product"]["gate_command"]))

    def test_a_kept_gate_command_of_the_user_runs_as_the_first_gate(self):
        self.local.write_text('[product]\ngate_command = "sh -c \'exit 0\'"\n', encoding="utf-8")
        code, row, _ = self.init("--local-origin", "--pack", "godot")
        self.assertEqual((0, "ready"), (code, row["state"]), row)
        self.assertEqual("sh -c 'exit 0'", self.product_table()["gate_command"])

    def test_without_a_pack_it_makes_the_repo_writes_the_path_and_binds_the_hooks_only(self):
        code, row, _ = self.init("--local-origin")
        self.assertEqual((0, "ready", None), (code, row["state"], row["gate_exit"]), row)
        self.assertEqual({"path": str(self.product)}, self.product_table())
        self.assertFalse((self.product / "tools").exists())
        self.assertEqual(git(self.product, "rev-parse", "HEAD"), git(self.base / "game-origin.git", "rev-parse", "main"))
        self.assertTrue(git(self.product, "config", "core.hooksPath"))

    def test_an_existing_repo_keeps_its_files_and_a_repo_inside_another_is_refused(self):
        git(self.base, "init", "-q", "-b", "main", str(self.product))
        (self.product / "project.godot").write_text("config_version=5\n; mine\n", encoding="utf-8")
        git(self.product, "add", "-A")
        git(self.product, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "mine")
        code, row, _ = self.init("--local-origin", "--pack", "godot")
        self.assertEqual((0, "ready"), (code, row["state"]), row)
        self.assertIn("; mine", (self.product / "project.godot").read_text(encoding="utf-8"))
        self.assertEqual(2, int(git(self.product, "rev-list", "--count", "HEAD")))
        code, row, _ = self.init("--local-origin", path=self.product / "sub")
        self.assertEqual((1, "failed", "repo"), (code, row["state"], row["step"]), row)

    def test_the_first_gate_leaves_a_repo_that_pushes_through_the_hub_pre_push_hook(self):
        # The real hub hook reads the harness.local.toml gate_command that init wrote, and the pack hook reads the verdict.
        self.init("--pack", "godot", "--local-origin")
        (self.product / "a.txt").write_text("a\n", encoding="utf-8")
        git(self.product, "add", "a.txt")
        git(self.product, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "a")
        refused = subprocess.run([GIT, "push", "-q", "origin", "HEAD:main"], cwd=str(self.product), capture_output=True, text=True)
        self.assertEqual(1, refused.returncode, refused.stderr)
        self.assertIn("no green gate verdict", refused.stderr)
        subprocess.run(["sh", "tools/gate.sh", "."], cwd=str(self.product), check=True, capture_output=True)
        done = subprocess.run([GIT, "push", "-q", "origin", "HEAD:main"], cwd=str(self.product), capture_output=True, text=True)
        self.assertEqual(0, done.returncode, done.stderr)

    def test_bad_arguments_exit_2(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            product_init.main(["--origin", "x", "--local-origin", "--path", str(self.product)], hub=self.hub)
        self.assertEqual(2, cm.exception.code)


if __name__ == "__main__":
    unittest.main()
