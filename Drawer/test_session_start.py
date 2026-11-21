"""Tests for Drawer/hooks/session-start.py and the Git freshness notice it carries."""
import contextlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
HOOK_DIR = ROOT / "Drawer" / "hooks"
sys.path.insert(0, str(HOOK_DIR))
sys.path.insert(0, str(ROOT / "Drawer"))
import git_freshness  # noqa: E402
import harness_config  # noqa: E402
import oscompat  # noqa: E402
from runtime import profile, seat  # noqa: E402

spec = importlib.util.spec_from_file_location("session_start", HOOK_DIR / "session-start.py")
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)
FRESHNESS_GUARD = git_freshness.active_env()


class StubPolicy:
    """git-branch-policy with a task_sweep that removes and holds nothing."""
    calls = []

    @classmethod
    def task_sweep(cls, repo, deadline=None):
        cls.calls.append((repo, deadline))
        return [], []


def run_hook(payload: dict, digest=(0, "OPEN TASKS (0):", ""), fresh=("GIT FRESHNESS CURRENT", True),
             steps="", setup=False) -> tuple:
    """Run main with a fake run and policy. fresh=None runs the real freshness; steps=None the real seat steps."""
    calls = []

    def fake_run(argv, timeout):
        calls.append(Path(argv[2]).name)
        return digest if argv[2].endswith("mem.py") else (0, "PRODUCT SWEEP: nothing to remove.", "")

    out = []
    fresh_patch = mock.patch.object(hook, "freshness", return_value=fresh) if fresh else contextlib.nullcontext()
    steps_patch = mock.patch.object(hook, "seat_steps", return_value=steps) if steps is not None \
        else contextlib.nullcontext()
    with mock.patch.object(hook, "run", side_effect=fake_run), fresh_patch, steps_patch, \
         mock.patch.object(hook, "policy", return_value=StubPolicy), \
         mock.patch.object(hook, "seed_scratchpads") as seed, \
         mock.patch.object(hook, "setup_needed", return_value=setup), \
         mock.patch.object(hook, "bind_hooks") as bind, \
         mock.patch("sys.stdin", mock.Mock(buffer=mock.Mock(read=lambda: json.dumps(payload).encode()))), \
         mock.patch("builtins.print", side_effect=out.append):
        hook.main()
    body = json.loads(out[0])["hookSpecificOutput"]
    assert body["hookEventName"] == "SessionStart"
    run_hook.bound = bind.called
    return body["additionalContext"], calls, seed.called


