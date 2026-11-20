"""A dead interpreter must not shadow a working runtime or setup guidance."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HUB = Path(__file__).resolve().parents[1]
SCRIPTS = ("Drawer/git-hooks/pre-commit", "Drawer/git-hooks/pre-push", "Drawer/statusline-command.sh")
# Sourced by the hooks, never run on its own: copied as it is.
SOURCED = ("Drawer/git-hooks/bound-gate.sh",)
SH = shutil.which("sh")
PAYLOAD = '{"model":{"display_name":"Fixture"},"effort":{"level":"ultra"},"context_window":{"used_percentage":17},"rate_limits":{"seven_day":{"used_percentage":23}}}'
# The repair line each script prints when no rung runs.
SETUP = {
    "Drawer/git-hooks/pre-commit": "pre-commit: no working Python; run uv sync at the hub root.\n",
    "Drawer/git-hooks/pre-push": "pre-push: no working Python; run uv sync at the hub root.\n",
    "Drawer/statusline-command.sh":
        "No working Python runtime; run the setup skill (.claude/skills/setup/SKILL.md).\n",
}
# The status line fields, read from PAYLOAD; colors are render, stripped before the compare.
STATUS = "Fixture effort:ultra ctx:17% wk:23%"
COLOR = re.compile(r"\x1b\[[0-9;]*m")


class PythonStubFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="python ladder ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.hub = self.root / "a hub"
        self.tools = self.root / "tools"
        self.tools.mkdir()
        self.log = self.root / "interpreters"
        self.calls = self.root / "calls"
        for script in SCRIPTS:
            target = self.hub / script
            target.parent.mkdir(parents=True, exist_ok=True)
            source = (HUB / script).read_text(encoding="utf-8")
            target.write_text(source.replace("\n", "\nPATH=$FIXTURE_PATH; export PATH\n", 1), encoding="utf-8")
            target.chmod(0o755)
        for script in SOURCED:
            (self.hub / script).write_text((HUB / script).read_text(encoding="utf-8"), encoding="utf-8")
        # The secret scanner is a PATH tool: present and clean, so the hook reaches its Python calls.
        self.write(self.tools / "gitleaks", "exit 0\n")
        self.write(self.tools / "git", 'case "$2" in\n'
                   '  --show-toplevel) printf "%s\\n" "$FIXTURE_REPO" ;;\n'
                   '  --git-path) printf "%s/.git/%s\\n" "$FIXTURE_REPO" "$3" ;;\n'
                   '  *) exit 2 ;;\nesac\n')
        self.write(self.tools / "dirname", 'test "$1" != -- || shift\nprintf "%s\\n" "${1%/*}"\n')
        probe = ("import json, os, sys\nfrom pathlib import Path\n"
                 "with open(os.environ['CALLS_LOG'], 'a', encoding='utf-8') as out:\n"
                 "    out.write(json.dumps({'script': Path(sys.argv[0]).name, "
                 "'argv': sys.argv[1:], 'stdin': sys.stdin.read()}) + '\\n')\n")
        for name in ("name-check.py", "rules_check.py", "harness-conform.py", "git-branch-policy.py"):
            (self.hub / "Drawer" / name).write_text(probe, encoding="utf-8")
        self.env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}
        self.env.update(FIXTURE_PATH=self.tools.as_posix(), FIXTURE_REPO=self.hub.as_posix(),
                        REAL_PYTHON=Path(sys.executable).as_posix(), INTERPRETER_LOG=self.log.as_posix(),
                        CALLS_LOG=self.calls.as_posix())
        # The status line reads ~/.claude stamps; an empty home keeps the real ones out.
        home = self.root / "home"
        home.mkdir()
        self.env.update(HOME=str(home), USERPROFILE=str(home))

    def write(self, path, body):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        path.chmod(0o755)

    def runtime(self, path, name, status=0):
        self.write(path, 'if test "$1" = -c && case "$2" in pass|*version_info*) true ;; *) false ;; esac; then\n'
                   '  read -r ignored\n  printf probe-noise\n'
                   f'  exit {status}\nfi\nprintf "%s\\n" "{name}" >> "$INTERPRETER_LOG"\n' +
                   (f'exit {status}\n' if status else 'exec "$REAL_PYTHON" "$@"\n'))

    def run_script(self, script):
        for path in (self.log, self.calls):
            path.unlink(missing_ok=True)
        # Each disposable script resets PATH after Git for Windows adds its host tools.
        run = subprocess.run([SH, (self.hub / script).as_posix(), "origin", "fixture://remote"],
                             cwd=self.hub, env=self.env, input=PAYLOAD, text=True,
                             capture_output=True, timeout=15, check=False)
        selected = self.log.read_text().splitlines() if self.log.exists() else []
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []
        return (run.returncode, COLOR.sub("", run.stdout), run.stderr, selected, calls)

    def accepted(self, script, name):
        if script == "Drawer/git-hooks/pre-commit":
            # The first Python call takes the hook's stdin; the probes before it read none.
            calls = [{"script": "name-check.py", "argv": [], "stdin": PAYLOAD},
                     {"script": "rules_check.py", "argv": ["--index", self.hub.as_posix()], "stdin": ""},
                     {"script": "harness-conform.py", "argv": ["--commit"], "stdin": ""},
                     {"script": "git-branch-policy.py", "argv": ["check-commit", "--repo", self.hub.as_posix()], "stdin": ""}]
        elif script == "Drawer/git-hooks/pre-push":
            calls = [{"script": "git-branch-policy.py", "argv": ["check-push", "--repo", self.hub.as_posix(), "--remote", "origin", "--url", "fixture://remote"], "stdin": PAYLOAD}]
        else:
            return (0, STATUS, "", [name], [])
        return (0, "", "", [name] * len(calls), calls)

    def test_each_candidate_must_run_and_the_first_working_one_keeps_stdin(self):
        """A dead prepared Python shadows a working lower rung; probes consume stdin."""
        candidates = [(self.hub / ".venv/bin/python", "bin"), (self.tools / "python3", "python3")]
        for chosen in range(len(candidates)):
            for index, (path, name) in enumerate(candidates):
                self.runtime(path, name, 103 if index < chosen else 0)
            # weak-test-lint: allow loop-only-assert -- Each fixture script executes and asserts its complete result and state.
            for script in SCRIPTS:
                with self.subTest(chosen=chosen, script=script):
                    actual = self.run_script(script)
                    self.assertEqual(self.accepted(script, candidates[chosen][1]), actual)

    # Defect: a python3 older than 3.12 answered -c pass and ran the hooks into a tomllib crash.
    # Injection: the candidate loops probe -c pass only.
    def test_an_old_python3_is_refused_like_no_python(self):
        """An interpreter that runs but fails the 3.12 version probe never reaches a hook script."""
        old = self.tools / "python3"
        self.write(old, 'if test "$1" = -c && test "$2" = pass; then exit 0; fi\n'
                   'if test "$1" = -c; then exit 1; fi\n'
                   'printf old >> "$INTERPRETER_LOG"\nexit 0\n')
        for script in SCRIPTS[:2]:
            with self.subTest(script=script):
                self.assertEqual((1, "", SETUP[script], [], []), self.run_script(script))

    def test_missing_or_broken_runtimes_refuse_with_setup_guidance(self):
        """A seat without a working interpreter returns bare command errors instead of the repair."""
        for broken in (False, True):
            if broken:
                for path in (self.hub / ".venv/bin/python", self.tools / "python3"):
                    self.runtime(path, "broken", 103)
            # weak-test-lint: allow loop-only-assert -- Each fixture script executes and asserts refusal plus absent side effects.
            for script in SCRIPTS:
                with self.subTest(broken=broken, script=script):
                    actual = self.run_script(script)
                    self.assertEqual((1, "", SETUP[script], [], []), actual)


if __name__ == "__main__":
    unittest.main()
