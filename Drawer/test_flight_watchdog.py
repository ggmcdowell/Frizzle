"""Hermetic tests for Drawer/flight-watchdog.py events: what it reads, and when it wakes Flight."""
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import warnings

spec = importlib.util.spec_from_file_location("flight_watchdog", Path(__file__).with_name("flight-watchdog.py"))
dog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dog)

sys.path.insert(0, str(Path(__file__).resolve().parent / "mem"))
import mem  # noqa: E402

INCIDENTS_VAR = dog.harness_config.env_name("INCIDENTS")
MEM_DB, MEM_EMBED = dog.harness_config.env_name("MEM_DB"), dog.harness_config.env_name("MEM_EMBED")  # the hub's prefix
# The real Monitor arm's script words (flight-hooks.py WATCH_COMMAND, a relative path), never a copy.
WATCH_WORDS = shlex.split(re.search(r'^WATCH_COMMAND = "(.*)"$', Path(__file__).with_name("flight-hooks.py").read_text(
    encoding="utf-8"), re.M).group(1))[2:]
S = "11111111-1111-4111-8111-111111111111"
S2 = "22222222-2222-4222-8222-222222222222"
S3 = "33333333-3333-4333-8333-333333333333"
S4 = "44444444-4444-4444-8444-444444444444"
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def at(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")


def mem_store(db):
    """The mem store at db, keyword search only."""
    class NoEmbed:
        dim = 1536

        def embed(self, texts):
            return None
    return mem.Mem(db, NoEmbed())


class ClaudeDirTest(unittest.TestCase):
    def test_the_sessions_and_projects_folders_follow_claude_config_dir(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": tmp}):
            moved = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(moved)
        self.assertEqual((Path(tmp) / "sessions", Path(tmp) / "projects"), (moved.SESSIONS, moved.PROJECTS))


class MainTest(unittest.TestCase):
    def test_events_is_the_only_mode(self):
        script = str(Path(dog.__file__))
        for argv, code in ((["--help"], 0), (["-h"], 0), ([], 2), (["keep"], 2), (["watch"], 2)):
            run = subprocess.run([sys.executable, "-B", script, *argv], capture_output=True, text=True, timeout=60)
            self.assertEqual(code, run.returncode, (argv, run.stderr))
        self.assertIn("flight-watchdog.py events", run.stderr)


class EventsTest(unittest.TestCase):
    """Event mode: one line per new event; a Flight is a live session with a seat file."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for name in ("PROJECTS", "SESSIONS", "STATE", "flight"):
            (self.root / name).mkdir()
        for name in ("PROJECTS", "SESSIONS", "STATE"):
            self.enterContext(mock.patch.object(dog, name, self.root / name))
        self.enterContext(mock.patch.object(dog, "flight_dir", lambda: self.root / "flight"))
        self.enterContext(mock.patch.object(dog, "HUB", self.root))  # seats: never the live hub's Flight/seats
        self.enterContext(mock.patch.object(dog, "alive", lambda pid: pid != 9))
        self.enterContext(mock.patch.object(dog, "STEER_PAUSES", self.root / "steer" / "pauses.jsonl"))
        # Every test runs on a scratch mem DB: an INCIDENT line once opened a record in the live memory.
        # The Monitor command runs in the Flight session (S2): runs_out reads its runs only.
        self.enterContext(mock.patch.dict(os.environ, {MEM_DB: str(self.root / "mem.db"), MEM_EMBED: "off",
                                                       INCIDENTS_VAR: str(self.root / "incidents.jsonl"),
                                                       "CLAUDE_CODE_SESSION_ID": S2}))
        self.enterContext(mock.patch.object(dog, "HANDOFF", self.root / "handoff"))  # never the live outboxes
        (self.root / "flight" / "SCRATCHPAD.md").write_text("## State\nnone\n## Later\n", encoding="utf-8")
        self.process(4242, S, "hand-1a")
        (self.root / "PROJECTS" / "hub").mkdir()
        self.lines = [{"type": "user", "timestamp": at(5), "message": {"content": "Resume the flight loop."}}]
        self.write()
        self.clock = NOW

    def process(self, pid, session, name):
        (self.root / "SESSIONS" / f"{pid}.json").write_text(
            json.dumps({"pid": pid, "sessionId": session, "name": name}), encoding="utf-8")

    def seat(self, session):
        """Seat the session as a Flight: Flight/seats/<session id> (runlib.seated)."""
        (self.root / "Flight" / "seats").mkdir(parents=True, exist_ok=True)
        (self.root / "Flight" / "seats" / session).write_text("{}", encoding="utf-8")

    def write(self, *rows):
        self.lines += rows
        (self.root / "PROJECTS" / "hub" / f"{S}.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in self.lines), encoding="utf-8")

    def said(self, text, minutes_ago=0, send=False):
        item = ({"type": "tool_use", "name": "SendMessage", "input": {"to": "flight", "message": text}} if send
                else {"type": "text", "text": text})
        return {"type": "assistant", "timestamp": at(minutes_ago), "message": {"content": [item]}}

    def run_events(self):
        """The stdout of one `events --once` run; the state file carries over between runs."""
        test = self

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return test.clock

        out = io.StringIO()
        with mock.patch.object(dog, "datetime", Clock), mock.patch("sys.stdout", out), warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            self.assertEqual(0, dog.events(["--once", "--lock", "test"]))
        return out.getvalue().splitlines()

    def test_an_incident_line_opens_one_incident_record_at_once(self):
        # Event mode: an INCIDENT line opens a mem task card, tag incident, with what broke, impact and cause,
        # and prints its id; a re-read opens no second card; a mem that refuses prints UNLOGGED and the line.
        self.seat(S)
        db = self.root / "mem.db"
        self.enterContext(mock.patch.dict(os.environ, {MEM_DB: str(db), MEM_EMBED: "off"}))
        said = "INCIDENT the sweep deleted a live tree; impact: its gate went red; cause: an unlocked tree read as dead"
        self.write(self.said("Working on it."), self.said(said))
        lines = self.run_events()
        store = mem_store(db)
        cards = store.list(kind="task", tag="incident")
        self.assertEqual([f"INCIDENT {S[:8]} {cards[0]['id'][:8]} the sweep deleted a live tree"], lines)
        self.assertEqual(("todo", "p1", "Incident: the sweep deleted a live tree"),
                         (cards[0]["status"], cards[0]["priority"], cards[0]["title"]))
        for part in (f"session {S[:8]}", "Incident: the sweep deleted a live tree,",
                     "Impact: its gate went red.", "Suspected cause: an unlocked tree read as dead."):
            self.assertIn(part, cards[0]["body"])
        self.write(self.said(said))
        self.assertEqual([], self.run_events())
        self.assertEqual(1, len(store.list(kind="task", tag="incident")))
        store.db.close()
        os.environ[MEM_DB] = str(self.root)  # a folder: every mem call fails
        self.write(self.said("INCIDENT the probe went red"))
        self.assertEqual([f"INCIDENT {S[:8]} UNLOGGED ("], [line[:len(S[:8]) + 20] for line in self.run_events()])

    def test_a_marked_incident_line_opens_its_record_and_a_bullet_without_impact_opens_none(self):
        # A line in Markdown counts as the bare line when it holds its impact part.
        self.seat(S)
        db = self.root / "mem.db"
        self.enterContext(mock.patch.dict(os.environ, {MEM_DB: str(db), MEM_EMBED: "off"}))
        self.write(self.said("Found two faults.\n- **INCIDENT** the prune ate a backup; impact: lost State; cause: a sort\n"
                             "- INCIDENT lines are how I report faults."))
        lines = self.run_events()
        store = mem_store(db)
        cards = store.list(kind="task", tag="incident")
        self.assertEqual([f"INCIDENT {S[:8]} {cards[0]['id'][:8]} the prune ate a backup"], lines)
        self.assertIn("Impact: lost State.", cards[0]["body"])
        store.db.close()

    def test_flights_own_incident_line_opens_one_record_and_its_other_lines_print_nothing(self):
        # Flight wrote an INCIDENT line in its reply text and no record opened. Flight's lines from its first
        # read on count; older ones (opened by hand) and its DONE and NEED lines never do.
        db = self.root / "mem.db"
        self.enterContext(mock.patch.dict(os.environ, {MEM_DB: str(db), MEM_EMBED: "off"}))
        self.process(4243, S2, "hand-2b")
        self.seat(S2)
        flight = self.root / "PROJECTS" / "hub" / f"{S2}.jsonl"
        old = "INCIDENT the wall flipped; impact: a false verdict; cause: load"
        flight_rows = [{"type": "user", "timestamp": at(30), "message": {"content": "Resume the flight loop."}},
                       self.said(old, minutes_ago=20)]
        flight.write_text("".join(json.dumps(r) + "\n" for r in flight_rows), encoding="utf-8")
        self.assertEqual([], self.run_events())
        said = "INCIDENT Flight relayed an unchecked claim; impact: the human misled; cause: no check"
        flight_rows += [self.said(f"NEED x\nDONE {S[:8]} done.\n{said}", minutes_ago=-1),
                        self.said(said, minutes_ago=-2)]
        flight.write_text("".join(json.dumps(r) + "\n" for r in flight_rows), encoding="utf-8")
        self.clock = NOW + timedelta(minutes=3)
        lines = self.run_events()
        store = mem_store(db)
        cards = store.list(kind="task", tag="incident")
        self.assertEqual([f"INCIDENT {S2[:8]} {cards[0]['id'][:8]} Flight relayed an unchecked claim"], lines)
        self.assertEqual(1, len(cards))
        flight_rows.append(self.said(said, minutes_ago=-3))  # the same line again: no second record
        flight.write_text("".join(json.dumps(r) + "\n" for r in flight_rows), encoding="utf-8")
        self.assertEqual([], self.run_events())
        self.assertEqual(1, len(store.list(kind="task", tag="incident")))
        store.db.close()

    def call(self, tool_id, command, output, error=False):
        """A turn with thinking and one Bash call and no text block, then its result."""
        return [{"type": "assistant", "timestamp": at(0), "message": {"content": [
                    {"type": "thinking", "thinking": "", "signature": "x"},
                    {"type": "tool_use", "id": tool_id, "name": "Bash", "input": {"command": command}}]}},
                {"type": "user", "timestamp": at(0), "message": {"content": [
                    {"type": "tool_result", "tool_use_id": tool_id, "content": output, "is_error": error}]}}]

        # An INCIDENT line in reply text stayed in hidden thinking and never reached the transcript.
        # Drawer/incident.py opens the record from its tool call and prints its id; the watchdog
        # wakes Flight with that id and opens no second record. A text copy of the line or a line that cites
        # the id opens nothing; a call whose mem refused gets its record from the watchdog.
        self.seat(S)
        db = self.root / "mem.db"
        self.enterContext(mock.patch.dict(os.environ, {MEM_DB: str(db), MEM_EMBED: "off"}))
        line = "the wall flipped on a rerun; impact: a bad ship; cause: box load"
        command = f"cd /hub && .venv/bin/python -B Drawer/incident.py '{line}' 2>&1 | tail -2"
        run = subprocess.run([sys.executable, "-B", str(Path(dog.__file__).with_name("incident.py")), line],
                             capture_output=True, text=True, env={**os.environ, "CLAUDE_CODE_SESSION_ID": S})
        self.assertEqual(0, run.returncode, run.stderr)
        store = mem_store(db)
        cards = store.list(kind="task", tag="incident")
        card = cards[0]["id"][:8]
        self.assertEqual(f"INCIDENT {card} the wall flipped on a rerun", run.stdout.splitlines()[0])
        self.assertEqual((f"session {S}", "Incident: the wall flipped on a rerun"), (cards[0]["source"], cards[0]["title"]))
        self.write(*self.call("toolu_7", command, run.stdout))
        self.assertEqual([f"INCIDENT {S[:8]} {card} the wall flipped on a rerun"], self.run_events())
        record = json.loads((self.root / "incidents.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual((S, card, "the wall flipped on a rerun", "a bad ship"),
                         (record["session"], record["card"], record["what"], record["impact"]))
        self.write(self.said(f"INCIDENT {line}"), self.said(run.stdout.splitlines()[0]),
                   self.said(f"- **INCIDENT** {card} the wall flipped; impact: a bad ship"),
                   self.said(f"I logged incident {card}.", send=True))
        self.assertEqual([], self.run_events())
        self.assertEqual(1, len(store.list(kind="task", tag="incident")))
        self.write(*self.call("toolu_8", "python3 Drawer/incident.py 'the probe hung; impact: none; cause: a lock'",
                              "incident: no record (mem exit 1: locked). The events watchdog opens it from this call.",
                              error=True))
        # A call whose result names no id and no refusal (an older script version, a usage
        # error) opens nothing; neither does the usage text with its placeholders.
        self.write(*self.call("toolu_9", "python3 Drawer/incident.py 'INCIDENT a; impact: b'", "INCIDENT a; impact: b"),
                   *self.call("toolu_10", "python3 Drawer/incident.py '<what broke>; impact: <impact>; cause: <cause>'",
                              "incident: no line.", error=True))
        lines = self.run_events()
        cards = store.list(kind="task", tag="incident")
        self.assertEqual(2, len(cards))
        hung = [c["id"][:8] for c in cards if c["title"] == "Incident: the probe hung"]
        self.assertEqual([f"INCIDENT {S[:8]} {hung[0]} the probe hung"], lines)
        store.db.close()

    def test_incident_py_refuses_a_line_with_no_impact_and_reads_each_call_in_a_command(self):
        spec = importlib.util.spec_from_file_location("incident", Path(dog.__file__).with_name("incident.py"))
        incident = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(incident)
        self.assertEqual(2, incident.main(["the probe went red"]))
        self.assertEqual(["INCIDENT a; impact: b", "INCIDENT c d; impact: e; cause: f"], incident.from_command(
            "python3 Drawer/incident.py 'a; impact: b' && .venv/bin/python -B Drawer/incident.py INCIDENT c d\\; "
            "impact: e\\; cause: f 2>&1; grep incident.py x; cat Drawer/incident.py"))
        self.assertEqual([], incident.from_command("python3 Drawer/incident.py 'INCIDENT 1a2b3c4d cited; impact: x'"))
        self.assertEqual([], incident.from_command("python3 Drawer/incident.py '<what broke>; impact: <impact>'"))

    def test_a_steer_pause_prints_once_from_the_first_read_on(self):
        # Drawer/steer_check.py appends a row per pause; Flight wakes on each new one, never on an older one.
        (self.root / "steer").mkdir()
        pauses = self.root / "steer" / "pauses.jsonl"
        row = lambda t, why: json.dumps({"t": t, "session": S[:8], "step": "upload commit", "why": why}) + "\n"
        pauses.write_text(row(NOW.timestamp() - 60, "old"), encoding="utf-8")
        self.assertEqual([], self.run_events())
        with pauses.open("a", encoding="utf-8") as f:
            f.write(row(NOW.timestamp() + 1, "Flight holds a STOP"))
        self.assertEqual([f"PAUSE {S[:8]} upload commit: Flight holds a STOP"], self.run_events())
        self.assertEqual([], self.run_events())

    def test_a_paused_flight_prints_nothing_and_gets_the_rest_at_resume(self):
        # While the State section opens with PAUSED, the watch prints nothing; the rest waits.
        # The human's resume order wakes Flight without the watch.
        self.assertEqual([], self.run_events())  # the first read learns the logs
        pad = self.root / "flight" / "SCRATCHPAD.md"
        pad.write_text("## State\nPAUSED until the human says resume.\n", encoding="utf-8")
        self.assertEqual([], self.run_events())
        rows = self.outbox("aaaa1111", {"row": "end", "order": "o1", "task": "t", "result": "done"})
        self.assertEqual([], self.run_events())
        self.assertTrue(dog.paused(pad))
        pad.write_text("## State\nRESUMED 22:48.\n", encoding="utf-8")
        self.assertFalse(dog.paused(pad))
        self.assertEqual(rows, self.run_events())

    def outbox(self, task8, *rows):
        path = self.root / "handoff" / task8 / "outbox.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))
        return [f"{task8} {json.dumps(r, separators=(',', ':'))}" for r in rows]

    def incident_row(self, card, what, session=S2):
        with (self.root / "incidents.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": "x", "session": session, "card": card, "what": what}) + "\n")


    def flight(self, minutes_ago):
        """A live seated Flight whose newest transcript row is minutes_ago old."""
        self.process(4243, S2, "hand-2b")
        self.seat(S2)
        (self.root / "PROJECTS" / "hub" / f"{S2}.jsonl").write_text(json.dumps(
            {"type": "user", "timestamp": at(minutes_ago), "message": {"content": "go"}}) + "\n", encoding="utf-8")

    def test_a_seated_flight_with_any_name_is_watched_and_an_unseated_session_is_not(self):
        # A seated Flight (Flight/seats/<session id>) is found by its seat, never by its name.
        self.seat(S3)
        for pid, session, name in ((4244, S3, "hand-5b"), (4245, S4, "FLIGHT-44444444")):
            self.process(pid, session, name)
            (self.root / "PROJECTS" / "hub" / f"{session}.jsonl").write_text(json.dumps(
                {"type": "user", "timestamp": at(30), "message": {"content": "go"}}) + "\n", encoding="utf-8")
        self.assertEqual([], self.run_events())  # the first read of each Flight: lines from now on
        for session in (S3, S4):
            with (self.root / "PROJECTS" / "hub" / f"{session}.jsonl").open("a", encoding="utf-8") as rows:
                rows.write(json.dumps(self.said(f"INCIDENT {session[:4]} fell; impact: none", minutes_ago=-1)) + "\n")
        lines = self.run_events()
        self.assertEqual(1, len(lines), lines)
        self.assertRegex(lines[0], rf"^INCIDENT {S3[:8]} [0-9a-f]{{8}} {S3[:4]} fell$")
        self.assertEqual([], self.run_events())

    def test_a_seated_flight_whose_process_ended_is_not_watched(self):
        self.seat(S)
        (self.root / "SESSIONS" / "4242.json").unlink()
        self.process(9, S, "hand-1a")  # pid 9 is a dead process
        self.clock = NOW + timedelta(minutes=40)
        self.assertEqual([], self.run_events())

    def test_a_new_outbox_row_prints_its_task8_line_once(self):
        self.outbox("aaaa1111", {"row": "start", "order": "o1", "task": "t"})  # history before the first read
        self.assertEqual([], self.run_events())
        want = self.outbox("aaaa1111", {"row": "end", "order": "o1", "task": "t", "result": "done"})
        want += self.outbox("bbbb2222", {"row": "start", "order": "o1", "task": "u"})
        self.assertEqual(want, self.run_events())
        self.assertEqual([], self.run_events())
        with (self.root / "handoff" / "aaaa1111" / "outbox.log").open("a", encoding="utf-8") as f:
            f.write('{"row":"part"')  # a row still being written waits for its newline
        self.assertEqual([], self.run_events())

    def run_row(self, task8, kind, session=S2):
        """One Drawer/rows.py row: a start row puts a run of session out, a result row closes it."""
        return self.outbox(task8, {"row": kind, "task": f"{task8}-0000-4000-8000-000000000001", "session": session})

    # A scripted run (claude -p) never ended: the watcher never ended after its work closed (D4).
    # Injection: drop the oneshot() branch or the runs_out() exit.
    def test_a_one_shot_watch_prints_its_rows_and_ends_when_no_run_is_out(self):
        self.flight(minutes_ago=10)
        self.enterContext(mock.patch.dict(os.environ, {dog.harness_config.env_name("FLIGHT_ONESHOT"): "1"}))
        self.run_row("aaaa1111", "start")
        self.assertEqual([], self.run_events())  # the first read learns the logs; the run is out
        self.clock = NOW + timedelta(minutes=30)
        want = self.run_row("aaaa1111", "result")
        self.run_row("bbbb2222", "start", session=S3)  # another session's run: not this Flight's
        with mock.patch.object(dog.time, "sleep", side_effect=AssertionError("looped on")), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out, warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            self.assertEqual(0, dog.events(["--lock", "test"]))  # no --once: the run ends by itself
        self.assertEqual([*want, f"bbbb2222 {json.dumps({'row': 'start', 'task': 'bbbb2222-0000-4000-8000-000000000001', 'session': S3}, separators=(',', ':'))}",
                          "ONESHOT DONE: no card-run out; report and end your turn"], out.getvalue().splitlines())

    # The watcher and the Stop hook once read the work out from two definitions and disagreed, so ONESHOT DONE
    # printed with work still out. Injection: give runs_out() a definition of its own.
    def test_the_watch_and_the_stop_hook_agree_on_each_run_out(self):
        spec = importlib.util.spec_from_file_location("flight_hooks_cmp", Path(dog.__file__).with_name("flight-hooks.py"))
        hooks = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hooks)
        self.enterContext(mock.patch.object(hooks, "HANDOFF", self.root / "handoff"))
        for kinds in ((), ("start",), ("start", "result"), ("start", "blocked"), ("start", "result", "start"),
                      ("blocked",)):
            with self.subTest(kinds=kinds):
                folder = self.root / "handoff" / "aaaa1111"
                if folder.exists():
                    (folder / "outbox.log").unlink()
                for kind in kinds:
                    self.run_row("aaaa1111", kind)
                self.assertEqual(bool(hooks.open_runs(S2)), bool(dog.runs_out()))

    # A one-shot watcher armed before Flight wrote its first start row saw nothing out and printed ONESHOT DONE at
    # once. Injection: drop the out-seen check before the ONESHOT DONE exit.
    def test_a_one_shot_watch_ends_only_after_a_run_of_its_session(self):
        self.run_row("0dd00001", "start", session=S3)  # an older session's run: never this Flight's
        self.run_row("0dd00001", "result", session=S3)
        self.flight(minutes_ago=1)
        self.enterContext(mock.patch.dict(os.environ, {dog.harness_config.env_name("FLIGHT_ONESHOT"): "1"}))
        self.assertEqual([], self.run_events())  # no run of this session yet: no early ONESHOT DONE
        rows = self.run_row("aaaa1111", "start")
        self.assertEqual(rows, self.run_events())
        rows = self.run_row("aaaa1111", "result")
        self.assertEqual([*rows, "ONESHOT DONE: no card-run out; report and end your turn"], self.run_events())
        # A watcher re-armed in the same session after its runs closed ends at its first read.
        self.assertEqual(["ONESHOT DONE: no card-run out; report and end your turn"], self.run_events())

    # E2E 2026-11-18: Flight started and closed its run within 11 s, before the watcher's first read; the watcher never
    # saw a run out, never printed ONESHOT DONE, and claude -p waited on the Monitor. Injection: drop started_here().
    def test_a_one_shot_watch_ends_when_its_run_closed_before_the_first_read(self):
        self.flight(minutes_ago=1)
        self.enterContext(mock.patch.dict(os.environ, {dog.harness_config.env_name("FLIGHT_ONESHOT"): "1"}))
        self.run_row("aaaa1111", "start")
        self.run_row("aaaa1111", "result")
        self.assertEqual(["ONESHOT DONE: no card-run out; report and end your turn"], self.run_events())
        # With no session id the watch cannot tell its own runs: only a run seen out ends it.
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}):
            self.assertEqual([], self.run_events())

    # P2 review: a one-shot Flight whose first run unblocked a second GO task got ONESHOT DONE once the first run
    # closed, before it started the second. Injection: drop the go_left() check before the ONESHOT DONE exit.
    def test_a_one_shot_watch_holds_while_the_board_has_a_go_task_the_session_has_not_started(self):
        task = lambda task8: f"{task8}-0000-4000-8000-000000000001"
        board = []
        self.enterContext(mock.patch.object(dog, "board_go", lambda: list(board)))
        self.flight(minutes_ago=1)
        self.enterContext(mock.patch.dict(os.environ, {dog.harness_config.env_name("FLIGHT_ONESHOT"): "1"}))
        done = "ONESHOT DONE: no card-run out; report and end your turn"
        self.assertEqual([], self.run_events())  # the first read learns the logs
        rows = self.run_row("aaaa1111", "start")
        self.assertEqual(rows, self.run_events())
        board.append(task("bbbb2222"))  # the first run's close unblocks a second task
        rows = self.run_row("aaaa1111", "result")
        self.assertEqual(rows, self.run_events())  # no early ONESHOT DONE: bbbb2222 waits for its start
        state = json.loads((self.root / "STATE" / "events-test.json").read_text(encoding="utf-8"))
        self.assertTrue(state.get("oneshot_out"))  # so the Stop hook holds too (flight-hooks.py oneshot_pending)
        board.clear()
        rows = self.run_row("bbbb2222", "start")
        self.assertEqual(rows, self.run_events())
        board.append(task("bbbb2222"))  # a blocked run puts its task back on the board: this session started it
        rows = self.run_row("bbbb2222", "blocked")
        self.assertEqual([*rows, done], self.run_events())

    def test_the_board_read_finds_each_go_task(self):
        seeded = subprocess.run([sys.executable, "-B", str(Path(dog.__file__).with_name("mem") / "mem.py"), "seed"],
                                capture_output=True, text=True)
        self.assertEqual(0, seeded.returncode, seeded.stderr)
        go = dog.board_go()
        self.assertEqual(1, len(go))  # the starter seed holds one todo task
        self.assertRegex(go[0], r"\A[0-9a-f]{8}-")
        self.assertEqual(go, dog.go_left())  # no start row of this session yet

    def test_the_normal_watch_never_ends_when_no_run_is_out(self):
        self.run_row("aaaa1111", "start")
        self.flight(minutes_ago=30)
        self.assertEqual([], self.run_events())
        self.run_row("aaaa1111", "result")
        with mock.patch.object(dog.time, "sleep", side_effect=AssertionError("looped on")), \
                mock.patch("sys.stdout", new_callable=io.StringIO), warnings.catch_warnings(), \
                self.assertRaisesRegex(AssertionError, "looped on"):
            warnings.simplefilter("ignore", ResourceWarning)
            dog.events(["--lock", "test"])

    def test_an_incidents_jsonl_row_prints_once_and_its_transcript_line_prints_none(self):
        self.seat(S)
        self.incident_row("c0ffee99", "old fault")  # history before the first read
        self.assertEqual([], self.run_events())
        self.incident_row("c0ffee11", "the gate fell open", session=S)
        self.assertEqual([f"INCIDENT {S[:8]} c0ffee11 the gate fell open"], self.run_events())
        self.write(*self.call("toolu_3", "python3 Drawer/incident.py 'the gate fell open; impact: x'",
                              "INCIDENT c0ffee11 the gate fell open"))
        self.assertEqual([], self.run_events())  # the same record, now from the transcript: no second line

    def test_a_rearm_replays_nothing_old(self):
        self.flight(minutes_ago=40)
        self.assertEqual([], self.run_events())
        rows = self.outbox("aaaa1111", {"row": "start", "order": "o1", "task": "t"})
        self.incident_row("c0ffee11", "a fault")
        self.clock = NOW + timedelta(seconds=300)
        self.assertEqual([*rows, f"INCIDENT {S2[:8]} c0ffee11 a fault"], self.run_events())
        self.clock = NOW + timedelta(seconds=900)  # a fresh run (the re-arm): every line above is old
        self.assertEqual([], self.run_events())

    HOLD = ("import fcntl, os, sys, time; f = open(sys.argv[1], 'w'); fcntl.flock(f, fcntl.LOCK_EX); "
            "open(sys.argv[2], 'w').write(str(os.getpid())); time.sleep(120)")

    def holder_argv(self, words):
        """A fake watcher: it takes events-test.lock, writes its pid and sleeps; words follow it on its command line."""
        return [sys.executable, "-c", self.HOLD, str(self.root / "STATE" / "events-test.lock"),
                str(self.root / "STATE" / "events-test.pid"), *words]

    def wait_pid(self):
        pid = self.root / "STATE" / "events-test.pid"
        for _ in range(100):
            if pid.exists() and pid.read_text():
                return int(pid.read_text())
            time.sleep(0.05)
        self.fail("the holder never took the lock")

    def orphan(self, words, cwd=None):
        """Start the fake watcher from cwd with its launching shell gone (a Monitor that died): its parent is pid 1."""
        (self.root / "STATE" / "events-test.pid").unlink(missing_ok=True)
        argv = self.holder_argv(words)
        subprocess.run(["/bin/sh", "-c", " ".join(shlex.quote(a) for a in argv) + " >/dev/null 2>&1 &"], cwd=cwd,
                       check=True)
        orphan = self.wait_pid()
        self.addCleanup(lambda: subprocess.run(["kill", "-9", str(orphan)], capture_output=True))
        return orphan

    def assert_reaped(self, orphan):
        lines = self.run_events()
        self.assertEqual(f"EVENTS reaped: orphan process {orphan} held test", lines[0])
        self.assertEqual(str(os.getpid()), (self.root / "STATE" / "events-test.pid").read_text())
        self.assertNotEqual(0, subprocess.run(["kill", "-0", str(orphan)], capture_output=True).returncode)

    def test_an_orphan_watcher_is_reaped_and_a_live_one_is_busy(self):
        pid = self.root / "STATE" / "events-test.pid"
        argv = self.holder_argv([str(Path(dog.__file__).resolve()), "events", "--lock", "test"])
        child = subprocess.Popen(argv)  # its parent (this test) lives: not an orphan
        self.addCleanup(lambda: child.poll() is None and (child.kill(), child.wait()))
        self.wait_pid()
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            self.assertEqual(1, dog.events(["--once", "--lock", "test"]))
        self.assertEqual("EVENTS busy: another events run holds test\n", out.getvalue())
        child.kill()
        child.wait()
        pid.unlink()
        # The same watcher with its launching shell gone (a Monitor that died): its parent is now pid 1.
        self.assert_reaped(self.orphan(argv[5:]))

    def test_a_real_arm_orphan_is_reaped_from_its_hub_and_busy_from_another_folder(self):
        hub = Path(dog.__file__).resolve().parents[1]
        self.assertEqual(["Drawer/flight-watchdog.py", "events"], WATCH_WORDS)  # the relative arm form
        self.assert_reaped(self.orphan([*WATCH_WORDS, "--lock", "test"], cwd=hub))
        # The same relative command from another folder that holds its own Drawer/flight-watchdog.py: not this hub's.
        other = self.root / "other"
        (other / "Drawer").mkdir(parents=True)
        (other / "Drawer" / "flight-watchdog.py").write_text("", encoding="utf-8")
        stray = self.orphan([*WATCH_WORDS, "--lock", "test"], cwd=other)
        out = io.StringIO()
        with mock.patch("sys.stdout", out), warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            self.assertEqual(1, dog.events(["--once", "--lock", "test"]))
        self.assertEqual("EVENTS busy: another events run holds test\n", out.getvalue())
        self.assertEqual(0, subprocess.run(["kill", "-0", str(stray)], capture_output=True).returncode)

    def test_an_orphan_with_the_lock_equals_form_is_reaped(self):
        hub = Path(dog.__file__).resolve().parents[1]
        self.assert_reaped(self.orphan([*WATCH_WORDS, "--lock=test"], cwd=hub))

    def test_events_refuses_an_abbreviated_flag(self):
        # The matcher reads --lock X and --lock=X only, so the parser must start no watcher on a prefix form.
        with mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()), \
                self.assertRaises(SystemExit) as raised:
            dog.events(["--once", "--lo", "test"])
        self.assertEqual(2, raised.exception.code)

    def test_a_state_write_killed_midway_keeps_the_state_whole(self):
        self.assertEqual([], self.run_events())  # the first read learns the logs
        rows = self.outbox("aaaa1111", {"row": "start", "order": "o1", "task": "t"})
        self.assertEqual(rows, self.run_events())
        real = Path.write_text

        def killed(path, text, *args, **kwargs):
            if path.name.startswith("events-"):
                real(path, text[:5], *args, **kwargs)
                raise OSError("killed mid-write")
            return real(path, text, *args, **kwargs)

        with mock.patch.object(Path, "write_text", killed), self.assertRaises(OSError):
            self.run_events()
        self.assertEqual([], self.run_events())  # the old state is whole: the DONE never prints twice



class WindowsBranchTest(unittest.TestCase):
    """The native Windows branches, run here with oscompat.WINDOWS mocked: no ps or lsof, tree kill, shared lock."""
    COMMAND = '"C:\\hub\\.venv\\Scripts\\python.exe" -B Drawer/flight-watchdog.py events --lock test'

    def orphan(self, lock="test", parent="gone", command=COMMAND):
        done = subprocess.CompletedProcess([], 0, stdout=json.dumps({"ParentProcessId": 7, "CommandLine": command}))
        with mock.patch.object(dog.oscompat, "WINDOWS", True), mock.patch.object(dog.subprocess, "run", return_value=done) as run, \
                mock.patch.object(dog.gatelib, "pid_state", return_value=parent):
            found = dog.orphan_watcher(4242, lock)
        self.assertIn("powershell", run.call_args.args[0][0])
        self.assertIn("ProcessId=4242", run.call_args.args[0][-1])
        return found

    def test_an_orphan_is_read_from_win32_process_and_its_parent_pid(self):
        self.assertTrue(self.orphan())
        self.assertFalse(self.orphan(lock="other"))
        self.assertFalse(self.orphan(parent="live"))  # its parent still runs: not an orphan
        self.assertFalse(self.orphan(command="python -B Drawer/other.py events --lock test"))
        self.assertFalse(self.orphan(command=None))

    def test_a_powershell_failure_is_no_orphan(self):
        with mock.patch.object(dog.oscompat, "WINDOWS", True), \
                mock.patch.object(dog.subprocess, "run", side_effect=FileNotFoundError("powershell")):
            self.assertFalse(dog.orphan_watcher(4242, "test"))

    def test_reap_stops_the_tree_with_kill_tree_and_takes_the_lock(self):
        with mock.patch.object(dog.oscompat, "WINDOWS", True), mock.patch.object(dog.oscompat, "kill_tree") as kill, \
                mock.patch.object(dog.oscompat, "lock"), mock.patch.object(dog.os, "kill", side_effect=AssertionError("os.kill")):
            self.assertTrue(dog.reap(4242, object()))
        kill.assert_called_once_with(4242)

    def test_reap_of_a_gone_pid_still_tries_the_lock_and_other_errors_give_up(self):
        with mock.patch.object(dog.oscompat, "WINDOWS", True), mock.patch.object(dog.oscompat, "lock"), \
                mock.patch.object(dog.oscompat, "kill_tree", side_effect=ProcessLookupError(4242)):
            self.assertTrue(dog.reap(4242, object()))
        with mock.patch.object(dog.oscompat, "WINDOWS", True), mock.patch.object(dog.oscompat, "lock"), \
                mock.patch.object(dog.oscompat, "kill_tree", side_effect=OSError("taskkill")):
            self.assertFalse(dog.reap(4242, object()))


if __name__ == "__main__":
    unittest.main()
