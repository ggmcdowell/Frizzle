"""Flight.command: the double-click start of the hub session, with Code Mode on, that then waits for input.

Claude Code takes CLAUDE_CODE_EXPERIMENTAL_CODE_MODE only from the shell it starts in, a --settings file or the user
settings: the env of the project .claude/settings.json never turned Code Mode on, so a new machine started Flight
with plain tools and no notice. The launcher sets the variable for its own start and passes no slash command: the
human types /flight to start the loop. Each case runs the tracked file in
a temp hub with a stub claude that records its arguments and that variable.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HUB = Path(__file__).resolve().parent.parent
LAUNCHER = HUB / "Flight.command"
SWITCH = "CLAUDE_CODE_EXPERIMENTAL_CODE_MODE"
MARKER = "HARNESS_FLIGHT_LAUNCHER"
STUB = ('#!/bin/sh\nprintf "%s|%s|%s|%s\\n" "$*" "${' + SWITCH + ':-unset}" "$PWD" "${' + MARKER + ':-unset}" > "$STUB_OUT"\n')


class FlightLauncherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hub = Path(self.tmp.name).resolve() / "my hub"  # a space: Finder folders have them
        self.hub.mkdir()
        shutil.copy2(LAUNCHER, self.hub / "Flight.command")
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        self.out = Path(self.tmp.name) / "out"
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.hub)], check=True, capture_output=True)

    def tearDown(self):
        self.tmp.cleanup()

    def stub_claude(self):
        (self.bin / "claude").write_text(STUB)
        (self.bin / "claude").chmod(0o755)

    def set_up(self, venv=True, bound=True):
        if venv:
            python = self.hub / ".venv/bin/python"
            python.parent.mkdir(parents=True)
            python.write_text("#!/bin/sh\n")
            python.chmod(0o755)
        if bound:
            subprocess.run(["git", "-C", str(self.hub), "config", "hub.bound", "true"], check=True)

    def launch(self, *args):
        # Started from another folder, as Finder does. The PATH holds the stub folder and the system tools only.
        env = {"PATH": f"{self.bin}{os.pathsep}/usr/bin{os.pathsep}/bin", "HOME": self.tmp.name, "STUB_OUT": str(self.out)}
        done = subprocess.run([str(self.hub / "Flight.command"), *args], cwd=self.tmp.name, env=env, capture_output=True,
                              text=True, stdin=subprocess.DEVNULL, timeout=30)
        return done, (self.out.read_text().strip().split("|") if self.out.exists() else None)

    def test_the_tracked_file_is_executable(self):
        mode = subprocess.run(["git", "ls-files", "-s", "Flight.command"], cwd=HUB, capture_output=True, text=True,
                              check=True).stdout.split()
        self.assertEqual("100755", mode[0] if mode else None, "a double-click needs the executable bit in Git")

    def test_a_set_up_hub_opens_the_session_with_code_mode_on_and_no_command(self):
        self.stub_claude()
        self.set_up()
        done, seen = self.launch()
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertEqual(["", "1", str(self.hub), "1"], seen)  # the marker flight-hooks.py seat reads

    def test_arguments_go_to_claude(self):
        self.stub_claude()
        self.set_up()
        done, seen = self.launch("--model", "opus", "/flight")
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertEqual(["--model opus /flight", "1", str(self.hub), "1"], seen)

    def test_a_hub_with_no_environment_starts_setup_with_plain_tools(self):
        self.stub_claude()
        self.set_up(venv=False)
        self.assertEqual(["/setup", "unset", str(self.hub), "unset"], self.launch()[1])

    def test_a_windows_environment_counts_as_set_up(self):
        # uv puts the interpreter at .venv/Scripts/python.exe on Windows; Git has no executable bit there.
        self.stub_claude()
        self.set_up(venv=False)
        python = self.hub / ".venv/Scripts/python.exe"
        python.parent.mkdir(parents=True)
        python.write_text("MZ\n")
        self.assertEqual(["", "1", str(self.hub), "1"], self.launch()[1])

    def test_flight_cmd_starts_flight_command_in_git_for_windows_bash(self):
        cmd = (HUB / "Flight.cmd").read_bytes()
        self.assertNotIn(b"\n", cmd.replace(b"\r\n", b""), "cmd.exe reads CRLF lines")
        text = cmd.decode()
        for part in ("%ProgramFiles%\\Git\\bin\\bash.exe", "where git", "..\\bin\\bash.exe", "Flight.command %*"):
            self.assertIn(part, text)
        eol = subprocess.run(["git", "check-attr", "eol", "--", "Flight.cmd"], cwd=HUB, capture_output=True, text=True,
                             check=True).stdout
        self.assertIn("eol: crlf", eol)

    def test_a_hub_that_setup_has_not_bound_starts_setup(self):
        self.stub_claude()
        self.set_up(bound=False)
        self.assertEqual(["/setup", "unset", str(self.hub), "unset"], self.launch()[1])

    def test_no_claude_names_the_install_and_starts_nothing(self):
        self.set_up()
        done, seen = self.launch()
        self.assertEqual(1, done.returncode)
        self.assertIsNone(seen)
        self.assertIn("Claude Code is not installed", done.stdout)


if __name__ == "__main__":
    unittest.main()
