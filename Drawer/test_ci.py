"""The CI workflow runs the hub smoke command that harness.toml names, and the conform gate, on macOS and Windows."""
import re
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

    # Defect: the gitleaks pin was 63 hex characters, so the Windows job threw a checksum mismatch on every run.
    # Injection: drop one character of the pin.
    def test_each_pinned_sha256_is_64_hex_characters(self):
        text = CI.read_text(encoding="utf-8")
        pins = re.findall(r'-Algorithm SHA256\)\.Hash -ne "([^"]*)"', text)
        self.assertTrue(pins)
        for pin in pins:
            with self.subTest(pin=pin):
                self.assertRegex(pin, r"^[0-9A-Fa-f]{64}$")


    # Defect: a red hub file showed only its last output line in the CI log, so a red run was undiagnosable.
    # Injection: drop the if: failure() step or the tee from either job.
    def test_each_job_keeps_the_smoke_output_and_reruns_each_red_file_verbosely_on_failure(self):
        import yaml
        doc = yaml.safe_load(CI.read_text(encoding="utf-8"))
        smoke = harness_config.load(ROOT)["smoke"]["command"].replace("{python}", ".venv/bin/python -B")
        for name, job in doc["jobs"].items():
            with self.subTest(job=name):
                steps = job["steps"]
                tee = [i for i, s in enumerate(steps) if s.get("run", "").startswith(smoke)]
                self.assertEqual(1, len(tee))
                self.assertEqual("bash", steps[tee[0]]["shell"])  # pipefail: the smoke's exit code survives the pipe
                self.assertRegex(steps[tee[0]]["run"], r"\|\s*tee smoke\.log")
                rerun = steps[tee[0] + 1]  # the step right after the smoke
                self.assertEqual("failure()", rerun["if"])
                self.assertIn("smoke: HUB RED", rerun["run"])
                self.assertIn("smoke.log", rerun["run"])
                self.assertIn("-m unittest -v", rerun["run"])

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
