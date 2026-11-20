#!/usr/bin/env python3
"""Start the real Claude client and check the MCP tools it serves.

Argv mocks pass while a client refuses its config or a server serves zero tools; this
gate asks the client. The probe sends no model turn, runs no session hook and keeps no
history: Claude runs stream-json with no session persistence, all hooks disabled, and
no user message. Every session serves the same one server (the memory server, Drawer/mem/mem.py mcp), so the probe
runs once, at the hub root.

    <python> Drawer/mcp-live-check.py
    <python> Drawer/mcp-live-check.py --json
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

HUB = Path(__file__).resolve().parents[1]
DRAWER = HUB / "Drawer"
if str(DRAWER) not in sys.path:
    sys.path.insert(0, str(DRAWER))
import harness_config  # noqa: E402

CLIENTS = ("claude",)
TIMEOUT = 45
# A probe is a separate client, not a child of the session that runs the gate.
PARENT_SESSION_VARS = ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
                       "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN")
# .mcp.json is rendered locally and git-ignored, so it never stages: the code and the
# sources that render it are the triggers.
TRIGGER_PATTERNS = (
    "Drawer/mem/*", "Drawer/settings-sync.py", "Drawer/settings-manifest.json",
    "Drawer/mcp-live-check.py",
    ".claude/settings.json", "*/.claude/settings.json",
)
# Triggers judged by content: a skill or common-key edit rewrites every settings.json
# (settings-sync), so only an MCP launch key changing in one runs the probe.
CONTENT_PATTERNS = (".claude/settings.json", "*/.claude/settings.json", "Drawer/settings-manifest.json")
CLAUDE_LAUNCH_KEYS = ("env", "enabledPlugins", "extraKnownMarketplaces")  # and every key naming "mcp"
# The manifest keys that render into .mcp.json (all_servers) or a settings.json server key
# (targets and each class's servers decide enabledMcpjsonServers). The render of
# .mcp.json never stages, so the manifest edit itself runs the probe.
MANIFEST_LAUNCH_KEYS = ("targets", "all_servers")
CLASS_LAUNCH_KEYS = ("servers",)


class Unavailable(RuntimeError):
    """The client could not be asked: missing CLI, missing auth, or no answer in time."""


class Refused(RuntimeError):
    """The client answered and refused: config load failure or an early exit."""


def manifest() -> dict:
    return json.loads((DRAWER / "settings-manifest.json").read_text(encoding="utf-8"))


def _matches(path: str, patterns) -> bool:
    """Segment-wise match: a "*" never crosses "/", so a root and a subfolder file each match their own pattern."""
    parts = path.replace("\\", "/").split("/")
    return any(len(parts) == len(pattern.split("/")) and
               all(fnmatch.fnmatchcase(part, glob) for part, glob in zip(parts, pattern.split("/")))
               for pattern in patterns)


def is_trigger(path: str) -> bool:
    return _matches(path, TRIGGER_PATTERNS)


def _blob(spec: str) -> bytes | None:
    """git show <spec> in HUB under the inherited GIT_INDEX_FILE (the hook's or the precheck's index)."""
    shown = subprocess.run(["git", "-C", str(HUB), "show", spec], capture_output=True, check=False)
    return None if shown.returncode else shown.stdout


def _claude_view(data: dict) -> dict:
    return {key: value for key, value in data.items() if "mcp" in key.lower() or key in CLAUDE_LAUNCH_KEYS}


def _launch_view(path: str, blob: bytes) -> dict:
    data = json.loads(blob)
    if path != "Drawer/settings-manifest.json":
        return _claude_view(data)
    view = {key: data.get(key) for key in MANIFEST_LAUNCH_KEYS}
    view["capability_classes"] = {name: {key: row.get(key) for key in CLASS_LAUNCH_KEYS}
                                  for name, row in (data.get("capability_classes") or {}).items()}
    return view


def launch_changed(path: str) -> bool:
    """False only when a staged settings file provably changes no MCP launch input.

    A path outside CONTENT_PATTERNS is launch code or a rendered server entry: True. A settings
    file compares its launch keys (_launch_view) at HEAD and in the index; a missing blob (added,
    deleted, unborn HEAD) or a parse error is True."""
    path = path.replace("\\", "/")
    if not _matches(path, CONTENT_PATTERNS):
        return True
    old, new = _blob(f"HEAD:{path}"), _blob(f":{path}")
    if old is None or new is None:
        return True
    try:
        return _launch_view(path, old) != _launch_view(path, new)
    except (ValueError, TypeError, AttributeError):
        return True


def server_name() -> str:
    return harness_config.server_name(harness_config.load(HUB))


def expected() -> dict[str, list[str]]:
    """Every server the client must serve at the hub root -> its required tools (any tool will do)."""
    return {server_name(): []}


def diff(label: str, want: dict[str, list[str]], observed: dict[str, tuple[str, list[str]]]) -> list[str]:
    """Findings for one probe; observed maps server -> (status, tool names). Other servers are not ours."""
    findings = []
    for server, tools in want.items():
        if server not in observed:
            findings.append(f"{label}: {server} missing")
            continue
        status, names = observed[server]
        if status != "connected":
            findings.append(f"{label}: {server} {status}")
        elif not names:
            findings.append(f"{label}: {server} connected, 0 tools")
        elif absent := [tool for tool in tools if tool not in names]:
            findings.append(f"{label}: {server} lacks {', '.join(absent)}")
    return findings


def _start(argv: list[str], cwd: Path) -> tuple[subprocess.Popen, queue.Queue, list[bytes]]:
    env = {key: value for key, value in os.environ.items() if key not in PARENT_SESSION_VARS}
    process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True)
    lines: queue.Queue = queue.Queue()
    errors: list[bytes] = []

    def pump():
        for line in process.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()
    threading.Thread(target=lambda: errors.extend(process.stderr), daemon=True).start()
    return process, lines, errors


def _kill(process: subprocess.Popen) -> None:
    """Kill only the recorded process tree; the probe spawned its MCP servers."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


