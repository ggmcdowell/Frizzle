"""Event watch for the Flight: `flight-watchdog.py events` runs under Flight's Monitor tool.

It prints one stdout line per new event and wakes Flight at once (events(), below). Flight has no tick: the
Monitor end notice (the CLI ends a Monitor at 10 min) wakes an idle Flight. The Flight is each live session with a seat file (Drawer/runlib.py
seated). At a fault a
session runs Drawer/incident.py, which opens the incident record and prints its id; the watchdog reads that call
and its result from the Flight's transcript and wakes Flight with the id (a line in reply text can stay in hidden
thinking). An INCIDENT text line that names no record id opens its record here as a backup (open_incident()).

Usage: flight-watchdog.py events [--lock NAME] [--step SECONDS] [--once]
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gatelib  # noqa: E402
import harness_config  # noqa: E402
import incident  # noqa: E402
import oscompat  # noqa: E402
import runlib  # noqa: E402

HOME = Path.home()
CLAUDE_DIR = runlib.claude_dir(HOME)  # CLAUDE_CONFIG_DIR when set
PROJECTS = CLAUDE_DIR / "projects"
SESSIONS = CLAUDE_DIR / "sessions"
HUB = Path(__file__).resolve().parent.parent
PLAN = Path(__file__).resolve().parent / "flight-plan.py"  # the board
_STATE_ROOT = harness_config.path(harness_config.load(harness_config.hub_root(Path(__file__).resolve().parent))
                                  ["project"]["state_root"])
STATE = _STATE_ROOT / "flight-watchdog"
HANDOFF = _STATE_ROOT / runlib.HANDOFF  # Drawer/rows.py writes each task's rows to <task8>/outbox.log
STEER_PAUSES = _STATE_ROOT / "steer" / "pauses.jsonl"  # Drawer/steer_check.py writes a row per pause
FLIGHT_VAR = harness_config.env_name("FLIGHT", harness_config.load(HUB))
EVENTS_STEP = 30    # seconds between event reads
BOARD_SECONDS = 30  # the flight-plan.py board read of a one-shot end
REAP_WAIT = 5       # seconds a reaped watcher gets to end on SIGTERM before SIGKILL
CARD8_RE = re.compile(r"[0-9a-f]{8}")
# 'INCIDENT line written ...' talks about the line: it is no INCIDENT line.
LINE_RE = re.compile(r"^\s*INCIDENT\b(?!\s+lines?\b)")
# The same line in Markdown (`INCIDENT ...`, **INCIDENT** ..., - INCIDENT ...): it counts only with its impact part,
# so a bullet that only names the word opens no record.
INCIDENT_MARKED_RE = re.compile(r"^\s*(?:[>*_`#-]+\s*)+INCIDENT\b[*_`]*[\s:]*(?P<rest>.*;\s*(?i:impact):.*?)[\s*_`]*$")
# A full pause: the State section of the pad opens with a PAUSED line. The events watch then prints nothing;
# every line waits for resume. The human's resume order wakes Flight without the watch.
PAUSED_RE = re.compile(r"^## State[ \t]*\n\s*PAUSED\b", re.M)
# A scripted run (<env_prefix>_FLIGHT_ONESHOT=1, flight-hooks.py ONE-SHOT): the watch ends once no card-run of the
# Flight session is out (runlib.open_runs, the Stop hook's own definition), after it saw one out (state key
# oneshot_out) or the Flight session wrote a start row (a run can start and close before the watcher's first read),
# and the board holds no GO task the session has not started (go_left: a task its first run unblocked). The session
# is the CLAUDE_CODE_SESSION_ID the Monitor command gets from Flight; with none, every session's runs count and only a
# run seen out ends the watch.
ONESHOT_VAR = harness_config.env_name("FLIGHT_ONESHOT", harness_config.load(HUB))


def stamp(entry):
    try:
        return datetime.fromisoformat(str(entry["timestamp"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None


def items(entry):
    content = (entry.get("message") or {}).get("content")
    return [i for i in content if isinstance(i, dict)] if isinstance(content, list) else []


def alive(pid):
    return gatelib.pid_state(pid) != "gone"


def live_flights():
    """The session id of each ~/.claude/sessions row whose process runs and whose session holds a seat in this hub."""
    found = set()
    for path in SESSIONS.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("pid"), int) and alive(data["pid"]) \
                and runlib.seated(HUB, data.get("sessionId")):
            found.add(str(data["sessionId"]))
    return found


def flight_dir():
    """The Flight folder the environment names, else <hub>/Flight."""
    value = os.environ.get(FLIGHT_VAR, "").strip()
    path = Path(value) if value else Path("Flight")
    return path if path.is_absolute() else HUB / path


def paused(pad):
    """True when the State section of the pad opens with a PAUSED line."""
    try:
        return bool(PAUSED_RE.search(pad.read_text(encoding="utf-8")))
    except OSError:
        return False


def oneshot():
    return os.environ.get(ONESHOT_VAR, "").strip() == "1"


def flight_session():
    """The CLAUDE_CODE_SESSION_ID the Monitor command gets from Flight, or None."""
    session = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip().lower()
    return session if runlib.SESSION_RE.fullmatch(session) else None


def runs_out():
    """The card-runs of the Flight session that are out (runlib.open_runs)."""
    return runlib.open_runs(HANDOFF, flight_session())


def started_here():
    """True when the Flight session is known and wrote a start row: its runs are its own, never an older run's."""
    session = flight_session()
    return bool(session) and bool(runlib.started(HANDOFF, session))


