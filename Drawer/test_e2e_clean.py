"""Tests for Drawer/e2e_clean.py and the preflight and PASS cleanup of Drawer/e2e.sh (the paid run itself is not run)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import e2e_clean  # noqa: E402
import oscompat  # noqa: E402

BASH = oscompat.git_shell("bash")  # Windows: Git for Windows' bash, never the WSL launcher

FAKE_TRASH = "#!/bin/sh\nfor f; do mv \"$f\" \"$TRASHED\"/; done\n"


class E2eCleanTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.temp = self.root / "frz-e2e.AbC123"
        self.config = self.root / ".claude.json"
        self.projects = self.root / "projects"
        self.trashed = self.root / "trashed"
        for folder in (self.projects, self.trashed):
            folder.mkdir()
        self.trash = self.root / "trash"
        self.trash.write_text(FAKE_TRASH)
        self.trash.chmod(0o755)
        self.addCleanup(os.environ.pop, "TRASHED", None)
        os.environ["TRASHED"] = str(self.trashed)

    def fake_recycle(self, path):
        shutil.move(path, str(self.trashed))

    def seed(self):
        other = self.root / "frz-e2e.AbC1234"  # a longer name that starts with the run's name keeps its row
        self.config.write_text(json.dumps({"userID": "u", "projects": {
            str(self.temp): {}, str(self.temp / "hub"): {"hasTrustDialogAccepted": True},
            str(self.temp / "hub_wt"): {"hasTrustDialogAccepted": True}, str(other): {"keep": 1},
            "/srv/other/work": {"hasTrustDialogAccepted": True}}}))
        self.config.chmod(0o600)
        for name in ("-tmp-frz-e2e-AbC123-hub", "-tmp-frz-e2e-AbC123-hub-wt-x", "-tmp-frz-e2e-Zzz999-hub"):
            (self.projects / name).mkdir()
        self.munged = e2e_clean.re.sub(r"[^A-Za-z0-9]", "-", str(self.temp))

    def test_a_pass_removes_the_trust_rows_and_the_transcript_folders_of_its_own_run_only(self):
        self.seed()
        (self.projects / (self.munged + "-hub")).mkdir()
        (self.projects / (self.munged + "-hub-wt-x")).mkdir()
        mode = self.config.stat().st_mode & 0o777  # seed() set 0o600; clean() keeps whatever mode the file had
        # POSIX runs the fake trash script; Windows recycles instead (no trash command), so a stand-in moves the folders
        with mock.patch.object(e2e_clean.oscompat, "recycle", side_effect=self.fake_recycle):
            rows, folders = e2e_clean.clean(str(self.temp), str(self.config), str(self.projects), str(self.trash))
        self.assertEqual((3, 2), (rows, folders))
        value = json.loads(self.config.read_text())
        self.assertEqual("u", value["userID"])
        self.assertEqual({str(self.root / "frz-e2e.AbC1234"), "/srv/other/work"}, set(value["projects"]))
        self.assertEqual(mode, self.config.stat().st_mode & 0o777)
        self.assertEqual({"-tmp-frz-e2e-AbC123-hub", "-tmp-frz-e2e-AbC123-hub-wt-x", "-tmp-frz-e2e-Zzz999-hub"},
                         {p.name for p in self.projects.iterdir()})  # none of these names match this run's path
        self.assertEqual({self.munged + "-hub", self.munged + "-hub-wt-x"}, {p.name for p in self.trashed.iterdir()})

    def test_a_missing_config_and_no_transcripts_is_a_clean_no_op(self):
        self.assertEqual((0, 0), e2e_clean.clean(str(self.temp), str(self.config), str(self.projects), str(self.trash)))

    def test_it_takes_only_an_e2e_temp_folder(self):
        with self.assertRaises(ValueError):
            e2e_clean.clean(str(self.root / "work"), str(self.config), str(self.projects), str(self.trash))

    def test_a_missing_trash_command_fails_loudly(self):
        with mock.patch.object(e2e_clean, "trash_command", return_value=None), \
                mock.patch.object(e2e_clean.oscompat, "WINDOWS", False), \
                mock.patch("sys.stderr", new_callable=__import__("io").StringIO) as err:
            self.assertEqual(1, e2e_clean.main([str(self.temp)]))
        self.assertIn("no trash command", err.getvalue())

    def test_the_e2e_claude_call_reads_the_clones_mcp_file_alone(self):
        text = (HERE / "e2e.sh").read_text(encoding="utf-8")
        call = text[text.index("claude --permission-mode"):].split("&\n", 1)[0]
        self.assertIn("--strict-mcp-config", call)
        self.assertIn('--mcp-config "$H/.mcp.json"', call)

    def test_e2e_sh_stops_at_once_when_trash_is_not_on_path(self):
        tools = self.root / "bin"
        tools.mkdir()
        for name in ("uv", "claude"):
            (tools / name).write_text("#!/bin/sh\n")
            (tools / name).chmod(0o755)
        done = subprocess.run([BASH, str(HERE / "e2e.sh")], capture_output=True, text=True, timeout=30,
                              env={"E2E_CLEAN": "1", "HOME": str(self.root), "PATH": str(tools)})
        self.assertEqual(2, done.returncode, done.stdout + done.stderr)
        self.assertIn("e2e needs trash on PATH", done.stdout)

    def test_e2e_sh_runs_the_cleanup_on_pass(self):
        text = (HERE / "e2e.sh").read_text(encoding="utf-8")
        self.assertIn('"$SYSPY" "$SRC/Drawer/e2e_clean.py" "$T"', text)
        self.assertIn("SYSPY=/usr/bin/python3", text)  # the POSIX run keeps the system Python
        self.assertNotIn('trash "$T" 2>/dev/null', text)

    def test_windows_matches_a_trust_row_in_either_slash_style_and_drive_case(self):
        with mock.patch.object(e2e_clean.oscompat, "WINDOWS", True):
            for key in ("C:\\Temp\\frz-e2e.X", "c:/temp/frz-e2e.X/hub", "C:/Temp/frz-e2e.X\\hub_wt"):
                self.assertTrue(e2e_clean.under(key, "C:\\Temp\\frz-e2e.X"), key)
            for key in ("C:/Temp/frz-e2e.X2", "D:/Temp/frz-e2e.X", "/srv/other"):
                self.assertFalse(e2e_clean.under(key, "C:\\Temp\\frz-e2e.X"), key)
        with mock.patch.object(e2e_clean.oscompat, "WINDOWS", False):
            self.assertFalse(e2e_clean.under("C:/Temp/frz-e2e.X", "C:\\Temp\\frz-e2e.X"))  # POSIX: spelled exactly

    def test_a_config_held_by_a_running_client_is_reported_not_a_traceback(self):
        self.seed()
        with mock.patch.object(e2e_clean.oscompat, "replace", side_effect=PermissionError(5, "Access is denied")):
            with self.assertRaisesRegex(RuntimeError, "is in use"):
                e2e_clean.clean(str(self.temp), str(self.config), str(self.projects), str(self.trash))
        self.assertEqual([], [p.name for p in self.config.parent.iterdir() if p.name.startswith(".claude.json.")])

    def test_windows_sends_the_transcript_folders_to_the_recycle_bin_with_no_trash_command(self):
        self.seed()
        (self.projects / (self.munged + "-hub")).mkdir()
        with mock.patch.object(e2e_clean.oscompat, "WINDOWS", True), \
                mock.patch.object(e2e_clean.oscompat, "recycle") as recycle:
            e2e_clean.clean(str(self.temp), str(self.config), str(self.projects), None)
        recycle.assert_called_once_with(str(self.projects / (self.munged + "-hub")))

    def test_windows_e2e_sh_passes_the_profile_through_env_i(self):
        """With uname saying MINGW64, the re-exec's env -i also carries SYSTEMROOT, USERPROFILE and PYTHONUTF8."""
        tools = self.root / "fakebin"
        tools.mkdir()
        record = self.root / "env-args"
        (tools / "uname").write_text("#!/bin/sh\necho MINGW64_NT-10.0\n")
        (tools / "env").write_text('#!/bin/sh\nprintf "%s\\n" "$@" >"$RECORD"\n')
        for name in ("uname", "env"):
            (tools / name).chmod(0o755)
        subprocess.run([BASH, str(HERE / "e2e.sh")], capture_output=True, text=True, timeout=30,
                       env={"HOME": str(self.root), "PATH": f"{tools}:/usr/bin:/bin", "RECORD": str(record),
                            "SYSTEMROOT": "C:\\Windows", "USERPROFILE": "C:\\Users\\me", "USERNAME": "me"})
        args = record.read_text().splitlines()
        for want in ("SYSTEMROOT=C:\\Windows", "USERPROFILE=C:\\Users\\me", "USERNAME=me", "PYTHONUTF8=1", "E2E_CLEAN=1"):
            self.assertIn(want, args)

    def test_posix_e2e_sh_adds_no_windows_variable_to_env_i(self):
        tools = self.root / "fakebin"
        tools.mkdir()
        record = self.root / "env-args"
        (tools / "uname").write_text("#!/bin/sh\necho Linux\n")  # Git Bash on Windows would say MINGW64
        (tools / "env").write_text('#!/bin/sh\nprintf "%s\\n" "$@" >"$RECORD"\n')
        for name in ("uname", "env"):
            (tools / name).chmod(0o755)
        subprocess.run([BASH, str(HERE / "e2e.sh")], capture_output=True, text=True, timeout=30,
                       env={"HOME": str(self.root), "PATH": f"{tools}:/usr/bin:/bin", "RECORD": str(record),
                            "SYSTEMROOT": "C:\\Windows"})
        self.assertFalse([a for a in record.read_text().splitlines() if a.startswith("SYSTEMROOT=")])

    def test_e2e_sh_parses_and_picks_the_windows_interpreter_and_a_cmd_godot_stub(self):
        self.assertEqual(0, subprocess.run([BASH, "-n", str(HERE / "e2e.sh")]).returncode)
        text = (HERE / "e2e.sh").read_text(encoding="utf-8")
        self.assertIn('PYBIN="$H/.venv/Scripts/python.exe"', text)
        self.assertIn('export GODOT="$T/bin/godot.cmd"', text)
        self.assertIn('T=$(cygpath -m "$T")', text)


if __name__ == "__main__":
    unittest.main()
