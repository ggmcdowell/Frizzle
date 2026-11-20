"""Drawer/rows.py: the start gate (Skills: line, incident, the max_items cap, one run per task), the claim before the
start row, the row release and row shape, --file removal and '-' as stdin, and the open runs of one session.

Hermetic: a temp state root and in-memory mem and task-lifecycle fakes.
"""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Drawer"))
import harness_config  # noqa: E402
import rows  # noqa: E402

A = "aaaaaaaa-0000-4000-8000-000000000001"
B = "bbbbbbbb-0000-4000-8000-000000000002"
C = "cccccccc-0000-4000-8000-000000000003"
INC = "dddddddd-0000-4000-8000-000000000004"
FLIGHT = "f1a90000-0000-4000-8000-000000000001"
OTHER = "0ce00000-0000-4000-8000-000000000002"


class FakeMem:
    """The mem cards and the task-lifecycle verbs start and row call."""

    def __init__(self):
        self.cards, self.calls = {}, []

    def card(self, task, body="plain task. Skills: flight", tags=(), status="todo"):
        self.cards[task] = {"id": task, "kind": "task", "status": status, "claim_id": None, "tags": list(tags),
                            "body": body}

    def get(self, task):
        card = self.cards.get(task)
        if card is None:
            raise rows.Refused(f"mem get {task[:8]}: no card")
        return dict(card)

    def lifecycle(self, verb, task, *rest):
        self.calls.append((verb, task, *rest))
        opts = dict(zip(rest[::2], rest[1::2]))
        card = self.cards[task]
        if verb == "begin":
            if card["claim_id"] or card["status"] not in ("todo", "parked"):
                return 1, {"error": f"task {task} is already claimed"}
            card.update(claim_id=str(uuid.uuid4()), claim_session=opts["--session"], status="doing")
            return 0, {"result": "CLAIMED", "id": task, "claim_id": card["claim_id"]}
        if verb == "release":
            if opts.get("--claim-id") != card["claim_id"]:
                return 1, {"error": "not the live claim"}
            card.update(claim_id=None, status="todo")
            return 0, {"result": "RELEASED"}
        raise AssertionError(verb)


class RowsCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="rows-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        cfg = harness_config.load(ROOT)
        self.cfg = dict(cfg, project=dict(cfg["project"], state_root=str(self.base / "state")),
                        product=dict(cfg.get("product") or {}, path=""), orchestrator={"max_items": 2})
        self.mem = FakeMem()
        self.sweep = mock.Mock(return_value=[])
        for name, value in {"HANDOFF": self.base / "state" / "flight-handoff", "CFG": self.cfg,
                            "get_card": self.mem.get, "lifecycle": self.mem.lifecycle}.items():
            patch = mock.patch.object(rows, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(rows.product_worktree, "sweep", self.sweep)
        patch.start()
        self.addCleanup(patch.stop)
        self.as_session(FLIGHT)

    def as_session(self, session):
        patch = mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": session})
        patch.start()
        self.addCleanup(patch.stop)

    def run_main(self, *argv, stdin=""):
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(sys, "stdin", io.StringIO(stdin)):
            rc = rows.main(list(argv))
        return rc, out.getvalue()

    def start(self, task, *extra):
        if task not in self.mem.cards:
            self.mem.card(task)
        return self.run_main("start", "--task", task, *extra)

    def all_rows(self, task):
        return [json.loads(n) for n in (rows.HANDOFF / task[:8] / "outbox.log").read_text(encoding="utf-8").splitlines()]


class StartTests(RowsCase):
    def test_start_claims_first_writes_the_start_row_and_prints_the_card_run_args(self):
        notes = self.base / "notes.txt"
        notes.write_text("the answer to the need\n", encoding="utf-8")
        rc, out = self.start(A, "--notes", str(notes))
        claim = self.mem.cards[A]["claim_id"]
        self.assertEqual(0, rc, out)
        self.assertEqual(("begin", A, "--session", FLIGHT), self.mem.calls[0])
        line = json.loads(out.strip().removeprefix("START "))
        self.assertEqual({"workflow": "card-run", "args": {"task": A, "claim_id": claim,
                                                           "notes": "the answer to the need"}}, line)
        row = self.all_rows(A)[-1]
        self.assertEqual(("start", FLIGHT, claim, "card-run"),
                         (row["row"], row["session"], row["claim_id"], row["workflow"]))

    def test_a_card_with_no_skills_line_or_a_harness_card_with_no_incident_field_is_refused(self):
        self.mem.card(A, body="hub work with no skills line")
        rc, out = self.start(A)
        self.assertEqual((1, "ROWS-REFUSED task aaaaaaaa: the card has no Skills: line; fix the card\n"), (rc, out))
        self.mem.card(B, body="hub work. Skills: Harness")
        rc, out = self.start(B)
        self.assertEqual(1, rc)
        self.assertIn("a harness run takes one of --incident", out)
        self.assertEqual([], self.mem.calls)  # a refusal claims nothing
        self.mem.card(INC, body="Incident: the bell hung.", tags=["incident"])
        rc, out = self.start(B, "--incident", INC)
        self.assertEqual(0, rc, out)
        self.assertIn("INCIDENT STEP: this task fixes incident dddddddd", out)
        rc, out = self.start(C, "--no-incident", "planned")
        self.assertEqual(0, rc, out)

    def test_each_seeded_starter_task_passes_the_start_gate(self):
        # A fresh hub's first run is the starter task: a card with no Skills: line is refused at start.
        seed = ROOT / "Drawer" / "mem" / "seed" / "starter.jsonl"
        tasks = [c for c in map(json.loads, seed.read_text(encoding="utf-8").splitlines()) if c["kind"] == "task"]
        self.assertTrue(tasks)
        for card in tasks:
            with self.subTest(card["id"][:8]):
                self.mem.card(card["id"], body=card["body"], tags=card.get("tags", []))
                rc, out = self.start(card["id"], "--no-incident", "starter task")
                self.assertEqual(0, rc, out)
                self.assertIn("Spec: state ", card["body"])  # the flight skill spec.md card form

    def test_max_items_runs_out_is_busy_a_task_with_a_run_out_is_refused_and_a_refused_claim_is_blocked(self):
        self.assertEqual(0, self.start(A)[0])
        self.assertEqual(1, self.start(A)[0])  # one run per task
        self.assertEqual(0, self.start(B)[0])
        rc, out = self.start(C)
        self.assertEqual(rows.BUSY, rc)
        self.assertIn("BUSY 2 of max_items 2 runs out (aaaaaaaa bbbbbbbb)", out)
        self.assertNotIn(C, self.mem.cards[C].get("claim_session") or "")
        self.as_session(OTHER)  # the cap counts the runs of this session only
        self.mem.card(C, status="done")
        rc, out = self.start(C)
        self.assertEqual(1, rc)
        self.assertIn("claim refused", out)
        self.assertEqual("blocked", self.all_rows(C)[-1]["row"])
        self.assertEqual([], rows.runlib.open_runs(rows.HANDOFF, OTHER))

    def test_a_dead_sessions_start_row_is_closed_so_the_task_starts_again(self):
        self.as_session(OTHER)
        self.assertEqual(0, self.start(A)[0])
        self.mem.cards[A].update(status="todo", claim_id=None, claim_session=None)  # the dead session's claim is parked
        self.as_session(FLIGHT)
        cfg = self.base / "cfg"
        for name, make, code in (("no folder", False, 1), ("empty folder", True, 0)):
            with self.subTest(name), mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(cfg)}):
                if make:
                    (cfg / "sessions").mkdir(parents=True)
                rc, out = self.run_main("start", "--task", A)
                self.assertEqual(code, rc, out)
                if code:
                    self.assertIn("unknown", out)
        self.assertIn(f"CLOSED aaaaaaaa start row of dead session {OTHER[:8]}\n", out)
        kinds = [(r["row"], r["session"]) for r in self.all_rows(A)]
        self.assertEqual([("start", OTHER), ("blocked", FLIGHT), ("start", FLIGHT)], kinds)
        self.assertEqual(["aaaaaaaa-0000-4000-8000-000000000001"], rows.runlib.open_runs(rows.HANDOFF, FLIGHT))

    def test_a_live_or_own_sessions_start_row_still_blocks_a_start(self):
        self.as_session(OTHER)
        self.assertEqual(0, self.start(A)[0])
        self.as_session(FLIGHT)
        cfg = self.base / "cfg"
        (cfg / "sessions").mkdir(parents=True)
        (cfg / "sessions" / "1.json").write_text(json.dumps({"sessionId": OTHER, "pid": os.getpid()}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(cfg)}):
            rc, out = self.run_main("start", "--task", A)
            self.assertEqual(1, rc)
            self.assertIn("is live", out)
            self.as_session(OTHER)
            rc, out = self.run_main("start", "--task", A)
            self.assertEqual(1, rc)
            self.assertIn("is own", out)
        self.assertEqual(["start"], [r["row"] for r in self.all_rows(A)])

    def test_start_needs_a_full_session_id(self):
        self.as_session("cb15c24c")
        rc, out = self.start(A)
        self.assertEqual(1, rc)
        self.assertIn("CLAUDE_CODE_SESSION_ID is no full session id", out)