class _Session:
    def __init__(self, argv: list[str], cwd: Path, timeout: float):
        self.process, self.lines, self.errors = _start(argv, cwd)
        self.deadline = time.monotonic() + timeout

    def send(self, message: dict) -> None:
        try:
            self.process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            self.process.stdin.flush()
        except OSError as exc:
            raise self.refused(f"stdin closed ({exc})") from exc

    def refused(self, why: str) -> Refused:
        text = re.sub(r"\[[0-9;]*m", "", b"".join(self.errors[-8:]).decode("utf-8", "replace"))
        tail = text.strip().splitlines()[-3:]
        return Refused(f"{why}; stderr: {' | '.join(tail) or 'none'}")

    def read(self, wait: float = 1.0) -> dict | None:
        """The next JSON object, None on a quiet interval; raises at exit or deadline."""
        if time.monotonic() > self.deadline:
            raise Unavailable("no complete MCP status before the timeout")
        try:
            line = self.lines.get(timeout=wait)
        except queue.Empty:
            return None
        if line is None:
            raise self.refused(f"client exited with {self.process.wait()}")
        try:
            value = json.loads(line)
        except ValueError:
            return None
        return value if isinstance(value, dict) else None


def _cli(name: str) -> str | None:
    """PATH first; a hook under a launchd-started Mac client keeps only /usr/bin:/bin."""
    found = shutil.which(name)
    if found:
        return found
    candidates = (Path.home() / ".local" / "bin" / name, Path("/opt/homebrew/bin") / name,
                  Path("/usr/local/bin") / name)
    return next((str(c) for c in candidates if os.access(c, os.X_OK)), None)