class SessionStartTests(unittest.TestCase):
    def test_startup_opens_with_the_digest_then_freshness_and_seeds(self):
        context, calls, seeded = run_hook({"source": "startup"})
        self.assertTrue(context.startswith("MEMORY DIGEST\nOPEN TASKS (0):"))
        self.assertIn("GIT FRESHNESS CURRENT", context)
        self.assertNotIn("SWEEP", context)
        self.assertTrue(seeded)
        self.assertIn("mem.py", calls)
        self.assertIn("product_worktree.py", calls)

    # Defect: a plain claude start in the hub ran without Code Mode and said nothing. Injection: drop the hint part.
    def test_a_plain_start_names_how_to_get_code_mode_and_a_launcher_start_does_not(self):
        with mock.patch.dict(os.environ, {hook.LAUNCHER_VAR: ""}):
            context, _, _ = run_hook({"source": "startup"})
            self.assertIn(hook.CODE_MODE_HINT, context)
            context, _, _ = run_hook({"source": "resume"})
            self.assertNotIn("CODE MODE:", context)
        with mock.patch.dict(os.environ, {hook.LAUNCHER_VAR: "1"}):
            context, _, _ = run_hook({"source": "startup"})
            self.assertNotIn("CODE MODE:", context)

    def test_startup_carries_the_rules_check_and_a_shadow_fails_it_loudly(self):
        # The run's HOME is a temp folder; a checkout under the real home sees its ~/.claude/CLAUDE.md as user scope.
        with mock.patch("rules_check.user_file", return_value=harness_config.real_home() / ".claude" / "CLAUDE.md"):
            context, _, _ = run_hook({"source": "startup"})
        self.assertIn("RULES CHECK: the root AGENTS.md line that starts 'Rules sentinel:'", context)
        self.assertNotIn("RULES SHADOWED", context)
        with mock.patch("rules_check.faults", return_value=["RULES SHADOWED: /x/CLAUDE.md turns AGENTS.md off"]):
            context, _, _ = run_hook({"source": "resume"})
        self.assertIn("send NEED rules fault with each line below.\nRULES SHADOWED: /x/CLAUDE.md", context)

    # Defect: a fresh clone with a system python3 booted with a MEMORY DIGEST (and made hub.db) and no
    # HUB SETUP REQUIRED. Injection: main() never asks setup_needed().
    def test_a_clone_with_no_prepared_environment_prints_setup_and_opens_no_memory(self):
        for source in ("startup", "resume", "compact"):
            with self.subTest(source=source):
                context, calls, seeded = run_hook({"source": source}, setup=True)
                self.assertEqual(hook.SETUP_NOTICE, context)
                self.assertTrue(context.startswith("HUB SETUP REQUIRED"))
                self.assertEqual(([], False, True), (calls, seeded, run_hook.bound))

    def test_setup_is_needed_without_a_venv_python_or_a_saved_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            hub = Path(tmp)
            with mock.patch.object(hook, "HUB_ROOT", hub), \
                 mock.patch.object(profile, "setup_required", return_value=False) as saved:
                self.assertTrue(hook.setup_needed())  # no .venv/bin/python
                oscompat.venv_python(hub).parent.mkdir(parents=True)
                oscompat.venv_python(hub).write_text("")
                self.assertFalse(hook.setup_needed())
                saved.return_value = True
                self.assertTrue(hook.setup_needed())  # no saved profile
                saved.assert_called_with(hub)

    def test_bind_hooks_sets_the_tracked_hooks_path_once_and_keeps_a_bound_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            hub = Path(tmp)
            subprocess.run(["git", "init", "-q", str(hub)], check=True, env=harness_config.git_env())

            def hooks_path():
                done = subprocess.run(["git", "-C", str(hub), "config", "--get", "core.hooksPath"],
                                      capture_output=True, text=True, env=harness_config.git_env())
                return done.stdout.strip()
            with mock.patch.object(hook, "HUB_ROOT", hub):
                hook.bind_hooks()
                self.assertEqual("Drawer/git-hooks", hooks_path())
                subprocess.run(["git", "-C", str(hub), "config", "core.hooksPath", "elsewhere"], check=True,
                               env=harness_config.git_env())
                hook.bind_hooks()
                self.assertEqual("elsewhere", hooks_path())

    def test_startup_runs_the_nightly_export_when_due_and_a_failure_never_blocks(self):
        context, calls, _ = run_hook({"source": "startup"})
        self.assertEqual(2, calls.count("mem.py"))  # the digest and nightly --if-stale
        argvs = []
        with mock.patch.object(hook, "run", side_effect=lambda argv, timeout: argvs.append((argv[3:], timeout)) or
                               (0, "", "")):
            self.assertEqual("", hook.nightly())
        self.assertEqual([(["nightly", "--if-stale"], hook.NIGHTLY_SECONDS)], argvs)
        with mock.patch.object(hook, "run", return_value=(1, "", "git: no identity\nfatal: no user")):
            self.assertEqual("NIGHTLY EXPORT FAILED: fatal: no user", hook.nightly())
        with mock.patch.object(hook, "run", side_effect=OSError("boom")):
            self.assertEqual("NIGHTLY EXPORT FAILED: boom", hook.nightly())
        with mock.patch.object(hook, "nightly", return_value="NIGHTLY EXPORT FAILED: x"):
            context, _, _ = run_hook({"source": "startup"})
        self.assertIn("NIGHTLY EXPORT FAILED: x", context)
        self.assertIn("GIT FRESHNESS CURRENT", context)

    def test_compaction_carries_only_the_digest(self):
        context, calls, seeded = run_hook({"source": "compact"})
        self.assertEqual("MEMORY DIGEST\nOPEN TASKS (0):", context)
        self.assertEqual(["mem.py"], calls)
        self.assertFalse(seeded)

    def test_a_failed_digest_names_its_fault_and_the_session_still_starts(self):
        context, _, _ = run_hook({"source": "resume"}, digest=(2, "", "mem: database is locked"))
        self.assertTrue(context.startswith("MEMORY DIGEST UNAVAILABLE: mem: database is locked"))
        self.assertIn("GIT FRESHNESS CURRENT", context)

    def test_a_long_digest_fits_the_budget_cutting_least_important_first_and_says_so(self):
        # Defect: the hook had no output budget; it once printed 4102 chars.
        rows = lambda n, word: "\n".join(f"- {i:08x} {word} " + "x" * 150 for i in range(n))
        long = (f"MEMORY (mem)\n\nOPEN TASKS (9):\n{rows(9, 'task')}\n\nTASTE:\n{rows(15, 'KEEP')}\n\n"
                f"NEWEST DECISIONS:\n{rows(10, 'decision')}\n\nROWS: design 1\nTOPICS: pivot 2")
        rules = hook.rules()
        context, _, _ = run_hook({"source": "startup"}, digest=(0, long, ""))
        printed = hook.render(context)
        self.assertLessEqual(len(printed), hook.OUTPUT_BUDGET)
        self.assertIn(rules, context)
        self.assertIn("GIT FRESHNESS CURRENT", context)
        self.assertNotIn("TOPICS: pivot", context)
        self.assertNotIn("decision", context)
        self.assertIn(f"- {0:08x} KEEP", context)
        self.assertIn(f"- {0:08x} task", context)  # OPEN TASKS keeps its top row while taste rows go
        self.assertIn(f"DIGEST CUT to fit {hook.OUTPUT_BUDGET} chars: TOPICS, ROWS, NEWEST DECISIONS rows 10, "
                      "NEWEST DECISIONS, OPEN TASKS rows 8, TASTE rows", context)
        self.assertTrue(context.startswith("MEMORY DIGEST\nMEMORY (mem)"))
        huge, _, _ = run_hook({"source": "startup"}, digest=(0, "MEMORY " + "y" * 9000, ""))
        self.assertLessEqual(len(hook.render(huge)), hook.OUTPUT_BUDGET)
        self.assertLessEqual(hook.OUTPUT_BUDGET, 1500)  # session-start output 1.5 KB or less
        self.assertIn("TRUNCATED digest", huge)
        self.assertIn(rules, huge)

    def test_scratchpads_seed_once_and_never_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            hub = Path(tmp)
            (hub / "Flight").mkdir()
            (hub / "Flight" / "SCRATCHPAD.md").write_text("mine")
            (hub / "UI").mkdir()
            with mock.patch.object(hook, "HUB_ROOT", hub):
                hook.seed_scratchpads()
            self.assertEqual("mine", (hub / "Flight" / "SCRATCHPAD.md").read_text())
            self.assertEqual(hook.SCRATCHPAD_SEED.format(name="Hub"), (hub / "SCRATCHPAD.md").read_text())
            self.assertFalse((hub / "UI" / "SCRATCHPAD.md").exists())

    # Defect: a fresh clone has no Flight folder, so the first Flight booted with STATE SECTION MISSING.
    def test_a_fresh_clone_gets_a_flight_pad_with_a_state_section_and_a_backlog_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            hub = Path(tmp)
            with mock.patch.object(hook, "HUB_ROOT", hub):
                hook.seed_scratchpads()
            pad = (hub / "Flight" / "SCRATCHPAD.md").read_text()
            self.assertIn("\n## State\nBACKLOG: none.\n", pad)


