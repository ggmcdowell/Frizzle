"""The smoke inventory: applicability, each check's verdict, and the repair ledger."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

DRAWER = Path(__file__).resolve().parents[1]
if str(DRAWER) not in sys.path:
    sys.path.insert(0, str(DRAWER))
import harness_config  # noqa: E402
from runtime import components, profile  # noqa: E402

FULL = profile.Machine("posix", "claude")
HUB_VAR = harness_config.env_name("HUB")
PROBE_ENV = {HUB_VAR: "hub", "PATH": os.environ.get("PATH", "")}
# The memory server name in this hub's registration, read from config as the checked code reads it.
SERVER = harness_config.server_name(harness_config.load(DRAWER.parent))
FAILED = {"name": "memory", "status": "FAIL", "reason": "the server did not answer", "detail": ""}
# The fix a refused repair names.
CLEAR = re.escape("seat.py repair memory --clear")
# The check that fails the probe writes these two lines; every memory row below reuses them.
GRAPH_FAILURE = f"{SERVER} did not answer a valid tool list\ncommand python"


def completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=["probe"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


def record(name, status, reason="", detail=""):
    """One component record, in the shape components._record writes."""
    return {"name": name, "status": status, "reason": reason, "detail": detail}


class ComponentsFixture(unittest.TestCase):
    """A temp home, so the repair ledger lands beside a temp runtime profile."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="runtime components ")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.hub = self.home / "a hub" / "Hub"
        self.hub.mkdir(parents=True)
        self.python = components._interpreter(self.hub)
        self.python.parent.mkdir(parents=True)
        self.python.write_text("", encoding="utf-8")
        home = patch.object(Path, "home", staticmethod(lambda: self.home))
        home.start()
        self.addCleanup(home.stop)
        probe_env = patch.object(components, "_probe_env", return_value=dict(PROBE_ENV))
        probe_env.start()
        self.addCleanup(probe_env.stop)
        self.servers = {SERVER: {"command": "python", "args": ["-B", SERVER]}}
        self.register()

    def register(self, **entries):
        """The Claude registration this seat's agent reads; a keyword replaces one server."""
        self.servers.update(entries)
        (self.hub / ".mcp.json").write_text(json.dumps({"mcpServers": self.servers}), encoding="utf-8")

    def check(self, name, machine=FULL):
        records = components.check_all(self.hub, machine, only=[name])
        self.assertEqual(1, len(records), records)
        return records[0]

    def prober(self, answer, started=None):
        """The stdio prober, mocked: it records every start and gives one answer."""
        def probe_stdio(argv, cwd, env, *_a):
            if started is not None:
                started.append((list(argv), cwd, env))
            return answer
        return patch.object(components, "_mcp_probe",
                            return_value=types.SimpleNamespace(probe_stdio=probe_stdio))

    def spy(self, owner, name, *answers):
        """Patch an argv-first callable; record each argv, answer in turn, keep the last."""
        calls = []
        replies = list(answers) or [completed(0)]

        def call(argv, *_a, **_k):
            calls.append([str(item) for item in argv])
            return replies[min(len(calls) - 1, len(replies) - 1)]

        return calls, patch.object(owner, name, side_effect=call)

    def smoke(self, command, seconds=900.0):
        """The [smoke] settings, fixed: the hub's harness.toml never decides a verdict here."""
        return patch.object(components, "_smoke_settings", return_value=(command, seconds))


class InventoryTests(ComponentsFixture):
    def test_the_inventory_keeps_one_fixed_order(self):
        self.assertEqual(["memory", "smoke"], [item.name for item in components.inventory(self.hub)])

    def test_a_check_that_raises_becomes_a_fail_record(self):
        def explode(_hub, _machine, _env):
            raise RuntimeError("the probe host vanished")
        with patch.object(components, "_check_memory", explode):
            got = self.check("memory")
        self.assertEqual(record("memory", "FAIL", "the check did not run (RuntimeError)",
                                "the probe host vanished"), got)


