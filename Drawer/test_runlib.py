"""Drawer/runlib.py: the run limit and the open runs, the seat, the session state, the incident step and
the reap."""

import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Drawer"))
import harness_config  # noqa: E402
import runlib  # noqa: E402

CFG = harness_config.load(ROOT)


class OpenRunTests(unittest.TestCase):
    # One definition of a run out for rows.py, flight-hooks.py and flight-watchdog.py.
    A, B, C = ("aaaaaaaa-0000-4000-8000-000000000001", "bbbbbbbb-0000-4000-8000-000000000002",
               "cccccccc-0000-4000-8000-000000000003")
    S1, S2 = "11111111-0000-4000-8000-000000000001", "22222222-0000-4000-8000-000000000002"

    def write(self, folder, task, *rows):
        log = Path(folder) / task[:8] / "outbox.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fh:
            for kind, session in rows:
                fh.write(json.dumps({"row": kind, "task": task, "session": session}) + "\n")

    def test_a_run_is_out_from_its_start_row_to_its_closing_row_and_a_session_sees_its_own(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(([], ({}, 0)), (runlib.open_runs(tmp), runlib.runs(tmp)))  # no rows at all
            self.write(tmp, self.A, ("start", self.S1), ("result", self.S1))
            self.write(tmp, self.B, ("start", self.S1), ("blocked", self.S1), ("start", self.S1))
            self.write(tmp, self.C, ("start", self.S2))
            (Path(tmp) / self.C[:8] / "outbox.log").open("a").write("not json\n[1]\n")
            found = (runlib.open_runs(tmp), runlib.open_runs(tmp, self.S1), runlib.open_runs(tmp, self.S2),
                     runlib.runs(tmp, self.S1)[1], runlib.open_runs(tmp, "33333333-0000-4000-8000-000000000003"))
        self.assertEqual(([self.B, self.C], [self.B], [self.C], 3, []), found)


class SeatTests(unittest.TestCase):
    def test_only_a_full_session_uuid_with_a_seat_file_is_seated(self):
        # A hand-started Flight is known by Flight/seats/<its session id>.
        session = "d7eba862-0000-4000-8000-000000000001"
        with tempfile.TemporaryDirectory() as tmp:
            hub = Path(tmp)
            before = runlib.seated(hub, session)
            (hub / "Flight" / "seats").mkdir(parents=True)
            (hub / "Flight" / "seats" / session).write_text("{}", encoding="utf-8")
            (hub / "Flight" / "seats" / "d7eba862").write_text("{}", encoding="utf-8")
            (hub / "Flight" / "seats" / "e0000000-0000-4000-8000-000000000002").mkdir()  # a folder is no seat
            found = [before, runlib.seated(hub, session), runlib.seated(hub, session.upper()),
                     runlib.seated(hub, "d7eba862"), runlib.seated(hub, ""), runlib.seated(hub, None),
                     runlib.seated(hub, "../seats/" + session),
                     runlib.seated(hub, "c0ffee00-0000-4000-8000-00000000000c"),
                     runlib.seated(hub, "e0000000-0000-4000-8000-000000000002"),
                     runlib.is_flight(hub, "Flight", None), runlib.is_flight(hub, " ", session),
                     runlib.is_flight(hub, "", "c0ffee00-0000-4000-8000-00000000000c")]
        self.assertEqual([False, True, True, False, False, False, False, False, False, True, True, False], found)


class RunLimitTests(unittest.TestCase):
    def test_the_hub_table_holds_a_positive_max_items(self):
        self.assertGreater(runlib.run_limit(CFG), 0)

    def test_a_missing_zero_bool_or_text_max_items_is_refused(self):
        self.assertEqual(4, runlib.run_limit({"orchestrator": {"max_items": 4}}))
        for value in (None, 0, True, "4", 2.5):
            with self.subTest(value=value), self.assertRaisesRegex(runlib.Usage, "max_items"):
                runlib.run_limit({"orchestrator": {"max_items": value}})


class SessionStateTests(unittest.TestCase):
    """session_state reads ~/.claude/sessions: live, dead or unknown (mem.py interrupt parks a dead claim only)."""

    def test_a_running_row_is_live_a_gone_row_is_dead_and_an_unreadable_row_is_unknown(self):
        session = "aaaaaaaa-0000-4000-8000-000000000001"
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            sessions = home / ".claude" / "sessions"
            found = {"no folder": runlib.session_state(session, home)}
            sessions.mkdir(parents=True)
            found["no session"] = runlib.session_state("", home)
            found["no row"] = runlib.session_state(session, home)
            row = sessions / "1.json"
            states = {}
            with unittest.mock.patch.object(runlib.gatelib, "pid_state", lambda pid: states[pid]):
                row.write_text(json.dumps({"sessionId": session, "pid": 11}), encoding="utf-8")
                for state, name in (("live", "runs"), ("gone", "gone"), ("unsure", "unsure pid")):
                    states[11] = state
                    found[name] = runlib.session_state(session, home)
                row.write_text(json.dumps({"sessionId": session, "pid": "11"}), encoding="utf-8")
                found["no int pid"] = runlib.session_state(session, home)
                row.write_text("{not json", encoding="utf-8")
                found["bad row"] = runlib.session_state(session, home)
                row.write_text(json.dumps({"sessionId": "bbbbbbbb-0000-4000-8000-000000000002", "pid": 11}),
                               encoding="utf-8")
                found["other session"] = runlib.session_state(session, home)
        self.assertEqual({"no folder": "unknown", "no session": "unknown", "no row": "dead", "runs": "live",
                          "gone": "dead", "unsure pid": "unknown", "no int pid": "unknown", "bad row": "unknown",
                          "other session": "dead"}, found)

    def test_this_process_is_live(self):
        session = "aaaaaaaa-0000-4000-8000-000000000001"
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / ".claude" / "sessions"
            sessions.mkdir(parents=True)
            (sessions / "1.json").write_text(json.dumps({"sessionId": session, "pid": os.getpid()}), encoding="utf-8")
            self.assertEqual("live", runlib.session_state(session, Path(tmp)))

    def test_a_claude_config_dir_holds_the_sessions_folder(self):
        """CLAUDE_CONFIG_DIR moves the sessions folder off <home>/.claude (the home argument has no row)."""
        session = "aaaaaaaa-0000-4000-8000-000000000001"
        with tempfile.TemporaryDirectory() as tmp:
            home, custom = Path(tmp) / "home", Path(tmp) / "cfg"
            (home / ".claude" / "sessions").mkdir(parents=True)
            (custom / "sessions").mkdir(parents=True)
            (custom / "sessions" / "1.json").write_text(json.dumps({"sessionId": session, "pid": os.getpid()}),
                                                        encoding="utf-8")
            self.assertEqual("dead", runlib.session_state(session, home))
            with unittest.mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(custom)}):
                self.assertEqual(custom, runlib.claude_dir(home))
                self.assertEqual("live", runlib.session_state(session, home))
                self.assertEqual(([os.getpid()], []), runlib.registry_pids(session, home))
            with unittest.mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": " "}):
                self.assertEqual(home / ".claude", runlib.claude_dir(home))


