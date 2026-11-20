"""Tests for Drawer/smoke.py, the hub's wrapper around the hub tests and the product step, [product] gate_command.

Run: .venv/bin/python -B -m unittest Drawer/test_smoke.py. Hermetic: the config and the product step run are
patched; nothing builds. Expected exit codes come from the module docstring: 0 green or skipped, 1 red or
broken, 2 config incomplete, the [smoke] blocked_exit from the product step passes through.
"""
import io
import os
import shutil
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import smoke  # noqa: E402

REAL_RUN = smoke.subprocess.run
REAL_HUB_TESTS = smoke.hub_tests
REAL_SCANNER_MISSING = smoke.scanner_missing
REAL_TRASH = smoke.run_tmp.trash


class SmokeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # A decoy checkout above the nearest one: the product step must run in the nearest.
        (self.tmp / ".git").mkdir()
        self.root = self.tmp / "product"
        self.project = self.root / "app"
        self.project.mkdir(parents=True)
        (self.root / ".git").write_text("gitdir: elsewhere\n")  # a worktree's .git is a file
        self.cfg = {"project": {"env_prefix": "SMOKETEST", "state_root": str(self.tmp / "state")},
                    "product": {"path": str(self.project), "gate_command": "tools/gate.sh --all"},
                    "smoke": {"blocked_exit": 75}}
        self.calls = []
        self.result = 0
        self.hub_reds = []
        self.hub_runs = 0
        self.trashed = []  # the run folders the run sent to the trash: a fixture bin, never the real trash
        for target in (patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True),
                       patch.object(smoke.harness_config, "load", lambda root=None: self.cfg),
                       patch.object(smoke.subprocess, "run", self.fake_run),
                       patch.object(smoke, "hub_tests", self.fake_hub_tests),
                       patch.object(smoke, "scanner_missing", lambda files, root=None: ""),
                       patch.object(smoke.run_tmp, "trash", self.fake_trash),
                       patch.object(smoke.run_tmp, "TEMP_ROOT", self.tmp / "temp")):
            target.start()
            self.addCleanup(target.stop)

    def fake_trash(self, path):
        self.trashed.append(Path(path))
        shutil.rmtree(path)

    def fake_run(self, argv, cwd=None, check=None, env=None):
        self.calls.append({"argv": argv, "cwd": Path(cwd)})
        self.run_env = env
        if isinstance(self.result, BaseException):
            raise self.result
        return SimpleNamespace(returncode=self.result)

    def fake_hub_tests(self, workers=None, files=None, env=None, stats=None):
        if stats is not None:
            stats.update(getattr(self, "hub_stats", {}))
        self.hub_runs += 1
        self.hub_env = env
        self.hub_workers = workers
        self.hub_files = files
        return self.hub_reds

    def run_main(self, argv=()):
        self.out = io.StringIO()
        with redirect_stdout(self.out):
            return smoke.main(list(argv))

    def test_gate_exit_maps_to_smoke_exit(self):
        for gate, expected in ((0, 0), (75, 75), (1, 1), (3, 1), (OSError("no exec"), 1)):
            with self.subTest(gate=gate):
                self.result = gate
                self.assertEqual(expected, self.run_main())

    def test_the_smoke_writes_no_ledger_row(self):
        # The smoke row's one reader (the trial log) is gone, so the smoke writes no ledger row.
        self.result = 1
        self.assertEqual(1, self.run_main())
        self.assertFalse((self.tmp / "state" / "ledger.jsonl").exists())
        self.assertFalse(hasattr(smoke, "note_" + "smoke"))  # built so a grep for the dead name stays empty

    def test_incomplete_config_returns_2_and_runs_nothing(self):
        for name, breaks in (
                ("project folder missing", lambda: self.cfg["product"].update(path=str(self.tmp / "gone"))),
                ("gate_command does not parse", lambda: self.cfg["product"].update(gate_command="tools/gate.sh 'open")),):
            with self.subTest(name), patch.dict(self.cfg["product"]), patch.dict(self.cfg["smoke"]):
                self.calls, self.hub_runs = [], 0
                breaks()
                try:
                    code = self.run_main()
                except Exception as exc:  # a crash is exit 1, never the config exit
                    code = exc
                self.assertEqual(2, code)
                self.assertEqual(([], 0), (self.calls, self.hub_runs))

    def test_no_gate_command_or_no_project_runs_the_hub_tests_and_skips_the_product_step_with_a_line(self):
        for name, breaks, line in (
                ("gate_command empty", lambda: self.cfg["product"].update(gate_command=" "),
                 "smoke: SKIP product step: [product] gate_command is empty; only the hub tests ran"),
                ("project_path unset", lambda: self.cfg["product"].update(path=""),
                 "smoke: SKIP product step: [product] path is unset; only the hub tests ran")):
            with self.subTest(name), patch.dict(self.cfg["product"]), patch.dict(self.cfg["smoke"]):
                self.calls, self.hub_runs, self.result = [], 0, 1  # a product run would be red
                breaks()
                self.assertEqual(0, self.run_main())
                self.assertEqual(([], 1), (self.calls, self.hub_runs))
                self.assertEqual(line, self.out.getvalue().splitlines()[-1])
        self.hub_reds = ["smoke: HUB RED Drawer/test_x.py: exit 1 (FAILED (failures=1))"]
        self.cfg["product"]["gate_command"] = ""
        self.assertEqual(1, self.run_main())

    def test_runs_the_gate_command_in_the_nearest_checkout_with_python_as_the_hub_interpreter(self):
        self.cfg["product"]["gate_command"] = "{python} tools/gate.py 'two words'"
        self.assertEqual(0, self.run_main())
        self.assertEqual([{"argv": [sys.executable, "tools/gate.py", "two words"], "cwd": self.root}], self.calls)

    def test_a_project_with_no_checkout_above_it_runs_the_product_step_in_the_project(self):
        bare = Path(tempfile.mkdtemp()) / "app"
        bare.mkdir()
        self.addCleanup(shutil.rmtree, bare.parent, True)
        self.cfg["product"]["path"] = str(bare)
        self.assertEqual(0, self.run_main())
        self.assertEqual([{"argv": ["tools/gate.sh", "--all"], "cwd": bare}], self.calls)

    def test_the_session_product_worktree_outranks_project_path(self):
        worktree = self.tmp / "product_wt" / "abcd1234"
        (worktree / "app").mkdir(parents=True)
        (worktree / ".git").write_text("gitdir: elsewhere\n")
        with patch.dict(os.environ, {"SMOKETEST_PRODUCT_PATH": str(worktree / "app")}):
            self.assertEqual(0, self.run_main())
        self.assertEqual([worktree], [call["cwd"] for call in self.calls])

    def test_a_red_hub_test_file_fails_the_smoke_and_starts_no_gate(self):
        self.hub_reds = ["smoke: HUB RED Drawer/test_x.py: exit 1 (FAILED (failures=1))"]
        self.assertEqual(1, self.run_main())
        self.assertEqual([], self.calls)
        self.assertIn("smoke: HUB RED Drawer/test_x.py", self.out.getvalue())
        self.assertIn("1 hub test file(s) red; the product step did not start", self.out.getvalue())

    # Defect: a green smoke printed no counts, so a seat could not tell 2 tests from 2000, or skips from runs.
    def test_a_green_and_a_red_run_print_one_summary_line(self):
        self.hub_stats = {"files": 12, "ran": 345, "skipped": 2}
        self.assertEqual(0, self.run_main())
        self.assertIn("smoke: 12 files, 345 tests run, 2 skipped, 0 red\n", self.out.getvalue())
        self.hub_reds = ["smoke: HUB RED Drawer/test_x.py: exit 1 (x)"]
        self.assertEqual(1, self.run_main())
        self.assertIn("smoke: 12 files, 345 tests run, 2 skipped, 1 red\n", self.out.getvalue())

    def test_hub_tests_count_the_tests_run_and_skipped_from_each_unittest_summary(self):
        hub = self.tmp / "hub"
        body = ("import unittest\nclass T(unittest.TestCase):\n    def test_a(self): pass\n"
                "    def test_b(self): pass\n    @unittest.skip('why')\n    def test_c(self): pass\n")
        red = "import unittest\nclass T(unittest.TestCase):\n    def test_a(self): self.fail('red')\n"
        for rel, text in (("Drawer/test_counted.py", body), ("Drawer/test_red.py", red)):
            (hub / rel).parent.mkdir(parents=True, exist_ok=True)
            (hub / rel).write_text(text)
        stats = {}
        with patch.object(smoke.subprocess, "run", REAL_RUN):
            reds = REAL_HUB_TESTS(hub, stats=stats)
        self.assertEqual(1, len(reds))
        self.assertEqual({"files": 2, "ran": 4, "skipped": 1}, stats)

    def test_a_windows_gate_command_with_no_git_sh_is_a_config_error_and_runs_nothing(self):
        self.cfg["product"]["gate_command"] = "sh tools/gate.sh ."
        with patch.object(smoke.harness_config.oscompat, "WINDOWS", True), \
             patch.object(smoke.harness_config.oscompat, "git_shell", side_effect=OSError("no Git for Windows sh.exe")):
            self.assertEqual(2, self.run_main())
        self.assertIn("cannot start: no Git for Windows sh.exe", self.out.getvalue())
        self.assertEqual(([], 0), (self.calls, self.hub_runs))

    def test_a_windows_gate_command_runs_through_git_for_windows_sh(self):
        self.cfg["product"]["gate_command"] = "sh tools/gate.sh ."
        with patch.object(smoke.harness_config.oscompat, "WINDOWS", True), \
             patch.object(smoke.harness_config.oscompat, "git_shell", return_value="C:/Git/usr/bin/sh.exe"):
            self.assertEqual(0, self.run_main())
        self.assertEqual(["C:/Git/usr/bin/sh.exe", "tools/gate.sh", "."], self.calls[0]["argv"])

    # Defect: with no gitleaks the secret-scan tests skipped, and the smoke stayed green.
    def test_a_missing_gitleaks_is_red_when_the_secret_scan_tests_would_run(self):
        hub = self.tmp / "hub"
        (hub / "Drawer").mkdir(parents=True)
        (hub / smoke.SCAN_TEST).write_text("")
        with patch.object(smoke.shutil, "which", return_value=None):
            for files, red in ((None, True), ([smoke.SCAN_TEST], True), (["Drawer/test_other.py"], False)):
                with self.subTest(files=files):
                    line = REAL_SCANNER_MISSING(files, hub)
                    self.assertEqual(red, bool(line), line)
            self.assertIn("brew install gitleaks", REAL_SCANNER_MISSING(None, hub))
        with patch.object(smoke.shutil, "which", return_value="/bin/gitleaks"):
            self.assertEqual("", REAL_SCANNER_MISSING(None, hub))

    def test_a_missing_gitleaks_fails_the_run_before_any_test_starts(self):
        with patch.object(smoke, "scanner_missing", REAL_SCANNER_MISSING), \
             patch.object(smoke.shutil, "which", return_value=None):
            self.assertEqual(1, self.run_main())
        self.assertEqual(0, self.hub_runs)
        self.assertIn("gitleaks is not installed", self.out.getvalue())

    def test_hub_tests_names_each_red_file_in_drawer_and_its_subfolders_and_no_green_one(self):
        hub = self.tmp / "hub"
        green = "import unittest\nclass T(unittest.TestCase):\n    def test_a(self): pass\n"
        red = "import unittest\nclass T(unittest.TestCase):\n    def test_a(self): self.fail('red')\n"
        broken = "import no_such_module_anywhere\n"
        for rel, body in (("Drawer/test_green.py", green), ("Drawer/test_red.py", red),
                          ("Drawer/runtime/test_deep_red.py", red), ("Drawer/runtime/test_broken.py", broken),
                          ("Drawer/runtime/test_deep_green.py", green), ("Drawer/not_a_test.py", red),
                          ("Other/test_outside_red.py", red)):
            (hub / rel).parent.mkdir(parents=True, exist_ok=True)
            (hub / rel).write_text(body)
        with patch.object(smoke.subprocess, "run", REAL_RUN):
            reds = REAL_HUB_TESTS(hub)
        self.assertEqual(["Drawer/runtime/test_broken.py",
                          "Drawer/runtime/test_deep_red.py", "Drawer/test_red.py"],
                         [line.split()[3].rstrip(":") for line in reds])
        self.assertTrue(all(line.startswith("smoke: HUB RED ") and "exit 1" in line for line in reds), reds)


    def test_hub_tests_run_each_file_with_no_local_toml(self):
        # A local toml that moves the roots (a second hub on the box) must not reach a hub test: the tests
        # isolate by HOME, and an absolute root in harness.local.toml would send their writes to live state.
        hub = self.tmp / "hub"
        (hub / "Drawer").mkdir(parents=True)
        (hub / "Drawer/test_env.py").write_text(
            "import os, unittest\nclass T(unittest.TestCase):\n"
            "    def test_a(self): self.assertEqual('1', os.environ.get('HARNESS_NO_LOCAL_TOML'))\n")
        with patch.object(smoke.subprocess, "run", REAL_RUN):
            self.assertEqual([], REAL_HUB_TESTS(hub))
            self.assertEqual([], REAL_HUB_TESTS(hub, env={"PATH": "/usr/bin"}))

    def test_hub_tests_run_each_file_with_a_temp_home(self):
        # A ~-rooted state_root or mem db under the real HOME is the live one: each test file gets a temp HOME.
        hub = self.tmp / "hub"
        (hub / "Drawer").mkdir(parents=True)
        real = str(smoke.harness_config.real_home())
        (hub / "Drawer/test_env.py").write_text(
            "import os, unittest\nclass T(unittest.TestCase):\n"
            f"    def test_a(self): self.assertNotEqual({real!r}, os.path.expanduser('~'))\n"
            f"    def test_b(self): self.assertEqual({real!r}, os.environ.get('HARNESS_TEST_REAL_HOME'))\n")
        with patch.object(smoke.subprocess, "run", REAL_RUN):
            self.assertEqual([], REAL_HUB_TESTS(hub))
            self.assertEqual([], REAL_HUB_TESTS(hub, env={"PATH": "/usr/bin", "HOME": real}))

    def test_hub_tests_never_run_more_than_max_parallel_files_at_once(self):
        hub = self.tmp / "hub"
        (hub / "Drawer").mkdir(parents=True)
        for n in range(9):
            (hub / f"Drawer/test_{n}.py").write_text("")
        lock, state = threading.Lock(), {"live": 0, "peak": 0, "runs": 0}

        def slow_run(argv, **kwargs):
            with lock:
                state["live"] += 1
                state["runs"] += 1
                state["peak"] = max(state["peak"], state["live"])
            threading.Event().wait(0.05)
            with lock:
                state["live"] -= 1
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        for cap in (1, 3):
            with self.subTest(cap=cap), patch.object(smoke.subprocess, "run", slow_run):
                state.update(peak=0, runs=0)
                self.assertEqual([], REAL_HUB_TESTS(hub, workers=cap))
                self.assertEqual((cap, 9), (state["peak"], state["runs"]))

    def test_main_passes_max_parallel_and_the_default_is_a_quarter_of_the_cores(self):
        self.cfg["smoke"]["max_parallel"] = 3
        self.run_main()
        self.assertEqual(3, self.hub_workers)
        for value in (0, None, -2, True, "4"):
            with self.subTest(value=value), patch.object(smoke.os, "cpu_count", lambda: 24):
                self.cfg["smoke"]["max_parallel"] = value
                self.assertEqual(6, smoke.max_parallel(self.cfg))
        with patch.object(smoke.os, "cpu_count", lambda: 2):
            self.assertEqual(1, smoke.max_parallel({}))



