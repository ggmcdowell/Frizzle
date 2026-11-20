"""Hermetic tests for Drawer/flight-hooks.py: the Flight bearings, compaction snapshots and the stop nudge."""
from datetime import datetime, timedelta
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("flight_hooks", HERE / "flight-hooks.py")
hooks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hooks)

NOW = datetime(2026, 10, 29, 12, 0)
S = "5e550000-0000-4000-8000-0000000000aa"  # the Flight session of a Stop or SessionStart payload
T, U, V = ("4fdccee3-0000-4000-8000-000000000001", "1a2b3c4d-0000-4000-8000-000000000002",
           "9e9e9e9e-0000-4000-8000-000000000003")
BACKLOG_SEEN_NAME = "backlog-seen.json"
PAD = """# Flight scratchpad

## State
### Goal
Ship the slice.

BACKLOG: fix the chest cue; trim the camp panel; cut HUD numbers

### Open asks
- forever session: waits on proof

## Older notes
not state
"""


class FlightHooksTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name) / "flight"
        self.base.mkdir()
        patcher = mock.patch.dict(os.environ, {hooks.VAR: str(self.base)})
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.base / "SCRATCHPAD.md").write_text(PAD, encoding="utf-8")
        self.lock = Path(tmp.name) / "flight-watchdog" / "events-flight.lock"
        self.lock.parent.mkdir()
        self.handoff = Path(tmp.name) / "flight-handoff"
        for name, value in (("EVENTS_LOCK", self.lock), ("reap_lines", lambda: []), ("lesson_lines", lambda: []),
                            ("HANDOFF", self.handoff)):
            patcher = mock.patch.object(hooks, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_hook(self, payload, now=NOW):
        with mock.patch.object(hooks, "datetime", wraps=datetime) as clock, \
                mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            clock.now.return_value = now
            self.assertEqual(0, hooks.main([]))
        return out.getvalue().strip()

    def log_rows(self):
        return [json.loads(line) for line in (self.base / "log.jsonl").read_text().splitlines()]

    def test_compact_bearings_carry_identity_state_log_and_orders(self):
        hooks.main(["log", "report", "sent build report"])
        out = json.loads(self.run_hook({"hook_event_name": "SessionStart", "source": "compact", "session_id": "s"}))
        text = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("You are Flight", text)
        self.assertIn("Ship the slice.", text)
        self.assertIn("forever session: waits on proof", text)
        self.assertNotIn("not state", text)
        self.assertNotIn("PR CURSOR", text)
        self.assertIn("sent build report", text)
        self.assertNotIn("standing loop", text)
        self.assertNotIn("/loop", text)
        self.assertIn("Re-arm the events watcher", text)  # no watcher holds the lock: the compacted Flight arms
        self.assertNotIn("improve step", text)
        self.assertEqual("session_start", self.log_rows()[-1]["event"])

    def test_resume_orders_the_arm_and_startup_orders_nothing(self):
        resume = self.run_hook({"hook_event_name": "SessionStart", "source": "resume"})
        startup = self.run_hook({"hook_event_name": "SessionStart", "source": "startup"})
        self.assertNotIn("standing loop", resume)
        self.assertNotIn("/loop", resume)
        self.assertIn("Re-arm the events watcher", resume)
        self.assertNotIn("gh pr", resume)
        self.assertNotIn("DO NOW", startup)
        self.assertIn("Ship the slice.", startup)

    def test_no_bearings_event_carries_an_advisor_rule(self):
        # The advisor gate and its Flight rule are retired.
        for source in ("startup", "resume", "compact"):
            self.assertNotIn("advisor", self.run_hook({"hook_event_name": "SessionStart", "source": source}).lower())
        self.assertFalse(hasattr(hooks, "ADVISOR_LINE"))

    # The CLI cuts a hook's output at 10000 characters (2.1.314); the compact bearings once
    # printed 11105 with DO NOW last. Injection: drop the two budget loops in bearings().
    def test_every_event_prints_under_the_hook_cap_with_do_now_first(self):
        filler = "".join(f"- state row {n} " + "x" * 110 + "\n" for n in range(120))
        (self.base / "SCRATCHPAD.md").write_text(
            PAD.replace("## Older notes", filler + "## Older notes"))
        for n in range(40):
            hooks.main(["log", "report", f"row {n} " + "y" * 300])
        long = self.run_hook({"hook_event_name": "SessionStart", "source": "compact"})
        self.assertLessEqual(len(long), hooks.OUTPUT_BUDGET)
        (self.base / "SCRATCHPAD.md").write_text("## State " + "z" * 12000 + "\n")  # one State line past the budget
        cut = self.run_hook({"hook_event_name": "SessionStart", "source": "compact"})
        self.assertLessEqual(len(cut), hooks.OUTPUT_BUDGET)
        self.assertIn("TRUNCATED: read", cut)
        self.assertIn("DO NOW:", cut)
        (self.base / "SCRATCHPAD.md").write_text(
            PAD.replace("## Older notes", filler + "## Older notes"))
        for source in ("startup", "resume", "compact", "clear"):
            with self.subTest(source=source):
                out = self.run_hook({"hook_event_name": "SessionStart", "source": source})
                self.assertLessEqual(len(out), hooks.OUTPUT_BUDGET)
                self.assertLess(hooks.OUTPUT_BUDGET, 10000)
                text = json.loads(out)["hookSpecificOutput"]["additionalContext"]
                self.assertIn("Ship the slice.", text)
                self.assertIn("STATE CUT:", text)
                if source != "startup":
                    self.assertEqual("DO NOW:", text.splitlines()[1])
                    self.assertLess(text.index("DO NOW:"), text.index("## State"))
        for payload in ({"hook_event_name": "PreCompact", "trigger": "auto"},
                        {"hook_event_name": "PostCompact", "compact_summary": "z" * 20000},
                        {"hook_event_name": "Stop", "stop_hook_active": False}):
            with self.subTest(event=payload["hook_event_name"]):
                self.assertLessEqual(len(self.run_hook(payload)), hooks.OUTPUT_BUDGET)

    def test_compaction_snapshots_state_and_summary(self):
        self.run_hook({"hook_event_name": "PreCompact", "trigger": "manual"})
        self.run_hook({"hook_event_name": "PostCompact", "trigger": "manual", "compact_summary": "we did X"})
        files = sorted(p.name for p in (self.base / "compactions").iterdir())
        self.assertEqual(["state-20261029-120000.md", "summary-20261029-120000.md"], files)
        self.assertIn("Ship the slice.", (self.base / "compactions" / files[0]).read_text())
        self.assertEqual("we did X\n", (self.base / "compactions" / files[1]).read_text())
        self.assertEqual(["pre_compact", "post_compact"], [r["event"] for r in self.log_rows()])

    def test_the_prune_keeps_the_newest_state_and_summary_snapshots_of_each_kind(self):
        start = NOW
        for n in range(hooks.KEEP_SNAPSHOTS + 5):
            now = start + timedelta(minutes=n)
            self.run_hook({"hook_event_name": "PreCompact", "trigger": "auto"}, now)
            self.run_hook({"hook_event_name": "PostCompact", "trigger": "auto", "compact_summary": "s"}, now)
        names = sorted(p.name for p in (self.base / "compactions").iterdir())
        last = hooks.stamp(start + timedelta(minutes=hooks.KEEP_SNAPSHOTS + 4))
        self.assertIn(f"state-{last}.md", names)
        self.assertIn(f"summary-{last}.md", names)
        self.assertEqual(hooks.KEEP_SNAPSHOTS // 2, sum(n.startswith("state-") for n in names))
        self.assertEqual(hooks.KEEP_SNAPSHOTS // 2, sum(n.startswith("summary-") for n in names))

    def stop_once(self):
        return self.run_hook({"hook_event_name": "Stop", "stop_hook_active": False, "session_id": S})

    def row(self, task, kind="start", session=S):
        """One Drawer/rows.py row of task: a start row puts a run out, a result row closes it."""
        log = self.handoff / task[:8] / "outbox.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"row": kind, "task": task, "session": session, "claim_id": "c1a10000-" + task[9:]}) + "\n")

    def test_a_state_that_lost_or_halved_its_backlog_line_blocks_once_and_names_the_snapshot(self):
        # A regex edit on the SHIPPED line once cut the BACKLOG line; State had no backlog for 12 h.
        snapshots = self.base / "compactions"
        snapshots.mkdir()
        old = snapshots / "state-20261028-100000.md"
        old.write_text("## State\nBACKLOG: short\n")
        snap = snapshots / "state-20261029-100000.md"  # the newest; the backlog sat inside the SHIPPED line
        snap.write_text("## State\nSHIPPED build 1. BACKLOG: fix the chest cue; trim the camp panel; cut HUD numbers\n")
        (snapshots / "summary-20261029-100100.md").write_text("no state\n")
        self.assertEqual("", self.stop_once())  # the same backlog: no block
        pad = self.base / "SCRATCHPAD.md"
        whole = pad.read_text()
        pad.write_text(whole.replace("BACKLOG: fix the chest cue; trim the camp panel; cut HUD numbers\n", ""))
        out = json.loads(self.stop_once())
        self.assertEqual("block", out["decision"])
        self.assertIn("no line that starts 'BACKLOG:'", out["reason"])
        self.assertIn(str(snap), out["reason"])
        self.assertEqual("", self.stop_once())  # once: the same fault does not block again
        self.assertEqual("", self.run_hook({"hook_event_name": "Stop", "stop_hook_active": True}))
        pad.write_text(whole.replace("; trim the camp panel; cut HUD numbers", ""))  # 17 of 55 characters
        out = json.loads(self.stop_once())
        self.assertIn("shrank from", out["reason"])
        self.assertIn(str(snap), out["reason"])
        self.assertEqual("", self.stop_once())
        pad.write_text(whole.replace("; cut HUD numbers", ""))  # over half kept: no block
        self.assertEqual("", self.stop_once())

    def test_a_fresh_pad_with_no_backlog_line_and_no_snapshot_orders_an_add_not_a_restore(self):
        # Oneshot run 2026-11-17: the first stop on a fresh pad was told to restore from compactions/, which did not exist.
        pad = self.base / "SCRATCHPAD.md"
        pad.write_text(re.sub(r"(?m)^BACKLOG:.*\n", "", pad.read_text()))
        self.assertFalse((self.base / "compactions").exists())
        out = json.loads(self.stop_once())
        self.assertEqual("block", out["decision"])
        self.assertIn("no State snapshot to restore it from", out["reason"])
        self.assertIn("'BACKLOG: none.'", out["reason"])
        self.assertNotIn("Restore the backlog", out["reason"])
        self.assertEqual("", self.stop_once())  # once
        pad.write_text(pad.read_text().replace("## State\n", "## State\nBACKLOG: none.\n", 1))
        self.assertEqual("", self.stop_once())

    def test_state_over_the_cap_blocks_stop_once_and_shows_in_the_bearings(self):
        self.assertEqual("", self.run_hook({"hook_event_name": "Stop"}))
        filler = "".join(f"- row {n}\n" for n in range(80))
        (self.base / "SCRATCHPAD.md").write_text(
            PAD.replace("## Older notes", filler + "## Older notes"))
        count = len(hooks.state_section(self.base).splitlines())
        self.assertGreater(count, hooks.STATE_CAP)
        out = json.loads(self.run_hook({"hook_event_name": "Stop", "stop_hook_active": False}))
        self.assertEqual("block", out["decision"])
        self.assertIn(f"has {count} lines, over the {hooks.STATE_CAP}-line cap", out["reason"])
        self.assertEqual(count, self.log_rows()[-1]["state_lines"])
        self.assertEqual("", self.run_hook({"hook_event_name": "Stop", "stop_hook_active": True}))
        start = self.run_hook({"hook_event_name": "SessionStart", "source": "compact"})
        self.assertIn(f"STATE OVER CAP: {count} lines, cap {hooks.STATE_CAP}", start)

    def test_state_at_the_cap_passes(self):
        filler = "".join(f"- row {n}\n" for n in range(hooks.STATE_CAP - 1 - len(hooks.state_section(self.base).splitlines())))  # the blank line joins State
        (self.base / "SCRATCHPAD.md").write_text(
            PAD.replace("## Older notes", filler + "## Older notes"))
        self.assertEqual(hooks.STATE_CAP, len(hooks.state_section(self.base).splitlines()))
        self.assertEqual("", self.run_hook({"hook_event_name": "Stop"}))
        self.assertNotIn("STATE OVER CAP", self.run_hook({"hook_event_name": "SessionStart", "source": "startup"}))

    # A Monitor run ends at 30 minutes; a Flight that stopped then missed every row until a
    # message woke it. Injection: drop the watch guard in stop().
    def with_state(self, lines, opener=""):
        pad = PAD.replace("## State\n", "## State\n" + opener).replace("### Open asks", lines + "\n### Open asks")
        (self.base / "SCRATCHPAD.md").write_text(pad, encoding="utf-8")

    def hold_lock(self):
        holder = subprocess.Popen(
            [sys.executable, "-c", "import fcntl,sys,time; f=open(sys.argv[1],'w'); "
             "fcntl.flock(f, fcntl.LOCK_EX); print('held', flush=True); time.sleep(60)", str(self.lock)],
            stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        self.assertEqual("held\n", holder.stdout.readline())
        return holder

    def test_a_run_out_with_no_watcher_blocks_every_stop_and_names_the_re_arm(self):
        self.row(T)
        self.row(U)
        self.row(U, "result")
        self.row(V, session="0ce00000-0000-4000-8000-000000000002")  # another session's run is not this Flight's
        for _ in range(2):  # every stop, not once: the stop after each Monitor expiry asks again
            out = json.loads(self.stop_once())
            self.assertEqual("block", out["decision"])
            self.assertIn("A card-run is out (4fdccee3 claim c1a10000)", out["reason"])
            self.assertNotIn("1a2b3c4d", out["reason"])
            self.assertNotIn("9e9e9e9e", out["reason"])
            self.assertIn("call the Monitor tool: command '.venv/bin/python -B Drawer/flight-watchdog.py events'", out["reason"])
            self.assertIn("Never as a background Bash run", out["reason"])
            self.assertIn(str(self.lock), out["reason"])
        self.assertEqual(["4fdccee3 claim c1a10000"], self.log_rows()[-1]["unwatched"])
        self.assertEqual("", self.run_hook({"hook_event_name": "Stop", "stop_hook_active": True}))
        self.row(T, "blocked")
        self.assertEqual("", self.stop_once())  # the run closed: nothing is out

    def test_a_stop_with_a_run_out_passes_while_a_watcher_holds_the_events_lock(self):
        self.row(T)
        holder = self.hold_lock()
        self.assertEqual("", self.stop_once())
        self.assertNotIn("Re-arm", self.run_hook({"hook_event_name": "SessionStart", "source": "resume", "session_id": S}))
        holder.kill()
        holder.wait()
        self.assertEqual("block", json.loads(self.stop_once())["decision"])  # the watcher ended: block again

    # A scripted run (claude -p) never ended: the loop went on after its work closed (D4).
    # Injection: drop the oneshot() pass in stop() or the ONE-SHOT line in bearings().
    def test_a_one_shot_run_passes_its_stop_once_no_run_is_out(self):
        self.row(T)
        (self.base / "SCRATCHPAD.md").write_text(
            (self.base / "SCRATCHPAD.md").read_text().replace("BACKLOG: fix the chest cue; trim the camp panel; cut HUD numbers\n", ""))
        self.assertNotIn("ONE-SHOT", self.run_hook({"hook_event_name": "SessionStart", "source": "startup"}))
        with mock.patch.dict(os.environ, {hooks.harness_config.env_name("FLIGHT_ONESHOT"): "1"}):
            start = self.run_hook({"hook_event_name": "SessionStart", "source": "startup", "session_id": S})
            self.assertIn(f"ONE-SHOT RUN ({hooks.harness_config.env_name("FLIGHT_ONESHOT")}=1)", start)
            self.assertIn("rows.py row", start)
            out = json.loads(self.stop_once())  # the run is out and no watcher runs: it still blocks
            self.assertIn("4fdccee3", out["reason"])
            self.row(T, "result")
            (self.base / BACKLOG_SEEN_NAME).unlink(missing_ok=True)
            self.assertEqual("", self.stop_once())  # every run is in: the run ends, no backlog nudge
        self.assertEqual("block", json.loads(self.stop_once())["decision"])  # the normal loop still nudges

    # Oneshot run 2026-11-17: Flight closed its work, then stopped the watcher with TaskStop before its next 30 s
    # read, so the watcher never printed ONESHOT DONE (events-flight.json kept oneshot_out true) and the run failed.
    # Injection: drop the oneshot_out check in stop() or the TaskStop words in ONESHOT_TEXT.
    def test_a_one_shot_stop_waits_for_the_watcher_to_print_oneshot_done(self):
        self.row(T)
        self.row(T, "result")
        events = self.lock.with_suffix(".json")
        with mock.patch.dict(os.environ, {hooks.harness_config.env_name("FLIGHT_ONESHOT"): "1"}):
            self.assertIn("TaskStop", self.run_hook({"hook_event_name": "SessionStart", "source": "startup"}))
            self.assertEqual("", self.stop_once())  # no out line seen by a watcher: nothing to wait for
            events.write_text(json.dumps({"oneshot_out": True}), encoding="utf-8")
            holder = self.hold_lock()
            out = json.loads(self.stop_once())  # the watcher runs and has not printed ONESHOT DONE yet
            self.assertEqual("block", out["decision"])
            self.assertIn("ONESHOT DONE", out["reason"])
            self.assertIn("TaskStop", out["reason"])
            self.assertNotIn("Re-arm", out["reason"])
            self.assertEqual("", self.run_hook({"hook_event_name": "Stop", "stop_hook_active": True}))
            holder.kill()
            holder.wait()
            out = json.loads(self.stop_once())  # the watcher was stopped first: re-arm it, it prints at its first read
            self.assertIn("Re-arm", out["reason"])
            self.assertIn("call the Monitor tool", out["reason"])
            events.write_text(json.dumps({}), encoding="utf-8")  # the watcher printed ONESHOT DONE and popped the key
            self.assertEqual("", self.stop_once())

    def test_a_paused_state_passes_with_a_run_out_and_no_watcher(self):
        self.row(T)
        self.with_state("", opener="PAUSED until the human plays build 12\n")
        self.assertEqual("", self.stop_once())
        self.assertNotIn("Re-arm", self.run_hook({"hook_event_name": "SessionStart", "source": "resume"}))

    # No tick wakes Flight; the events watcher is its one wake path. A resumed process has no
    # watcher, so the bearings order the arm with no work out. Injection: rearm_step() back to work-out only.
    def test_an_idle_resume_with_no_watcher_orders_the_arm_and_a_held_lock_or_paused_orders_none(self):
        for source in ("resume", "compact", "clear"):
            with self.subTest(source=source):
                self.assertIn("Re-arm the events watcher: call the Monitor tool: ",
                              self.run_hook({"hook_event_name": "SessionStart", "source": source}))
        holder = self.hold_lock()
        self.assertNotIn("Re-arm", self.run_hook({"hook_event_name": "SessionStart", "source": "resume"}))
        holder.kill()
        holder.wait()
        self.with_state("", opener="PAUSED until the human plays build 12\n")
        self.assertNotIn("Re-arm", self.run_hook({"hook_event_name": "SessionStart", "source": "resume"}))

    # The CLI (2.1.326, flag tengu_breezy_crescent on) caps a Monitor at 30 minutes and ignores persistent: true.
    # Its end notice is Flight's idle wake (flight skill section 1), so the arm asks for no more than the cap.
    def test_the_arm_form_keeps_the_monitor_inside_the_cli_cap(self):
        timeout = int(re.search(r"timeout (\d+) ms", hooks.ARM_FORM).group(1))
        self.assertLessEqual(timeout, 1800000)  # the CLI cap; a larger value is cut to it
        self.assertIn("call the Monitor tool: command '.venv/bin/python -B Drawer/flight-watchdog.py events'", hooks.ARM_FORM)
        self.assertIn("in a REPL, tools.Monitor", hooks.ARM_FORM)  # one form for a headless and a Code Mode Flight

    def test_no_tick_wake_path_is_left(self):
        self.assertFalse((HERE.parent / ".claude" / "loop.md").exists())
        for path in (HERE / "flight-hooks.py", HERE / "flight-watchdog.py", HERE.parent / ".claude/skills/flight/SKILL.md"):
            text = path.read_text(encoding="utf-8")
            for word in ("ScheduleWakeup", "/loop", "loop.md", "standing loop"):
                with self.subTest(path=path.name, word=word):
                    self.assertNotIn(word, text)

    def test_no_per_task_spawn_instruction_is_left(self):
        # Flight runs every task as a card-run in its own session; rows.py keeps the rows, not a session table.
        for path in (HERE.parent / ".claude/skills/flight/SKILL.md", HERE.parent / "AGENTS.md",
                     HERE.parent / ".claude/skills/harness/SKILL.md"):
            text = path.read_text(encoding="utf-8").lower()
            for words in ("session per task", "spawn per task", "spawn ready tasks", "respawn the task", "session table",
                          "spawn the harness trim", "spawns a harness trim", "one row per session"):
                with self.subTest(path=path.name, words=words):
                    self.assertNotIn(words, text)
        for path in (HERE / "flight-hooks.py", HERE / "flight-watchdog.py"):
            with self.subTest(path=path.name):
                self.assertNotIn("session table", path.read_text(encoding="utf-8"))

    def test_no_cycle_verdict_reader_is_left(self):
        # The self-improve trials flip on noise; their readers go before the file.
        word = "cycle" + "_log"  # built, so this file stays out of the reader grep
        for path in (HERE / "flight-hooks.py", HERE / "flight-digest.py", HERE.parent / ".claude/skills/flight/SKILL.md"):
            with self.subTest(path=path.name):
                self.assertNotIn(word, path.read_text(encoding="utf-8"))
        for source in ("startup", "resume", "compact"):
            text = self.run_hook({"hook_event_name": "SessionStart", "source": source})
            with self.subTest(source=source):
                self.assertNotIn("CYCLE VERDICT", text)
                self.assertNotIn("improve step", text)
        self.assertFalse(hasattr(hooks, "verdict"))

    def test_bearings_print_the_runs_out_reap_candidates_and_the_re_arm(self):
        self.row(T)
        rows = [{"id": f"{n:08x}-0000", "state": "dead", "title": f"card {n}"} for n in range(hooks.REAP_SHOW + 2)]
        fake = subprocess.CompletedProcess([], 0, stdout=json.dumps(rows), stderr="")
        real = importlib.util.module_from_spec(spec)  # a fresh load: setUp stubs reap_lines on hooks
        spec.loader.exec_module(real)
        with mock.patch.object(real.subprocess, "run", return_value=fake):
            reap = real.reap_lines()
        self.assertEqual("REAP 00000000 dead card 0", reap[0])
        self.assertEqual(hooks.REAP_SHOW + 1, len(reap))
        self.assertIn("REAP +2 more", reap[-1])
        with mock.patch.object(hooks, "reap_lines", lambda: reap):
            text = self.run_hook({"hook_event_name": "SessionStart", "source": "resume", "session_id": S})
        self.assertIn("RUNS OUT:\n4fdccee3 claim c1a10000", json.loads(text)["hookSpecificOutput"]["additionalContext"])
        self.assertIn("REAP 00000000 dead card 0", text)
        self.assertIn("Re-arm the events watcher: call the Monitor tool: ", text)

    def test_bearings_print_the_lesson_gate_red_of_each_unlessoned_incident(self):
        # The bearings run incident.py gate and print its red.
        records = [f"UNLESSONED {n:08x} todo incident {n}" for n in range(hooks.REAP_SHOW + 2)]
        red = "\n".join([*records, f"LESSON GATE RED {len(records)}: write each lesson with mem add --kind lesson"])
        real = importlib.util.module_from_spec(spec)  # a fresh load: setUp stubs lesson_lines on hooks
        spec.loader.exec_module(real)
        calls = []

        def gate(out, code):
            def run(argv, **kw):
                calls.append(argv)
                return subprocess.CompletedProcess(argv, code, stdout=out, stderr="")
            return run
        with mock.patch.object(real.subprocess, "run", gate(red, 1)):
            lines = real.lesson_lines()
        self.assertEqual(["incident.py", "gate"], [Path(calls[0][-2]).name, calls[0][-1]])
        self.assertEqual([*records[:hooks.REAP_SHOW], red.splitlines()[-1]], lines)
        with mock.patch.object(real.subprocess, "run", gate("LESSON GATE GREEN\n", 0)):
            self.assertEqual([], real.lesson_lines())
        with mock.patch.object(real.subprocess, "run", side_effect=subprocess.TimeoutExpired(["gate"], 20)):
            self.assertTrue(real.lesson_lines()[0].startswith("LESSON GATE UNREAD"))
        for source in ("startup", "resume", "compact"):
            with mock.patch.object(hooks, "lesson_lines", lambda: lines), self.subTest(source=source):
                text = self.run_hook({"hook_event_name": "SessionStart", "source": source})
                self.assertIn("UNLESSONED 00000000 todo incident 0", text)
                self.assertIn("LESSON GATE RED", text)

    def test_runs_out_need_a_full_session_id_and_paused_needs_the_state_heading(self):
        self.row(T)
        self.assertEqual((["4fdccee3 claim c1a10000"], [], []),
                         (hooks.open_runs(S), hooks.open_runs(None), hooks.open_runs("5e550000")))
        self.assertIsNone(hooks.PAUSED_RE.search("## State of play\nPAUSED x"))

    def test_pre_compact_points_at_the_compact_instructions_and_keeps_run_goal_and_limits(self):
        self.with_state("RUN GOAL: ship the cutover\nLIMITS: no spend over 5 USD; one editor")
        out = self.run_hook({"hook_event_name": "PreCompact", "trigger": "auto"})
        self.assertIn("Keep word for word (flight skill FLT-14): the task uuid8 and claim", out)
        self.assertIn("RUN GOAL: ship the cutover", out)
        self.assertIn("LIMITS: no spend over 5 USD; one editor", out)
        self.assertIn(str(self.base / "SCRATCHPAD.md"), out)
        self.assertIn("FLT-14 compact-keep", (HERE.parent / ".claude/skills/flight/SKILL.md").read_text())  # its rule
        (self.base / "SCRATCHPAD.md").write_text(PAD, encoding="utf-8")
        bare = self.run_hook({"hook_event_name": "PreCompact", "trigger": "auto"})
        self.assertIn("RUN GOAL: (none in State", bare)

    def test_the_log_keeps_only_the_newest_rows(self):
        with mock.patch.object(hooks, "LOG_KEEP", 5):
            for n in range(8):
                hooks.main(["log", "note", f"row {n}"])
        self.assertEqual([f"row {n}" for n in range(3, 8)], [r["text"] for r in self.log_rows()])
        self.assertEqual(["SCRATCHPAD.md", "log.jsonl"], sorted(p.name for p in self.base.iterdir()))

    def test_a_session_that_is_not_the_flight_gets_nothing(self):
        with mock.patch.dict(os.environ, {hooks.VAR: ""}):
            self.assertEqual("", self.run_hook({"hook_event_name": "Stop"}))
            self.assertEqual("", self.run_hook({"hook_event_name": "SessionStart", "source": "compact"}))
        self.assertFalse((self.base / "log.jsonl").exists())

    def test_the_shell_guard_keeps_another_session_from_starting_the_script(self):
        # The manifest guard: the script starts when <PFX>_FLIGHT is set or the hub has a Flight/seats folder;
        # the script then checks the payload session id against the seats.
        hub = self.base.parent / "hub"
        hub.mkdir()
        command = (f'{{ [ -z "${{{hooks.VAR}:-}}" ] && [ ! -d "$CLAUDE_PROJECT_DIR/Flight/seats" ]; }} '
                   '|| echo ran')
        env = {**{k: v for k, v in os.environ.items() if k != hooks.VAR}, "CLAUDE_PROJECT_DIR": str(hub)}
        other = subprocess.run(["sh", "-c", command], env=env, capture_output=True, text=True)
        flight = subprocess.run(["sh", "-c", command], env={**env, hooks.VAR: "Flight"}, capture_output=True, text=True)
        (hub / "Flight" / "seats").mkdir(parents=True)
        seated = subprocess.run(["sh", "-c", command], env=env, capture_output=True, text=True)
        self.assertEqual((0, ""), (other.returncode, other.stdout))
        self.assertEqual(("ran\n", "ran\n"), (flight.stdout, seated.stdout))
        manifest = (HERE / "settings-manifest.json").read_text(encoding="utf-8")
        self.assertEqual(4, manifest.count(
            '{ [ -z \\"${{ENV_PREFIX}_FLIGHT:-}\\" ] && [ ! -d \\"{HUB}/Flight/seats\\" ]; } || sh '))

    def test_the_manifest_runs_no_flight_hook_after_a_tool_call(self):
        # The owed-update mark (SendUserFile, PushNotification) fed a PR post order that is gone.
        manifest = json.loads((HERE / "settings-manifest.json").read_text())
        self.assertEqual([], [r for r in manifest["hooks"]["PostToolUse"] if "flight-hooks.py" in r["command"]])
        self.assertNotIn("PostToolUse", hooks.EVENTS)

    def test_broken_input_exits_0_quietly(self):
        with mock.patch("sys.stdin", io.StringIO("not json")), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(0, hooks.main([]))
        self.assertEqual("", out.getvalue())



SEATED = "d7eba862-0000-4000-8000-000000000001"
OTHER = "c0ffee00-0000-4000-8000-00000000000c"


class HandFlightTests(unittest.TestCase):
    """A Flight started by hand has no <PFX>_FLIGHT: it seats itself (flight-hooks.py seat) and the hooks then know
    it by its session id."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.hub = Path(tmp.name) / "hub"
        self.base = self.hub / "Flight"
        self.base.mkdir(parents=True)
        (self.base / "SCRATCHPAD.md").write_text(PAD, encoding="utf-8")
        lock = Path(tmp.name) / "flight-watchdog" / "events-flight.lock"
        lock.parent.mkdir()
        for name, value in (("HUB", self.hub), ("EVENTS_LOCK", lock), ("reap_lines", lambda: []),
                            ("lesson_lines", lambda: [])):
            patcher = mock.patch.object(hooks, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # This suite may run inside a Flight session: set each identity variable explicitly.
        self.env({})

    def env(self, values):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in (hooks.VAR, "CLAUDE_CODE_SESSION_ID"):
            os.environ.pop(name, None)
        os.environ.update(values)

    def seat(self, session=SEATED):
        (self.base / "seats").mkdir(exist_ok=True)
        (self.base / "seats" / session).write_text(json.dumps({"session": session}), encoding="utf-8")

    def run_main(self, argv, payload=None):
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload or {}))), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            code = hooks.main(argv)
        return code, out.getvalue().strip(), err.getvalue().strip()

    def log_rows(self):
        path = self.base / "log.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_a_seated_hand_flight_gets_the_bearings_and_a_log_row(self):
        self.seat()
        code, out, _ = self.run_main([], {"hook_event_name": "SessionStart", "source": "compact", "session_id": SEATED})
        self.assertEqual(0, code)
        self.assertIn("FLIGHT BEARINGS (compact)", out)
        self.assertEqual([("session_start", SEATED)], [(r["event"], r.get("session")) for r in self.log_rows()])

    def test_an_unseated_session_gets_nothing_while_another_session_holds_a_seat(self):
        self.seat()
        for payload in ({"hook_event_name": "SessionStart", "source": "compact", "session_id": OTHER},
                        {"hook_event_name": "Stop", "session_id": OTHER},
                        {"hook_event_name": "SessionStart", "source": "compact"}):
            self.assertEqual((0, ""), self.run_main([], payload)[:2])
        self.env({"CLAUDE_CODE_SESSION_ID": OTHER})
        self.assertEqual(0, self.run_main(["log", "note", "other row"])[0])
        self.assertEqual([], self.log_rows())

    def test_the_seat_verb_seats_a_hand_session_and_its_log_rows_land(self):
        self.env({"CLAUDE_CODE_SESSION_ID": SEATED, hooks.LAUNCHER_VAR: "1"})  # started by Flight.command
        code, out, _ = self.run_main(["seat"])
        self.assertEqual((0, f"SEATED {SEATED[:8]}"), (code, out))
        self.assertEqual(SEATED, json.loads((self.base / "seats" / SEATED).read_text())["session"])
        self.assertEqual(0, self.run_main(["log", "note", "hand row"])[0])
        self.assertEqual([("note", "hand row")], [(r["event"], r["text"]) for r in self.log_rows()])

    def test_the_seat_verb_of_a_plain_claude_flight_says_code_mode_is_off(self):
        # A Flight started by plain `claude` has no launcher marker; the project settings set the Code Mode variable
        # whatever the start, so only the marker shows it. Each case sets the marker itself: this suite may run in one.
        self.env({"CLAUDE_CODE_SESSION_ID": SEATED})
        code, out, _ = self.run_main(["seat"])
        self.assertEqual(0, code)
        first, second = out.split("\n")
        self.assertEqual(f"SEATED {SEATED[:8]}", first)
        self.assertEqual(hooks.CODE_MODE_OFF, second)
        self.assertIn("./Flight.command", second)
        self.assertEqual(SEATED, json.loads((self.base / "seats" / SEATED).read_text())["session"])  # still seated
        for marker in ("", "0"):
            self.env({"CLAUDE_CODE_SESSION_ID": SEATED, hooks.LAUNCHER_VAR: marker})
            self.assertIn("CODE MODE OFF", self.run_main(["seat"])[1])

    def test_the_seat_verb_refuses_a_missing_or_bad_session_id(self):
        found = {}
        for case, values in (("no session", {}), ("bad session", {"CLAUDE_CODE_SESSION_ID": "d7eba862"})):
            self.env(values)
            code, out, err = self.run_main(["seat"])
            found[case] = (code, out.startswith("SEATED"), bool(err))
        self.assertEqual({case: (1, False, True) for case in found}, found)
        self.assertFalse((self.base / "seats").exists() and any((self.base / "seats").iterdir()))


if __name__ == "__main__":
    unittest.main()
