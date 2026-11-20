#!/usr/bin/env python3
"""Flight's board in one call: what to start, what waits, and the card-runs that are out.

  <python> Drawer/flight-plan.py [--json]

Lines, in this order (--json gives the same as one JSON object):
  RUNS none | RUNS <n>: <task8> ...
        the card-runs of any session that are out: a start row of Drawer/rows.py with no closing row
        (runlib.open_runs over <state_root>/flight-handoff).
  GO   <task8> chain=<n> <priority> <area> <milestone> <title>
  PARK <task8> chain=<n> <priority> <area> <milestone> <title> | <reason>
  HOLD <task8> chain=<n> <priority> <area> <milestone> <title> | shares <files> with <task8>

Tasks: mem list --ready (open, unclaimed, every depends_on closed), in its order: the longest chain of open
tasks that wait on it first, then priority, then age. A todo task is GO, a parked one PARK with the detail of
its last task_log row. A task whose Files: line shares a file with a doing task, or with a task above it on
this board, is HOLD instead: start it after that one. milestone is the value of its milestone:<m> tag, else -.
Flight starts every GO task as a card-run (Drawer/rows.py start, then the card-run workflow).

Reads only. It opens the mem database in-process.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "mem"))
import harness_config  # noqa: E402
import mem  # noqa: E402
import runlib  # noqa: E402


def files_of(body):
    """The paths of a task body's Files: line."""
    found = re.search(r"^Files:(.*)$", body or "", re.M)
    return {f.strip() for f in (found[1] if found else "").split(",") if f.strip()}


def park_reason(m, task):
    log = m.task_log(task["id"])
    return " ".join(str((log[-1].get("detail") or log[-1].get("event")) if log else "no log row").split())[:120]


def milestone_of(task):
    return next((t.split(":", 1)[1] for t in task.get("tags") or [] if t.startswith("milestone:")), "-")


def board(m, handoff):
    """The board as a dict. handoff is the result rows folder."""
    taken = {f: t["id"][:8] for t in m.list(kind="task", status="doing", limit=1000) for f in files_of(t.get("body"))}
    tasks = []
    for t in m.ready():
        row = {"task8": t["id"][:8], "id": t["id"], "chain": t.get("chain", 0),
               "priority": t.get("priority") or "-", "area": t.get("area") or "-", "milestone": milestone_of(t),
               "title": t.get("title") or ""}
        mine = files_of(t.get("body"))
        shared = sorted(f for f in mine if f in taken)
        if shared:
            row.update(verb="HOLD", reason=f"shares {', '.join(shared)} with {taken[shared[0]]}")
        elif t.get("status") == "parked":
            row.update(verb="PARK", reason=park_reason(m, t))
        else:
            row.update(verb="GO")
        if row["verb"] != "HOLD":
            taken.update({f: row["task8"] for f in mine if f not in taken})
        tasks.append(row)
    return {"runs": [t[:8] for t in runlib.open_runs(handoff)], "tasks": tasks}


def lines(b):
    out = [f"RUNS {len(b['runs'])}: {' '.join(b['runs'])}" if b["runs"] else "RUNS none"]
    for t in b["tasks"]:
        head = (f"{t['verb']} {t['task8']} chain={t['chain']} {t['priority']} {t['area']} "
                f"{t['milestone']} {t['title']}")
        out.append(head + (f" | {t['reason']}" if t.get("reason") else ""))
    return out


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv in (["--help"], ["-h"]):
        print(__doc__)
        return 0
    if argv not in ([], ["--json"]):
        print("usage: <python> Drawer/flight-plan.py [--json]", file=sys.stderr)
        return 2
    cfg = harness_config.load(HERE.parent)
    m = mem.Mem(mem.db_path(cfg.get("mem", {})))
    b = board(m, runlib.handoff(cfg))
    print(json.dumps(b, ensure_ascii=False) if argv else "\n".join(lines(b)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
