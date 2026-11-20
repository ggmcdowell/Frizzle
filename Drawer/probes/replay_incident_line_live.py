"""Replay and canary: an INCIDENT line must open its record through the events watchdog, for the Flight.

A session once reported 7 faults and the watchdog opened no record: the nearest live miss was a line in
Markdown (`INCIDENT ...`, **INCIDENT** ...), and the watchdog read only a bare line. Fix: Drawer/flight-watchdog.py
reads a marked line too, when it holds its impact part.

Flight once wrote an INCIDENT line in its reply text and no record opened, as the watchdog never read Flight's
own transcript. Fix: the watchdog reads each live Flight's INCIDENT lines from its first read of that Flight on.

INCIDENT lines in reply text can stay in hidden thinking and never reach the transcript. Fix: Drawer/incident.py
opens the record from a tool call and prints its id; the watchdog reads the call and its result and wakes Flight
with that id; a text line that names no id is the backup.

The replay is end to end. It copies the hub's real flight-watchdog.py, harness_config.py and mem.py into a
scratch hub, gives a Flight a seat file and a sessions row on a live pid and a transcript-shaped file under a
scratch HOME, and runs `flight-watchdog.py events` as its own process on a scratch mem DB, so the live memory
gets nothing. Two paths, each red with its fix undone:
  Flight  -- each INCIDENT line (bare, marked, in a SendMessage) opens one record within one poll; its line from
             before the first read and its NEED open nothing; a re-read opens none
  tool    -- the Flight runs the real Drawer/incident.py in a turn with no text block; the call opens one record
             and the watchdog prints its id within one poll and opens none itself
"""
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

INCIDENT = "an INCIDENT line from the live Flight opened no record"
FLIGHT = "c0ffee01-1111-4222-8333-444455556666"
LINES = ("INCIDENT canary plain line; impact: none, a probe; cause: the probe wrote it",
         "`INCIDENT canary marked line; impact: none, a probe; cause: the probe wrote it`")
SENT = "INCIDENT canary sent line; impact: none, a probe; cause: the probe wrote it"
FLIGHT_OLD = "INCIDENT canary old Flight line; impact: none; cause: written before the watch"
FLIGHT_LINE = "INCIDENT canary Flight line; impact: none, a probe; cause: the probe wrote it"
TOOL = "canary Flight tool line; impact: none, a probe; cause: the probe ran incident.py"
STEP = 1        # seconds between the canary watchdog's reads
WAIT = 60       # seconds the records may take (each opens through its own mem process)


def row(kind, content, when=None):
    return json.dumps({"type": kind, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(when)),
                       "message": {"role": kind, "content": content}}) + "\n"


def said(text, when=None):
    return row("assistant", [{"type": "text", "text": text}], when)


def sent(text):
    return row("assistant", [{"type": "tool_use", "name": "SendMessage", "input": {"to": "flight", "message": text}}])


def tool_turn(fake, env, session, text):
    """A turn with thinking and a Bash call of the real incident.py and no text block, then the call's result."""
    command = f".venv/bin/python -B Drawer/incident.py '{text}'"
    run = subprocess.run([sys.executable, "-B", str(fake / "Drawer" / "incident.py"), text], capture_output=True,
                         text=True, env={**env, "CLAUDE_CODE_SESSION_ID": session}, timeout=WAIT)
    tool_id = f"toolu_{session[:8]}"
    return (row("assistant", [{"type": "thinking", "thinking": "", "signature": "x"},
                              {"type": "tool_use", "id": tool_id, "name": "Bash", "input": {"command": command}}])
            + row("user", [{"type": "tool_result", "tool_use_id": tool_id, "content": run.stdout + run.stderr,
                            "is_error": run.returncode != 0}]), run.stdout.split("\n")[0])


def scratch_hub(hub, root):
    """A hub copy that holds only what the events path runs: the config, the watchdog and mem."""
    fake = root / "hub"
    (fake / "Drawer" / "mem").mkdir(parents=True)
    (fake / "Drawer" / "project-health").mkdir()
    # The scratch hub's state_root is scratch too: its incidents.jsonl, outboxes and events state never touch the live ones.
    toml = (hub / "harness.toml").read_text(encoding="utf-8")
    (fake / "harness.toml").write_text(re.sub(r'(?m)^state_root = "[^"]*"', f'state_root = "{(root / "state").as_posix()}"',
                                              toml, count=1), encoding="utf-8")
    sys.path.insert(0, str(hub / "Drawer"))
    import harness_config
    cfg = harness_config.load(hub)
    (fake / cfg["project"]["marker_file"]).touch()
    for name in ("flight-watchdog.py", "gatelib.py", "harness_config.py", "oscompat.py", "incident.py", "rules_audit.py", "pushretry.py",
                 "runlib.py", "mem/mem.py"):
        shutil.copy(hub / "Drawer" / name, fake / "Drawer" / name)
    return fake, harness_config.env_name("FLIGHT", cfg)


def read_lines(stream, out):
    for line in stream:
        out.put(line.rstrip("\n"))


