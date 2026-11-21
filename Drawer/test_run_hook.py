"""The hook wrapper: interpreter ladder, argument pass-through, and the no-Python answers."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HUB = Path(__file__).resolve().parents[1]
WRAPPERS = HUB / "Drawer" / "hooks"
SH = shutil.which("sh")
PROBE ="import json, sys\nprint(json.dumps({'exe': sys.executable, 'argv': sys.argv[1:], 'stdin': sys.stdin.read()}))\n"
# The wrapper's four answers, written out from Drawer/hooks/run-hook.sh. The duplication is the
# point: an edit to one of these texts must move this file too, and a substring check would let a
# rewritten banner or a lost repair command through.
NO_PYTHON_NOTICE = (
    "HUB SETUP REQUIRED (no Python 3.12): this checkout is not set up on this machine. "
    "Read .claude/skills/setup/SKILL.md and follow it (brew on a Mac, winget on Windows). Do nothing else.")
NO_PYTHON_REASON = (
    "HUB SETUP REQUIRED (no Python 3.12): fail-closed denies this call. Only uv sync, or brew or "
    "winget install of uv or gitleaks, pass alone. Read .claude/skills/setup/SKILL.md.")
WRAPPER_BROKEN_WARNING = (
    "HUB HOOK WRAPPER BROKEN: Drawer/hooks/run-hook.sh stopped before its gate answered, so this "
    "call is ALLOWED UNCHECKED. Repair it now: git checkout -- Drawer/hooks/run-hook.sh in the hub, "
    "or git pull when main carries the break.")
GATE_CRASHED_WARNING = (
    "HUB GATE CRASHED (exit {status}): {script} did not finish, so this call is ALLOWED UNCHECKED. "
    "Fix the gate now.")
# A crashing gate's stderr and the tail its deny carries: the last five lines joined by one
# space, printable ASCII only (the non-ASCII é drops), the quote and backslash kept.
CRASH_STDERR = b'first\nsecond\nthird\nfourth\nfifth\nsay "no" at C:\\dir\\ caf\xc3\xa9'
CRASH_DETAIL = ' Gate stderr: second third fourth fifth say "no" at C:\\dir\\ caf'
# A budget no other hook uses, so the scan below finds only this test's sleep. The scan lists
# every process as "pid args": msys and Linux from /proc, a Mac from ps.
BUDGET = "121.37"
BUDGET_SLEEPS = r'''
import os, re, subprocess
SCAN = r"""if [ -r /proc/self/cmdline ]; then
  awk 'BEGIN { RS = "\0" } FNR == 1 { split(FILENAME, p, "/"); printf "\n%s", p[3] } { printf " %s", $0 }
       END { print "" }' /proc/[0-9]*/cmdline 2>/dev/null
