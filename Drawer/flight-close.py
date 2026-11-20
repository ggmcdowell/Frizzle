#!/usr/bin/env python3
"""Flight's checked close of one mem task: CLOSE only when the delivery holds again.

    .venv/bin/python -B Drawer/flight-close.py <task id or id8> [--json]

CLOSE needs mem status done, evidence set and delivered=1, and each cited repo=sha still on origin/main after a
fresh fetch (task_delivery.verify). Exit and output:
  0 CLOSE <id8>: ...        the task holds
  1 HOLD <id8>: <why>       the task does not hold
  2 ERROR <id8>: <why>      mem failed
--json prints one JSON line instead: {"result": "CLOSE"|"HOLD"|"ERROR", "id", "why"|"commits", ...}.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import task_delivery  # noqa: E402

PROG = ".venv/bin/python -B Drawer/flight-close.py"
MEM = HERE / "mem" / "mem.py"


class Hold(Exception):
    pass


def read_card(task):
    run = subprocess.run([sys.executable, "-B", str(MEM), "get", task, "--json"], capture_output=True, text=True,
                         timeout=120)
    lines = run.stdout.strip().splitlines()
    try:
        card = json.loads(lines[-1]) if lines else None
    except ValueError:
        card = None
    if not isinstance(card, dict) or run.returncode or "error" in card:
        why = card.get("error") if isinstance(card, dict) else (run.stderr.strip() or run.stdout.strip())[-300:]
        raise RuntimeError(f"mem get {task}: {why or f'exit {run.returncode}'}")
    return card


def commits_of(card):
    value = card.get("commits") or []
    if isinstance(value, str):
        value = json.loads(value)
    return [f"{c['repo']}={c['commit']}" for c in value]


def card_gap(card):
    """Hold with the reason the task does not show a checked, still-delivered close."""
    if card.get("kind") != "task":
        raise Hold(f"{card.get('id')} is a {card.get('kind')}, not a task")
    if card.get("status") != "done":
        raise Hold(f"task is {card.get('status')}, not done")
    if not str(card.get("evidence") or "").strip():
        raise Hold("task is done with no evidence: close it by task-lifecycle.py complete")
    if not card.get("delivered"):
        raise Hold("task is done with delivered=0: it was closed outside the checked complete")
    commits = commits_of(card)
    for cite in commits:  # one at a time, so the hold names the sha that fails
        try:
            task_delivery.verify([cite])
        except task_delivery.DeliveryError as exc:
            raise Hold(f"delivery does not hold again: {exc}") from exc
    return commits


def judge(a):
    try:
        card = read_card(a.task)
    except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
        return 2, {"result": "ERROR", "id": a.task, "why": str(exc)}
    row = {"id": card.get("id", a.task)}
    try:
        commits = card_gap(card)
    except Hold as exc:
        return 1, {"result": "HOLD", **row, "why": str(exc)}
    return 0, {"result": "CLOSE", **row, "commits": commits, "evidence": str(card.get("evidence"))[:600]}


def text_line(out):
    head = f"{out['result']} {out['id'][:8]}: "
    if out["result"] != "CLOSE":
        return head + out["why"]
    shas = ", ".join(c.rpartition("=")[2][:12] for c in out["commits"]) or "no file changes"
    return head + f"done with evidence; delivered on origin/main: {shas}"


def main(argv=None):
    ap = argparse.ArgumentParser(prog=PROG, description=__doc__.split("\n\n")[0])
    ap.add_argument("task")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    code, out = judge(a)
    print(json.dumps(out, ensure_ascii=False) if a.json else text_line(out))
    return code


if __name__ == "__main__":
    sys.exit(main())
