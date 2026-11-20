"""runlib.py -- the run policy the Flight tools share.

Incident: incident_step() demands an incident or a no-incident reason of each harness item and checks the record.
Runs: run_limit() reads [orchestrator] max_items, the card-runs Flight runs at once. open_runs() reads the result rows
(<state_root>/flight-handoff/<task8>/outbox.log, written by Drawer/rows.py): a run is out from its start row to its
closing row. rows.py (the cap), flight-hooks.py (Stop, bearings) and flight-watchdog.py (ONESHOT DONE) all read it
here, so they never disagree.
Flight: is_flight() knows the Flight by <env_prefix>_FLIGHT, or by a seat: <hub>/Flight/seats/<full session id>, which
only `Drawer/flight-hooks.py seat` writes, for a hand-started Flight. flight-hooks.py calls it.
Session: session_state() tells whether a session is live, dead or unknown from ~/.claude/sessions.
Reap: reap_session() stops the leftover processes of a session that has ended. A
process is the session's own when its resolved cwd has the session uuid8 as a whole name token in any path component,
or when the CLAUDE_CODE_SESSION_ID values in the ps -E lines of it and its ancestors are that session alone (ps -E
shows the env of some binaries only: python and node, not bash or sleep). It keeps pid 1, the caller and its
ancestors, the tree of each other live registered session, any process under an env that names another session, and
each long-job.py runner (its ps line holds long-job.py _run <name>) and every process under it: a long job outlives
its session, so a replacement waits on it. It reaps only a session proven not live: a
~/.claude/sessions row of the session whose pid runs skips the reap. It stops each by its literal pid (SIGTERM, then
SIGKILL after the grace to the ones a fresh scan still finds) and logs each to <state_root>/reap.log.
It scans on darwin only.
Each function takes the config (harness_config.load) as an argument, so a caller or a test passes its own.
"""

from datetime import datetime
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gatelib  # noqa: E402
import harness_config  # noqa: E402

HUB_SKILL = "harness"  # the skill of hub code work
INCIDENT_TAG = "incident"  # the tag of an incident record (Drawer/mem/mem.py)
SEATS = Path("Flight") / "seats"  # hub-relative: one file per seated hand Flight session (flight-hooks.py seat)
SESSION_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")  # a full session id
SESSION_ENV_RE = re.compile(r"(?<![A-Za-z0-9_])CLAUDE_CODE_SESSION_ID=(" + SESSION_RE.pattern + r")\b")
LONG_JOB_RE = re.compile(r"(?:^|[\s/\\])long-job\.py\s+_run\s+[A-Za-z0-9._-]+")  # a long-job.py runner (its RUN_MODE argv)
HANDOFF = "flight-handoff"  # under [project] state_root: <task8>/outbox.log, one result row per line
CLOSING = ("result", "blocked")  # the rows that close a run
REAP_LOG = "reap.log"  # under [project] state_root: one line per reaped pid
INCIDENT_STEP = ("INCIDENT STEP: this task fixes incident {id} ({title}). Write the fix's lesson with "
                 "<python> Drawer/mem/mem.py add --kind lesson --closes {id} --body \"<what broke, why, the fix>\": "
                 "it closes the incident record. Name the incident and the lesson in your close.")


class Usage(Exception):
    """A refusal: the caller prints it and launches nothing."""


def run_limit(cfg):
    """[orchestrator] max_items: the card-runs Flight runs at once. Raises Usage when it is not a positive integer."""
    value = (cfg.get("orchestrator") or {}).get("max_items")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise Usage(f"[orchestrator] max_items must be a positive integer, not {value!r}.")
    return value


def handoff(cfg):
    """<state_root>/flight-handoff: the folder of the result rows."""
    return harness_config.path(cfg["project"]["state_root"]) / HANDOFF


