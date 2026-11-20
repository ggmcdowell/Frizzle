"""The CI workflow runs the hub smoke command that harness.toml names, and the conform gate."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Drawer"))
import harness_config  # noqa: E402

CI = ROOT / ".github" / "workflows" / "ci.yml"


class CiTests(unittest.TestCase):
    def test_the_workflow_runs_the_smoke_command_of_harness_toml(self):
        text = CI.read_text(encoding="utf-8")
        command = harness_config.load(ROOT)["smoke"]["command"].replace("{python}", ".venv/bin/python -B")
        self.assertIn(f"run: {command}", text)

    def test_the_workflow_runs_on_macos_installs_the_tools_syncs_and_runs_conform(self):
        text = CI.read_text(encoding="utf-8")
        for needed in ("runs-on: macos-latest", "brew install uv gitleaks", "run: uv sync",
                       "run: .venv/bin/python -B Drawer/harness-conform.py", "pull_request:", "branches: [main]"):
            with self.subTest(needed=needed):
                self.assertIn(needed, text)
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
        line = next(l for l in text.splitlines() if "run: .venv/bin/python -B Drawer/harness-conform.py" in l)
        names = line.split("harness-conform.py", 1)[1].split()
        self.assertEqual(sorted(set(conform.OFFLINE) - {"settings"}), sorted(names))
        self.assertIn("run: .venv/bin/python -B Drawer/settings-sync.py --tracked", text)
        # The tests swap HOME, so the uv Python must live outside it (a clean-HOME run went red without it).
        self.assertIn('echo "UV_PYTHON_INSTALL_DIR=$RUNNER_TEMP/uv-python" >> "$GITHUB_ENV"', text)
        # GitHub rejects the runner context in a job-level env block: the first public run failed on it.
        self.assertNotIn("${{ runner.", text)


if __name__ == "__main__":
    unittest.main()