class IncidentStepTests(unittest.TestCase):
    CARDS = {"aaaa1111": {"id": "aaaa1111-full", "kind": "task", "tags": ["incident"], "status": "todo", "body": "x broke"},
             "bbbb2222": {"id": "bbbb2222-full", "kind": "task", "tags": [], "status": "todo", "body": "plain"},
             "cccc3333": {"id": "cccc3333-full", "kind": "task", "tags": ["incident"], "status": "done", "body": "old"}}

    def step(self, skills, **kw):
        return runlib.incident_step(self.CARDS.__getitem__, skills, **kw)

    def test_a_harness_run_names_one_open_incident_record_or_none(self):
        self.assertEqual((None, None), self.step(["qa"]))
        self.assertEqual((None, None), self.step(["harness"], no_incident="planned"))
        for kw, text in (({}, "takes one of"), ({"incident": "aaaa1111", "no_incident": "x"}, "takes one of"),
                         ({"incident": "bbbb2222"}, "no incident record"), ({"incident": "cccc3333"}, "is done")):
            with self.subTest(kw=kw), self.assertRaisesRegex(runlib.Usage, text):
                self.step(["harness"], **kw)
        step, full = self.step(["harness"], incident="aaaa1111")
        self.assertEqual("aaaa1111-full", full)
        self.assertIn("fixes incident aaaa1111 (x broke)", step)


OWN = "1234abcd-0000-4000-8000-000000000001"
PEER = "9999eeee-0000-4000-8000-000000000002"


