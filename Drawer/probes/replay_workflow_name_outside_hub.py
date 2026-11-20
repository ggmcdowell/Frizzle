"""Replay: a worker ran a Bash cd to a state folder outside the hub, then called a saved Workflow by name
and got "not found" with no refusal; it ran a weaker review. The PreToolUse gate never saw the
call: the CLI checks a workflow name before any hook. Fix: Drawer/seat-note.py, a
PostToolUse note on that cd that names the scriptPath, registered on Bash in the settings render.

The replay checks the rendered .claude/settings.json registers the note on Bash, then feeds it the
worker's cd event and expects a note that names the hub's risky-review scriptPath folder.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib

INCIDENT = "a named Workflow call outside the hub root failed with no refusal"


def replay(hub, product):
    hub = Path(hub)
    settings = json.loads((hub / ".claude" / "settings.json").read_text())
    commands = [h.get("command", "") for e in settings.get("hooks", {}).get("PostToolUse", [])
                if e.get("matcher") == "^Bash$" for h in e.get("hooks", [])]
    if not any("seat-note.py" in c for c in commands):
        return "the settings render registers no seat-note PostToolUse hook on Bash"
    state = tomllib.loads((hub / "harness.toml").read_text())["project"]["state_root"] + "/jobs"
    event = {"hook_event_name": "PostToolUse", "tool_name": "Bash", "cwd": os.path.expanduser(state),
             "tool_input": {"command": f"cd {state}; ls"}}
    out = subprocess.run([sys.executable, "-B", str(hub / "Drawer" / "seat-note.py")], input=json.dumps(event),
                         capture_output=True, text=True, timeout=60)
    try:
        text = json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"]
    except (ValueError, KeyError, TypeError):
        return f"seat-note gave no note on a cd out of the hub (exit {out.returncode})"
    if str(hub / ".claude" / "workflows") not in text or "risky-review" not in text:
        return "the seat note does not name the risky-review scriptPath"
    return None