else ps -ax -o pid=,args=; fi"""
def budget_sleeps():
    out = subprocess.run([os.environ["SCAN_SH"], "-c", SCAN], capture_output=True, text=True).stdout
    return re.findall(r"(?m)^\s*(\d+) (?:\S*/)?sleep 121\.37\s*$", out)
'''


def deny(reason):
    """The whole PreToolUse answer a denying wrapper prints."""
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def warn(text):
    """The whole answer a wrapper prints when a broken gate lets the call through: no decision,
    one loud warning the session shows."""
    return {"systemMessage": text}


def announce(event, notice=NO_PYTHON_NOTICE):
    """The whole SessionStart or SubagentStart answer a wrapper with no interpreter prints."""
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": notice}}


def answered(result):
    """The wrapper's JSON answer, or the streams it printed instead. A wrapper that answers
    nothing — a deny that fell through as a silent pass — must FAIL at the assert below, not
    ERROR on the parse, so the failure names the answer that went missing."""
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"no answer": {"returncode": result.returncode,
                              "stdout": result.stdout, "stderr": result.stderr}}


class WrapperFixture(unittest.TestCase):
    temp_prefix, hub_parent = "run-hook ", "a hub"

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix=cls.temp_prefix)
        cls.home = Path(cls.temp.name).resolve()
        cls.hub = cls.home / cls.hub_parent / "Sample_Hub"
        (cls.hub / "Drawer" / "hooks").mkdir(parents=True)
        shutil.copy2(WRAPPERS / "run-hook.sh", cls.hub / "Drawer" / "hooks" / "run-hook.sh")
        # The gate passes the preparation command only from inside this checkout, so the fixture
        # needs a corner folder of its own and a decoy sibling checkout outside it.
        cls.corner = cls.hub / "Operator"
        cls.corner.mkdir()
        cls.decoy = cls.home / "another checkout" / "Operator"
        cls.decoy.mkdir(parents=True)
        cls.probe = cls.hub / "Drawer" / "hooks" / "probe.py"
        cls.probe.write_text(PROBE, encoding="utf-8")
        # A dead python3 ahead of the shell tools, like the Mac stub without the command line
        # tools: it fails -c pass, so the wrapper must fall through to its no-Python answers. It
        # swallows stdin, so a probe that handed it the hook payload loses the SubagentStart name.
        cls.stub_dir = cls.home / "dead python3"
        cls.stub_dir.mkdir()
        (cls.stub_dir / "python3").write_text(
            "#!/bin/sh\ncat >/dev/null\necho 'xcode-select: note: No developer tools were found' >&2\nexit 1\n",
            encoding="utf-8")
        (cls.stub_dir / "python3").chmod(0o755)
        shutil.copy2(cls.stub_dir / "python3", cls.stub_dir / "python")  # the ladder tries python after python3
        # An old python3, like stock macOS 3.9: it answers -c pass, but its version probe fails.
        cls.old_dir = cls.home / "old python3"
        cls.old_dir.mkdir()
        (cls.old_dir / "python3").write_text(
            "#!/bin/sh\ncat >/dev/null\n[ \"$2\" = pass ] && exit 0\nexit 1\n", encoding="utf-8")
        (cls.old_dir / "python3").chmod(0o755)
        shutil.copy2(cls.old_dir / "python3", cls.old_dir / "python")
        cls.tools = os.pathsep.join(["/usr/bin", "/bin"])

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def hook_env(self, path, extra_env):
        env = {key: value for key, value in os.environ.items() if key != "PATH"}
        env["PATH"] = path if path is not None else os.environ["PATH"]
        env.update(extra_env or {})
        return env

    def sh(self, mode, script, *args, stdin="", path=None, cwd=None, extra_env=None):
        if SH is None:
            raise unittest.SkipTest("no POSIX shell")
        return subprocess.run([SH, str(self.hub / "Drawer" / "hooks" / "run-hook.sh"), mode, script, *args],
                              input=stdin, capture_output=True, text=True,
                              env=self.hook_env(path, extra_env), check=False,
                              cwd=str(cwd or self.hub))


# Every payload the no-Python gate denies, and every command it passes. One table, two tests.
DENIED = (
    {"tool_name": "Bash", "tool_input": {"command": "echo hi"}},
    {"tool_name": "Bash", "tool_input": {"command": "echo uv sync && git push"}},
    {"tool_name": "Bash", "tool_input": {"command": "uv sync; rm -rf x"}},
    {"tool_name": "Bash", "tool_input": {"command": "uv sync --frozen --reinstall"}},
    {"tool_name": "Bash", "tool_input": {"command": "uv run python -c pass"}},
    {"tool_name": "Bash", "tool_input": {"command": "brew install wget"}},
    {"tool_name": "Bash", "tool_input": {"command": "brew install uv gitleaks"}},
    {"tool_name": "Bash", "tool_input": {"command": "brew install uv && git push"}},
    {"tool_name": "Bash", "tool_input": {"command": "winget install --id astral-sh.uv"}},
    {"tool_name": "Bash", "tool_input": {"command": "winget install --id Other.Tool -e"}},
    {"tool_name": "Bash", "tool_input": {"command": "winget install --id Gitleaks.Gitleaks -e && git push"}},
    {"tool_name": "Write", "tool_input": {"file_path": "pyproject.toml", "content": "x"}},
    {"tool_name": "Edit", "tool_input": {"file_path": "a.md", "old_string": "uv sync"}},
    # Two command keys: a substring parser must not pick either one.
    {"tool_name": "Bash", "tool_input": {"command": "git push"}, "command": "uv sync"},
    {"tool_name": "Bash", "command": "uv sync", "tool_input": {"command": "git push"}},
    # A nested decoy tool_name must not vouch for a top-level Write.
    {"tool_name": "Write", "decoy": {"tool_name": "Bash"}, "tool_input": {"command": "uv sync"}},
    # Keys are case-sensitive: an upper-case decoy is neither counted nor matched.
    {"tool_name": "Bash", "decoy": {"COMMAND": "uv sync"}, "tool_input": {"command": "git push"}},
    {"tool_name": "Write", "decoy": {"TOOL_NAME": "Bash"}, "tool_input": {"command": "uv sync"}},
)
ALLOWED = ("uv sync", "uv sync --frozen", "brew install uv", "brew install gitleaks",
           "winget install --id astral-sh.uv -e", "winget install --id Gitleaks.Gitleaks -e")


class NoPythonTests(WrapperFixture):
    """PATH holds a dead python3 ahead of the shell tools: no interpreter, so the wrapper answers."""

    def no_python_path(self):
        return os.pathsep.join([str(self.stub_dir), self.tools])

    def test_sh_boot_announces_setup_for_each_part(self):
        for part in ("hub", "corner"):
            with self.subTest(part=part):
                result = self.sh("boot", str(self.probe), "--part", part, stdin="{}", path=self.no_python_path())
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(announce("SessionStart"), answered(result))

    def gate(self, command, cwd):
        return self.sh("gate", str(self.probe), path=self.no_python_path(), cwd=cwd,
                       stdin=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}))

    def test_sh_gate_denies_everything_but_the_exact_preparation_command(self):
        # One whole denial and one whole pass outside the tables: an emptied table proves nothing.
        denied = self.sh("gate", str(self.probe), path=self.no_python_path(),
                         stdin=json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo hi"}}))
        self.assertEqual((0, ""), (denied.returncode, denied.stderr))
        self.assertEqual(deny(NO_PYTHON_REASON), answered(denied))
        passed = self.gate("uv sync", self.hub)
        self.assertEqual((0, "", ""), (passed.returncode, passed.stdout, passed.stderr))
        for payload in DENIED:
            with self.subTest(payload=payload):
                denied = self.sh("gate", str(self.probe), stdin=json.dumps(payload), path=self.no_python_path())
                self.assertEqual(0, denied.returncode, denied.stderr)
                self.assertEqual(deny(NO_PYTHON_REASON), answered(denied))
        # The hub root and a corner folder: both sit inside this checkout.
        for prep in ALLOWED:
            for cwd in (self.hub, self.corner):
                with self.subTest(prep=prep, cwd=cwd.name):
                    passed = self.gate(prep, cwd)
                    self.assertEqual((0, ""), (passed.returncode, passed.stdout.strip()), passed.stderr)

    def test_sh_gate_fences_a_preparation_command_outside_this_checkout(self):
        """The right shape run from the wrong place prepares another checkout: deny it."""
        for prep in ALLOWED:
            for wrong in (self.decoy, self.decoy.parent, self.home):
                with self.subTest(prep=prep, cwd=str(wrong)):
                    denied = self.gate(prep, wrong)
                    self.assertEqual(0, denied.returncode, denied.stderr)
                    self.assertEqual(deny(NO_PYTHON_REASON), answered(denied))

    # Defect: a python3 older than 3.12 (stock macOS 3.9) ran the hooks, crashed on tomllib, opened
    # every gate and told the agent to stop all work. Injection: runs() probes only -c pass.
    def test_sh_an_old_python3_counts_as_no_python(self):
        path = os.pathsep.join([str(self.old_dir), self.tools])
        boot = self.sh("boot", str(self.probe), "--part", "hub", stdin="{}", path=path)
        self.assertEqual(announce("SessionStart"), answered(boot))
        denied = self.sh("gate", str(self.probe), path=path,
                         stdin=json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}))
        self.assertEqual(deny(NO_PYTHON_REASON), answered(denied))
        passed = self.sh("gate", str(self.probe), path=path,
                         stdin=json.dumps({"tool_name": "Bash", "tool_input": {"command": "brew install gitleaks"}}))
        self.assertEqual((0, ""), (passed.returncode, passed.stdout.strip()))

    def test_deny_reasons_stay_within_200_characters(self):
        self.assertLessEqual(len(NO_PYTHON_REASON), 200)

    def test_the_version_probe_matches_the_project_python(self):
        """The floor in run-hook.sh and both git hooks is the pyproject requires-python."""
        floor = re.search(r'requires-python\s*=\s*">=(\d+)\.(\d+)"', (HUB / "pyproject.toml").read_text()).groups()
        probe = f"sys.version_info < ({floor[0]}, {floor[1]})"
        for rel in ("Drawer/hooks/run-hook.sh", "Drawer/git-hooks/pre-commit", "Drawer/git-hooks/pre-push"):
            with self.subTest(rel=rel):
                self.assertIn(probe, (HUB / rel).read_text())

    def test_sh_boot_names_a_subagent_start(self):
        result = self.sh("boot", str(self.probe), "--part", "hub",
                         stdin=json.dumps({"hook_event_name": "SubagentStart"}), path=self.no_python_path())
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(announce("SubagentStart"), answered(result))

    def test_sh_pass_exits_zero_without_an_interpreter(self):
        """A pass hook has nothing to say with no Python: it exits 0 and prints nothing at all.
        The arguments it hands through belong to the interpreter path, and are proven there."""
        result = self.sh("pass", str(self.probe), stdin="{}", path=self.no_python_path())
        self.assertEqual((0, "", ""), (result.returncode, result.stdout, result.stderr))

    def test_sh_unknown_mode_fails_closed(self):
        result = self.sh("other", str(self.probe), path=self.no_python_path())
        self.assertEqual((2, "", "run-hook.sh: unknown mode 'other' (boot|gate|pass)\n"),
                         (result.returncode, result.stdout, result.stderr))

    def test_sh_broken_wrapper_allows_loudly_and_names_its_repair(self):
        wrapper = self.hub / "Drawer" / "hooks" / "run-hook.sh"
        good = wrapper.read_text(encoding="utf-8")
        self.addCleanup(wrapper.write_text, good, encoding="utf-8", newline="\n")
        wrapper.write_text(good.replace("script=$2\n", "script=$2\nif then fi (\n", 1), encoding="utf-8", newline="\n")
        allowed = self.sh("gate", str(self.probe), stdin="{}", path=self.no_python_path())
        self.assertEqual(0, allowed.returncode, allowed.stderr)
        # The shell names the break its own way and the seat picks the shell, so only "the shell
        # complained" is contract here: bash says "syntax error", dash "Syntax error", zsh "parse
        # error". An unbroken wrapper prints nothing on stderr, so this still fails on a stale anchor.
        self.assertRegex(allowed.stderr, r"(?i)(syntax|parse) error")
        self.assertEqual(warn(WRAPPER_BROKEN_WARNING), answered(allowed))
        # Only a gate turns the break into a loud allow; a boot or pass still fails as before.
        self.assertNotEqual(0, self.sh("pass", str(self.probe), stdin="{}", path=self.no_python_path()).returncode)


class InterpreterLadderTests(WrapperFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.venv = cls.hub / ".venv"
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(cls.venv)], check=True,
                       capture_output=True)
        # The venv puts its interpreter at Scripts/python.exe on Windows, bin/python elsewhere.
        cls.venv_python = (cls.venv / "Scripts" / "python.exe") if os.name == "nt" else (cls.venv / "bin" / "python")

    # Defect: under dash, read -t is an illegal option, so every gate was killed and every call allowed
    # unchecked. Injection: run-hook.sh drops the bash re-exec line.
    def test_a_gate_run_by_dash_still_judges_the_call(self):
        dash = shutil.which("dash") or ("/bin/dash" if os.path.exists("/bin/dash") else None)
        if dash is None or shutil.which("bash") is None:
            self.skipTest("no dash or no bash on this box")
        gate = self.hub / "Drawer" / "hooks" / "judge.py"
        gate.write_text("import json, sys\nprint(json.dumps({'judged': json.load(sys.stdin)['tool_name']}))\n",
                        encoding="utf-8")
        result = subprocess.run([dash, str(self.hub / "Drawer" / "hooks" / "run-hook.sh"), "gate", str(gate)],
                                input=json.dumps({"tool_name": "Bash"}), capture_output=True, text=True,
                                env=self.hook_env(None, None), cwd=str(self.hub), check=False)
        self.assertEqual((0, {"judged": "Bash"}), (result.returncode, answered(result)), result.stderr)

    def test_sh_prefers_the_prepared_environment_and_passes_arguments_and_stdin(self):
        result = self.sh("boot", str(self.probe), "--part", "corner", stdin='{"source": "startup"}')
        self.assertEqual(0, result.returncode, result.stderr)
        shown = answered(result)
        self.assertEqual((["--part", "corner"], '{"source": "startup"}'),
                         (shown.get("argv"), shown.get("stdin")))
        self.assertEqual(os.path.normcase(str(self.venv_python)),
                         os.path.normcase(str(shown.get("exe", ""))))

    def launcher(self, folder, name):
        """A working interpreter of our own under `name`: the rung runs on every seat."""
        folder = self.home / folder
        folder.mkdir(exist_ok=True)
        shim = folder / name
        shim.write_text(f'#!/bin/sh\nexec "{Path(sys.executable).as_posix()}" "$@"\n', encoding="utf-8")
        shim.chmod(0o755)
        return folder

    def test_sh_prefers_the_windows_interpreter_over_the_posix_one(self):
        """uv puts the interpreter at .venv/Scripts/python.exe on Windows: -f finds it, with no x bit."""
        scripts = self.venv / "Scripts"
        fake = scripts / "python.exe"
        if scripts.exists():
            # A Windows venv already has a real Scripts/python.exe: swap it for the fake, restore it after.
            real = scripts / "python.exe.real"
            fake.rename(real)
            self.addCleanup(real.rename, fake)
            self.addCleanup(fake.unlink)
        else:
            scripts.mkdir()
            self.addCleanup(shutil.rmtree, scripts)
        fake.write_text("not a program\n", encoding="utf-8")
        fake.chmod(0o644)
        # The ladder picks Scripts/python.exe before bin/python: running it fails, which proves the pick.
        result = self.sh("pass", str(self.probe), "--answer", stdin="{}")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("python.exe", result.stderr)

    def test_sh_falls_back_to_a_python_when_there_is_no_python3(self):
        moved = self.venv.with_name(".venv-off")
        self.venv.rename(moved)
        self.addCleanup(lambda: moved.rename(self.venv))
        real = self.launcher("real-python", "python")
        # A dead python3 comes first: the ladder passes it and reaches python.
        dead = self.home / "dead python3 only"
        dead.mkdir(exist_ok=True)
        shutil.copy2(self.stub_dir / "python3", dead / "python3")
        result = self.sh("boot", str(self.probe), "--part", "hub", stdin="{}",
                         path=os.pathsep.join([str(dead), str(real), self.tools]))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual({"exe": sys.executable, "argv": ["--part", "hub"], "stdin": "{}"}, answered(result))

    def test_sh_runs_every_hook_python_in_utf8_mode(self):
        utf8 = self.hub / "Drawer" / "hooks" / "utf8.py"
        utf8.write_text("import os, sys\nprint(os.environ.get('PYTHONUTF8'), sys.flags.utf8_mode)\n", encoding="utf-8")
        for mode in ("pass", "boot", "gate"):
            with self.subTest(mode=mode):
                result = self.sh(mode, str(utf8), stdin="{}", extra_env={"PYTHONUTF8": ""})
                self.assertEqual("1 1", result.stdout.strip(), result.stderr)

    def test_sh_falls_back_to_a_real_python3(self):
        moved = self.venv.with_name(".venv-off")
        self.venv.rename(moved)
        self.addCleanup(lambda: moved.rename(self.venv))
        real = self.launcher("real", "python3")
        result = self.sh("boot", str(self.probe), "--part", "hub", stdin="{}",
                         path=os.pathsep.join([str(real), self.tools]))
        self.assertEqual(0, result.returncode, result.stderr)
        # The shim execs this interpreter, so the ladder reached the real python3.
        self.assertEqual({"exe": sys.executable, "argv": ["--part", "hub"], "stdin": "{}"},
                         answered(result))

    def test_sh_pass_hands_its_arguments_and_stdin_to_the_prepared_interpreter(self):
        """A pass hook may carry arguments (`run-hook.sh pass <script> --answer`), so a lost "$@"
        would strip them from the hook and change its answer."""
        result = self.sh("pass", str(self.probe), "--answer", stdin="{}")
        self.assertEqual(0, result.returncode, result.stderr)
        shown = answered(result)
        self.assertEqual((["--answer"], "{}"), (shown.get("argv"), shown.get("stdin")))
        self.assertEqual(os.path.normcase(str(self.venv_python)),
                         os.path.normcase(str(shown.get("exe", ""))))

    # Defect: a live session keeps the hook list it started with, so a pass hook whose script a
    # later commit deleted runs Python on a missing file (exit 2) on every prompt or tool call.
    # Injection: run-hook.sh drops the `[ ! -f "$script" ]` pass line.
    def test_sh_pass_on_a_missing_script_is_a_no_op_and_a_gate_warns(self):
        gone = str(self.hub / "Drawer" / "gone.py")
        passed = self.sh("pass", gone, stdin="{}")
        self.assertEqual((0, "", ""), (passed.returncode, passed.stdout, passed.stderr))
        gated = self.sh("gate", gone, stdin='{"tool_name": "Write"}')
        self.assertEqual(0, gated.returncode)
        self.assertNotIn("hookSpecificOutput", answered(gated))
        self.assertTrue(answered(gated).get("systemMessage", "").startswith("HUB GATE CRASHED (exit 2): gone.py"))

    def test_sh_unknown_mode_fails_closed_on_a_prepared_seat(self):
        """The mode decides what an answer means, so a typo in a registered hook must refuse the
        same way on a set-up machine as on a bare one: never exec the script as a silent pass."""
        result = self.sh("other", str(self.probe), stdin="{}")
        self.assertEqual((2, "", "run-hook.sh: unknown mode 'other' (boot|gate|pass)\n"),
                         (result.returncode, result.stdout, result.stderr))

    def gate_scripts(self):
        """A gate that exits non-zero with no JSON, and one that never returns."""
        crash = self.hub / "Drawer" / "hooks" / "crash.py"
        # Raw bytes, no CRLF translation: six lines, the last with a quote, a backslash and
        # non-ASCII, so a deny that escaped nothing would not parse as JSON.
        crash.write_text("import sys\nsys.stderr.buffer.write(" + repr(CRASH_STDERR) + ")\n"
                         "raise SystemExit(1)\n", encoding="utf-8")
        hang = self.hub / "Drawer" / "hooks" / "hang.py"
        hang.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
        return crash, hang

    def test_sh_gate_allows_a_crash_and_a_run_past_the_budget_loudly(self):
        """A gate that crashes or hangs must never lock the hub: the call passes with a warning
        that names the gate. The crash's last five stderr lines ride in it, printable ASCII only."""
        crash, hang = self.gate_scripts()
        # The crash exits 1; the budget kills the hang, which a POSIX shell reports as 128 + SIGKILL.
        for script, reason in ((crash, GATE_CRASHED_WARNING.format(status=1, script="crash.py") + CRASH_DETAIL),
                               (hang, GATE_CRASHED_WARNING.format(status=137, script="hang.py"))):
            with self.subTest(script=script.name), mock.patch.dict(os.environ, {"HOOK_GATE_BUDGET": "2"}):
                result = self.sh("gate", str(script), stdin='{"tool_name": "Bash"}')
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(warn(reason), answered(result))

    def test_sh_gate_that_answers_in_time_leaves_no_budget_sleep_alive(self):
        """Every Bash call runs the gate: a budget sleep that outlives its answer piles up (222
        were once live). The budget is a timed read, never a sleep process, and a gate that
        answers at once returns at once, never after the budget."""
        # The gate counts the budget sleeps while it runs; the test counts again once the
        # wrapper has returned.
        watch = self.hub / "Drawer" / "hooks" / "budget-watch.py"
        watch.write_text(BUDGET_SLEEPS + (
            "import json, sys\n"
            "sys.stdin.read()\n"
            "print(json.dumps({'seen': len(budget_sleeps())}))\n"), encoding="utf-8")
        env = {"HOOK_GATE_BUDGET": BUDGET, "SCAN_SH": SH}
        started = time.monotonic()
        result = self.sh("gate", str(watch), stdin="{}", extra_env=env)
        elapsed = time.monotonic() - started
        scope = {}
        exec(BUDGET_SLEEPS, scope)  # the same scan the gate ran
        with mock.patch.dict(os.environ, env):
            left = scope["budget_sleeps"]()
        for pid in left:
            subprocess.run([SH, "-c", f"kill -9 {pid}"], capture_output=True)
        self.assertEqual((0, {"seen": 0}), (result.returncode, answered(result)), result.stderr)
        self.assertLess(elapsed, 60, "the wrapper waited out the budget")
        self.assertEqual([], left, "a budget sleep outlived the wrapper")


if __name__ == "__main__":
    unittest.main()
