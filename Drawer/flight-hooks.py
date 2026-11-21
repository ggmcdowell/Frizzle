"""Flight-only hooks: the forever Flight keeps its identity on disk and compacts in place.

A session is a Flight when its environment carries <env_prefix>_FLIGHT, the Flight folder (hub-relative
or absolute; a headless run sets it, and a proof sets a folder of its own), or when it holds a seat (Drawer/runlib.py is_flight): a Flight started by plain
claude and /flight runs `<python> Drawer/flight-hooks.py seat` first (flight skill), which writes
<hub>/Flight/seats/<CLAUDE_CODE_SESSION_ID> and prints SEATED <uuid8>; its folder is then <hub>/Flight.
seat refuses (exit 1) a missing or bad session id. The settings manifest runs this
script behind a shell guard: <env_prefix>_FLIGHT set or a <hub>/Flight/seats folder, so another session never starts it on a
hub with no seat; with seats, the script reads the payload session_id and exits 0 for a session with no seat. Limit: a
Flight's own Workflow legs share its session id, so they count as the Flight too. One script answers each event by
hook_event_name:

  SessionStart  the bearings, in order: DO NOW (the steps to take now for the source: resume, compact,
                clear), the State section of <folder>/SCRATCHPAD.md, identity, a STATE OVER CAP line when State passes STATE_CAP lines
                and the last LOG_TAIL lines of <folder>/log.jsonl, all within
                OUTPUT_BUDGET characters: log rows drop first, oldest first, then the State tail, then
                characters from the end.
                The bearings also list each card-run of this session that is out (RUNS OUT: runlib.open_runs, the
                start rows of Drawer/rows.py with no closing row) and the dead claims
                `mem reap --dry-run` would park (REAP lines), the lesson gate red of `incident.py gate` (an open
                incident record with no lesson: UNLESSONED lines), and order a re-arm when no watcher runs and
                State is not PAUSED: at resume, compact and clear always, else when a run is out. The events
                watcher is Flight's one wake path; no tick wakes it.
  PreCompact    copies the State section to <folder>/compactions/state-<stamp>.md and prints the compaction
                instructions: the keep list of flight skill FLT-14, and the State RUN GOAL and LIMITS lines.
  PostCompact   writes compact_summary to <folder>/compactions/summary-<stamp>.md.
  ONE-SHOT      <env_prefix>_FLIGHT_ONESHOT=1 (a scripted run, claude -p): the bearings carry a ONE-SHOT line (run
                the GO tasks as card-runs, arm the watcher, never TaskStop it, end the turn when no run is out).
                The Stop hook passes when no run of this session is out and the watcher state
                (events-flight.json beside the events lock) holds no oneshot_out: the watcher has printed ONESHOT
                DONE. Else it blocks once: wait for the watcher, or re-arm it when none runs.
  Stop          the watch guard: blocks every stop while a card-run of this session is out and no process
                holds the events lock (<state_root>/flight-watchdog/
                events-flight.lock, held by `flight-watchdog.py events`): a Monitor run ends at 30 minutes, and
                a Flight that stops then misses rows. It also blocks once, with the trim order, when the State section passes STATE_CAP
                lines (flight skill section 6), and once per fault with the restore order when State has no line that starts 'BACKLOG:' or that
                line shrank by more than half since the newest State snapshot (a regex edit once cut it); with no
                snapshot (a fresh pad) the order is to add the line, 'BACKLOG: none.' when empty;
                never while stop_hook_active or while State opens with PAUSED.

Each event appends one row to <folder>/log.jsonl, the Flight log; each append keeps only the newest LOG_KEEP
rows and drops the rest (the digest and memory hold the history). Flight adds its own rows with
`<python> -B Drawer/flight-hooks.py log <kind> <text...>` (a hand Flight: by its CLAUDE_CODE_SESSION_ID seat). Any failure exits 0 quietly:
a broken hook must never stop the Flight.
"""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys

DRAWER = Path(__file__).resolve().parent
HUB = DRAWER.parent
sys.path.insert(0, str(DRAWER))
import harness_config  # noqa: E402
import oscompat  # noqa: E402
import runlib  # noqa: E402

_CONFIG = harness_config.load(HUB)
VAR = harness_config.env_name("FLIGHT", _CONFIG)
HANDOFF = runlib.handoff(_CONFIG)  # the result rows of Drawer/rows.py
ONESHOT_VAR = harness_config.env_name("FLIGHT_ONESHOT", _CONFIG)  # 1: a scripted run ends after its GO tasks
LAUNCHER_VAR = "HARNESS_FLIGHT_LAUNCHER"  # 1: Flight.command started this session with Code Mode on (prefix-free, a shell reads it)
CODE_MODE_OFF = ("CODE MODE OFF: ./Flight.command did not start this Flight, and Code Mode is off unless your user "
                 "settings turn it on. To get it: quit, run ./Flight.command, type /flight.")
ONESHOT_TEXT = ("ONE-SHOT RUN ({var}=1): run the GO tasks, then end; take no ladder work. Start each by rows.py "
                "start and a card-run Workflow tool call with its START args (section 3; in a REPL, unawaited), then "
                "arm the events watcher. Pass each result to rows.py row; when all are in, close each (section 4) and "
                "report (section 5), citing the land smoke or gate of each land item. Never TaskStop the watcher: it "
                "prints ONESHOT DONE and ends. End your turn until it does, then end it again: the Stop hook passes "
                "when no run of yours is out and the watcher has printed ONESHOT DONE.")
LOG_TAIL = 5
KEEP_SNAPSHOTS = 40  # newest files kept in compactions/, half of each kind
LOG_KEEP = 2000  # newest rows kept in log.jsonl
STATE_CAP = 40  # lines in the State section (flight skill section 6)
# The bearings print, JSON and all, fits this budget (the CLI cuts a hook's output at 10000 characters, 2.1.314):
# the log rows go first, oldest first, then the State tail.
OUTPUT_BUDGET = 4000
BACKLOG_SEEN = "backlog-seen.json"  # the last backlog fault the Stop hook named, so it blocks once per fault
BACKLOG_RE = re.compile(r"BACKLOG:(.*)")  # a snapshot may hold the backlog inside another line
BACKLOG_REASON = ("The State section of {pad} {fault} (a regex edit can cut it). Restore the "
                  "backlog from {snapshot} plus the items added since, as one State line that starts 'BACKLOG:'. "
                  "If you trimmed it on purpose, go on. Then stop.")
BACKLOG_ADD = ("The State section of {pad} has no line that starts 'BACKLOG:' and compactions/ holds no State "
               "snapshot to restore it from. Add one State line that starts 'BACKLOG:' with the open backlog items, "
               "or 'BACKLOG: none.' when there are none. Then stop.")
CAP_REASON = ("The State section of {pad} has {lines} lines, over the {cap}-line cap (flight skill section 6). "
              "Trim it to {cap} lines or fewer: keep run goal, limits, goal, done, next, open decisions, backlog "
              "and the open asks; move done detail to memory or the backlog. Then stop.")
EVENTS_LOCK = harness_config.path(_CONFIG["project"]["state_root"]) / "flight-watchdog" / "events-flight.lock"
WATCH_COMMAND = ".venv/bin/python -B Drawer/flight-watchdog.py events"
# One form for a headless claude -p Flight (plain tools, no REPL) and a Code Mode one (the same call from the REPL).
ARM_FORM = (f"call the Monitor tool: command '{WATCH_COMMAND}', timeout 1800000 ms, description 'Flight events' "
            "(in a REPL, tools.Monitor with timeout_ms). Never as a background Bash run: the watcher never ends")
WATCH_REASON = ("A card-run is out ({work}) and no events watcher runs for you (no process holds {lock}; a Monitor run "
                "ends at 30 minutes). Re-arm it now: " + ARM_FORM.replace("{", "{{").replace("}", "}}") + ". If a run has ended, pass its result to "
                "rows.py row instead. Then stop.")