class SweepTests(unittest.TestCase):
    """The session-start sweep: the product task trees by subprocess, the hub task trees by task_sweep, one line."""

    def sweep(self, product=(0, "PRODUCT SWEEP: nothing to remove.", ""), removed=(), held=(), fault=None):
        runs, policy = [], mock.Mock()
        policy.task_sweep.side_effect = fault or (lambda repo, deadline=None: (list(removed), list(held)))

        def fake_run(argv, timeout):
            runs.append((Path(argv[2]).name, argv[3:], timeout))
            return product
        with mock.patch.object(hook, "run", side_effect=fake_run), \
             mock.patch.object(hook, "policy", return_value=policy):
            started = time.monotonic()
            line = hook.sweep()
        return line, runs, policy, (started, time.monotonic())

    def test_nothing_removed_gives_no_line(self):
        line, runs, policy, (started, ended) = self.sweep()
        self.assertEqual("", line)
        self.assertEqual([("product_worktree.py", ["sweep"], hook.SWEEP_SECONDS)], runs)
        deadline = policy.task_sweep.call_args.kwargs["deadline"]
        self.assertLessEqual(started + hook.SWEEP_SECONDS, deadline)
        self.assertLessEqual(deadline, ended + hook.SWEEP_SECONDS)

    def test_removals_and_holds_join_into_one_line(self):
        line, _, _, _ = self.sweep(product=(0, "PRODUCT SWEEP: product worktree /g_wt/aaaaaaaa removed.", ""),
                                   removed=["task/1a2b3c4d-gate"],
                                   held=[{"item": "task/aaaa1111-x", "reason": "task doing"}])
        self.assertEqual("SWEEP: product worktree /g_wt/aaaaaaaa removed; hub task tree task/1a2b3c4d-gate removed; "
                         "hub task/aaaa1111-x kept: task doing.", line)

    def test_a_fault_is_one_failed_line(self):
        line, _, _, _ = self.sweep(fault=RuntimeError("mem down\nmore"))
        self.assertEqual("SWEEP FAILED: mem down", line)
        line, _, policy, _ = self.sweep(product=(1, "", "Traceback\nKeyError: product"))
        self.assertEqual("SWEEP FAILED: product sweep exit 1: KeyError: product", line)
        policy.task_sweep.assert_not_called()


