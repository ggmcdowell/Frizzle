"""Seat note -- a PostToolUse hook on Bash: tell the session when a cd leaves the hub's saved workflows.

The Workflow tool finds a saved workflow by name in <present folder>/.claude/workflows and checks the
name before any hook runs: from another folder the call fails "not found. Available: deep-research",
and neither PreToolUse nor PostToolUseFailure sees it (CLI 2.1.316). So no gate
can refuse that call. This note fires at the cd that causes it, while the session can still act:
it names the scriptPath form that works from any folder.

The hook event's cwd is the folder after the command. Silent unless the command holds a cd and that
folder lacks one of the hub's saved workflows. Prints a hookSpecificOutput additionalContext; never blocks.
"""
import json
import os
import re
import sys

HUB_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CD = re.compile(r"(?:^|[;&|(\n]|\bthen|\bdo)\s*cd(?:\s|$)")
NOTE = (
    "seat-note: the shell now sits in %s, outside the hub root %s. From here a Workflow call by name "
    "fails 'not found' before any hook can refuse it. Call a hub workflow by "
    "scriptPath: %s/.claude/workflows/<name>.js (saved: %s). Or cd %s first."
)


def hub_workflows():
    folder = os.path.join(HUB_ROOT, ".claude", "workflows")
    try:
        return sorted(f[:-3] for f in os.listdir(folder) if f.endswith(".js"))
    except OSError:
        return []


def note(event):
    """The note text, or None when the call keeps the hub's saved workflows in reach."""
    if not isinstance(event, dict) or event.get("tool_name") != "Bash":
        return None
    command = (event.get("tool_input") or {}).get("command")
    cwd = event.get("cwd")
    if not isinstance(command, str) or not isinstance(cwd, str) or not CD.search(command):
        return None
    names = hub_workflows()
    here = os.path.join(cwd, ".claude", "workflows")
    if not names or all(os.path.isfile(os.path.join(here, n + ".js")) for n in names):
        return None
    return NOTE % (cwd, HUB_ROOT, HUB_ROOT, ", ".join(names), HUB_ROOT)


def main():
    try:
        event = json.loads(sys.stdin.buffer.read())
    except (json.JSONDecodeError, ValueError):
        return 0
    text = note(event)
    if text:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