class StarterCardTests(unittest.TestCase):
    """The starter card, the smoke and the card-run brief agree on how a hub test is written and tagged."""
    HUB = Path(__file__).resolve().parents[1]

    # Defect: the card asked for a pytest test, the land smoke runs unittest, so a pytest-style test went red
    # with "NO TESTS RAN".
    def test_the_starter_task_asks_for_a_unittest_test_the_smoke_runs(self):
        import json
        rows = [json.loads(line) for line in (self.HUB / "Drawer/mem/seed/starter.jsonl").read_text().splitlines()]
        task = next(row for row in rows if row["kind"] == "task")["body"]
        self.assertNotIn("pytest", task)
        self.assertIn("python -B -m unittest Drawer/test_hub_hello.py", task)
        self.assertIn('"-m", "unittest", rel', (self.HUB / "Drawer/smoke.py").read_text())

    # Defect: the brief said "commit message", CITE reads only the subject, and landed work faulted in a loop.
    def test_the_card_run_brief_puts_the_task_tag_in_the_commit_subject(self):
        brief = (self.HUB / ".claude/workflows/card-run.js").read_text()
        self.assertIn("in each commit SUBJECT line", brief)
        self.assertNotIn("in each commit message", brief)


class ImpactTests(unittest.TestCase):
    """smoke.py --impact runs only the hub test files a diff can reach; a core file or an unmapped
    change runs them all."""

    def setUp(self):
        self.hub = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.hub, True)
        for rel, body in (
                ("Drawer/widget.py", "X = 1\n"),
                ("Drawer/core_user.py", "import os, widget as w\n"),  # a module that imports it
                ("Drawer/test_core_user.py", "pass\n"),
                ("Drawer/test_widget.py", "import unittest\n"),  # named for the module
                ("Drawer/test_uses.py", "import os\nfrom widget import X\n"),  # imports it
                ("Drawer/test_runs.py", "SCRIPT = 'Drawer/hub-gear.py'\n"),  # runs a script by its file name
                ("Drawer/hub-gear.py", "pass\n"),
                ("Drawer/test_hub_gear.py", "pass\n"),
                ("Drawer/test_borrow.py", "import test_widget as tw\n"),  # borrows a test file's helpers
                ("Drawer/test_other.py", "import big_widget\nNAME = 'my-widget.py'\n"),  # near names only
                ("Drawer/runtime/test_seat.py", "from runtime import seat\n"),
                ("Drawer/runtime/seat.py", "pass\n"),
                ("Drawer/lonely.py", "pass\n"),
                ("Drawer/tools/make_widget.py", "pass\n"),
                ("Drawer/tools/test_widget_check.py", "TOOL = 'make_widget.py'\n")):  # names a tool, no same name
            (self.hub / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.hub / rel).write_text(body)

    def pick(self, *changed, full_on=()):
        return smoke.impact(self.hub, list(changed), list(full_on))

    def test_a_one_module_diff_runs_only_its_mapped_tests(self):
        # test_core_user tests a module that imports widget; test_borrow borrows test_widget's fixture.
        self.assertEqual(["Drawer/test_borrow.py", "Drawer/test_core_user.py", "Drawer/test_uses.py",
                          "Drawer/test_widget.py"], self.pick("Drawer/widget.py")[0])
        self.assertEqual(["Drawer/test_hub_gear.py", "Drawer/test_runs.py"], self.pick("Drawer/hub-gear.py")[0])
        self.assertEqual(["Drawer/runtime/test_seat.py"], self.pick("Drawer/runtime/seat.py")[0])

    def test_a_test_that_runs_a_script_by_path_is_reached_through_the_module_the_script_imports(self):
        (self.hub / "Drawer/hub-gear.py").write_text("import widget\n")  # Drawer/test_runs.py runs it by path
        self.assertIn("Drawer/test_runs.py", self.pick("Drawer/widget.py")[0])
        (self.hub / "Drawer/core_user.py").write_text('"""Runs Drawer/lonely.py."""\nimport widget\n')
        self.assertIsNone(self.pick("Drawer/lonely.py")[0])  # a module's docstring links the changed file only

    def test_a_tool_change_runs_the_test_that_names_it(self):
        self.assertEqual((["Drawer/tools/test_widget_check.py"], "1 changed file(s)"),
                         self.pick("Drawer/tools/make_widget.py"))

    def test_always_run_files_join_each_pick(self):
        self.assertEqual(["Drawer/test_hub_gear.py", "Drawer/test_other.py", "Drawer/test_runs.py"],
                         smoke.impact(self.hub, ["Drawer/hub-gear.py"], [], ["Drawer/test_other.py", "Drawer/gone.py"])[0])

    def test_a_changed_test_file_runs_itself_and_each_test_file_that_imports_it(self):
        self.assertEqual(["Drawer/test_borrow.py", "Drawer/test_widget.py"], self.pick("Drawer/test_widget.py")[0])
        self.assertEqual(["Drawer/test_other.py"], self.pick("Drawer/test_other.py")[0])

    def test_a_core_file_diff_runs_the_full_set(self):
        for core in ("harness.toml", "Drawer/runlib.py", "Drawer/gatelib.py", "Drawer/harness_config.py",
                     "Drawer/smoke.py"):
            with self.subTest(core=core):
                files, why = self.pick("Drawer/widget.py", core)
                self.assertIsNone(files)
                self.assertIn(core, why)
        files, why = self.pick("Drawer/git-hooks/pre-push", full_on=["Drawer/git-hooks/*"])
        self.assertIsNone(files)
        self.assertIn("Drawer/git-hooks/pre-push", why)

    def test_an_unmapped_change_falls_back_to_the_full_set(self):
        for changed in (["Drawer/lonely.py"], ["Drawer/widget.py", "README.md"], []):
            with self.subTest(changed=changed):
                files, why = self.pick(*changed)
                self.assertIsNone(files)
                self.assertIn("unmapped" if changed else "no changed file", why)

    def test_each_import_form_reaches_the_module(self):
        for body in ("from runtime.seat import X\n", "import runtime.seat as s\n", "import os, runtime.seat\n",
                     "from runtime import (\n    children,\n    seat,\n)\n",
                     "mod = importlib.import_module('runtime.seat')\n", "    from runtime import seat  # late\n"):
            with self.subTest(body=body):
                (self.hub / "Drawer/test_uses.py").write_text(body)
                self.assertIn("Drawer/test_uses.py", self.pick("Drawer/runtime/seat.py")[0])
        (self.hub / "Drawer/test_uses.py").write_text("seats = 1\nimport reseat\n")
        self.assertNotIn("Drawer/test_uses.py", self.pick("Drawer/runtime/seat.py")[0])
        (self.hub / "Drawer/test_uses.py").write_text("with patch('runtime.seat.check'):\n    pass\n")
        self.assertIn("Drawer/test_uses.py", self.pick("Drawer/runtime/seat.py")[0])
        (self.hub / "Drawer/test_uses.py").write_text("seats = 1\n")
        self.assertNotIn("Drawer/test_uses.py", self.pick("Drawer/runtime/seat.py")[0])

    def test_a_deleted_test_file_runs_only_its_importers_and_none_left_runs_the_full_set(self):
        (self.hub / "Drawer/test_widget.py").unlink()
        self.assertEqual((["Drawer/test_borrow.py"], "1 changed file(s)"), self.pick("Drawer/test_widget.py"))
        (self.hub / "Drawer/test_other.py").unlink()
        self.assertEqual((None, "no test file reached"), self.pick("Drawer/test_other.py"))

    def test_changed_files_reads_the_diff_against_the_base_with_both_names_of_a_rename(self):
        git = lambda *a: REAL_RUN(["git", *a], cwd=self.hub, check=True, capture_output=True)
        git("init", "-q", "-b", "main")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "base")
        git("add", "-A")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "all")
        git("branch", "base")
        git("mv", "Drawer/lonely.py", "Drawer/alone.py")
        (self.hub / "Drawer/widget.py").write_text("X = 2\n")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-am", "task")
        self.assertEqual(["Drawer/alone.py", "Drawer/lonely.py", "Drawer/widget.py"],
                         smoke.changed_files(self.hub, "base"))
        self.assertIsNone(smoke.changed_files(self.hub, "no-such-ref"))


