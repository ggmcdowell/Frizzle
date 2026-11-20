"""The smoke inventory: one record per component, its live check, and its bounded repair.

Every check proves the component on this machine with a
real probe, never a file-exists guess, and answers with one record:

    {"name": ..., "status": "PASS" | "SKIPPED" | "FAIL" | "BLOCKED", "reason": ..., "detail": ...}

There is no aggregate boolean. A caller reads the records, or ``summary`` for the counts,
and decides for itself. SKIPPED means the component does not apply to this seat. BLOCKED
means a human must act: no automated repair can fix the record.

``repair`` runs the one bounded repair a component owns, then reruns its check. A repair
that already failed today for the same reason is refused: repeating it wastes the run and hides
the real cause.

Components: ``memory`` (the mem MCP server, started as this seat's agent starts it) and ``smoke`` (the
command in harness.toml ``[smoke]``; the engine pack sets it, and an empty command skips).
"""

from __future__ import annotations

from collections import namedtuple
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

DRAWER = Path(__file__).resolve().parents[1]
if str(DRAWER) not in sys.path:
    sys.path.insert(0, str(DRAWER))
import harness_config  # noqa: E402
from runtime import profile  # noqa: E402
from runtime.mcp_probe import _scrub  # noqa: E402

STATUSES = ("PASS", "SKIPPED", "FAIL", "BLOCKED")
TAIL_LINES = 40
# The memory repair: the locked hub environment, then this box's MCP registration.
UV_SYNC = ("uv", "sync", "--frozen")
MCP_RENDER = ("Drawer/settings-sync.py", "--mcp")

Component = namedtuple("Component", "name applies check repair agent_op")
"""One inventory entry.

applies(machine)   -> "" when the component applies here, else the reason it is skipped.
check(hub, machine, env) -> Record.
repair(hub, record, env) -> a short detail string, or None when no automated repair exists.
agent_op           -> the manual route, for a record no repair can fix.
"""


class RepeatRefused(RuntimeError):
    """This repair already failed today for the same reason; repeating it hides the cause."""


def _tail(text, limit: int = TAIL_LINES) -> str:
    """The last non-empty lines of a captured stream."""
    lines = [line.rstrip() for line in str(text or "").splitlines() if line.strip()]
    return "\n".join(lines[-limit:])


def _record(name: str, status: str, reason: str = "", detail="") -> dict:
    if status not in STATUSES:
        raise ValueError(f"unknown component status: {status}")
    return {"name": name, "status": status, "reason": reason, "detail": _scrub(detail)}


def _probe_env(hub) -> dict[str, str]:
    """The environment every probe and repair carries; never a hand-built environment."""
    env = dict(os.environ)
    env[profile.env_name("HUB")] = str(hub)  # roots derive from the hub, never the caller's cwd
    return env


def _interpreter(hub: Path) -> Path:
    """The interpreter the locked sync prepared: the hub root .venv."""
    return Path(hub) / ".venv" / "bin" / "python"


def _mcp_probe():
    """The stdio MCP prober, imported on use so a check can be mocked without it."""
    from runtime import mcp_probe
    return mcp_probe


def _server() -> str:
    """The memory MCP server name in this hub's registration."""
    return harness_config.server_name(harness_config.load(DRAWER.parent))


def _smoke_settings() -> tuple[str, float]:
    """The configured smoke command and its time limit, from harness.toml [smoke]."""
    smoke = harness_config.load(DRAWER.parent).get("smoke", {})
    return str(smoke.get("command") or "").strip(), float(smoke.get("timeout_seconds") or 900)


def _smoke_blocked_exit() -> int | None:
    """The smoke command's exit code for "cannot run here now" ([smoke] blocked_exit), or None."""
    code = harness_config.load(DRAWER.parent).get("smoke", {}).get("blocked_exit")
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def _run(argv: Sequence[str], cwd, env: Mapping[str, str], timeout: float):
    return subprocess.run([str(item) for item in argv], cwd=str(cwd), env=dict(env),
                          stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          timeout=timeout, check=False)


# ---------------------------------------------------------------- applicability


def _applies_always(_machine) -> str:
    return ""


def _applies_smoke(_machine) -> str:
    return "" if _smoke_settings()[0] else "no [smoke] command in harness.toml"


# ---------------------------------------------------------------------- probes


