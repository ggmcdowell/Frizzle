"""The INCIDENT path: a tool call opens the incident record and prints its id.

Why: Flight and a session each said they wrote an INCIDENT line, and their transcripts
hold none. Each such turn holds thinking and a tool call and no text block, while every text block after
thinking in Flight's transcript (62 of 62) was saved: the line stayed in the hidden thinking. A tool call's
input is always saved, so this command is the path:

    <python> Drawer/incident.py '<what broke>; impact: <impact>; cause: <suspected cause>'

It opens a mem task card, tag incident, p1, sourced to the session, and prints `INCIDENT <card uuid8> <what>`:
cite that id. Exit 0 with the record, 2 when the line lacks what broke or its impact part, 1 when mem refused.
The events watchdog (Drawer/flight-watchdog.py events) reads this call and its output from the transcript and
wakes Flight; an INCIDENT line in reply or SendMessage text that names no record id is its backup path.
Each record opened (here or by the watchdog's backup path) also goes to <[project] state_root>/incidents.jsonl as
one row {at, session, card, what, impact, cause}; the watchdog prints each new row. <env_prefix>_INCIDENTS names
another file (a test or a probe). Under a hub test run (mem.test_run) the default file is a scratch
file per run, never the live one: a test that reaches the writer cannot print a false INCIDENT line.

The lesson gate:

    <python> Drawer/incident.py gate

prints UNLESSONED <card uuid8> <status> <title> for each open incident record (status todo, doing or parked: a
lesson added with mem add --kind lesson --closes <id> moves its record to done), then LESSON GATE RED <n> and exit 1,
or LESSON GATE GREEN and exit 0. When mem does not answer it prints LESSON GATE UNREAD and exits 1: fail closed.
The Flight bearings (Drawer/flight-hooks.py, each session start) run it and print its red.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePath
import re
import shlex
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_config  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent / "mem"))
import mem  # noqa: E402

HUB = Path(__file__).resolve().parent.parent
MEM_CLI = HUB / "Drawer" / "mem" / "mem.py"
MEM_SECONDS = 90
# The INCIDENT protocol line: `INCIDENT <what broke>; impact: <impact>; cause: <suspected cause>`.
INCIDENT_RE = re.compile(r"^\s*INCIDENT\b[\s:]*(?P<what>.*?)\s*(?:;\s*impact:\s*(?P<impact>.*?)\s*)?"
                         r"(?:;\s*cause:\s*(?P<cause>.*?)\s*)?$", re.I)
# What broke, then the impact part; the cause part may follow.
WHOLE_RE = re.compile(r"^INCIDENT\b[\s:]*[^;\s][^;]*;\s*impact:\s*\S", re.I)
# This command's output, or a line that cites a record: `INCIDENT <card uuid8> ...`.
CITED_RE = re.compile(r"^\s*INCIDENT\s+(?P<card>[0-9a-f]{8})\b")
# This command's own words when mem refused the record: only then does the watchdog open it from the call.
REFUSED = "incident: no record ("
OPEN = ("todo", "doing", "parked")  # an incident record in these states has no closing lesson yet
OPERATORS = {";", "&&", "||", "|", "&", "(", ")", ";;", "|&"}


def line(words):
    """The INCIDENT line the words make, or None when it lacks what broke or its impact part."""
    text = " ".join(" ".join(words).split())
    if not re.match(r"INCIDENT\b", text, re.I):
        text = f"INCIDENT {text}"
    text = "INCIDENT" + text[len("INCIDENT"):]
    placeholder = re.match(r"INCIDENT[\s:]*<", text)  # the usage text, '<what broke>; impact: <impact>'
    return text if WHOLE_RE.match(text) and not CITED_RE.match(text) and not placeholder else None


def from_command(command):
    """Each INCIDENT line a shell command gives this script, in order."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return []
    found = []
    for n, token in enumerate(tokens):
        if PurePath(token).name != "incident.py":
            continue
        words, rest = [], tokens[n + 1:]
        for k, word in enumerate(rest):
            if word in OPERATORS or word[:1] in "<>" or (word.isdigit() and "".join(rest[k + 1:k + 2])[:1] in ("<", ">")):
                break
            words.append(word)
        if (said := line(words)) is not None:
            found.append(said)
    return found


def card_in(output, said):
    """The record id this command's output names for the line; REFUSED when it says mem refused the record;
    else None: the call ran no current incident.py (a test run of an older version)."""
    what = INCIDENT_RE.match(said).group("what").strip().rstrip(".") or "not given"
    for card, named in re.findall(r"^INCIDENT ([0-9a-f]{8}) (.*)$", output, re.M):
        if named.strip() == what:
            return card
    return REFUSED if REFUSED in output else None