class GitFreshnessNoticeTests(unittest.TestCase):
    def setUp(self):
        guard = mock.patch.dict(os.environ, {}, clear=False)
        guard.start()
        self.addCleanup(guard.stop)
        os.environ.pop(FRESHNESS_GUARD, None)

    def test_policy_message_is_returned_and_guard_is_restored(self):
        class Policy:
            @staticmethod
            def freshness(_root, source):
                return {"message": f"GIT FRESHNESS CURRENT — {source}"}

        with mock.patch.object(git_freshness, "_load_policy", return_value=Policy):
            self.assertEqual("GIT FRESHNESS CURRENT — startup", git_freshness.notice(ROOT, "startup"))
        self.assertIsNone(os.environ.get(FRESHNESS_GUARD))

    def test_recursive_or_failed_policy_is_blocking(self):
        loaded = []
        with mock.patch.dict(os.environ, {FRESHNESS_GUARD: "1"}, clear=False), \
             mock.patch.object(git_freshness, "_load_policy", side_effect=lambda *a: loaded.append(a)):
            recursive = git_freshness.notice(ROOT, "startup")
        self.assertEqual([], loaded)
        self.assertEqual("GIT FRESHNESS BLOCKED — recursive session-start freshness invocation was refused. "
                         "Do not claim this checkout is current.", recursive)
        with mock.patch.object(git_freshness, "_load_policy", side_effect=RuntimeError("broken")):
            self.assertIn("broken", git_freshness.notice(ROOT, "startup"))


class Policy:
    """A Git branch policy whose freshness answers one state."""
    state = "rebased"

    @classmethod
    def freshness(cls, _root, source):
        return {"state": cls.state, "message": f"GIT FRESHNESS {cls.state.upper()} — {source}"}


def sync_run(code=0, out="in sync: .mcp.json", calls=None):
    def fake(argv, **_keywords):
        if calls is not None:
            calls.append(argv)
        return subprocess.CompletedProcess(argv, code, out, "")
    return fake