class GraphTests(ComponentsFixture):
    def test_a_missing_hub_environment_fails_before_any_probe(self):
        self.python.unlink()
        started = []
        with self.prober((True, ""), started):
            got = self.check("memory")
        self.assertEqual([], started)
        self.assertEqual(record("memory", "FAIL", "run repair memory",
                                f"the hub environment is missing: {self.python}"), got)

    def test_a_passing_probe_passes(self):
        with self.prober((True, "3 tools")):
            got = self.check("memory")
        self.assertEqual(record("memory", "PASS", "", "3 tools"), got)

    def test_a_failing_probe_names_the_repair_and_the_started_command(self):
        with self.prober((False, "the server is down")):
            got = self.check("memory")
        self.assertEqual(record("memory", "FAIL", "run repair memory",
                                f"{GRAPH_FAILURE}\nthe server is down"), got)


    def test_a_locked_package_that_does_not_import_names_the_repair(self):
        # A half-deleted environment fails the import, not the probe: the repair loop acts on the reason.
        def probe_stdio(*_a):
            raise ModuleNotFoundError("No module named 'anyio'", name="anyio")
        with patch.object(components, "_mcp_probe", return_value=types.SimpleNamespace(probe_stdio=probe_stdio)):
            got = self.check("memory")
        self.assertEqual(record("memory", "FAIL", "run repair memory",
                                "a locked package did not import\n"
                                "ModuleNotFoundError: No module named 'anyio'"), got)


class RegistrationTests(ComponentsFixture):
    """The memory row starts its server as this seat's agent does: the registration it reads."""

    def probe(self, row, machine=FULL, answer=(True, "3 tools: get_status")):
        started = []
        with self.prober(answer, started), \
             patch.dict(os.environ, {"QA_CLIENT_ENV": "kept", HUB_VAR: "the client hub"}):
            return self.check(row, machine), started

    def test_a_seat_starts_the_mcp_json_command_in_the_client_environment(self):
        self.register(**{SERVER: {"command": "py", "args": ["-B", "-c", "bootstrap"],
                                  "env": {"QA_ENTRY_ENV": "1"}}})
        got, started = self.probe("memory")
        (argv, cwd, env), = started
        self.assertEqual((["py", "-B", "-c", "bootstrap"], str(self.hub)), (argv, cwd))
        # The client's own environment,
        # and its hub variable (PROBE_ENV) must not reach the started server.
        self.assertEqual(("kept", "1", "the client hub"),
                         (env.get("QA_CLIENT_ENV"), env.get("QA_ENTRY_ENV"), env.get(HUB_VAR)))
        self.assertEqual(record("memory", "PASS", "", "3 tools: get_status"), got)

    def test_a_server_missing_from_the_registration_fails_before_any_probe(self):
        del self.servers[SERVER]
        self.register()
        got, started = self.probe("memory")
        self.assertEqual([], started)
        self.assertEqual(record("memory", "FAIL",
                                f"{SERVER} is not registered for this seat: run seat.py configure",
                                f"KeyError: {SERVER!r}"), got)

    def test_a_box_without_a_registration_file_fails_before_any_probe(self):
        (self.hub / ".mcp.json").unlink()
        got, started = self.probe("memory")
        self.assertEqual([], started)
        self.assertEqual(f"{SERVER} is not registered for this seat: run seat.py configure", got["reason"])
        self.assertTrue(got["detail"].startswith("FileNotFoundError: "), got)


