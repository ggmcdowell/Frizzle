"""Replay: Pivot step 2 edited Drawer/doc-gate.py in the live hub, and a half-done edit broke every session's
hooks twice. Fix: doc-gate refuses a write to any non-ignored file in the live hub checkout.

The replay clones the hub's HEAD into a temp folder (a main checkout, as the live hub is), then feeds
that clone's doc-gate an Edit of its own Drawer/doc-gate.py. It expects a deny decision that names the
task worktree. DOC_GATE=dry: no log line.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

INCIDENT = "a session edited the gate in the live hub and broke every session's hooks"


def replay(hub, product):
    with tempfile.TemporaryDirectory() as tmp:
        clone = Path(tmp) / "hub"
        done = subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", str(hub), str(clone)],
                              capture_output=True, text=True, timeout=120)
        if done.returncode:
            return f"git clone of the hub failed: {done.stderr.strip()}"
        target = clone / "Drawer" / "doc-gate.py"
        payload = {"tool_name": "Edit", "cwd": str(clone),
                   "tool_input": {"file_path": str(target), "old_string": "a", "new_string": "b"}}
        env = {k: v for k, v in os.environ.items() if not k.endswith("_PRODUCT_PATH")}
        env["DOC_GATE"] = "dry"
        gate = subprocess.run([sys.executable, "-B", str(target)], input=json.dumps(payload), capture_output=True,
                              text=True, timeout=60, cwd=clone, env=env)
        try:
            decision = json.loads(gate.stdout)["hookSpecificOutput"]
        except (ValueError, KeyError, TypeError):
            decision = {}
        if decision.get("permissionDecision") != "deny" or "task worktree" not in decision.get("permissionDecisionReason", ""):
            return f"doc-gate let an Edit of the live gate pass (exit {gate.returncode})"
    return None