def drain(out, until, want):
    """The lines the watchdog prints until the deadline, or until want INCIDENT lines are in."""
    got = []
    while time.monotonic() < until and sum(line.startswith("INCIDENT") for line in got) < want:
        try:
            got.append(out.get(timeout=0.2))
        except queue.Empty:
            pass
    return got


def cards(fake, env):
    run = subprocess.run([sys.executable, "-B", str(fake / "Drawer" / "mem" / "mem.py"), "list", "--kind", "task",
                          "--tag", "incident", "--json"], capture_output=True, text=True, env=env, timeout=60)
    try:
        return json.loads(run.stdout)
    except ValueError:
        return f"mem list exit {run.returncode}: {run.stderr.strip()[-200:]}"


def append(path, *rows):
    with path.open("a", encoding="utf-8") as stream:
        stream.write("".join(rows))


def replay(hub, product):
    hub = Path(hub)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fake, flight_var = scratch_hub(hub, root)
        home = root / "home"
        projects, sessions = home / ".claude" / "projects" / "-hub", home / ".claude" / "sessions"
        projects.mkdir(parents=True)
        sessions.mkdir()
        (root / "flight").mkdir()
        (root / "flight" / "SCRATCHPAD.md").write_text("## State\n", encoding="utf-8")
        (fake / "Flight" / "seats").mkdir(parents=True)
        (fake / "Flight" / "seats" / FLIGHT).write_text("", encoding="utf-8")  # the watchdog finds the Flight by its seat
        transcript = projects / f"{FLIGHT}.jsonl"
        transcript.write_text(row("user", "Run the loop. Write an INCIDENT line at a fault."), encoding="utf-8")
        append(transcript, said(FLIGHT_OLD, time.time() - 600))
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        (sessions / "0.json").write_text(json.dumps({"pid": holder.pid, "sessionId": FLIGHT, "name": "PROBE-FLIGHT"}),
                                         encoding="utf-8")
        env = {**os.environ, "HOME": str(home), flight_var.removesuffix("FLIGHT") + "MEM_DB": str(root / "mem.db"),
               flight_var.removesuffix("FLIGHT") + "MEM_EMBED": "off", flight_var: str(root / "flight")}  # <PREFIX>_ names
        env.pop(flight_var + "_ONESHOT", None)  # a card-run leg inherits a one-shot Flight's env (incident befb286b)
        dog = subprocess.Popen([sys.executable, "-B", str(fake / "Drawer" / "flight-watchdog.py"), "events",
                                "--lock", "probe", "--step", str(STEP)], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, env=env, cwd=fake)
        out = queue.Queue()
        threading.Thread(target=read_lines, args=(dog.stdout, out), daemon=True).start()
        try:
            time.sleep(STEP * 2)  # the loop is running and has read the Flight once
            append(transcript, said("Working on it.\n" + "\n".join(LINES)), sent(SENT),
                   said(f"NEED canary from Flight\n{FLIGHT_LINE}"))
            turn, printed = tool_turn(fake, env, FLIGHT, TOOL)
            append(transcript, turn)
            first = drain(out, time.monotonic() + WAIT, len(LINES) + 3)
            append(transcript, said(LINES[0]), said(FLIGHT_LINE), sent(SENT))  # the same lines again: no second record
            again = drain(out, time.monotonic() + STEP * 4, 1)
        finally:
            dog.terminate()
            holder.terminate()
            dog.wait(10)
            holder.wait(10)
        found = cards(fake, env)
        if isinstance(found, str):
            return found
        return verdict(first, again, found, printed)


def verdict(first, again, found, printed):
    """None when each path holds, else the first path that went red and what the watchdog said."""
    tool_line = None
    if printed.startswith("INCIDENT "):
        tool_line = f"INCIDENT {FLIGHT[:8]} {printed.split(' ', 2)[1]} {TOOL.split(';')[0]}"
    if tool_line is None or tool_line not in first:
        return f"tool path: incident.py printed {printed!r}; no {tool_line!r} from the watchdog; it said {first[-8:]}"
    first = [line for line in first if line != tool_line]
    tools = [card for card in found if "tool line" in card.get("title", "")]
    if len(tools) != 1:
        return f"tool path: {len(tools)} of 1 incident.py records, so the watchdog opened a second one"
    found = [card for card in found if card not in tools]
    opened = [line for line in first if line.startswith("INCIDENT")]
    flight = [line for line in opened if line.startswith(f"INCIDENT {FLIGHT[:8]} ")]
    if len(flight) != len(LINES) + 2 or any("UNLOGGED" in line for line in opened) or any("NEED" in line for line in first):
        return f"Flight path: {len(flight)} of {len(LINES) + 2} INCIDENT lines opened a record; watchdog said {first[-8:]}"
    if [line for line in again if line.startswith("INCIDENT")] or len(found) != len(LINES) + 2:
        return f"a re-read line opened a second record: {len(found)} records, then {again}"
    titles = sorted(card.get("title", "") for card in found)
    if titles != ["Incident: canary Flight line", "Incident: canary marked line", "Incident: canary plain line",
                  "Incident: canary sent line"]:
        return f"wrong records: {titles}"
    return None


if __name__ == "__main__":
    why = replay(Path(__file__).resolve().parents[2], None)
    print("PASS" if why is None else f"RED {why}")
    sys.exit(0 if why is None else 1)