class SmokeTests(ComponentsFixture):
    """The configured smoke command: skipped when empty, run from the hub root, exit 0 passes."""

    def test_an_empty_command_skips_and_runs_nothing(self):
        ran, runs = self.spy(components, "_run")
        with self.smoke(""), runs:
            got = self.check("smoke")
        self.assertEqual([], ran)
        self.assertEqual(record("smoke", "SKIPPED", "no [smoke] command in harness.toml"), got)

    def test_the_command_runs_split_from_the_hub_root_with_the_probe_environment(self):
        seen = []

        def run(argv, cwd, env, timeout):
            seen.append((list(argv), Path(cwd), dict(env), timeout))
            return completed(0, stdout="12 tests passed\n", stderr="")
        with self.smoke('engine-smoke --suite "edit mode"', 42.0), \
             patch.object(components, "_run", side_effect=run):
            got = self.check("smoke")
        expected = ["engine-smoke", "--suite", "edit mode"]
        self.assertEqual([(expected, self.hub, PROBE_ENV, 42.0)], seen)
        self.assertEqual(record("smoke", "PASS", "", "12 tests passed"), got)

    def test_a_nonzero_exit_fails_with_the_output_tail(self):
        with self.smoke("engine-smoke"), \
             patch.object(components, "_run", return_value=completed(3, "ran 2", "1 test failed")):
            got = self.check("smoke")
        self.assertEqual(record("smoke", "FAIL", "the smoke command exited 3", "ran 2\n1 test failed"), got)

    def test_the_python_word_is_the_hub_interpreter(self):
        seen = []

        def run(argv, cwd, env, timeout):
            seen.append(list(argv))
            return completed(0, stdout="ok\n", stderr="")
        with self.smoke("{python} app/smoke.py"), patch.object(components, "_run", side_effect=run):
            self.check("smoke")
        venv = components._interpreter(self.hub)
        self.assertEqual([[str(venv) if venv.is_file() else sys.executable, "app/smoke.py"]], seen)

    def test_the_blocked_exit_is_blocked_with_the_last_line_as_reason(self):
        out = "smoke: 3 assemblies\nEDITOR HOLDS PROJECT: an editor (pid 7) has the project open.\n\n"
        with self.smoke("engine-smoke"), patch.object(components, "_smoke_blocked_exit", return_value=75), \
             patch.object(components, "_run", return_value=completed(75, out, "")):
            got = self.check("smoke")
        self.assertEqual(("BLOCKED", "EDITOR HOLDS PROJECT: an editor (pid 7) has the project open."),
                         (got["status"], got["reason"]))

    def test_the_blocked_exit_fails_when_none_is_configured(self):
        with self.smoke("engine-smoke"), patch.object(components, "_smoke_blocked_exit", return_value=None), \
             patch.object(components, "_run", return_value=completed(75, "busy", "")):
            got = self.check("smoke")
        self.assertEqual(("FAIL", "the smoke command exited 75"), (got["status"], got["reason"]))

    def test_the_blocked_exit_comes_from_the_hub_config(self):
        code = harness_config.load(DRAWER.parent)["smoke"].get("blocked_exit")
        self.assertEqual(code, components._smoke_blocked_exit())

    def test_a_command_past_its_time_limit_fails(self):
        late = subprocess.TimeoutExpired(["engine-smoke"], 5)
        with self.smoke("engine-smoke", 5.0), patch.object(components, "_run", side_effect=late):
            got = self.check("smoke")
        self.assertEqual(record("smoke", "FAIL", "the smoke command did not finish in 5 s", "engine-smoke"), got)

    def test_a_command_that_cannot_start_fails(self):
        with self.smoke(str(self.home / "no such tool")):
            got = self.check("smoke")
        self.assertEqual(("FAIL", "the smoke command did not start"), (got["status"], got["reason"]))
        self.assertTrue(got["detail"].startswith("FileNotFoundError: "), got)

    def test_a_command_that_does_not_parse_fails_before_it_runs(self):
        ran, runs = self.spy(components, "_run")
        # A lone trailing backslash fails only the POSIX split the Mac seat uses.
        with self.smoke('engine-smoke open\\'), runs:
            got = self.check("smoke")
        self.assertEqual([], ran)
        self.assertEqual(("FAIL", "the [smoke] command does not parse"), (got["status"], got["reason"]))

    def test_the_settings_come_from_this_hubs_config(self):
        cfg = harness_config.load(DRAWER.parent)["smoke"]
        self.assertEqual((str(cfg.get("command") or "").strip(), float(cfg["timeout_seconds"])),
                         components._smoke_settings())