class ImpactMainTests(unittest.TestCase):
    """main() with --impact: the picked files go to hub_tests; a full fallback or no flag passes files=None."""
    # Borrowed by name, never by subclassing: a subclass would run each SmokeTests test twice.
    setUp_smoke = SmokeTests.setUp
    fake_run, fake_hub_tests = SmokeTests.fake_run, SmokeTests.fake_hub_tests
    fake_trash, run_main = SmokeTests.fake_trash, SmokeTests.run_main

    def setUp(self):
        self.setUp_smoke()
        self.cfg["product"]["gate_command"] = ""
        self.cfg["smoke"]["full_on"] = ["Drawer/git-hooks/*"]
        self.diff = ["Drawer/smoke.py"]
        for target in (patch.object(smoke, "changed_files", lambda root, base: self.diff),
                       patch.object(smoke, "hub_tests_all", lambda root=None: [f"Drawer/test_{n}.py" for n in range(5)])):
            target.start()
            self.addCleanup(target.stop)

    def test_impact_runs_the_picked_files_and_names_the_count(self):
        with patch.object(smoke, "impact", lambda root, changed, full_on, always: (["Drawer/test_a.py"], "1 changed file(s)")):
            self.assertEqual(0, self.run_main(["--impact", "origin/main"]))
        self.assertEqual(["Drawer/test_a.py"], self.hub_files)
        self.assertIn("smoke: IMPACT 1 of 5 hub test file(s) for 1 changed file(s) against origin/main",
                      self.out.getvalue())

    def test_a_core_file_or_no_flag_or_an_unreadable_diff_runs_the_full_set(self):
        self.assertEqual(0, self.run_main(["--impact", "origin/main"]))  # Drawer/smoke.py is core
        self.assertIsNone(self.hub_files)
        self.assertIn("smoke: FULL: core file changed: Drawer/smoke.py", self.out.getvalue())
        self.diff = None
        self.assertEqual(0, self.run_main(["--impact", "origin/main"]))
        self.assertIsNone(self.hub_files)
        self.assertIn("smoke: FULL: no diff against origin/main", self.out.getvalue())
        self.assertEqual(0, self.run_main())
        self.assertIsNone(self.hub_files)
        self.assertNotIn("FULL", self.out.getvalue())

    def test_a_full_on_string_is_one_pattern(self):
        self.cfg["smoke"]["full_on"] = "Drawer/git-hooks/*"
        self.diff = ["Drawer/git-hooks/pre-push"]
        self.assertEqual(0, self.run_main(["--impact", "origin/main"]))
        self.assertIsNone(self.hub_files)
        self.assertIn("smoke: FULL: core file changed: Drawer/git-hooks/pre-push", self.out.getvalue())


