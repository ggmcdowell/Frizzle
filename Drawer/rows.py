#!/usr/bin/env python3
"""rows -- Flight's card-run result rows: Flight runs each task as a card-run Workflow itself, and this script claims
the task, writes its rows and releases a claim the run left doing.

    <python> Drawer/rows.py start --task <uuid> [--notes <file|->] [--incident <id> | --no-incident '<why>']
    <python> Drawer/rows.py row --task <uuid> [--file <f>]      the card-run result JSON on stdin or in a file
    <python> Drawer/rows.py open                                 the runs of this session still out

The session is CLAUDE_CODE_SESSION_ID: Flight's own, which its Workflow legs share.

start reads the card from mem and gates it: it needs a Skills: line, and a harness card needs --incident (an open
incident record; its INCIDENT STEP joins the notes) or --no-incident (runlib.incident_step). It refuses a task with a
run out, unless the run's start row belongs to another session that runlib.session_state reads dead: then it
writes a blocked row that closes it (CLOSED line) and goes on; an own, live or unknown session still refuses. BUSY (exit 3) while [orchestrator] max_items runs of this session are out (runlib.run_limit). Then it
claims the task (task-lifecycle begin --session <the session>), writes the start row and prints
START {"workflow":"card-run","args":{"task","claim_id","notes"?}}: Flight calls the Workflow tool card-run with those
args. A refused claim writes a blocked row and exits 1.

row reads the result on stdin, or from --file and then removes that file. It keeps the result row fields (ROW_KEYS),
releases the card when it is still doing under the start row's claim_id (task-lifecycle release) and writes the
result row. It prints ROW <task8> <status>, then OUT <n>: <task8 ...> or ALL IN for this session. Last it runs
product_worktree.sweep (a done task's product worktree goes now) and prints one PRODUCT SWEEP line; a sweep fault
never changes its exit code.

open prints one OUT <task8> claim <claim8> at <time> line per run of this session that is out, or NONE.

Rows: <state_root>/flight-handoff/<task8>/outbox.log, one JSON object per line: {row, task, at, session, ...}.
start {workflow, claim_id}; result {status, claim_id, commits, land, delivered, need, extras, cause, claim}; blocked
{cause}. Closing rows: result and blocked (runlib.CLOSING); a run is out from its start row to its closing row
(runlib.open_runs). A refusal prints ROWS-REFUSED <why>, exit 1.
Owner: Harness.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys

HERE = Path(__file__).resolve().parent
HUB = HERE.parent
sys.path.insert(0, str(HERE))
import harness_config  # noqa: E402
import product_worktree  # noqa: E402
import runlib  # noqa: E402

CFG = harness_config.load(HUB)
HANDOFF = runlib.handoff(CFG)
LIFECYCLE = HERE / "task-lifecycle.py"
MEM = HERE / "mem" / "mem.py"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SKILLS_RE = re.compile(r"\bSkills:[ \t]*([a-z0-9, \t-]*)", re.I)
ROW_KEYS = ("status", "claim_id", "commits", "land", "delivered", "need", "extras", "cause")  # the card-run result row
BUSY = 3


class Refused(Exception):
    """A refusal: main prints ROWS-REFUSED <why> and exits 1."""


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def session():
    value = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip().lower()
    if not UUID_RE.fullmatch(value):
        raise Refused("CLAUDE_CODE_SESSION_ID is no full session id; run rows.py from the Flight session")
    return value


def task_arg(value):
    value = (value or "").strip().lower()
    if not UUID_RE.fullmatch(value):
        raise Refused("--task takes the full task uuid")
    return value


def write_row(task, kind, **fields):
    path = HANDOFF / task[:8] / "outbox.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"row": kind, "task": task, "at": now(), **fields}
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n")


def lifecycle(*argv):
    """(exit code, the JSON line) of one task-lifecycle verb."""
    try:
        run = subprocess.run([sys.executable, "-B", str(LIFECYCLE), *argv], cwd=HUB, capture_output=True, text=True,
                             timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, {"error": f"task-lifecycle {argv[0]} did not run: {type(exc).__name__}"}
    lines = run.stdout.strip().splitlines()
    try:
        out = json.loads(lines[-1]) if lines else None
    except ValueError:
        out = None
    if not isinstance(out, dict):
        out = {"error": (run.stderr.strip() or run.stdout.strip())[-300:] or f"exit {run.returncode}"}
    return run.returncode, out


def get_card(task):
    """The mem card; Refused when mem does not answer."""
    try:
        run = subprocess.run([sys.executable, "-B", str(MEM), "get", task, "--json"], cwd=HUB, capture_output=True,
                             text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refused(f"mem get {task[:8]} did not run: {type(exc).__name__}") from exc
    lines = run.stdout.strip().splitlines()
    try:
        card = json.loads(lines[-1]) if lines else None
    except ValueError:
        card = None
    if run.returncode or not isinstance(card, dict) or "error" in card:
        why = card.get("error") if isinstance(card, dict) else (run.stderr.strip() or run.stdout.strip())[-300:]
        raise Refused(f"mem get {task[:8]}: {why or f'exit {run.returncode}'}")
    return card


def read_text(value):
    """The text of a --notes or --file value: '-' is stdin, else a file."""
    if value == "-":
        return sys.stdin.read()
    try:
        return Path(value).read_text(encoding="utf-8")
    except OSError as exc:
        raise Refused(f"{value} does not read: {exc}") from exc


def cmd_start(a):
    me, task = session(), task_arg(a.task)
    card = get_card(task)
    found = SKILLS_RE.search(card.get("body") or "")
    skills = [w.lower() for w in re.split(r"[,\s]+", found[1]) if w] if found else []
    if not skills:  # the harness gate reads this line: no line fails closed
        raise Refused(f"task {task[:8]}: the card has no Skills: line; fix the card")
    try:
        step, _ = runlib.incident_step(get_card, skills, incident=a.incident, no_incident=a.no_incident)
        limit = runlib.run_limit(CFG)
    except runlib.Usage as exc:
        raise Refused(f"task {task[:8]}: {exc}") from exc
    old = runlib.runs(HANDOFF)[0].get(task)
    if old:
        theirs = old.get("session") or ""
        state = "own" if theirs == me else runlib.session_state(theirs, Path.home())
        if state != "dead":
            raise Refused(f"task {task[:8]} has a run out (its start row session is {state}); "
                          "pass its result to rows.py row first")
        write_row(task, "blocked", session=me, cause=f"closed the start row of dead session {theirs[:8]}")
        print(f"CLOSED {task[:8]} start row of dead session {theirs[:8]}")
    out = runlib.open_runs(HANDOFF, me)
    if len(out) >= limit:
        print(f"BUSY {len(out)} of max_items {limit} runs out ({' '.join(t[:8] for t in out)}); "
              "start this task after a row comes in")
        return BUSY
    notes = "\n\n".join(n for n in ((read_text(a.notes).strip() if a.notes else ""), step or "") if n)
    rc, got = lifecycle("begin", task, "--session", me)
    if rc or got.get("result") != "CLAIMED" or not got.get("claim_id"):
        write_row(task, "blocked", session=me, cause=f"claim refused: {got.get('error') or got}"[:600])
        raise Refused(f"task {task[:8]}: claim refused: {got.get('error') or got}"[:300])
    write_row(task, "start", session=me, workflow="card-run", claim_id=got["claim_id"])
    args = {"task": task, "claim_id": got["claim_id"], **({"notes": notes} if notes else {})}
    print("START " + json.dumps({"workflow": "card-run", "args": args}, separators=(",", ":"), ensure_ascii=False))
    return 0


def parse_result(text):
    """The result object in the text, or None."""
    try:
        return json.loads(text[text.index("{"):text.rindex("}") + 1])
    except ValueError:
        return None


def settle_claim(task, claim, reason):
    """Release the card's claim when the card is still doing under claim; the result word, 'none' when it is not,
    or the error."""
    if not claim:
        return "none"
    try:
        card = get_card(task)
    except Refused as exc:
        return str(exc)[:300]
    if card.get("status") != "doing" or card.get("claim_id") != claim:
        return "none"
    rc, out = lifecycle("release", task, "--claim-id", claim, "--reason", reason)
    return out.get("result") if rc == 0 and out.get("result") else str(out.get("error") or out)[:300]


def out_line(me):
    out = runlib.open_runs(HANDOFF, me)
    return f"OUT {len(out)}: {' '.join(t[:8] for t in out)}" if out else "ALL IN"


def cmd_row(a):
    me, task = session(), task_arg(a.task)
    start = runlib.runs(HANDOFF)[0].get(task)
    if not start:
        raise Refused(f"task {task[:8]} has no run out; rows.py start writes its start row")
    path = Path(a.file) if a.file and a.file != "-" else None  # '-' is stdin, no file to remove
    text = (read_text(a.file) if path else sys.stdin.read()).strip()
    result = parse_result(text)
    if isinstance(result, dict) and isinstance(result.get("status"), str):
        keep = {k: result[k] for k in ROW_KEYS if k in result}
        if isinstance(keep.get("cause"), str):
            keep["cause"] = keep["cause"][:600]
    elif isinstance(result, dict):  # a workflow with its own return shape
        keep = {"status": "returned", "result": json.dumps(result, ensure_ascii=False)[:600]}
    else:
        keep = {"status": "failed", "cause": (text or "the workflow returned nothing")[:600]}
    keep["claim"] = settle_claim(task, start.get("claim_id"), f"card-run returned {keep['status']} with the claim held")
    write_row(task, "result", session=me, **keep)
    if path:
        path.unlink(missing_ok=True)  # a scratch row file never outlives its row
    print(f"ROW {task[:8]} {keep['status']}")
    print(out_line(me))
    try:
        line = "; ".join(product_worktree.sweep(CFG)) or "nothing to remove"
    except Exception as exc:
        line = f"held: {(str(exc).strip().splitlines() or [type(exc).__name__])[0]}"
    print(f"PRODUCT SWEEP: {line}.")
    return 0


def cmd_open(_a):
    me = session()
    out = runlib.runs(HANDOFF, me)[0]
    for task in sorted(out):
        print(f"OUT {task[:8]} claim {str(out[task].get('claim_id') or '')[:8]} at {out[task].get('at')}")
    if not out:
        print("NONE")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="rows.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("--task", required=True)
    s.add_argument("--notes")
    gate = s.add_mutually_exclusive_group()
    gate.add_argument("--incident")
    gate.add_argument("--no-incident")
    s = sub.add_parser("row")
    s.add_argument("--task", required=True)
    s.add_argument("--file")
    sub.add_parser("open")
    a = p.parse_args(argv)
    try:
        return {"start": cmd_start, "row": cmd_row, "open": cmd_open}[a.cmd](a)
    except Refused as exc:
        print(f"ROWS-REFUSED {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