ONESHOT_WAIT = ("ONE-SHOT: every run is in, but the events watcher has not printed ONESHOT DONE ({events} holds "
                "oneshot_out). It holds while the board has a GO task you have not started: start it. Never TaskStop "
                "the watcher. {how} Then end your turn.")
ONESHOT_RUNS = "It reads the rows every 30 s: end your turn now and wait for its ONESHOT DONE line."
ONESHOT_REARM = ("No watcher runs. Re-arm it: " + ARM_FORM.replace("{", "{{").replace("}", "}}")
                 + "; it prints ONESHOT DONE at its first read.")
PAUSED_RE = re.compile(r"\A## State[ \t]*\n\s*PAUSED\b")  # flight-watchdog.py paused(): a full pause
REAP_SHOW = 8  # REAP lines in the bearings; the rest is a count
PRE_COMPACT_TEXT = ("LEAN COMPACT, Flight. Keep word for word (flight skill FLT-14): the task uuid8 and claim, each task "
                    "worktree path, branch and unpushed sha, the last gate verdict, and the RUN GOAL and LIMITS lines "
                    "below. Drop the rest: the State section of {pad} holds it, and the bearings print it after "
                    "the compaction.")


def oneshot():
    """True for a scripted run: <env_prefix>_FLIGHT_ONESHOT=1."""
    return os.environ.get(ONESHOT_VAR, "").strip() == "1"


def folder(session=None):
    """The Flight folder this session's environment names, else <hub>/Flight for a seated session, else None
    (not the Flight)."""
    value = os.environ.get(VAR, "").strip()
    if not value:
        return HUB / runlib.SEATS.parent if runlib.seated(HUB, session) else None
    path = Path(value)
    return path if path.is_absolute() else HUB / path


def seat(now):
    """(exit code, line): write <hub>/Flight/seats/<CLAUDE_CODE_SESSION_ID> for a hand-started Flight, or refuse."""
    session = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip().lower()
    if not runlib.SESSION_RE.fullmatch(session):
        return 1, f"NO SEAT: CLAUDE_CODE_SESSION_ID {session!r} is no full session id; run seat from the Flight session."
    seats = HUB / runlib.SEATS
    seats.mkdir(parents=True, exist_ok=True)
    (seats / session).write_text(json.dumps({"at": now.isoformat(timespec="seconds"), "session": session}) + "\n",
                                 encoding="utf-8")
    line = f"SEATED {session[:8]}"
    # The project settings env puts CLAUDE_CODE_EXPERIMENTAL_CODE_MODE=1 in every tool process, but Claude Code ignores
    # that key for the session itself, so the variable cannot show Code Mode; the launcher marker can.
    return 0, line if os.environ.get(LAUNCHER_VAR, "").strip() == "1" else f"{line}\n{CODE_MODE_OFF}"


def state_section(base):
    """The State section of the pad: from `## State` to the next level-2 heading."""
    try:
        text = (base / "SCRATCHPAD.md").read_text(encoding="utf-8")
    except OSError:
        return ""
    found = re.search(r"^## State\b.*?(?=^## (?!#)|\Z)", text, re.M | re.S)
    return found.group(0).rstrip() if found else ""


def over_cap(state):
    """The State section's line count when it passes STATE_CAP, else 0."""
    lines = len(state.splitlines())
    return lines if lines > STATE_CAP else 0