def incidents_path():
    """The incidents file: <env_prefix>_INCIDENTS when set, else a scratch file per hub test run, else <[project] state_root>/incidents.jsonl."""
    cfg = harness_config.load(HUB)
    named = os.environ.get(harness_config.env_name("INCIDENTS", cfg), "").strip()
    if named:
        return Path(named)
    if (run := mem.test_run()) is not None:
        return Path(tempfile.gettempdir()) / "hub-test-incidents" / f"{run}.jsonl"
    return harness_config.path(cfg["project"]["state_root"]) / "incidents.jsonl"


def append_row(row):
    """Append one record row to the incidents file; a fault there never loses the record (mem holds it)."""
    try:
        path = incidents_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, separators=(",", ":")) + "\n")
    except OSError as error:
        print(f"incident: incidents.jsonl not kept: {error}", file=sys.stderr)


def open_record(session, said):
    """Open the incident record for one INCIDENT line: a mem task card, tag incident, p1, in the task form
    with no date (the row's created_at holds the time). (card uuid8, what) when mem opened it, else (None, why)."""
    hit = INCIDENT_RE.match(said)
    part = lambda name: (hit.group(name) or "").strip().rstrip(".") or "not given"  # noqa: E731
    what = part("what")
    body = (f"Incident: {what}, reported by session {session[:8]}. Impact: {part('impact')}. "
            f"Suspected cause: {part('cause')}. Why: Flight must see each fault at detection. "
            "Done when: the fix lands and its lesson closes this record (mem add --kind lesson --closes <this id>).")
    try:
        run = subprocess.run([sys.executable, "-B", str(MEM_CLI), "add", "--kind", "task", "--tag", "incident",
                              "--priority", "p1", "--title", f"Incident: {what}"[:120], "--body", body,
                              "--source", f"session {session}", "--new"],
                             capture_output=True, text=True, timeout=MEM_SECONDS, cwd=HUB)
        card = re.match(r"([0-9a-f]{8}) task\b", run.stdout) if run.returncode == 0 else None
        why = f"mem exit {run.returncode}: {(run.stderr.strip().splitlines() or [''])[-1][:200]}"
    except (OSError, subprocess.SubprocessError) as error:
        card, why = None, f"mem failed: {error}"
    if not card:
        return None, why
    append_row({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "session": session,
                "card": card.group(1), "what": what, "impact": part("impact"), "cause": part("cause")})
    return card.group(1), what


def unlessoned():
    """The open incident records (no lesson closed them yet), oldest first; raises RuntimeError when mem fails."""
    try:
        run = subprocess.run([sys.executable, "-B", str(MEM_CLI), "list", "--kind", "task", "--tag", "incident",
                              "--limit", "1000", "--json"], capture_output=True, text=True, timeout=MEM_SECONDS, cwd=HUB)
        rows = json.loads(run.stdout) if run.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise RuntimeError(f"mem failed: {error}") from error
    if not isinstance(rows, list):
        raise RuntimeError(f"mem exit {run.returncode}: {(run.stderr.strip().splitlines() or [''])[-1][:200]}")
    return sorted((r for r in rows if r.get("status") in OPEN), key=lambda r: r.get("created_at") or "")


def gate():
    """The lesson gate: 0 when no incident record is open, else 1 (also when mem does not answer)."""
    try:
        records = unlessoned()
    except RuntimeError as error:
        print(f"LESSON GATE UNREAD: {error}")
        return 1
    for r in records:
        print(f"UNLESSONED {r['id'][:8]} {r['status']} {r.get('title') or r.get('body', '')[:100]}")
    if records:
        print(f"LESSON GATE RED {len(records)}: write each lesson with mem add --kind lesson --closes <id>")
        return 1
    print("LESSON GATE GREEN")
    return 0


def main(argv):
    if argv in (["--help"], ["-h"]):
        print(__doc__)
        return 0
    if argv == ["gate"]:
        return gate()
    said = line(argv)
    if said is None:
        print("incident: no line. Give: '<what broke>; impact: <impact>; cause: <suspected cause>'", file=sys.stderr)
        return 2
    session = os.environ.get("CLAUDE_CODE_SESSION_ID") or "unknown"
    card, what = open_record(session, said)
    if card is None:
        print(f"{REFUSED}{what}). The events watchdog opens it from this call.", file=sys.stderr)
        return 1
    print(f"INCIDENT {card} {what}")
    print(f"Record {card} is open. Cite {card} when you report it; open no record by hand.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