def board_go():
    """The task uuid of each GO task on Flight's board (Drawer/flight-plan.py --json); [] when the board is
    unreadable: the one-shot end then falls back to the runs alone, and the Stop hook still holds while one is out."""
    try:
        done = subprocess.run([sys.executable, "-B", str(PLAN), "--json"], cwd=PLAN.parent.parent, capture_output=True,
                              text=True, timeout=BOARD_SECONDS)
        tasks = json.loads(done.stdout).get("tasks") if done.returncode == 0 else []
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return []
    return [str(t.get("id")) for t in tasks or [] if isinstance(t, dict) and t.get("verb") == "GO"]


def go_left():
    """The GO tasks the Flight session has not started yet: a one-shot Flight starts each at its next wake."""
    mine = runlib.started(HANDOFF, flight_session())
    return [task for task in board_go() if task not in mine]


class Tail:
    """One Flight transcript, read on from the last whole line each pass."""

    def __init__(self):
        self.path, self.offset = None, 0
        self.calls = {}  # tool_use id: the INCIDENT lines of a Drawer/incident.py call that has no result yet

    def read(self, session):
        """[(kind, line, time)]: LOGGED (`<card uuid8> <line>`) for each line a Drawer/incident.py call opened a
        record for, read from its result, and INCIDENT for its line when mem refused it; INCIDENT for each
        INCIDENT line in reply text or SendMessage text. A text line that cites a record
        (`INCIDENT <card uuid8> ...`) opens nothing."""
        if self.path is None:
            self.path = next(PROJECTS.glob(f"*/{session}.jsonl"), None)
            if self.path is None:
                return []
        try:
            with self.path.open("rb") as stream:
                stream.seek(self.offset)
                chunk = stream.read()
        except OSError:
            return []
        whole = chunk[:chunk.rfind(b"\n") + 1]
        self.offset += len(whole)
        found = []
        for line in whole.decode("utf-8", "replace").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict) or entry.get("type") not in ("user", "assistant"):
                continue
            when = stamp(entry)
            if entry["type"] == "user":
                for result in (i for i in items(entry) if i.get("type") == "tool_result"):
                    output = result.get("content")
                    output = output if isinstance(output, str) else " ".join(
                        str(i.get("text") or "") for i in output or [] if isinstance(i, dict))
                    for said in self.calls.pop(result.get("tool_use_id"), []):
                        card = incident.card_in(output, said)
                        if card == incident.REFUSED:
                            found.append(("INCIDENT", said, when))
                        elif card:
                            found.append(("LOGGED", f"{card} {said}", when))
                continue
            calls = [i for i in items(entry) if i.get("type") == "tool_use"]
            texts = [i.get("text") for i in items(entry) if i.get("type") == "text"]
            texts += [(i.get("input") or {}).get("message") for i in calls if i.get("name") == "SendMessage"]
            for call in calls:
                command = str((call.get("input") or {}).get("command") or "") if call.get("name") == "Bash" else ""
                if lines := incident.from_command(command):
                    self.calls[call.get("id")] = lines
            for text in texts:
                for said in str(text or "").splitlines():
                    if incident.CITED_RE.match(said) or (
                            (marked := INCIDENT_MARKED_RE.match(said)) and incident.CITED_RE.match(
                                f"INCIDENT {marked.group('rest')}")):
                        continue  # it cites a record: the record is open
                    if LINE_RE.match(said):
                        found.append(("INCIDENT", said.strip(), when))
                    elif marked:
                        found.append(("INCIDENT", f"INCIDENT {marked.group('rest').strip()}", when))
        return found