class SettingsRenderTests(unittest.TestCase):
    """A pulled manifest change reaches the checkout: an accepted refresh renders .mcp.json."""

    def setUp(self):
        guard = mock.patch.dict(os.environ, {}, clear=False)
        guard.start()
        self.addCleanup(guard.stop)
        os.environ.pop(FRESHNESS_GUARD, None)
        self.addCleanup(setattr, Policy, "state", "rebased")

    def test_rebased_and_current_freshness_call_settings_sync_mcp_never_apply(self):
        for state in ("rebased", "current"):
            with self.subTest(state=state):
                Policy.state, calls = state, []
                with mock.patch.object(git_freshness, "_load_policy", return_value=Policy), \
                     mock.patch.object(git_freshness.subprocess, "run", side_effect=sync_run(calls=calls)):
                    text, fresh = git_freshness.check(ROOT, "startup")
                self.assertEqual((f"GIT FRESHNESS {state.upper()} — startup", True), (text, fresh))
                self.assertEqual(1, len(calls))
                self.assertEqual(["-B", str(ROOT / "Drawer" / "settings-sync.py"), "--mcp"], calls[0][1:])
                self.assertNotIn("--apply", calls[0])

    def test_a_blocked_checkout_renders_nothing(self):
        Policy.state, calls = "blocked", []
        with mock.patch.object(git_freshness, "_load_policy", return_value=Policy), \
             mock.patch.object(git_freshness.subprocess, "run", side_effect=sync_run(calls=calls)):
            self.assertEqual(("GIT FRESHNESS BLOCKED — startup", False), git_freshness.check(ROOT, "startup"))
        self.assertEqual([], calls)

    def test_a_failed_render_adds_one_line_and_the_digest_stays(self):
        with mock.patch.object(git_freshness, "_load_policy", return_value=Policy), \
             mock.patch.object(git_freshness.subprocess, "run",
                               side_effect=sync_run(2, "ERROR: manifest is not JSON")):
            context, _, _ = run_hook({"source": "startup"}, fresh=None)
        self.assertTrue(context.startswith("MEMORY DIGEST\nOPEN TASKS (0):"))
        self.assertIn("GIT FRESHNESS REBASED — startup\nSETTINGS SYNC FAILED — exit 2: ERROR: manifest is not "
                      "JSON; this checkout's .mcp.json may be stale.", context)
        self.assertNotIn("GIT FRESHNESS BLOCKED", context)

    def test_a_render_past_its_time_is_one_line_too(self):
        def slow(argv, **keywords):
            raise subprocess.TimeoutExpired(argv, keywords["timeout"])
        with mock.patch.object(git_freshness.subprocess, "run", side_effect=slow):
            self.assertEqual("SETTINGS SYNC FAILED — no answer in 45 s; this checkout's .mcp.json may be stale. "
                             "Run <python> Drawer/settings-sync.py --mcp.", git_freshness.render_mcp(ROOT))


class FakeChild:
    def __init__(self, code):
        self.code = code

    def wait(self, timeout=None):
        return self.code