class RowTests(RowsCase):
    def test_row_releases_a_card_still_doing_under_its_claim_and_keeps_the_row_shape(self):
        self.start(A)
        self.start(B)
        claim = self.mem.cards[A]["claim_id"]
        result = {"task": A, "claim_id": claim, "status": "need", "commits": [], "delivered": False,
                  "need": "a key", "extras": ["x"], "land": [{"repo": "hub", "state": "landed", "smoke": "green"}],
                  "junk": 1}
        rc, out = self.run_main("row", "--task", A, stdin="returned: " + json.dumps(result))
        self.assertEqual((0, "ROW aaaaaaaa need\nOUT 1: bbbbbbbb\nPRODUCT SWEEP: nothing to remove.\n"), (rc, out))
        row = self.all_rows(A)[-1]
        self.assertEqual({"status": "need", "claim_id": claim, "commits": [], "delivered": False, "need": "a key",
                          "extras": ["x"], "land": [{"repo": "hub", "state": "landed", "smoke": "green"}],
                          "claim": "RELEASED", "session": FLIGHT},
                         {k: row[k] for k in row if k not in ("row", "task", "at")})
        self.assertEqual(("todo", None), (self.mem.cards[A]["status"], self.mem.cards[A]["claim_id"]))
        self.mem.cards[B].update(status="done", claim_id=None)  # card-run completed it
        rc, out = self.run_main("row", "--task", B, stdin=json.dumps({"status": "done", "delivered": True}))
        self.assertIn("ALL IN", out)
        self.assertEqual("none", self.all_rows(B)[-1]["claim"])
        self.assertEqual(1, sum(1 for c in self.mem.calls if c[0] == "release"))
        rc, out = self.run_main("row", "--task", B, stdin="{}")
        self.assertEqual((1, "ROWS-REFUSED task bbbbbbbb has no run out; rows.py start writes its start row\n"),
                         (rc, out))

    def test_no_json_is_a_failed_row_and_another_shape_is_returned(self):
        self.start(A)
        self.run_main("row", "--task", A, stdin="Error: the workflow died")
        self.assertEqual(("failed", "Error: the workflow died"),
                         (self.all_rows(A)[-1]["status"], self.all_rows(A)[-1]["cause"]))
        self.start(B)
        self.run_main("row", "--task", B, stdin='{"summary": "ok"}')
        self.assertEqual("returned", self.all_rows(B)[-1]["status"])

    def test_row_reads_a_file_and_removes_it(self):
        self.start(A)
        path = self.base / "row-1.json"
        path.write_text(json.dumps({"status": "done", "cause": "a\\b"}), encoding="utf-8")
        self.run_main("row", "--task", A, "--file", str(path))
        row = self.all_rows(A)[-1]
        self.assertEqual(("result", "done", "a\\b"), (row["row"], row["status"], row["cause"]))
        self.assertFalse(path.exists())

    # row --file - read stdin, then Path('-').unlink removed a file named '-' in the cwd.
    def test_row_file_dash_reads_stdin_and_removes_no_file(self):
        self.start(A)
        cwd = self.base / "cwd"
        cwd.mkdir()
        (cwd / "-").write_text("keep", encoding="utf-8")
        old = os.getcwd()
        os.chdir(cwd)
        try:
            self.run_main("row", "--task", A, "--file", "-", stdin=json.dumps({"status": "done"}))
        finally:
            os.chdir(old)
        self.assertEqual(("result", "done"), (self.all_rows(A)[-1]["row"], self.all_rows(A)[-1]["status"]))
        self.assertEqual("keep", (cwd / "-").read_text(encoding="utf-8"))

    def test_a_sweep_fault_is_a_held_line_and_keeps_exit_0(self):
        self.start(A)
        self.sweep.side_effect = RuntimeError("mem locked\nmore")
        rc, out = self.run_main("row", "--task", A, stdin=json.dumps({"status": "done"}))
        self.assertEqual(0, rc)
        self.assertIn("PRODUCT SWEEP: held: mem locked.", out)


class OpenTests(RowsCase):
    def test_open_lists_the_runs_of_this_session_only(self):
        self.assertEqual((0, "NONE\n"), self.run_main("open"))
        self.start(A)
        self.as_session(OTHER)
        self.start(B)
        rc, out = self.run_main("open")
        self.assertEqual(0, rc)
        self.assertTrue(out.startswith(f"OUT bbbbbbbb claim {self.mem.cards[B]['claim_id'][:8]} at "), out)
        self.assertNotIn("aaaaaaaa", out)


if __name__ == "__main__":
    unittest.main()
