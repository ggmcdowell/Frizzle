import importlib.util
import io
import json
from contextlib import redirect_stdout
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path


PATH = Path(__file__).with_name("seat-doctor.py")
SPEC = importlib.util.spec_from_file_location("seat_doctor", PATH)
DOCTOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DOCTOR)
sys.path.insert(0, str(PATH.parent))
import harness_config  # noqa: E402
from runtime import profile as RUNTIME_PROFILE  # noqa: E402

CFG = harness_config.load(PATH.parent.parent)
SERVER = harness_config.server_name(CFG)
# The seat contract, written out rather than read back from the doctor: the two values a
# launching client can carry into the process (seat-doctor.py module docstring).
HUB_KEY, BOX_KEY = (harness_config.env_name(s, CFG) for s in ("HUB", "BOX"))
SEAT_KEY_NAMES = (HUB_KEY, BOX_KEY)
SOURCES = ("harness.toml", CFG["project"]["marker_file"], "AGENTS.md", ".gitattributes", "pyproject.toml",
           "uv.lock", "Drawer/settings-manifest.json", "Drawer/statusline-command.sh", "Drawer/mem/mem.py")
# The two remedies main() prints before any seat item.
SETUP_REQUIRED = (
    'HUB SETUP REQUIRED: this checkout has no saved runtime profile on this machine. '
    'Read .claude/skills/setup/SKILL.md and follow it; the seat doctor is repair, not setup.'
)
PROFILE_INVALID = (
    "saved profile is invalid; run `<python> Drawer/runtime/seat.py init` at the hub root "
    "(it moves the bad file aside and writes a fresh profile), then rerun the doctor."
)


def stub_runtime_profile(*, setup_required=False, activate=None):
    """The two runtime.profile entries main() reads before any seat item, under the fixture's control.

    Each must be called with this hub's root."""
    def on_hub(answer):
        def call(hub):
            assert hub == DOCTOR.HUB, hub
            return answer()
        return call
    return unittest.mock.patch.multiple(RUNTIME_PROFILE, setup_required=on_hub(lambda: setup_required),
                                        activate=on_hub(activate or (lambda: None)))


def row(name, ok, detail, skipped=False):
    """One item as --json prints it."""
    return {"name": name, "ok": ok, "detail": detail, "skipped": skipped}