class SeatStepsHookTests(unittest.TestCase):
    """A moved uv.lock reaches the checkout: session-start runs seat configure."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="session start seat ")
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()
        self.hub = self.home / "hub"
        self.hub.mkdir()
        (self.hub / "uv.lock").write_bytes(b"version = 1\n")
        for patcher in (mock.patch.object(Path, "home", return_value=self.home),
                        mock.patch.object(hook, "HUB_ROOT", self.hub)):
            patcher.start()
            self.addCleanup(patcher.stop)
        profile.save_profile(profile.default_profile_path(self.hub),
                             dict(schema_version=1, hub_root=str(self.hub), member_name="m", agent="claude",
                                  box_name="b"))
        self.spawned, real = [], subprocess.Popen

        def fake_popen(argv, **keywords):  # rules_check runs claude --version through the same Popen
            if not any(str(a).endswith("seat.py") for a in argv):
                return real(argv, **keywords)
            self.spawned.append(argv)
            return FakeChild(0)
        popen = mock.patch.object(seat.subprocess, "Popen", side_effect=fake_popen)
        popen.start()
        self.addCleanup(popen.stop)

    def test_a_moved_uv_lock_runs_configure_once_and_prints_seat_steps(self):
        context, _, _ = run_hook({"source": "startup"}, steps=None)
        self.assertIn("SEAT STEPS — configure (hub-uv-lock) ran ok.", context)
        self.assertEqual(1, len(self.spawned))
        self.assertEqual("configure", self.spawned[0][-1])
        # The real configure stamps; once stamped, a boot runs nothing.
        seat._pins_path(self.hub).write_text(json.dumps(seat.pins(self.hub)))
        context, _, _ = run_hook({"source": "startup"}, steps=None)
        self.assertNotIn("SEAT STEPS", context)
        self.assertEqual(1, len(self.spawned))

    def test_a_task_worktree_skips_the_seat_steps(self):
        (self.hub / ".git").write_text("gitdir: /elsewhere/.git/worktrees/hub\n")
        context, _, _ = run_hook({"source": "startup"}, steps=None)
        self.assertNotIn("SEAT STEPS", context)
        self.assertEqual([], self.spawned)

    def test_a_blocked_checkout_runs_no_seat_step(self):
        context, _, _ = run_hook({"source": "startup"}, fresh=("GIT FRESHNESS BLOCKED — x", False), steps=None)
        self.assertNotIn("SEAT STEPS", context)
        self.assertEqual([], self.spawned)

    def test_compaction_runs_no_seat_step(self):
        context, _, _ = run_hook({"source": "compact"}, steps=None)
        self.assertEqual([], self.spawned)


class BudgetTests(unittest.TestCase):
    """The worst case of every bounded part fits the manifest's session-start timeout."""

    def test_the_240_s_budget_holds(self):
        manifest = json.loads((ROOT / "Drawer" / "settings-manifest.json").read_text(encoding="utf-8"))
        entry = next(e for e in manifest["hooks"]["SessionStart"] if "session-start.py" in e["command"])
        self.assertEqual(240, entry["timeout"])
        policy = git_freshness._load_policy(ROOT)
        fresh = (policy.FRESHNESS_LOCK_SECONDS + policy.FETCH_TIMEOUT_SECONDS + policy.REBASE_TIMEOUT_SECONDS
                 + policy.RECOVERY_TIMEOUT_SECONDS + policy.INTEGRATION_CHECK_TIMEOUT_SECONDS)
        beside = max(hook.SWEEP_SECONDS, hook.DIGEST_SECONDS, hook.RULES_SECONDS, hook.NIGHTLY_SECONDS,
                     seat.BOOT_BUDGET_SECONDS)
        chain = fresh + git_freshness.SETTINGS_SECONDS + beside
        self.assertLessEqual(chain + 30, entry["timeout"])  # 30 s for Python start and untimed git calls

    def test_the_digest_and_the_sweep_run_beside_the_chain(self):
        def slow_run(argv, timeout):
            time.sleep(0.4)
            return (0, "OPEN TASKS (0):", "") if argv[2].endswith("mem.py") else (0, "PRODUCT SWEEP: nothing to remove.", "")

        def slow_steps():
            time.sleep(0.4)
            return "SEAT STEPS — configure (hub-uv-lock) ran ok."
        out = []
        started = time.monotonic()
        with mock.patch.object(hook, "run", side_effect=slow_run), \
             mock.patch.object(hook, "policy", return_value=StubPolicy), \
             mock.patch.object(hook, "freshness", return_value=("GIT FRESHNESS CURRENT", True)), \
             mock.patch.object(hook, "seat_steps", side_effect=slow_steps), \
             mock.patch.object(hook, "seed_scratchpads"), \
             mock.patch.object(hook, "setup_needed", return_value=False), \
             mock.patch("sys.stdin", mock.Mock(buffer=mock.Mock(read=lambda: b'{"source": "startup"}'))), \
             mock.patch("builtins.print", side_effect=out.append):
            hook.main()
        self.assertLess(time.monotonic() - started, 0.7)  # one serial pair makes 0.8 s, all serial 1.2 s
        context = json.loads(out[0])["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(context.startswith("MEMORY DIGEST\nOPEN TASKS (0):"))
        self.assertIn("SEAT STEPS — configure (hub-uv-lock) ran ok.", context)


if __name__ == "__main__":
    unittest.main()