class RunTmpTests(unittest.TestCase):
    """A hub test run keeps its temp folders in <cache>/<state name>-<hash>-test-tmp/<run id> and trashes it at the
    end, red or green; the next run trashes a killed run's folder when no live pid owns it."""
    setUp_smoke = SmokeTests.setUp
    fake_run, fake_hub_tests = SmokeTests.fake_run, SmokeTests.fake_hub_tests
    run_main, fake_trash = SmokeTests.run_main, SmokeTests.fake_trash

    def setUp(self):
        self.setUp_smoke()
        self.base = smoke.run_tmp.base(self.cfg)
        self.assertEqual(self.tmp / "temp", self.base.parent)

    def test_windows_trash_sends_the_folder_to_the_recycle_bin_and_names_a_refusal(self):
        with patch.object(smoke.run_tmp.oscompat, "WINDOWS", True), \
             patch.object(smoke.run_tmp.oscompat, "recycle") as recycle:
            self.assertIsNone(REAL_TRASH(self.tmp))
            recycle.assert_called_once_with(self.tmp)
            recycle.side_effect = OSError("Recycle Bin refused x (code 5)")
            self.assertIn("Recycle Bin refused", REAL_TRASH(self.tmp))

    def test_the_hub_tests_and_the_product_step_get_one_run_folder_that_is_trashed_after_green_and_red(self):
        for reds, code in (([], 0), (["smoke: HUB RED Drawer/test_x.py: exit 1 (x)"], 1)):
            self.hub_reds, self.trashed = reds, []
            self.assertEqual(code, self.run_main())
            folder = Path(self.hub_env["TMPDIR"])
            self.assertEqual(self.base, folder.parent)
            self.assertEqual({folder}, {Path(self.hub_env[k]) for k in ("TMPDIR", "TMP", "TEMP")})
            if not reds:
                self.assertEqual(self.hub_env, self.run_env)
            self.assertEqual([folder], self.trashed)
            self.assertFalse(self.base.exists())

    def test_a_run_folder_is_trashed_when_the_run_breaks(self):
        def broken(**_kwargs):
            raise RuntimeError("broke")
        with patch.object(smoke, "hub_tests", broken), self.assertRaises(RuntimeError):
            self.run_main()
        self.assertEqual(1, len(self.trashed))
        self.assertFalse(self.base.exists())

    def test_a_run_inside_a_run_folder_uses_it_and_leaves_its_cleanup_to_the_outer_run(self):
        outer = self.base / "1-outer"
        outer.mkdir(parents=True)
        with patch.dict(os.environ, {"TMPDIR": str(outer)}):
            self.assertEqual(0, self.run_main())
        self.assertEqual(str(outer), self.hub_env["TMPDIR"])
        self.assertEqual([], self.trashed)
        self.assertTrue(outer.is_dir())

    def test_a_killed_run_folder_is_trashed_by_the_next_run_and_a_live_one_stays(self):
        import subprocess
        code = ("import sys, time; sys.path.insert(0, sys.argv[1]); import run_tmp\n"
                "from pathlib import Path; run_tmp.TEMP_ROOT = Path(sys.argv[3])\n"
                "with run_tmp.run_folder({'project': {'state_root': sys.argv[2]}}) as env:\n"
                "    print(env['TMPDIR'], flush=True); time.sleep(120)\n")
        proc = subprocess.Popen([sys.executable, "-B", "-c", code, os.path.dirname(os.path.realpath(__file__)),
                                 str(self.tmp / "state"), str(self.tmp / "temp")], stdout=subprocess.PIPE, text=True,
                                env={"PATH": "/usr/bin:/bin"})
        killed = Path(proc.stdout.readline().strip())
        proc.kill()
        proc.wait()
        proc.stdout.close()
        self.assertTrue(killed.is_dir())
        self.assertEqual(str(proc.pid), (killed / ".owner").read_text())
        live = self.base / "live"
        live.mkdir()
        (live / ".owner").write_text(str(os.getpid()))
        self.assertEqual(0, self.run_main())
        self.assertIn(killed, self.trashed)
        self.assertFalse(killed.exists())
        self.assertTrue(live.is_dir())
        self.assertNotIn(live, self.trashed)


if __name__ == "__main__":
    unittest.main()