def backlog_fault(base, state):
    """(fault text, snapshot) when State has no line that starts 'BACKLOG:' or that line holds less than half
    the backlog of the newest State snapshot with one; None when the fault was already named once."""
    snapshot, before = None, ""
    for path in sorted((base / "compactions").glob("state-*.md"), reverse=True):
        try:
            found = BACKLOG_RE.search(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if found:
            snapshot, before = path, found.group(1).strip()
            break
    line = re.search(r"^BACKLOG:(.*)", state, re.M)
    now = line.group(1).strip() if line else None
    if now is None:
        fault = "has no line that starts 'BACKLOG:'"
    elif snapshot and len(now) * 2 < len(before):
        fault = f"has a BACKLOG line that shrank from {len(before)} to {len(now)} characters since {snapshot.name}"
    else:
        (base / BACKLOG_SEEN).unlink(missing_ok=True)  # a later fault blocks again
        return None
    key = {"fault": fault, "snapshot": str(snapshot)}
    try:
        if json.loads((base / BACKLOG_SEEN).read_text(encoding="utf-8")) == key:
            return None
    except (OSError, ValueError):
        pass
    (base / BACKLOG_SEEN).write_text(json.dumps(key), encoding="utf-8")
    return fault, snapshot  # snapshot None: a fresh pad, nothing to restore from


def open_runs(session):
    """'<task8> claim <claim8>' of each card-run of session that is out (runlib.runs); none with no session."""
    if not runlib.SESSION_RE.fullmatch(str(session or "")):
        return []
    out = runlib.runs(HANDOFF, session)[0]
    return [f"{task[:8]} claim {str(out[task].get('claim_id') or '')[:8]}" for task in sorted(out)]


def events_state():
    """flight-watchdog.py events keeps its state beside its lock: events-flight.json."""
    return EVENTS_LOCK.with_suffix(".json")


def oneshot_pending():
    """True when a one-shot watcher saw a run out and has not yet printed ONESHOT DONE (it pops
    oneshot_out then)."""
    try:
        return bool(json.loads(events_state().read_text(encoding="utf-8")).get("oneshot_out"))
    except (OSError, ValueError, AttributeError):
        return False


def watcher_alive():
    """True when a process holds the events lock (flight-watchdog.py events). The probe takes the lock for an
    instant when it is free."""
    try:
        stream = open(EVENTS_LOCK, "a")
    except OSError:
        return False  # no flight-watchdog folder: no watcher ever ran
    with stream:
        try:
            oscompat.lock(stream)
        except OSError:
            return True
        oscompat.unlock(stream)
        return False


def reap_lines():
    """One REAP line per dead claim `mem reap --dry-run` would park; [] when it fails or finds none."""
    try:
        done = subprocess.run([sys.executable, "-B", str(DRAWER / "mem" / "mem.py"), "reap", "--dry-run", "--json"],
                              capture_output=True, text=True, timeout=20, cwd=HUB)
        rows = json.loads(done.stdout or "[]")
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    if not isinstance(rows, list):
        return []
    lines = [f"REAP {str(r.get('id', ''))[:8]} {r.get('state', '')} {str(r.get('title') or r.get('body') or '')[:60]}"
             .rstrip() for r in rows[:REAP_SHOW] if isinstance(r, dict)]
    if len(rows) > REAP_SHOW:
        lines.append(f"REAP +{len(rows) - REAP_SHOW} more: mem reap --dry-run --json")
    return lines


def lesson_lines():
    """The lesson gate (`incident.py gate`): each UNLESSONED record, at most REAP_SHOW, and its
    LESSON GATE RED or UNREAD line; [] when it is green. A failed run reads as UNREAD: the gate fails closed."""
    try:
        done = subprocess.run([sys.executable, "-B", str(DRAWER / "incident.py"), "gate"],
                              capture_output=True, text=True, timeout=20, cwd=HUB)
    except (OSError, subprocess.SubprocessError) as error:
        return [f"LESSON GATE UNREAD: {error}"]
    if done.returncode == 0:
        return []
    lines = done.stdout.strip().splitlines()
    records = [n for n in lines if n.startswith("UNLESSONED ")]
    return records[:REAP_SHOW] + [n for n in lines if not n.startswith("UNLESSONED ")]


def state_line(state, key):
    """The first State line that starts '<key>:', or ''."""
    found = re.search(rf"^{key}:.*$", state, re.M)
    return found.group(0).strip() if found else ""


def stamp(now):
    return now.strftime("%Y%m%d-%H%M%S")


def log(base, row, now):
    base.mkdir(parents=True, exist_ok=True)
    path = base / "log.jsonl"
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"t": now.isoformat(timespec="seconds"), **row}) + "\n")
    rows = path.read_text(encoding="utf-8").splitlines(keepends=True)
    if len(rows) > LOG_KEEP:
        part = path.with_name(path.name + ".tmp")
        part.write_text("".join(rows[-LOG_KEEP:]), encoding="utf-8")
        oscompat.replace(part, path)


