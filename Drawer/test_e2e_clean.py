"""Tests for Drawer/e2e_clean.py and the preflight and PASS cleanup of Drawer/e2e.sh (the paid run itself is not run)."""
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
import e2e_clean  # noqa: E402

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
        rows, folders = e2e_clean.clean(str(self.temp), str(self.config), str(self.projects), str(self.trash))
        self.assertEqual((3, 2), (rows, folders))
        value = json.loads(self.config.read_text())
        self.assertEqual("u", value["userID"])
        self.assertEqual({str(self.root / "frz-e2e.AbC1234"), "/srv/other/work"}, set(value["projects"]))
        self.assertEqual(0o600, self.config.stat().st_mode & 0o777)
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
        done = subprocess.run(["/bin/bash", str(HERE / "e2e.sh")], capture_output=True, text=True, timeout=30,
                              env={"E2E_CLEAN": "1", "HOME": str(self.root), "PATH": str(tools)})
        self.assertEqual(2, done.returncode, done.stdout + done.stderr)
        self.assertIn("e2e needs trash on PATH", done.stdout)

    def test_e2e_sh_runs_the_cleanup_on_pass(self):
        text = (HERE / "e2e.sh").read_text(encoding="utf-8")
        self.assertIn('/usr/bin/python3 "$SRC/Drawer/e2e_clean.py" "$T"', text)
        self.assertNotIn('trash "$T" 2>/dev/null', text)


if __name__ == "__main__":
    unittest.main()