class RepairTests(ComponentsFixture):
    def ledger(self):
        return json.loads(components.ledger_path(self.hub).read_text(encoding="utf-8"))

    def entry(self):
        """The one ledger row, without the stamp only the clock can name."""
        row, = self.ledger().values()
        return {key: value for key, value in row.items() if key != "attempted_at"}

    def repairs(self, note, answer):
        """The memory repair, mocked: it records each call and the recheck gives one answer."""
        calls = []
        # The fresh-process recheck runs the same check in this process, so the prober mock answers it.
        recheck = lambda hub, name, env: components.check_all(hub, FULL, only=[name])[0]
        return calls, (patch.object(components, "_repair_memory",
                                    side_effect=lambda hub, rec, env: calls.append(rec["reason"]) or note),
                       self.prober(answer),
                       patch.object(components, "_recheck", side_effect=recheck))

    def test_a_pass_or_skipped_record_never_triggers_a_repair(self):
        calls = []
        with patch.object(components, "_repair_memory",
                          side_effect=lambda hub, rec, env: calls.append(rec["reason"])):
            for status in ("PASS", "SKIPPED"):
                with self.subTest(status=status):
                    given = {"name": "memory", "status": status, "reason": "", "detail": "fine"}
                    self.assertEqual(given, components.repair(self.hub, "memory", given))
        self.assertEqual([], calls)
        self.assertFalse(components.ledger_path(self.hub).exists())

    def test_a_component_without_an_automated_repair_is_blocked(self):
        got = components.repair(self.hub, "smoke",
                                {"name": "smoke", "status": "FAIL", "reason": "the smoke command exited 3"})
        self.assertEqual(record("smoke", "BLOCKED", "no automated repair",
                                "fix what the smoke command reports; the engine pack owns it. "
                                "the smoke command exited 3"), got)
        self.assertFalse(components.ledger_path(self.hub).exists())

    def test_a_successful_repair_rechecks_and_records_repaired(self):
        calls, (repairer, prober, machine) = self.repairs("uv exit 0", (True, "ok"))
        with repairer, prober, machine:
            got = components.repair(self.hub, "memory", FAILED)
        self.assertEqual(["the server did not answer"], calls)
        self.assertEqual(record("memory", "PASS", "", "ok"), got)
        self.assertEqual({"name": "memory", "reason": "the server did not answer",
                          "result": "repaired"}, self.entry())

    def test_the_memory_repair_syncs_the_hub_environment_then_renders_the_registration(self):
        # The memory row starts the registered command, so its repair must also restore the
        # registration: a sync alone leaves a broken .mcp.json broken.
        render = [str(self.python), "-B", str(self.hub / "Drawer" / "settings-sync.py"), "--mcp"]
        ran, runs = self.spy(components, "_run")
        with runs, patch.object(components, "_interpreter", return_value=self.python):
            note = components._repair_memory(self.hub, FAILED, dict(PROBE_ENV))
        self.assertEqual([["uv", "sync", "--frozen"], render], ran)
        self.assertEqual(f"uv exit 0; {self.python.name} exit 0", note)

    def test_a_memory_repair_that_leaves_no_venv_keeps_the_sync_cause(self):
        self.python.unlink()
        ran, runs = self.spy(components, "_run", completed(1, stderr="uv: no lock file"))
        with runs, patch.object(components, "_interpreter", return_value=self.python):
            note = components._repair_memory(self.hub, FAILED, dict(PROBE_ENV))
        self.assertEqual([["uv", "sync", "--frozen"]], ran)
        self.assertEqual("uv exit 1: uv: no lock file", note)

    def test_a_failed_repair_records_failed_and_a_repeat_is_refused(self):
        calls, (repairer, prober, machine) = self.repairs("uv exit 1", (False, "still down"))
        with repairer, prober, machine:
            got = components.repair(self.hub, "memory", FAILED)
            self.assertEqual(record("memory", "FAIL", "run repair memory",
                                    f"repair: uv exit 1\n{GRAPH_FAILURE}\nstill down"), got)
            self.assertEqual({"name": "memory", "reason": "the server did not answer",
                              "result": "failed"}, self.entry())
            with self.assertRaisesRegex(components.RepeatRefused, CLEAR):
                components.repair(self.hub, "memory", FAILED)
            self.assertEqual(["the server did not answer"], calls)
            # A different reason is a different repair: the refusal is per reason, not per name.
            components.repair(self.hub, "memory", {**FAILED, "reason": "a different reason"})
            self.assertEqual(["the server did not answer", "a different reason"], calls)
        self.assertEqual(2, len(self.ledger()))

    def test_a_refusal_lasts_the_day_it_was_recorded_only(self):
        calls, (repairer, prober, machine) = self.repairs("uv exit 1", (False, "still down"))
        with repairer, prober, machine:
            components.repair(self.hub, "memory", FAILED)
            ledger = self.ledger()
            key, = ledger
            ledger[key]["attempted_at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
            components._write_ledger(self.hub, ledger)
            components.repair(self.hub, "memory", FAILED)
            self.assertEqual(["the server did not answer"] * 2, calls)
            with self.assertRaisesRegex(components.RepeatRefused, CLEAR):
                components.repair(self.hub, "memory", FAILED)

    def test_clearing_drops_only_that_components_entries(self):
        components._write_ledger(self.hub, {"memory:1": {"result": "failed"}, "memory:2": {"result": "failed"},
                                            "smoke:3": {"result": "failed"}})
        self.assertEqual(2, components.clear_refusals(self.hub, "memory"))
        self.assertEqual(["smoke:3"], list(self.ledger()))
        self.assertEqual(0, components.clear_refusals(self.hub, "memory"))

    def test_a_truncated_ledger_reads_as_empty_and_the_repair_proceeds(self):
        path = components.ledger_path(self.hub)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"memory:abc": {"name"', encoding="utf-8")
        self.assertEqual({}, components._read_ledger(self.hub))
        calls, (repairer, prober, machine) = self.repairs("uv exit 1", (False, "still down"))
        with repairer, prober, machine:
            components.repair(self.hub, "memory", FAILED)
        self.assertEqual(["the server did not answer"], calls)
        self.assertEqual({"name": "memory", "reason": "the server did not answer",
                          "result": "failed"}, self.entry())

    def test_a_repair_that_raises_is_recorded_not_propagated(self):
        _, (_, prober, recheck) = self.repairs("", (False, "still down"))
        with patch.object(components, "_repair_memory", side_effect=OSError("uv is missing")), \
             prober, recheck:
            got = components.repair(self.hub, "memory", FAILED)
        self.assertEqual(record("memory", "FAIL", "run repair memory",
                                "repair: the repair did not run (OSError: uv is missing)\n"
                                f"{GRAPH_FAILURE}\nstill down"), got)
        self.assertEqual({"name": "memory", "reason": "the server did not answer",
                          "result": "failed"}, self.entry())

    # The recheck after a repair runs in a fresh interpreter: this one's import system still
    # holds the lookups that failed before the environment was rebuilt.
    def test_a_repair_returns_the_fresh_process_record_not_an_in_process_check(self):
        fresh = record("memory", "PASS", "", "3 tools")
        with patch.object(components, "_repair_memory", return_value="uv exit 0"), \
             patch.object(components, "_recheck", return_value=fresh) as recheck, \
             patch.object(components, "check_all", side_effect=AssertionError("in-process recheck")):
            got = components.repair(self.hub, "memory", FAILED)
        self.assertEqual(fresh, got)
        self.assertEqual([((self.hub, "memory", dict(PROBE_ENV)), {})], [tuple(c) for c in recheck.call_args_list])
        self.assertEqual("repaired", self.entry()["result"])

    def test_the_recheck_starts_the_hub_interpreter_and_reads_its_record(self):
        fresh = record("memory", "PASS", "", "3 tools")
        ran, runs = self.spy(components, "_run", completed(0, stdout="noise\n" + json.dumps(fresh) + "\n"))
        with runs:
            got = components._recheck(self.hub, "memory", dict(PROBE_ENV))
        self.assertEqual(fresh, got)
        argv, = ran
        self.assertEqual([str(self.python), "-B", "-c"], argv[:3])
        self.assertEqual([str(self.hub), "memory"], argv[-2:])

    def test_a_recheck_that_gives_no_record_fails_with_the_process_tail(self):
        ran, runs = self.spy(components, "_run", completed(1, stderr="Traceback\nboom"))
        with runs:
            got = components._recheck(self.hub, "memory", dict(PROBE_ENV))
        self.assertEqual(record("memory", "FAIL", "the recheck did not run (exit 1)", "Traceback\nboom"), got)

    def test_a_recheck_that_cannot_start_fails_instead_of_raising(self):
        with patch.object(components, "_run", side_effect=subprocess.TimeoutExpired("python", 300)):
            got = components._recheck(self.hub, "memory", dict(PROBE_ENV))
        self.assertEqual(("FAIL", "the recheck did not run"), (got["status"], got["reason"]))

    def test_the_recheck_runs_the_real_check_in_a_real_fresh_process(self):
        # No .mcp.json: the fresh process must answer the registration fault, with the hub's own check.
        (self.hub / ".mcp.json").unlink()
        self.python.unlink()
        self.python.symlink_to(sys.executable)
        got = components._recheck(self.hub, "memory", os.environ.copy())
        self.assertEqual(("memory", "FAIL"), (got["name"], got["status"]))
        self.assertIn("is not registered for this seat", got["reason"])


class ScrubTests(unittest.TestCase):
    def test_a_token_shaped_string_never_reaches_a_record(self):
        secret = "sk-" + "a1b2c3d4e5" * 5
        got = components._record("memory", "FAIL", "the server refused",
                                 f"Authorization: Bearer {secret}\ntoken={secret}\n{secret}")
        self.assertEqual(record("memory", "FAIL", "the server refused",
                                "Authorization: Bearer [redacted]\ntoken [redacted]\n[redacted]"), got)

    def test_an_unknown_status_is_refused(self):
        with self.assertRaisesRegex(ValueError, r"\Aunknown component status: GREEN\Z"):
            components._record("memory", "GREEN", "")


if __name__ == "__main__":
    unittest.main()