def probe_claude(want: dict, timeout: float = TIMEOUT) -> dict[str, tuple[str, list[str]]]:
    claude = _cli("claude")
    if not claude:
        raise Unavailable("claude CLI not found")
    argv = [claude, "-p", "--verbose", "--input-format", "stream-json", "--output-format", "stream-json",
            "--no-session-persistence", "--settings", json.dumps({"disableAllHooks": True})]
    session = _Session(argv, HUB, timeout)
    try:
        session.send({"type": "control_request", "request_id": "init", "request": {"subtype": "initialize"}})
        asked = 0
        while True:
            message = session.read()
            if message is None or message.get("type") != "control_response":
                continue
            response = message.get("response") or {}
            if response.get("subtype") == "error":
                raise session.refused(f"control request failed: {response.get('error')}")
            if response.get("request_id") != "init" and not str(response.get("request_id", "")).startswith("status"):
                continue
            servers = (response.get("response") or {}).get("mcpServers")
            if servers is not None:
                observed = {s["name"]: (s.get("status", ""), sorted(t["name"] for t in s.get("tools") or []))
                            for s in servers}
                if not any(observed[name][0] == "pending" for name in want if name in observed):
                    return observed
                time.sleep(1)
            asked += 1
            session.send({"type": "control_request", "request_id": f"status{asked}",
                          "request": {"subtype": "mcp_status"}})
    finally:
        _kill(session.process)


PROBES = {"claude": probe_claude}


def run(clients: tuple[str, ...] = CLIENTS) -> dict:
    """Probe each client at the hub root; report state PASS, FAIL, or UNAVAILABLE with findings."""
    want = expected()
    probes = []
    started = time.monotonic()
    for client in clients:
        label = f"hub {client}"
        began = time.monotonic()
        try:
            findings = diff(label, want, PROBES[client](want))
            state = "FAIL" if findings else "PASS"
        except Refused as exc:
            state, findings = "FAIL", [f"{label}: {exc}"]
        except (Unavailable, OSError) as exc:
            state, findings = "UNAVAILABLE", [f"{label}: {exc}"]
        probes.append({"client": client, "target": ".", "state": state, "findings": findings,
                       "seconds": round(time.monotonic() - began, 1)})
    states = {p["state"] for p in probes}
    state = "FAIL" if "FAIL" in states else "UNAVAILABLE" if "UNAVAILABLE" in states else "PASS"
    return {"state": state, "probes": probes, "seconds": round(time.monotonic() - started, 1)}


def unstaged_triggers() -> list[str]:
    """Trigger paths whose working tree differs from the index; the probe would test those bytes."""
    changed = subprocess.run(["git", "-C", str(HUB), "diff", "--name-only", "--no-renames", "-z"],
                             capture_output=True, text=True, check=True).stdout.split("\0")
    untracked = subprocess.run(["git", "-C", str(HUB), "ls-files", "--others", "--exclude-standard", "-z"],
                               capture_output=True, text=True, check=True).stdout.split("\0")
    return sorted({path for path in changed + untracked if path and is_trigger(path)})


def gate(staged: list[str]) -> tuple[str, str, list[str]]:
    """(state, summary, findings) for the commit gate."""
    triggers = [path for path in staged if is_trigger(path)]
    if not triggers:
        return "PASS", "no MCP launch path staged; live probe skipped", []
    if not any(launch_changed(path) for path in triggers):
        return "PASS", "staged settings change no MCP launch key; live probe skipped", []
    dirty = unstaged_triggers()
    if dirty:
        return "FAIL", "MCP launch paths differ from the index; stage or stash them", dirty
    report = run()
    findings = [f for probe in report["probes"] for f in probe["findings"]]
    return report["state"], f"{len(report['probes'])} live client probes in {report['seconds']} s", findings


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = run()
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for probe in report["probes"]:
            print(f"[{probe['state']:11}] hub {probe['client']:6} {probe['seconds']} s")
            for finding in probe["findings"]:
                print(f"              {finding}")
        print(f"RESULT: {report['state']} in {report['seconds']} s")
    return 0 if report["state"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