class SeatFixture(unittest.TestCase):
    """One conforming seat on disk. Holds no case of its own, so nothing runs twice."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="seat doctor ")
        self.addCleanup(self._dir.cleanup)
        self.hub = Path(self._dir.name).resolve()
        home = unittest.mock.patch.object(Path, "home", return_value=self.hub)
        home.start()
        self.addCleanup(home.stop)
        for name in SOURCES:
            (self.hub / name).parent.mkdir(parents=True, exist_ok=True)
            (self.hub / name).write_text("", encoding="utf-8")
        self.mcp_json({"mcpServers": {SERVER: {"command": "python"}}})
        self.denylist = self.hub / "deny" / "names.txt"
        self.denylist.parent.mkdir()
        self.denylist.write_text("# mine\nsub:secret-word\n", encoding="utf-8")
        self.cfg = {**CFG, "names": {**CFG["names"], "denylist": str(self.denylist)}}
        self.env = {
            HUB_KEY: str(self.hub),
            BOX_KEY: "alpha",
        }

    def mcp_json(self, data):
        (self.hub / ".mcp.json").write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")

    def runner(self, tools=None):
        answers = {"git": (0, "git version 2.50.0\n"), "uv": (0, "uv 0.9.0\n"),
                   "gitleaks": (0, "gitleaks version 8.30.1\n"),
                   "claude": (0, "2.1.328 (Claude Code)\n"), **(tools or {})}

        def run(args, timeout=30):
            return answers[args[0]]
        return run

    def found(self, env=None, runner=None, **kwargs):
        kwargs.setdefault("python", (3, 12, 4))
        return DOCTOR.check_seat(env=self.env if env is None else env,
                                 runner=runner or self.runner(), hub=self.hub, cfg=self.cfg, **kwargs)

    def items(self, env=None, runner=None, **kwargs):
        return {item.name: item for item in self.found(env, runner, **kwargs)}

    def rows(self, env=None, runner=None, **kwargs):
        """Every item in the order the doctor reports it: (name, ok, skipped, detail)."""
        return [(item.name, item.ok, item.skipped, item.detail)
                for item in self.found(env, runner, **kwargs)]

    def state(self, item):
        return (item.ok, item.skipped, item.detail)


TOOL_ROWS = [
    ("python", True, False, "Python 3.12.4"),
    ("git", True, False, "git version 2.50.0"),
    ("uv", True, False, "uv 0.9.0"),
    ("gitleaks", True, False, "gitleaks version 8.30.1"),
    ("claude", True, False, "Claude Code 2.1.328"),
]


class SeatDoctorTests(SeatFixture):
    def test_every_item_passes_on_a_conforming_seat(self):
        self.assertEqual(
            [(HUB_KEY, True, False, str(self.hub)),
             (BOX_KEY, True, False, "alpha"),
             ("mcp_json", True, False, f"registers {SERVER}"),
             ("denylist", True, False, f"1 rule(s) in {self.denylist}"),
             ("sources", True, False, f"all {len(SOURCES)} distributed inputs present"),
             *TOOL_ROWS],
            self.rows(),
        )

    def test_source_only_checks_the_inputs_and_tools_without_a_seat(self):
        self.assertEqual(
            [("sources", True, False, f"all {len(SOURCES)} distributed inputs present"), *TOOL_ROWS],
            self.rows(env={}, source_only=True),
        )

    def json_mode(self, env):
        """Run --json over one seat: (exit code, the parsed document, the whole printed line)."""
        items = self.found(env)
        output = io.StringIO()
        with stub_runtime_profile(), unittest.mock.patch.object(
            DOCTOR, "check_seat", return_value=items
        ), unittest.mock.patch.dict(
            DOCTOR.os.environ, env, clear=True
        ), redirect_stdout(output):
            code = DOCTOR.main(["--json"])
        return code, json.loads(output.getvalue()), output.getvalue()

    def test_json_mode_reports_actual_failure_and_the_seat_source(self):
        # This process carries every value of the seat contract, so seat_source names them all:
        # a contract key the doctor stops carrying drops out of the detail below.
        carried = ", ".join(sorted(SEAT_KEY_NAMES))
        code, document, printed = self.json_mode(dict(self.env, **{BOX_KEY: " "}))
        self.assertEqual(1, code)
        self.assertEqual(
            {"status": "FAIL",
             "seat_source": {"ok": True, "detail": "this process carries the seat and it "
                             f"matches the saved profile: {', '.join(sorted(set(SEAT_KEY_NAMES) - {BOX_KEY}))}"},
             "items": [
                 row(HUB_KEY, True, str(self.hub)),
                 row(BOX_KEY, False, "unset or empty"),
                 row("mcp_json", True, f"registers {SERVER}"),
                 row("denylist", True, f"1 rule(s) in {self.denylist}"),
                 row("sources", True, f"all {len(SOURCES)} distributed inputs present"),
                 *(row(name, ok, detail) for name, ok, _skip, detail in TOOL_ROWS),
             ]},
            document,
        )

        code, document, printed = self.json_mode(self.env)
        self.assertEqual(0, code)
        self.assertEqual("PASS", document["status"])
        self.assertEqual({"ok": True, "detail": "this process carries the seat and it "
                          f"matches the saved profile: {carried}"}, document["seat_source"])

    def test_hub_root_compares_case_insensitively_but_rejects_another_root(self):
        env = dict(self.env, **{HUB_KEY: str(self.hub).upper()})
        self.assertEqual((True, False, str(self.hub)), self.state(self.items(env)[HUB_KEY]))
        env = dict(self.env, **{HUB_KEY: str(self.hub.parent)})
        self.assertEqual(
            (False, False, f"{self.hub.parent} is not the hub root {self.hub}"),
            self.state(self.items(env)[HUB_KEY]),
        )
        env = dict(self.env)
        del env[HUB_KEY]
        self.assertEqual((False, False, "unset"), self.state(self.items(env)[HUB_KEY]))

    def test_box_must_be_non_empty(self):
        item = self.items(dict(self.env, **{BOX_KEY: "   "}))[BOX_KEY]
        self.assertEqual((False, False, "unset or empty"), self.state(item))

    def test_mcp_json_must_parse_and_register_the_memory_server(self):
        self.mcp_json({"mcpServers": {"other": {}}})
        self.assertEqual((False, False, f"registers no {SERVER} server: ['other']"),
                         self.state(self.items()["mcp_json"]))
        self.mcp_json("{not json")
        self.assertEqual((False, False, "does not parse (JSONDecodeError)"),
                         self.state(self.items()["mcp_json"]))
        (self.hub / ".mcp.json").unlink()
        self.assertEqual((False, False, "missing; run Drawer/settings-sync.py --apply"),
                         self.state(self.items()["mcp_json"]))

    def test_a_missing_distributed_input_is_named(self):
        (self.hub / "AGENTS.md").unlink()
        (self.hub / "uv.lock").unlink()
        self.assertEqual((False, False, "missing: AGENTS.md, uv.lock"),
                         self.state(self.items(source_only=True)["sources"]))

    def test_an_old_python_fails(self):
        self.assertEqual((False, False, "Python 3.11.9 is older than 3.12"),
                         self.state(self.items(python=(3, 11, 9))["python"]))

    def test_a_missing_or_broken_tool_fails(self):
        runner = self.runner({"git": (None, "No such file or directory: 'git'"), "uv": (2, "boom")})
        items = self.items(runner=runner)
        self.assertEqual((False, False, "not found: No such file or directory: 'git'"), self.state(items["git"]))
        self.assertEqual((False, False, "--version exited 2: boom"), self.state(items["uv"]))

    # Defect: a seat with no claude passed the doctor, and so did a claude too old for the Workflow tool.
    def test_a_missing_or_old_claude_fails_and_a_current_one_passes(self):
        for answer in ((None, "No such file or directory: 'claude'"), (1, "boom"), (0, "no number\n")):
            with self.subTest(answer=answer):
                failed = self.items(runner=self.runner({"claude": answer}))["claude"]
                self.assertEqual((False, False, "claude is missing or gave no version; install Claude Code 2.1.316 or newer"),
                                 self.state(failed))
        old = self.items(runner=self.runner({"claude": (0, "2.1.315 (Claude Code)\n")}))["claude"]
        self.assertEqual((False, False, "Claude Code 2.1.315 is older than 2.1.316; run claude update"), self.state(old))
        self.assertEqual((True, False, "Claude Code 2.1.316"),
                         self.state(self.items(runner=self.runner({"claude": (0, "2.1.316 (Claude Code)\n")}))["claude"]))
        self.assertEqual((True, False, "Claude Code 2.1.328"), self.state(self.items()["claude"]))
        self.assertEqual("2.1.316", ".".join(map(str, DOCTOR.MIN_CLI)))

    # Defect: the doctor passed a seat with no gitleaks, and the first commit then failed.
    def test_a_missing_gitleaks_fails_in_both_modes(self):
        runner = self.runner({"gitleaks": (None, "No such file or directory: 'gitleaks'")})
        for kwargs in ({}, {"source_only": True, "env": {}}):
            with self.subTest(kwargs=kwargs):
                self.assertEqual((False, False, "not found: No such file or directory: 'gitleaks'"),
                                 self.state(self.items(runner=runner, **kwargs)["gitleaks"]))

    # Defect: the doctor passed a seat with no deny list, and the first commit then failed.
    def test_the_deny_list_must_exist_hold_a_rule_and_say_when_it_is_only_the_example(self):
        self.denylist.write_text("# only a comment\n\n", encoding="utf-8")
        self.assertEqual((False, False, f"no rule in {self.denylist}; the commit hook refuses an empty list"),
                         self.state(self.items()["denylist"]))
        self.denylist.unlink()
        ok, skipped, detail = self.state(self.items()["denylist"])
        self.assertEqual((False, False), (ok, skipped))
        self.assertIn(f"missing or unreadable: {self.denylist}", detail)
        (self.hub / "name-denylist.example").write_text("# x\nre:example-name\n", encoding="utf-8")
        self.denylist.write_text("re:example-name\n", encoding="utf-8")
        self.assertEqual((True, False, f"{self.denylist} still holds only the example rules; add your own names"),
                         self.state(self.items()["denylist"]))
        self.assertNotIn("denylist", self.items(source_only=True, env={}))

    def test_a_process_seat_that_disagrees_with_the_profile_fails(self):
        carried = {name: "" for name in SEAT_KEY_NAMES}
        carried[BOX_KEY] = "gamma"
        item = DOCTOR.seat_source_item(carried, self.env)
        self.assertEqual(
            (False, f"this process carries seat values the saved profile does not match: {BOX_KEY}='gamma'"),
            (item.ok, item.detail),
        )

    def test_a_process_seat_equal_to_the_profile_passes_and_says_so(self):
        carried = {name: self.env.get(name, "") for name in SEAT_KEY_NAMES}
        item = DOCTOR.seat_source_item(carried, self.env)
        self.assertEqual(
            (True, "this process carries the seat and it matches the saved profile: "
                   + ", ".join(sorted(SEAT_KEY_NAMES))),
            (item.ok, item.detail),
        )

    def test_a_task_worktree_passes_with_the_main_checkout_hub_carried(self):
        # A session seated at the main checkout carries its hub into its linked task worktree,
        # whose saved profile (git-branch-policy.py worktree-add) names the worktree.
        main, tree = self.hub / "main", self.hub / "main_wt" / "t1"
        main.mkdir()
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(main)]
        for args in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "root"],
                     ["worktree", "add", "-q", "-b", "task/t1", str(tree)]):
            DOCTOR.subprocess.run([*git, *args], check=True, env=harness_config.git_env())
        tree = tree.resolve()

        def activate():
            DOCTOR.os.environ.update({HUB_KEY: str(tree), BOX_KEY: "alpha"})
        output = io.StringIO()
        with unittest.mock.patch.object(DOCTOR, "HUB", tree), stub_runtime_profile(activate=activate), \
                unittest.mock.patch.object(DOCTOR, "check_seat", return_value=[]), \
                unittest.mock.patch.dict(DOCTOR.os.environ, {HUB_KEY: str(main), BOX_KEY: "alpha"}), \
                redirect_stdout(output):
            code = DOCTOR.main(["--json"])
        self.assertEqual((0, {"ok": True, "detail": "this process carries the seat and it matches the saved "
                                                    "profile: " + ", ".join(sorted(SEAT_KEY_NAMES))}),
                         (code, json.loads(output.getvalue())["seat_source"]))
        # Another checkout's hub still fails in the worktree.
        item = DOCTOR.seat_source_item({HUB_KEY: str(self.hub), BOX_KEY: "alpha"},
                                       {HUB_KEY: str(tree), BOX_KEY: "alpha"}, main.resolve())
        self.assertFalse(item.ok)

    def test_main_exits_one_on_any_failure_and_zero_when_clean(self):
        source = ("PASS  seat_source: this process carried no seat; "
                  "every item is from the saved profile\n")
        for items, expected_code, expected_out in (
            ([DOCTOR.Item("a", True, "ok")], 0,
             source + "PASS  a: ok\n\nRESULT: PASS - all 2 seat items clean\n"),
            ([DOCTOR.Item("a", True, "ok"), DOCTOR.Item("b", False, "no")], 1,
             source + "PASS  a: ok\nFAIL  b: no\n\n"
             "RESULT: FAIL - 1 of 3 seat items failed: b\n"),
            ([DOCTOR.Item("a", True, "ok"), DOCTOR.Item("b", True, "none of it", skipped=True)], 0,
             source + "PASS  a: ok\nSKIP  b: none of it\n\n"
             "RESULT: PASS - all 2 seat items clean, 1 skipped\n"),
        ):
            with self.subTest(expected_code=expected_code, items=len(items)):
                output = io.StringIO()
                with stub_runtime_profile(), unittest.mock.patch.object(
                    DOCTOR, "check_seat", return_value=items
                ), unittest.mock.patch.dict(
                    DOCTOR.os.environ, {}, clear=True
                ), redirect_stdout(output):
                    self.assertEqual(expected_code, DOCTOR.main([]))
                self.assertEqual(expected_out, output.getvalue())


class StartupRemedyTests(unittest.TestCase):
    """Before any seat item, the doctor names the one repair the human can run."""

    def doctor(self, *, setup_required, activate=None, argv=(), items=None):
        out = io.StringIO()
        seen = []

        def check(**kwargs):
            seen.append(kwargs)
            return items or [DOCTOR.Item("sources", True, "ok")]

        with stub_runtime_profile(setup_required=setup_required, activate=activate), \
                unittest.mock.patch.object(DOCTOR, "check_seat", side_effect=check), \
                redirect_stdout(out):
            code = DOCTOR.main(list(argv))
        return code, out.getvalue(), seen

    def test_a_checkout_with_no_profile_names_the_setup_skill(self):
        code, out, seen = self.doctor(setup_required=True)
        self.assertEqual(1, code)
        self.assertEqual(f"FAIL  setup_required: {SETUP_REQUIRED}\n", out)
        self.assertEqual([], seen)

    def test_source_only_runs_before_setup_and_reads_no_profile(self):
        def never():
            raise AssertionError("source-only must not activate a profile")

        code, out, seen = self.doctor(setup_required=True, activate=never, argv=("--source-only",))
        self.assertEqual(0, code)
        self.assertEqual("PASS  sources: ok\n\nRESULT: PASS - all 1 seat items clean\n", out)
        self.assertEqual([{"source_only": True}], seen)

    def test_an_invalid_profile_names_the_profile_step(self):
        # Every failure class activate_current is allowed to raise takes the same remedy.
        for error in (ValueError("the profile is invalid"),
                      OSError("the profile file is unreadable"),
                      RuntimeError("the profile loader refused")):
            with self.subTest(error=type(error).__name__):
                def broken():
                    raise error

                code, out, _seen = self.doctor(setup_required=False, activate=broken)
                self.assertEqual(1, code)
                self.assertEqual(f"FAIL  runtime_profile: {PROFILE_INVALID}\n", out)

    def test_the_json_form_carries_the_same_remedy(self):
        code, out, _seen = self.doctor(setup_required=True, argv=("--json",))
        self.assertEqual(1, code)
        self.assertEqual(
            {"status": "FAIL", "items": [{"name": "setup_required", "ok": False,
                                          "detail": SETUP_REQUIRED, "skipped": False}]},
            json.loads(out),
        )


if __name__ == "__main__":
    unittest.main()