def open_incident(session, said):
    """The backup path: open the record for one INCIDENT line in text (incident.open_record). The wake line:
    `INCIDENT <uuid8> <card uuid8> <what>`, else `INCIDENT <uuid8> UNLOGGED (<why>): <line>`."""
    card, what = incident.open_record(session, said)
    return f"INCIDENT {session[:8]} {card} {what}" if card else f"INCIDENT {session[:8]} UNLOGGED ({what}): {said}"


def steer_pauses():
    """The pause rows of Drawer/steer_check.py (state_root/steer/pauses.jsonl), oldest first."""
    rows = []
    try:
        text = STEER_PAUSES.read_text(encoding="utf-8")
    except OSError:
        return rows
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def event_lines(tails, state, now):
    """One read of each live Flight: the wake lines due now; updates state (printed keys)."""
    printed, lines = set(state.get("printed", [])), []

    def emit(key, line):
        if key not in printed:
            printed.add(key)
            lines.append(line() if callable(line) else line)

    def opened(s, kind, said):
        """The record opens once: a printed key never runs open_incident again, and a line already seen opens
        none. LOGGED: Drawer/incident.py opened it. Each record prints once by its card id (INCIDENT-CARD),
        whether the transcript or incidents.jsonl shows it first."""
        card, said = said.split(" ", 1) if kind == "LOGGED" else (None, said)
        if f"INCIDENT * {said}" in printed or f"INCIDENT {s} {said}" in printed:
            return
        printed.add(f"INCIDENT * {said}")
        if card is None:
            line = open_incident(s, said)
            if opened_card := re.match(r"INCIDENT \S+ ([0-9a-f]{8}) ", line):
                printed.add(f"INCIDENT-CARD {opened_card.group(1)}")
        elif f"INCIDENT-CARD {card}" in printed:
            printed.add(f"INCIDENT {s} {said}")
            return
        else:
            printed.add(f"INCIDENT-CARD {card}")
            line = f"INCIDENT {s[:8]} {card} {incident.INCIDENT_RE.match(said).group('what').strip().rstrip('.')}"
        emit(f"INCIDENT {s} {said}", line)

    # A Flight's INCIDENT lines open records from the first read that saw it on: the lines it wrote before were
    # opened by hand. Its other lines print nothing.
    since = dict(state.get("flight_since", {}))
    for s in sorted(live_flights()):
        start = datetime.fromisoformat(since.setdefault(s, now.isoformat()))
        for kind, said, when in tails.setdefault(s, Tail()).read(s):
            if when is not None and when >= start:
                opened(s, kind, said)
    steer_since = state.get("steer_since") or now.timestamp()
    for row in steer_pauses():
        if float(row.get("t") or 0) >= steer_since:
            emit(f"PAUSE {row.get('t')} {row.get('session')}",
                 f"PAUSE {row.get('session')} {row.get('step')}: {row.get('why')}")
    state.update(printed=sorted(printed)[-5000:], flight_since=since, steer_since=steer_since)
    return lines


def write_whole(path, text):
    """Write text to path through a temp file and a rename, so a kill mid-write keeps the old file whole."""
    part = path.with_name(path.name + ".tmp")
    part.write_text(text, encoding="utf-8")
    oscompat.replace(part, path)