def task_rows(folder, task8):
    """The rows of <folder>/<task8>/outbox.log, oldest first; a line that is no JSON object is skipped."""
    try:
        text = (Path(folder) / task8 / "outbox.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            found.append(row)
    return found


def runs(folder, session=None):
    """{task: its last start row} of each task whose last start row has no closing row after it (a run out), and
    the count of start rows read; with session, only the start rows of that session count."""
    out, started = {}, 0
    for log in sorted(Path(folder).glob("*/outbox.log")):
        for row in task_rows(folder, log.parent.name):
            task = row.get("task")
            if row.get("row") == "start" and (session is None or row.get("session") == session):
                out[task], started = row, started + 1
            elif row.get("row") in CLOSING:
                out.pop(task, None)
    return out, started


def started(folder, session=None):
    """The task uuid of each task with a start row; with session, of that session's start rows only."""
    return {row.get("task") for log in Path(folder).glob("*/outbox.log") for row in task_rows(folder, log.parent.name)
            if row.get("row") == "start" and (session is None or row.get("session") == session)}


def open_runs(folder, session=None):
    """The task uuid of each run out (runs), in task order."""
    return sorted(runs(folder, session)[0])


def claude_dir(home=None):
    """The Claude config folder: a non-empty CLAUDE_CONFIG_DIR, else <home>/.claude (home defaults to the user's home)."""
    custom = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return Path(custom).expanduser() if custom else Path(home or Path.home()) / ".claude"


def session_state(session, home):
    """'live' when a live process carries the session in <claude_dir>/sessions (~/.claude/sessions by default); 'dead' when every row reads and none
    does; 'unknown' when it cannot tell: no session string, no sessions folder, or an unreadable row."""
    folder = claude_dir(home) / "sessions"
    if not session or not folder.is_dir():
        return "unknown"
    unsure = False
    try:
        rows = list(folder.glob("*.json"))
    except OSError:
        return "unknown"
    for row in rows:
        try:
            data = json.loads(row.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except (OSError, ValueError):
            unsure = True
            continue
        if data.get("sessionId") != session:
            continue
        pid = data.get("pid")
        if not isinstance(pid, int):
            unsure = True
            continue
        state = gatelib.pid_state(pid)
        if state == "gone":
            continue
        if state == "unsure":
            unsure = True
            continue
        return "live"
    return "unknown" if unsure else "dead"


def seated(hub, session):
    """True when session is a full session id with a seat file: <hub>/Flight/seats/<session>."""
    session = str(session or "").strip().lower()
    return bool(SESSION_RE.fullmatch(session)) and (Path(hub) / SEATS / session).is_file()


def is_flight(hub, env_value, session):
    """True for the Flight: env_value (its <env_prefix>_FLIGHT) is set, or session holds a seat (seated)."""
    return bool(str(env_value or "").strip()) or seated(hub, session)


def incident_step(get, skills, incident=None, no_incident=None):
    """(INCIDENT_STEP, full record id) of an incident fix, else (None, None). get(id) returns a mem card.
    Raises Usage for a harness run with neither or both of incident and no_incident, or when the card is no
    open incident record."""
    if HUB_SKILL in skills and bool(incident) == bool(no_incident):
        raise Usage("a harness run takes one of --incident <incident id> (the incident record it fixes) or "
                    "--no-incident '<why>' (it fixes none); open the record first with an INCIDENT line or "
                    "mem add --kind task --tag incident.")
    if not incident:
        return None, None
    card = get(incident)
    if card.get("kind") != "task" or INCIDENT_TAG not in (card.get("tags") or []):
        raise Usage(f"--incident {incident} is no incident record (a mem task card with the tag {INCIDENT_TAG}).")
    if card.get("status") not in ("todo", "doing", "parked"):
        raise Usage(f"--incident {incident} is {card.get('status')}: a closed incident takes no fix run.")
    return INCIDENT_STEP.format(id=card["id"][:8], title=card.get("title") or card["body"][:80]), card["id"]


def own_processes(session, rows, cwds, live, keep):
    """[(pid, why, command)] of the processes of rows [(pid, ppid, ps -E line)] that are the session's own: why is
    'cwd <path>' when a component of its cwd (cwds {pid: path}) holds the session uuid8 as a whole token, else 'env'
    when the CLAUDE_CODE_SESSION_ID values of it and its ancestors are the session alone. Keeps pid 1 and below, each
    pid in keep (the caller and its ancestors), each pid in live (another live session) or under one, each pid
    under an env that names another session, and each long-job.py runner (LONG_JOB_RE) and each pid under one."""
    session, token = session.lower(), session[:8].lower()
    parent = {pid: ppid for pid, ppid, _ in rows}
    lines = {pid: line for pid, _, line in rows}
    ids = {pid: {m.lower() for m in SESSION_ENV_RE.findall(line)} for pid, _, line in rows}
    found = []
    for pid, _, line in rows:
        if pid <= 1 or pid in keep:
            continue
        chain, up = [], pid
        while up > 1 and up in parent and up not in chain:
            chain.append(up)
            up = parent[up]
        if any(LONG_JOB_RE.search(lines[p]) for p in chain):
            continue
        named = set().union(*(ids[p] for p in chain))
        if named - {session} or any(p in live for p in chain):
            continue
        cwd = cwds.get(pid, "")
        if any(token in re.split(r"[^0-9a-z]+", part.lower()) for part in Path(cwd).parts):
            found.append((pid, f"cwd {cwd}", line))
        elif named == {session}:
            found.append((pid, "env", line))
    return found


def process_scan():
    """([(pid, ppid, ps -E line)] of every process, {pid: cwd} of this user's) by absolute tool paths."""
    ps = subprocess.run(["/bin/ps", "-axwwE", "-o", "pid=,ppid=,command="], capture_output=True, text=True,
                        timeout=60, check=True).stdout
    rows = []
    for line in ps.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            rows.append((int(parts[0]), int(parts[1]), parts[2] if len(parts) > 2 else ""))
    lsof = subprocess.run(["/usr/sbin/lsof", "-nP", "-a", "-d", "cwd", "-u", str(os.getuid()), "-Fpn"],
                          capture_output=True, text=True, timeout=60).stdout
    cwds, pid = {}, None
    for line in lsof.splitlines():
        if line[:1] == "p" and line[1:].isdigit():
            pid = int(line[1:])
        elif line[:1] == "n" and pid is not None:
            cwds[pid] = line[1:]
    return rows, cwds


def running_pid(pid):
    """Whether pid runs; a zombie does not."""
    ps = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=30)
    return ps.returncode == 0 and not ps.stdout.strip().startswith("Z")


def registry_pids(session, home):
    """(the pid of each <claude_dir>/sessions row of session, the pid of each row of another session)."""
    own, other = [], []
    for row in (claude_dir(home) / "sessions").glob("*.json"):
        try:
            data = json.loads(row.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("pid"), int):
            (own if data.get("sessionId") == session else other).append(data["pid"])
    return own, other


def session_leftovers(session, home):
    """own_processes of session on this box now: live is the pid of each ~/.claude/sessions row of another
    session that runs; keep is this process, its ancestors and its children."""
    rows, cwds = process_scan()
    parent = {pid: ppid for pid, ppid, _ in rows}
    live = {pid for pid in registry_pids(session, home)[1] if pid in parent}
    keep, up = set(), os.getpid()
    while up > 1 and up not in keep:
        keep.add(up)
        up = parent.get(up, 0)
    mine = {pid for pid, ppid, _ in rows if ppid == os.getpid()}  # the caller's children: its own scans
    return own_processes(session, rows, cwds, live, keep | mine)


def send(pid, sig):
    """os.kill(pid, sig) by the literal pid: the outcome word."""
    try:
        os.kill(int(pid), sig)
    except ProcessLookupError:
        return "GONE"
    except PermissionError:
        return "DENIED"
    return "TERM" if sig == signal.SIGTERM else "KILL"


def reap_session(cfg, session, home, grace=10):
    """Stop the leftover processes of session, an ended session (session_leftovers): SIGTERM to each by its pid, a
    wait of up to grace seconds, then SIGKILL to each a fresh scan still finds. Logs one line per pid to
    <state_root>/reap.log. Returns the REAPED line. A ~/.claude/sessions row of session whose pid runs (a zombie
    does not) skips it: the session is not proven ended, so nothing is stopped. Off darwin it scans nothing; a failed
    scan stops nothing."""
    if sys.platform != "darwin":
        return "REAPED: skipped, no process scan on this box"
    try:
        live = next((pid for pid in registry_pids(session, home)[0] if running_pid(pid)), None)
        if live is not None:
            return f"REAPED: skipped, session {session[:8]} still runs (pid {live})"
        found = session_leftovers(session, home)
        outcome = {pid: send(pid, signal.SIGTERM) for pid, _, _ in found}
        deadline = time.monotonic() + grace
        while any(running_pid(pid) for pid, word in outcome.items() if word == "TERM") and time.monotonic() < deadline:
            time.sleep(0.2)
        again = [row for row in session_leftovers(session, home) if running_pid(row[0])] if found else []
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return f"REAPED: unknown, the process scan failed ({type(error).__name__}); nothing more stopped"
    for row in again:
        if row[0] not in outcome:
            found.append(row)
        outcome[row[0]] = send(row[0], signal.SIGKILL)
    if not found:
        return "REAPED: none"
    stamp = datetime.now().isoformat(timespec="seconds")
    lines = "".join(f"{stamp} reap session={session[:8]} pid={pid} why={why} cmd={command[:80]} {outcome[pid]}\n"
                    for pid, why, command in found)
    try:
        log = harness_config.path(cfg["project"]["state_root"]) / REAP_LOG
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as handle:
            handle.write(lines)
    except OSError:
        pass
    return (f"REAPED: {len(found)} process(es) of session {session[:8]}: "
            + ", ".join(f"{pid} ({why})" for pid, why, _ in found))