def tail(base):
    try:
        return (base / "log.jsonl").read_text(encoding="utf-8").splitlines()[-LOG_TAIL:]
    except OSError:
        return []


def steps(source):
    """The orders for this session start; none for a fresh startup, whose first message leads."""
    if source not in ("resume", "compact", "clear"):
        return []
    return ["Load the flight skill with the Skill tool unless its rules are in your context."]


def rearm_step(state, source, session):
    """The DO NOW re-arm order when no watcher runs, State is not PAUSED, and a run is out or the source is resume,
    compact or clear (a new process has no watcher, and an idle Flight wakes only by it); else None."""
    if PAUSED_RE.search(state) or watcher_alive():
        return None
    if source not in ("resume", "compact", "clear") and not open_runs(session):
        return None
    return "Re-arm the events watcher: " + ARM_FORM + "."


def session_start_json(text):
    return json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}})


def bearings(base, payload, now):
    source = str(payload.get("source") or "")
    state = state_section(base)
    head = [f"FLIGHT BEARINGS ({source or 'start'})"]
    order = steps(source)
    if rearm := rearm_step(state, source, payload.get("session_id")):
        order.append(rearm)
    if order:
        head += ["DO NOW:", *(f"{n}. {step}" for n, step in enumerate(order, 1))]
    rest = ["You are Flight, the lead session (flight skill). Your identity lives on disk: the flight skill, "
             f"the State section of {base / 'SCRATCHPAD.md'} and {base / 'log.jsonl'}. "
             "You compact in place; you hand off to a fresh Flight only for a model change."]
    if oneshot():
        rest.append(ONESHOT_TEXT.format(var=ONESHOT_VAR))
    if count := over_cap(state):
        rest.append(f"STATE OVER CAP: {count} lines, cap {STATE_CAP} (flight skill section 6); trim it now.")
    if out := open_runs(payload.get("session_id")):
        rest += ["RUNS OUT:", *out]
    rest += reap_lines()
    rest += lesson_lines()
    state_lines = (state or "STATE SECTION MISSING: rebuild it from memory.").splitlines()
    recent = tail(base)

    def text(state_lines, recent, cut):
        lines = [*head, "", *state_lines]
        if cut:
            lines.append(f"STATE CUT: {cut} more lines; read the State section of {base / 'SCRATCHPAD.md'}.")
        lines += ["", *rest]
        if recent:
            lines += ["", f"LAST {len(recent)} LOG ROWS:", *recent]
        return "\n".join(lines)

    cut = 0
    while recent and len(session_start_json(text(state_lines, recent, cut))) > OUTPUT_BUDGET:
        recent = recent[1:]
    while len(state_lines) > 1 and len(session_start_json(text(state_lines, recent, cut))) > OUTPUT_BUDGET:
        state_lines, cut = state_lines[:-1], cut + 1
    out = text(state_lines, recent, cut)
    if len(session_start_json(out)) > OUTPUT_BUDGET:  # one line alone past the budget: cut characters
        marker = f"\nTRUNCATED: read {base / 'SCRATCHPAD.md'} and {base / 'log.jsonl'}."
        while out and len(session_start_json(out + marker)) > OUTPUT_BUDGET:
            out = out[:-200]
        out += marker
    log(base, {"event": "session_start", "source": source, "session": payload.get("session_id")}, now)
    return session_start_json(out)