def new_rows(seen, key, path, learn):
    """The whole rows of a log added since the count seen[key]; updates the count. A shorter file is a new file:
    every row is new. learn: count only (a fresh state learns the logs, so it replays no history)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        seen[key] = 0  # a file made later reads from its start
        return []
    except OSError:
        return []
    rows = text.replace("\r", "").split("\n")[:-1]  # a last line with no newline is still being written
    count = seen.get(key, 0)
    new = rows[count:] if len(rows) >= count else rows
    seen[key] = len(rows)
    return [] if learn else [row for row in new if row.strip()]


def row_lines(state):
    """One '<task8> <row json>' line per new outbox row of HANDOFF/<task8>/outbox.log, then one
    'INCIDENT <uuid8> <card uuid8> <what>' line per new incidents.jsonl row whose record no line printed yet."""
    learn = not isinstance(state.get("seen"), dict)
    seen = state["seen"] = {} if learn else state["seen"]
    lines = []
    for path in sorted(HANDOFF.glob("*/outbox.log")):
        card = path.parent.name
        if CARD8_RE.fullmatch(card):
            lines += [f"{card} {row}" for row in new_rows(seen, f"outbox:{card}", path, learn)]
    printed = set(state.get("printed", []))
    for row in new_rows(seen, "incidents", incident.incidents_path(), learn):
        try:
            record = json.loads(row)
        except ValueError:
            continue
        card = str(record.get("card") or "") if isinstance(record, dict) else ""
        if CARD8_RE.fullmatch(card) and f"INCIDENT-CARD {card}" not in printed:
            printed.add(f"INCIDENT-CARD {card}")
            lines.append(f"INCIDENT {str(record.get('session') or 'unknown')[:8]} {card} {record.get('what') or ''}")
    state["printed"] = sorted(printed)[-5000:]
    return lines


def events_holder(lock):
    """The pid in events-<lock>.pid, or None."""
    try:
        return int((STATE / f"events-{lock}.pid").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def orphan_facts(pid):
    """(words of the command line, cwd) of pid when its parent is gone, else None. POSIX: ps and lsof, an orphan
    has ppid 1. Windows: Win32_Process through PowerShell (no cwd to read: the Monitor arm runs from the hub
    root), the parent pid no longer runs. Every doubt is None."""
    try:
        if oscompat.WINDOWS:
            script = (f"Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}' | "
                      "Select-Object ParentProcessId,CommandLine | ConvertTo-Json")
            info = json.loads(subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                                             capture_output=True, text=True, encoding="utf-8", errors="replace",
                                             timeout=30).stdout)
            command = info["CommandLine"]
            if not command or gatelib.pid_state(info["ParentProcessId"]) != "gone":
                return None
            return [word.strip('"') for word in shlex.split(command, posix=False)], str(HUB)
        out = subprocess.run(["ps", "-ww", "-o", "ppid=", "-o", "command=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=30).stdout.split(None, 1)
        if len(out) != 2 or out[0] != "1":
            return None
        cwd = next((line[1:] for line in subprocess.run(
            [shutil.which("lsof") or "/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], capture_output=True,
            text=True, timeout=30).stdout.splitlines() if line.startswith("n")), "")
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return None
    return (out[1].split(), cwd) if cwd else None


def orphan_watcher(pid, lock):
    """True when pid is an orphan events watcher of this hub and lock: its parent is gone (orphan_facts), a word of
    its command line names this file, as an absolute path or relative to the process cwd (the Monitor arm runs
    Drawer/flight-watchdog.py from the hub root), the next word is events, and its --lock X or --lock=X (the last
    one; none: flight) is lock. Every doubt is False."""
    facts = orphan_facts(pid)
    if facts is None:
        return False
    words, cwd = facts
    me = Path(__file__).resolve()

    def names_me(word):
        try:
            return (Path(cwd) / word).resolve() == me
        except (OSError, ValueError, RuntimeError):
            return False

    at = next((n for n, word in enumerate(words) if names_me(word)), None)
    if at is None or words[at + 1:at + 2] != ["events"]:
        return False
    rest, named = words[at + 2:], "flight"
    for n, word in enumerate(rest):
        if word == "--lock":
            named = rest[n + 1] if n + 1 < len(rest) else None
        elif word.startswith("--lock="):
            named = word[len("--lock="):]
    return named == lock


def reap(pid, stream):
    """End the orphan watcher pid (SIGTERM, then SIGKILL; Windows: its process tree at once) and take the lock;
    True when the lock is ours."""
    for sig in (None,) if oscompat.WINDOWS else (signal.SIGTERM, signal.SIGKILL):
        try:
            if oscompat.WINDOWS:
                oscompat.kill_tree(pid)
            else:
                os.kill(pid, sig)
        except ProcessLookupError:
            pass
        except OSError:
            return False
        deadline = time.monotonic() + REAP_WAIT
        while time.monotonic() < deadline:
            try:
                oscompat.lock(stream)
                return True
            except OSError:
                time.sleep(0.1)
    return False


def events(argv):
    """Event mode for Flight's Monitor: one stdout line per new event, nothing else. It prints:
      INCIDENT <uuid8> <card uuid8> <what>  -- a Drawer/incident.py call in a live Flight's transcript and the record
                             it opened, or an INCIDENT text line that names no record id and the record it opened
                             here (UNLOGGED and the line when mem refused: open the record by hand); from the first
                             read of that Flight on; a line already seen opens no second record
      PAUSE <uuid8> <step>: <why>  -- Drawer/steer_check.py paused an irreversible step, from the first read on
      <task8> <row json>     -- one new row of state_root/flight-handoff/<task8>/outbox.log (Drawer/rows.py)
      INCIDENT <uuid8> <card uuid8> <what>  -- also from each new state_root/incidents.jsonl row (Drawer/incident.py);
                             a record prints once, whichever path shows it first
      EVENTS reaped: orphan process <pid> held <lock>  -- the lock holder was an orphan watcher (parent gone, its
                             command line names this file from its cwd, events, the same lock): it is ended and
                             this run takes the lock. Any other holder: EVENTS busy, exit 1
      ONESHOT DONE: ...     -- <env_prefix>_FLIGHT_ONESHOT=1 only: no card-run of the Flight session is out
                             (runs_out), one was seen out since the last ONESHOT DONE or the session wrote a start
                             row, and the board holds no GO task the session has not started (go_left); the run
                             then exits 0.
    While the State section of the pad opens with PAUSED (a full pause), it prints nothing; every line waits and
    prints after the pause ends.
    state_root/flight-watchdog/events-<lock>.json keeps the events printed and each log's row count, so a re-armed
    run prints none twice; a fresh state learns the logs and prints no history.
    One run per lock reads at a time; events-<lock>.pid names its pid.
    """
    p = argparse.ArgumentParser(prog="flight-watchdog.py events", allow_abbrev=False)
    p.add_argument("--lock", default="flight")
    p.add_argument("--step", type=float, default=EVENTS_STEP)
    p.add_argument("--once", action="store_true", help="one read, then exit")
    a = p.parse_args(argv)
    if not re.fullmatch(r"[a-z0-9-]{1,40}", a.lock):
        p.error("--lock must be lower-case letters, digits and hyphens")
    STATE.mkdir(parents=True, exist_ok=True)
    lock = open(STATE / f"events-{a.lock}.lock", "a")  # append: a second run's open never truncates a held file
    try:
        oscompat.lock(lock)
    except OSError:
        holder = events_holder(a.lock)
        if not (holder and orphan_watcher(holder, a.lock) and reap(holder, lock)):
            print(f"EVENTS busy: another events run holds {a.lock}", flush=True)
            return 1
        print(f"EVENTS reaped: orphan process {holder} held {a.lock}", flush=True)
    (STATE / f"events-{a.lock}.pid").write_text(str(os.getpid()), encoding="utf-8")
    state_path = STATE / f"events-{a.lock}.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state = state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        state = {}
    tails = {}
    while True:
        now = datetime.now(timezone.utc)
        pad = flight_dir() / "SCRATCHPAD.md"
        hold = paused(pad)
        lines = [] if hold else event_lines(tails, state, now) + row_lines(state)
        out = runs_out() if oneshot() else []
        # oneshot_out: this one-shot has work and no ONESHOT DONE yet, so the Stop hook holds. A watcher armed before
        # the first start row ends only after it; a run that started and closed before the first read counts too.
        if out or (oneshot() and started_here()):
            state["oneshot_out"] = True
        # A GO task the session has not started yet (one its first run unblocked) holds the end.
        done = not hold and oneshot() and not out and bool(state.get("oneshot_out")) and not go_left()
        if done:
            state.pop("oneshot_out", None)  # the next one-shot run waits for its own run out
        write_whole(state_path, json.dumps(state))
        for line in lines:
            print(line, flush=True)
        if done:
            print("ONESHOT DONE: no card-run out; report and end your turn", flush=True)
            return 0
        if a.once:
            return 0
        time.sleep(a.step)


if __name__ == "__main__":
    if sys.argv[1:] in (["-h"], ["--help"]):
        print(__doc__)
        sys.exit(0)
    if sys.argv[1:2] == ["events"]:
        sys.exit(events(sys.argv[2:]))
    print("usage: flight-watchdog.py events [--lock NAME] [--step SECONDS] [--once]", file=sys.stderr)
    sys.exit(2)
