"""Replay: a session edited a product test file while the product gate ran, and the repeat leg went red with
SkipAllowList.GateAssertion: the tests the gate listed no longer reconciled with the run.
Fix: doc-gate refuses a write under a tests/ folder of a product checkout while its
gate verdict file (doc-gate's GATE_VERDICT) says running.

The replay clones the hub's HEAD into a temp folder and points the clone's [product] path
(harness.local.toml) at a temp product checkout. It feeds the clone's doc-gate an Edit of a test file:
with the verdict running it expects a deny that names the gate; with the verdict done, a pass.
DOC_GATE=dry: no log line.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

INCIDENT = "a test file edited while the product gate ran reddened its repeat leg"


def gate_verdict(clone):
    """The verdict file doc-gate reads, relative to a product checkout (its GATE_VERDICT)."""
    read = ("import importlib.util, sys; spec = importlib.util.spec_from_file_location('g', sys.argv[1]); "
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); print(m.GATE_VERDICT)")
    done = subprocess.run([sys.executable, "-B", "-c", read, str(clone / "Drawer" / "doc-gate.py")],
                          capture_output=True, text=True, timeout=60, cwd=clone, check=True)
    return done.stdout.strip().splitlines()[-1]


def decision(clone, target, env):
    payload = {"tool_name": "Edit", "cwd": str(clone),
               "tool_input": {"file_path": str(target), "old_string": "a", "new_string": "b"}}
    gate = subprocess.run([sys.executable, "-B", str(clone / "Drawer" / "doc-gate.py")], input=json.dumps(payload),
                          capture_output=True, text=True, timeout=60, cwd=clone, env=env)
    try:
        return json.loads(gate.stdout)["hookSpecificOutput"]
    except (ValueError, KeyError, TypeError):
        return {}


def replay(hub, product):
    with tempfile.TemporaryDirectory() as tmp:
        clone, repo = Path(tmp) / "hub", Path(tmp) / "product"
        done = subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", str(hub), str(clone)],
                              capture_output=True, text=True, timeout=120)
        if done.returncode:
            return f"git clone of the hub failed: {done.stderr.strip()}"
        (repo / "app").mkdir(parents=True)
        (repo / "target").mkdir()
        subprocess.run(["git", "init", "--quiet", str(repo)], check=True, timeout=60)
        (clone / "harness.local.toml").write_text(f"[product]\npath = {json.dumps(str(repo / 'app'))}\n",
                                                  encoding="utf-8")
        target = repo / "tests" / "save_test.gd"
        env = {k: v for k, v in os.environ.items()  # the clone's harness.local.toml binds the product: read it
               if not k.endswith("_PRODUCT_PATH") and k != "HARNESS_NO_LOCAL_TOML"}
        env["DOC_GATE"] = "dry"
        verdict = repo / gate_verdict(clone)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        verdict.write_text(json.dumps({"status": "running", "startedAtUtc": now}), encoding="utf-8")
        running = decision(clone, target, env)
        if running.get("permissionDecision") != "deny" or "gate is running" not in running.get("permissionDecisionReason", ""):
            return "doc-gate let a test file Edit pass while the product gate ran"
        verdict.write_text(json.dumps({"status": "done", "startedAtUtc": now, "green": True}), encoding="utf-8")
        if decision(clone, target, env).get("permissionDecision") == "deny":
            return "doc-gate refused a test file Edit after the product gate finished"
    return None
