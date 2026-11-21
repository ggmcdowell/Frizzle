"""The CI workflow runs the hub smoke command that harness.toml names, and the conform gate, on macOS and Windows."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Drawer"))
import harness_config  # noqa: E402

CI = ROOT / ".github" / "workflows" / "ci.yml"


class CiTests(unittest.TestCase):
    def test_the_workflow_is_valid_yaml_with_a_macos_job_and_a_windows_job(self):
        # An unquoted ': ' in a step name made GitHub reject the file: the first public run had no job.
        import yaml
        doc = yaml.safe_load(CI.read_text(encoding="utf-8"))
        self.assertEqual(["hub", "hub-windows"], list(doc["jobs"]))
        self.assertEqual("macos-latest", doc["jobs"]["hub"]["runs-on"])
        self.assertEqual("windows-latest", doc["jobs"]["hub-windows"]["runs-on"])
        self.assertEqual("bash", doc["jobs"]["hub-windows"]["defaults"]["run"]["shell"])  # Git Bash, as on a seat
        for job in doc["jobs"].values():
            self.assertTrue(all(isinstance(step.get("name", ""), str) for step in job["steps"]))

    def test_the_workflow_runs_the_smoke_command_of_harness_toml(self):
        text = CI.read_text(encoding="utf-8")
        command = harness_config.load(ROOT)["smoke"]["command"].replace("{python}", ".venv/bin/python -B")
        self.assertEqual(2, text.count(f"run: {command}"))  # the macOS job and the Windows job

    def test_the_workflow_runs_on_macos_and_windows_installs_the_tools_syncs_and_runs_conform(self):
        text = CI.read_text(encoding="utf-8")
        for needed in ("runs-on: macos-latest", "brew install uv gitleaks", "runs-on: windows-latest",
                       "python -m pip install uv", "gitleaks_8.30.1_windows_x64.zip", "Get-FileHash",
                       "Drawer/runtime/seat.py shims", "run: uv sync",
                       "run: .venv/bin/python -B Drawer/harness-conform.py", "pull_request:", "branches: [main]"):
            with self.subTest(needed=needed):
                self.assertIn(needed, text)
        self.assertEqual(2, text.count("run: uv sync"))
        self.assertNotIn("e2e.sh", text.split("jobs:")[1])  # the paid run stays manual


    def test_the_workflow_reads_only_and_runs_only_the_offline_conform_checks(self):
        # A runner has no seat profile, no claude and no local .mcp.json: the full conform run and the untracked
        # settings check are red there, so settings runs as settings-sync --tracked.
        import importlib.util
        spec = importlib.util.spec_from_file_location("conform", ROOT / "Drawer" / "harness-conform.py")
        conform = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(conform)
        text = CI.read_text(encoding="utf-8")
        self.assertIn("permissions:\n  contents: read", text)
        lines = [l for l in text.splitlines() if "run: .venv/bin/python -B Drawer/harness-conform.py" in l]
        self.assertEqual(2, len(lines))  # one per job
        for line in lines:
            names = line.split("harness-conform.py", 1)[1].split()
            self.assertEqual(sorted(set(conform.OFFLINE) - {"settings"}), sorted(names))
        self.assertEqual(2, text.count("run: .venv/bin/python -B Drawer/settings-sync.py --tracked"))
        # The tests swap HOME, so the uv Python must live outside it (a clean-HOME run went red without it).
        self.assertEqual(2, text.count('echo "UV_PYTHON_INSTALL_DIR=$RUNNER_TEMP/uv-python" >> "$GITHUB_ENV"'))
        # GitHub rejects the runner context in a job-level env block: the first public run failed on it.
        self.assertNotIn("${{ runner.", text)


if __name__ == "__main__":
    unittest.main()