def prune(snapshots):
    # Each kind keeps its own newest half: one sort over both kinds puts every state- name first.
    for kind in ("state", "summary"):
        for old in sorted(snapshots.glob(f"{kind}-*.md"))[:-(KEEP_SNAPSHOTS // 2)]:
            old.unlink()


def pre_compact(base, payload, now):
    snapshots = base / "compactions"
    snapshots.mkdir(parents=True, exist_ok=True)
    (snapshots / f"state-{stamp(now)}.md").write_text(state_section(base) + "\n", encoding="utf-8")
    prune(snapshots)
    log(base, {"event": "pre_compact", "trigger": payload.get("trigger"), "session": payload.get("session_id")},
        now)
    state = state_section(base)
    lines = [PRE_COMPACT_TEXT.format(pad=base / "SCRATCHPAD.md"),
             state_line(state, "RUN GOAL") or "RUN GOAL: (none in State; read the State section)",
             state_line(state, "LIMITS") or "LIMITS: (none in State)"]
    return "\n\n".join(lines)[:OUTPUT_BUDGET]


def post_compact(base, payload, now):
    snapshots = base / "compactions"
    snapshots.mkdir(parents=True, exist_ok=True)
    (snapshots / f"summary-{stamp(now)}.md").write_text(str(payload.get("compact_summary") or "") + "\n",
                                                        encoding="utf-8")
    prune(snapshots)
    log(base, {"event": "post_compact", "trigger": payload.get("trigger"), "session": payload.get("session_id")},
        now)
    return ""


def stop(base, payload, now):
    if payload.get("stop_hook_active"):
        return ""
    state = state_section(base)
    if PAUSED_RE.search(state):
        return ""
    work = open_runs(payload.get("session_id"))
    if oneshot() and not work:  # a scripted run ends once every run is in and the watcher has printed ONESHOT DONE
        if not oneshot_pending():
            return ""
        how = ONESHOT_RUNS if watcher_alive() else ONESHOT_REARM
        log(base, {"event": "stop_nudge", "oneshot_wait": True, "session": payload.get("session_id")}, now)
        return json.dumps({"decision": "block", "reason": ONESHOT_WAIT.format(events=events_state(), how=how)})
    unwatched = bool(work) and not watcher_alive()
    count = over_cap(state)
    lost = backlog_fault(base, state)
    if not count and not lost and not unwatched:
        return ""
    pad = base / "SCRATCHPAD.md"
    reasons = ([WATCH_REASON.format(work="; ".join(work), lock=EVENTS_LOCK)] if unwatched else []) + \
        ([CAP_REASON.format(pad=pad, lines=count, cap=STATE_CAP)] if count else []) + \
        ([BACKLOG_REASON.format(pad=pad, fault=lost[0], snapshot=lost[1]) if lost[1] else BACKLOG_ADD.format(pad=pad)]
         if lost else [])
    log(base, {"event": "stop_nudge", "state_lines": count or None, "unwatched": work if unwatched else None,
               "backlog": lost[0] if lost else None, "session": payload.get("session_id")}, now)
    return json.dumps({"decision": "block", "reason": " ".join(reasons)})


EVENTS = {"SessionStart": bearings, "PreCompact": pre_compact, "PostCompact": post_compact, "Stop": stop}


def main(argv):
    if argv in (["--help"], ["-h"]):
        print(__doc__)
        return 0
    now = datetime.now()
    if argv[:1] == ["seat"]:
        code, line = seat(now)
        print(line, file=sys.stderr if code else sys.stdout)
        return code
    if argv[:1] == ["log"] and len(argv) >= 3:
        base = folder(os.environ.get("CLAUDE_CODE_SESSION_ID"))
        if base is not None:
            log(base, {"event": argv[1], "text": " ".join(argv[2:])}, now)
        return 0
    try:
        payload = json.load(sys.stdin)
        base = folder(payload.get("session_id"))
        if base is None:
            return 0
        out = EVENTS.get(payload.get("hook_event_name"), lambda *_: "")(base, payload, now)
    except Exception:  # a broken hook must never stop the Flight
        return 0
    if out:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
