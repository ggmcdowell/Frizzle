"""Selftest for Drawer/seat-note.py.

Run: .venv/bin/python -B -m unittest Drawer/test_seat_note.py.
Worker 01e87fe0 ran a Bash cd to the art state folder, then called Workflow name taste-check and got
"not found" with no refusal: the CLI checks the name before any PreToolUse hook. The note must fire on
that cd, through the registered hook, and name the scriptPath.
"""
import importlib.util
import json
import os
import subprocess
import tempfile
import tomllib
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
HUB = os.path.dirname(HERE)
SCRIPT = os.path.join(HERE, "seat-note.py")
WRAPPER = os.path.join(HERE, "hooks", "run-hook.sh")

# The exact Bash call and hook cwd that came before the failed Workflow call.
with open(os.path.join(HUB, "harness.toml"), "rb") as f:
    ART_RAW = tomllib.load(f)["project"]["state_root"] + "/art"  # the art state folder, ~-rooted
ART = os.path.expanduser(ART_RAW)
WORKER_CD = {
    "session_id": "01e87fe0-36b0-4a12-bfde-a8a46f0ed71e",
    "hook_event_name": "PostToolUse",
    "cwd": ART,
    "tool_name": "Bash",
    "tool_input": {"command": f"cd {ART_RAW}; python3 - <<'E'\nfrom PIL import Image\nE",
                   "description": "Compose chest sheet"},
    "tool_response": {"stdout": "", "stderr": "", "interrupted": False},
}


def load():
    spec = importlib.util.spec_from_file_location("seat_note", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def hook(event):
    """Run the note the way the harness does: the wrapper in pass mode, the event on stdin."""
    out = subprocess.run(["sh", WRAPPER, "pass", SCRIPT], input=json.dumps(event), capture_output=True,
                         text=True, timeout=60, cwd=HUB)
    return out.stdout.strip()


class SeatNote(unittest.TestCase):
    def test_the_worker_cd_out_of_the_hub_gets_the_note_with_the_script_path(self):
        reply = json.loads(hook(WORKER_CD))["hookSpecificOutput"]
        self.assertEqual("PostToolUse", reply["hookEventName"])
        self.assertIn(ART, reply["additionalContext"])
        self.assertIn(os.path.join(HUB, ".claude", "workflows", "<name>.js"), reply["additionalContext"])
        self.assertIn("risky-review", reply["additionalContext"])

    def test_a_folder_that_holds_the_saved_workflows_and_a_call_with_no_cd_stay_silent(self):
        self.assertEqual("", hook(dict(WORKER_CD, cwd=HUB)))
        self.assertEqual("", hook(dict(WORKER_CD, tool_input={"command": f"ls {ART_RAW}"})))
        self.assertEqual("", hook(dict(WORKER_CD, tool_name="Read")))

    def test_cd_forms_in_a_chain_count(self):
        note = load().note
        with tempfile.TemporaryDirectory() as away:
            for command in ("cd /x", "ls && cd /x", "true; cd /x", "if x; then cd /x; fi", "(cd /x)"):
                with self.subTest(command=command):
                    self.assertIsNotNone(note(dict(WORKER_CD, cwd=away, tool_input={"command": command})))
            for command in ("echo abcd /x", "git commit -m 'cd x'", "grep -n cdx f"):
                with self.subTest(command=command):
                    self.assertIsNone(note(dict(WORKER_CD, cwd=away, tool_input={"command": command})))

    def test_the_manifest_registers_the_note_on_bash_post_tool_use(self):
        with open(os.path.join(HERE, "settings-manifest.json")) as f:
            entries = json.load(f)["hooks"]["PostToolUse"]
        self.assertIn({"matcher": "^Bash$",
                       "command": "sh \"{HUB}/Drawer/hooks/run-hook.sh\" pass \"{HUB}/Drawer/seat-note.py\"",
                       "timeout": 30}, entries)


if __name__ == "__main__":
    unittest.main(verbosity=1)