def _registration(hub, server: str) -> tuple[list[str], dict]:
    """The command and environment this seat's agent starts the server with.

    Claude reads the box's .mcp.json (git-ignored, written by settings-sync.py) and passes
    its whole environment, plus the entry's own env.
    """
    entry = json.loads((Path(hub) / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"][server]
    env = dict(os.environ)
    env.update(entry.get("env") or {})
    return [str(entry["command"]), *map(str, entry.get("args", []))], env


def _probe_record(name: str, hub, server: str, *, repair: str | None = None) -> dict:
    """Start the server as the seat's agent does and read its tool list.

    A repairable component names its repair as the reason.
    """
    try:
        argv, env = _registration(hub, server)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return _record(name, "FAIL", f"{server} is not registered for this seat: run seat.py configure",
                       f"{type(exc).__name__}: {exc}")
    # The client's own environment: the server builds nothing from the probe's.
    passed, detail = _mcp_probe().probe_stdio(argv, str(Path(hub)), env)
    if passed:
        return _record(name, "PASS", "", _tail(detail))
    failure = f"{server} did not answer a valid tool list"
    detail = f"command {argv[0]}\n{detail}"
    if repair:
        return _record(name, "FAIL", repair, _tail(f"{failure}\n{detail}"))
    return _record(name, "FAIL", failure, _tail(detail))


def _check_memory(hub, machine, env) -> dict:
    python = _interpreter(Path(hub))
    if not python.is_file():
        return _record("memory", "FAIL", "run repair memory",
                       f"the hub environment is missing: {python}")
    try:
        return _probe_record("memory", hub, _server(), repair="run repair memory")
    except ImportError as exc:  # a locked package is gone: only the repair can bring it back
        return _record("memory", "FAIL", "run repair memory",
                       f"a locked package did not import\n{type(exc).__name__}: {exc}")


def _check_smoke(hub, machine, env) -> dict:
    """Run the configured smoke command from the hub root; exit 0 passes.

    A ``{python}`` word in the command is the hub interpreter. The exit code ``[smoke] blocked_exit``
    means the command cannot run here now (an editor holds the project): BLOCKED, with the last
    output line as the reason, never a FAIL.
    """
    command, seconds = _smoke_settings()
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return _record("smoke", "FAIL", "the [smoke] command does not parse", f"{exc}: {command}")
    python = _interpreter(Path(hub))
    python = str(python) if python.is_file() else sys.executable
    argv = [python if item == "{python}" else item for item in argv]
    try:
        result = _run(argv, Path(hub), env, seconds)
    except subprocess.TimeoutExpired:
        return _record("smoke", "FAIL", f"the smoke command did not finish in {seconds:g} s", command)
    except OSError as exc:
        return _record("smoke", "FAIL", "the smoke command did not start", f"{type(exc).__name__}: {exc}")
    tail = _tail(f"{result.stdout or ''}\n{result.stderr or ''}")
    if result.returncode == 0:
        return _record("smoke", "PASS", "", tail)
    if result.returncode == _smoke_blocked_exit():
        last = next((line.strip() for line in reversed((result.stdout or "").splitlines()) if line.strip()), "")
        return _record("smoke", "BLOCKED", last or f"the smoke command cannot run here now "
                       f"(exit {result.returncode})", tail)
    return _record("smoke", "FAIL", f"the smoke command exited {result.returncode}", tail)


# --------------------------------------------------------------------- repairs


def _repair_memory(hub, record, env) -> str:
    root = Path(hub)
    note = _repair_note(UV_SYNC, root, env, 900)
    python = _interpreter(root)
    if not python.is_file():
        return note  # no venv to render with: uv's own exit and tail are the cause
    # The row starts the registered command, so the repair also re-renders the registration
    # (configure's first step, run direct: configure would wait on the seat lock this repair holds).
    render = [str(python), "-B", str(root / MCP_RENDER[0]), *MCP_RENDER[1:]]
    return f"{note}; {_repair_note(render, root, env, 300)}"


def _repair_note(argv: Sequence[str], cwd, env, timeout: float) -> str:
    result = _run(argv, cwd, env, timeout)
    tail = _tail(result.stderr or result.stdout, 5)
    return f"{Path(str(argv[0])).name} exit {result.returncode}" + (f": {tail}" if tail else "")


# A fresh interpreter's check: argv is the Drawer folder, the hub, the component name.
RECHECK = ("import json, sys; sys.path.insert(0, sys.argv[1]); from runtime import components, profile; "
           "hub, name = sys.argv[2:]; env = components._probe_env(hub); "
           "print(json.dumps(components.check_all(hub, profile.machine(hub, env), only=[name])[0]))")


def _recheck(hub, name: str, env: Mapping[str, str]) -> dict:
    """The check after a repair, in a fresh interpreter.

    This process cached its failed imports, and its sys.path never gained a site-packages that
    was missing at start, so it cannot see the rebuilt environment.
    """
    python = _interpreter(Path(hub))
    python = str(python) if python.is_file() else sys.executable
    try:
        result = _run([python, "-B", "-c", RECHECK, DRAWER, hub, name], hub, env, 300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _record(name, "FAIL", "the recheck did not run", f"{type(exc).__name__}: {exc}")
    lines = [line for line in (result.stdout or "").splitlines() if line.strip()]
    try:
        found = json.loads(lines[-1])
        return _record(found["name"], found["status"], found["reason"], found["detail"])
    except (IndexError, ValueError, KeyError, TypeError):
        return _record(name, "FAIL", f"the recheck did not run (exit {result.returncode})",
                       _tail(result.stderr or result.stdout))


# ------------------------------------------------------------------- inventory


def inventory(hub) -> tuple[Component, ...]:
    """Every component this hub can smoke, in a fixed order."""
    return (
        Component("memory", _applies_always, _check_memory, _repair_memory,
                  "run `uv sync` at the hub root, then seat.py configure"),
        Component("smoke", _applies_smoke, _check_smoke, None,
                  "fix what the smoke command reports; the engine pack owns it"),
    )


def check_all(hub, machine, only: Iterable[str] | None = None) -> list[dict]:
    """Check every component that applies to this machine, in inventory order."""
    root = Path(hub)
    wanted = None if only is None else ({only} if isinstance(only, str) else set(only))
    env = _probe_env(root)
    records = []
    for component in inventory(root):
        if wanted is not None and component.name not in wanted:
            continue
        skipped = component.applies(machine)
        if skipped:
            records.append(_record(component.name, "SKIPPED", skipped))
            continue
        try:
            records.append(component.check(root, machine, env))
        except Exception as exc:
            records.append(_record(component.name, "FAIL",
                                   f"the check did not run ({type(exc).__name__})", exc))
    return records


def summary(records: Iterable[Mapping]) -> dict[str, int]:
    """The counts per status. There is no aggregate boolean; the caller decides."""
    counts = {status: 0 for status in STATUSES}
    for record in records:
        status = str(record.get("status"))
        if status in counts:
            counts[status] += 1
    return counts


# ---------------------------------------------------------------- repair ledger


def ledger_path(hub) -> Path:
    return profile.default_profile_path(hub).with_name("repairs.json")


def _ledger_key(name: str, reason: str) -> str:
    return f"{name}:{hashlib.sha256(str(reason).encode('utf-8')).hexdigest()[:16]}"


def _read_ledger(hub) -> dict:
    """A truncated or invalid ledger reads as empty: a lost record must not block a repair."""
    try:
        data = json.loads(ledger_path(hub).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_ledger(hub, ledger: Mapping) -> None:
    path = ledger_path(hub)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(ledger), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _failed_today(entry) -> bool:
    """A refusal lasts the local day it was recorded."""
    if not isinstance(entry, Mapping) or entry.get("result") != "failed":
        return False
    try:
        attempted = datetime.fromisoformat(str(entry.get("attempted_at")))
    except ValueError:
        return False
    return attempted.astimezone().date() == datetime.now().astimezone().date()


def clear_refusals(hub, name: str) -> int:
    """Drop every remembered repair of one component; return how many entries went."""
    ledger = _read_ledger(Path(hub))
    kept = {key: entry for key, entry in ledger.items() if not key.startswith(f"{name}:")}
    if len(kept) != len(ledger):
        _write_ledger(Path(hub), kept)
    return len(ledger) - len(kept)


def repair(hub, name: str, record: Mapping) -> dict:
    """Run one component's repair, then rerun its check and return the new record.

    A PASS or SKIPPED record is returned unchanged. A component with no automated repair
    answers BLOCKED. A repair already recorded as failed today for the same reason raises
    RepeatRefused.
    """
    root = Path(hub)
    if record.get("status") in ("PASS", "SKIPPED"):
        return dict(record)
    component = next((item for item in inventory(root) if item.name == name), None)
    if component is None:
        return _record(name, "BLOCKED", "no such component", record.get("detail", ""))
    if component.repair is None:
        return _record(name, "BLOCKED", "no automated repair",
                       f"{component.agent_op}. {record.get('reason', '')}".strip())
    reason = str(record.get("reason") or "")
    key = _ledger_key(name, reason)
    ledger = _read_ledger(root)
    if _failed_today(ledger.get(key)):
        raise RepeatRefused(
            f"{name}: this repair already failed today for the same reason ({reason}). It may run "
            f"again tomorrow; `seat.py repair {name} --clear` ends the refusal sooner.")
    attempted_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    env = _probe_env(root)
    try:
        note = component.repair(root, record, env)
    except Exception as exc:
        note = f"the repair did not run ({type(exc).__name__}: {exc})"
    rechecked = _recheck(root, name, env)
    ledger[key] = {"name": name, "reason": reason, "attempted_at": attempted_at,
                   "result": "repaired" if rechecked["status"] == "PASS" else "failed"}
    _write_ledger(root, ledger)
    if rechecked["status"] != "PASS" and note:
        rechecked["detail"] = _scrub(f"repair: {note}\n{rechecked['detail']}".strip())
    return rechecked
