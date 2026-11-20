"""Replay: on 2026-10-29 Flight had 4 incidents and wrote 0 lessons until the human asked. Fix: a lesson gate refuses
while an incident has no lesson. It lives in `incident.py gate`; the trial log that held it
first is gone.

The replay runs the hub's real Drawer/incident.py as its own process on a scratch mem DB (<env_prefix>_MEM_DB) and a scratch
incidents file, so the live memory gets nothing. It opens an incident record through incident.py; `incident.py gate`
must exit 1 and name the record. Then a lesson closes it (mem add --kind lesson --closes <id>); the gate must exit 0.
"""
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

INCIDENT = "Flight closed a cycle with 4 incidents and wrote no lesson until the human asked"
WAIT = 90  # seconds per mem or incident.py process


def replay(hub, product):
    sys.path.insert(0, str(hub / "Drawer"))
    import harness_config
    with tempfile.TemporaryDirectory() as tmp:
        cfg = harness_config.load(hub)
        env = {**os.environ, harness_config.env_name("MEM_DB", cfg): str(Path(tmp) / "mem.db"),
               harness_config.env_name("MEM_EMBED", cfg): "off",
               harness_config.env_name("INCIDENTS", cfg): str(Path(tmp) / "incidents.jsonl")}
        run = lambda *argv: subprocess.run([sys.executable, "-B", *argv], capture_output=True, text=True,  # noqa: E731
                                           env=env, cwd=hub, timeout=WAIT)
        incident, mem = str(hub / "Drawer" / "incident.py"), str(hub / "Drawer" / "mem" / "mem.py")
        gate = run(incident, "gate")
        if gate.returncode != 0:
            return f"the gate is not green on an empty mem: exit {gate.returncode}, {gate.stdout}{gate.stderr}"
        opened = run(incident, "probe fault; impact: none, a probe; cause: the unlessoned replay")
        card = re.search(r"^INCIDENT ([0-9a-f]{8}) ", opened.stdout, re.M)
        if opened.returncode != 0 or not card:
            return f"incident.py opened no record: exit {opened.returncode}, {opened.stdout}{opened.stderr}"
        gate = run(incident, "gate")
        if gate.returncode != 1 or f"UNLESSONED {card.group(1)} " not in gate.stdout:
            return f"an unlessoned incident did not turn the gate red: exit {gate.returncode}, {gate.stdout}"
        closed = run(mem, "add", "--kind", "lesson", "--closes", card.group(1), "--body",
                     "A probe fault needs no fix; this lesson closes its record to prove the lesson gate.")
        if closed.returncode != 0:
            return f"mem refused the closing lesson: exit {closed.returncode}, {closed.stdout}{closed.stderr}"
        gate = run(incident, "gate")
        if gate.returncode != 0 or "LESSON GATE GREEN" not in gate.stdout:
            return f"a closing lesson did not turn the gate green: exit {gate.returncode}, {gate.stdout}"
    return None