class OwnProcessesTests(unittest.TestCase):
    """own_processes picks the leftovers of a closed session, and keeps every process another session may own."""

    def pids(self, rows, cwds, live=(), keep=()):
        return [pid for pid, *_ in runlib.own_processes(OWN, rows, cwds, set(live), set(keep))]

    def test_a_cwd_with_the_uuid8_as_a_whole_token_at_any_depth_matches(self):
        rows = [(10, 1, "/bin/bash"), (11, 1, "/bin/bash"), (12, 1, "/bin/bash"), (13, 1, "/bin/bash"), (14, 1, "x")]
        cwds = {10: "/var/cache/rv-1234abcd/hub_wt/aaaaaaaa-own", 11: "/tmp/1234ABCD_scratch",
                12: "/tmp/x1234abcd", 13: "/tmp/1234abcde/y"}
        self.assertEqual([10, 11], self.pids(rows, cwds))
        self.assertEqual((10, "cwd /var/cache/rv-1234abcd/hub_wt/aaaaaaaa-own", "/bin/bash"),
                         runlib.own_processes(OWN, rows, cwds, set(), set())[0])

    def test_env_matches_only_when_the_session_ids_of_its_tree_are_the_closed_one_alone(self):
        rows = [(20, 1, f"python x CLAUDE_CODE_SESSION_ID={OWN} PATH=/bin"),
                (21, 1, f"rg CLAUDE_CODE_SESSION_ID={OWN} CLAUDE_CODE_SESSION_ID={PEER}"),  # a peer's search quotes it
                (22, 1, f"python y CLAUDE_CODE_SESSION_ID={PEER}"),
                (23, 20, "/bin/bash -c loop"),  # ps -E shows no env of its own: its parent's counts
                (24, 22, "/bin/bash"),  # in an own folder, but under a peer's process
                (25, 1, "python z XCLAUDE_CODE_SESSION_ID=" + OWN)]
        self.assertEqual([20, 23], self.pids(rows, {24: "/tmp/rv-1234abcd", 21: "/tmp/rv-1234abcd"}))
        self.assertEqual("env", runlib.own_processes(OWN, rows, {}, set(), set())[0][1])

    def test_pid_1_the_caller_and_its_ancestors_and_a_live_session_tree_are_kept(self):
        rows = [(1, 0, f"launchd CLAUDE_CODE_SESSION_ID={OWN}"), (30, 1, f"python CLAUDE_CODE_SESSION_ID={OWN}"),
                (31, 30, f"python close CLAUDE_CODE_SESSION_ID={OWN}"), (40, 1, "claude"), (41, 40, "/bin/bash"),
                (42, 41, "/bin/sleep 300"), (50, 1, "/bin/sleep 300")]
        cwds = {pid: "/tmp/rv-1234abcd" for pid in (1, 40, 41, 42, 50)}
        self.assertEqual([50], self.pids(rows, cwds, live={40}, keep={30, 31}))
        self.assertEqual([30, 31, 40, 41, 42, 50], self.pids(rows, cwds))

    def test_a_long_job_runner_and_its_tree_are_kept(self):
        # A long-job.py runner outlives its session by contract (long-job.py
        # docstring), so neither rule takes it or a process under it, in an own folder or with only the session's
        # id in env. A plain own-env python and a long-job wait call still go.
        rows = [(60, 1, f"/usr/bin/python3 -B /h_wt/x/Drawer/long-job.py _run aaaa1111-x1 CLAUDE_CODE_SESSION_ID={OWN}"),
                (61, 60, f"python -B -m unittest CLAUDE_CODE_SESSION_ID={OWN}"), (62, 61, "/bin/sleep 300"),
                (63, 1, f"python z CLAUDE_CODE_SESSION_ID={OWN}"),
                (64, 1, f"python -B Drawer/long-job.py wait --name aaaa1111-x1 CLAUDE_CODE_SESSION_ID={OWN}")]
        self.assertEqual([63, 64], self.pids(rows, {61: "/tmp/rv-1234abcd", 62: "/tmp/rv-1234abcd"}))

    def test_a_session_whose_registry_row_runs_is_not_reaped(self):
        # Reap only a session proven not live. A ~/.claude/sessions row of the
        # session whose pid runs skips the reap before any signal; once that pid has ended (a zombie counts), the
        # reap goes on.
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / ".claude" / "sessions"
            sessions.mkdir(parents=True)
            (sessions / "777.json").write_text(json.dumps({"sessionId": OWN, "pid": 777}), encoding="utf-8")
            cfg = dict(CFG, project=dict(CFG["project"], state_root=str(Path(tmp) / "state")))
            rows = [(80, 1, f"python z CLAUDE_CODE_SESSION_ID={OWN}"), (777, 1, "claude")]
            for runs, expect in ((True, "REAPED: skipped, session 1234abcd still runs (pid 777)"),
                                 (False, "REAPED: 1 process(es) of session 1234abcd: 80 (env)")):
                with self.subTest(runs=runs), \
                        unittest.mock.patch.object(runlib.sys, "platform", "darwin"), \
                        unittest.mock.patch.object(runlib, "process_scan", return_value=(rows, {})), \
                        unittest.mock.patch.object(runlib, "running_pid", lambda pid: runs and pid == 777), \
                        unittest.mock.patch.object(runlib, "send", return_value="TERM") as send:
                    self.assertEqual(expect, runlib.reap_session(cfg, OWN, Path(tmp), grace=0))
                    self.assertEqual([] if runs else [unittest.mock.call(80, signal.SIGTERM)], send.call_args_list)

    def test_off_darwin_no_scan_runs(self):
        with unittest.mock.patch.object(runlib.sys, "platform", "linux"), \
                unittest.mock.patch.object(runlib.subprocess, "run") as run:
            self.assertEqual("REAPED: skipped, no process scan on this box",
                             runlib.reap_session(CFG, OWN, Path("/nonexistent")))
        run.assert_not_called()



class LongJobRunnerPatternTests(unittest.TestCase):
    def test_the_runner_is_found_after_a_slash_a_space_or_a_backslash(self):
        for command in ("python -B /h/Drawer/long-job.py _run aaaa1111-x1", "python -B Drawer/long-job.py _run x",
                        r"C:\hub\.venv\Scripts\python.exe -B C:\hub\Drawer\long-job.py _run aaaa1111-x1"):
            with self.subTest(command=command):
                self.assertTrue(runlib.LONG_JOB_RE.search(command))
        self.assertFalse(runlib.LONG_JOB_RE.search("python -B Drawer/long-job.py wait --name x"))


if __name__ == "__main__":
    unittest.main()
