"""Smoke-owned child processes: spawn, record, close, reclaim.

A smoke run starts real helper processes (a probe
server, a tool the smoke command needs). Each one is recorded beside the runtime profile so a run that
dies still leaves a way to take its children down. A leftover is terminated only when the
recorded creation identity still names the same process; a bare PID never authorizes a
kill, and no child is ever matched by executable name.

Standard library only: this module runs under any seat interpreter, before the hub
environment (the root .venv) exists.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

DRAWER = Path(__file__).resolve().parents[1]
if str(DRAWER) not in sys.path:
    sys.path.insert(0, str(DRAWER))
import gatelib  # noqa: E402
import oscompat  # noqa: E402
from runtime import filelock, profile  # noqa: E402

RECORD_NAME = "smoke-children.json"
LOCK_NAME = "smoke-children.lock"
LOCK_SECONDS = 15.0
TEMP_PREFIX = "hub-smoke-"
STOP_SECONDS = 5.0
REMOVE_SECONDS = 2.0
CREATION_IDENTITIES = ("proc-start:",)
# What one reclaim attempt proved about the recorded process.
TERMINATED = "terminated"
MISMATCH = "mismatch"
UNCERTAIN = "uncertain"

# Children this process started, by PID: the entry as recorded plus its live handle.
_OWNED: dict[int, tuple[dict, subprocess.Popen]] = {}


def record_path(hub: str | Path) -> Path:
    """The child record, beside this checkout's runtime profile."""
    return profile.default_profile_path(hub).with_name(RECORD_NAME)


def lock_path(hub: str | Path) -> Path:
    return profile.default_profile_path(hub).with_name(LOCK_NAME)


def _record_lock(hub: str | Path) -> filelock.FileLock:
    """The one exclusive lock on the child record; a busy record raises LockBusy."""
    return filelock.FileLock(lock_path(hub), LOCK_SECONDS)


def log_path(temp_root: str | os.PathLike[str], name: str) -> Path:
    """The child's combined output log inside its own smoke scratch root."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(name)) or "child"
    return Path(temp_root) / f"{safe}.log"


def _darwin_started(pid: int) -> str | None:
    """A Darwin start-time identity from ps lstart, or None when it is not readable."""
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="],
                                env={**os.environ, "LC_ALL": "C"}, capture_output=True,
                                text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = " ".join((result.stdout or "").split()) if result.returncode == 0 else ""
    if not text:
        return None
    try:
        # ps pads the day of month with a space, so the whitespace is normalised first.
        return "proc-start:%d" % int(time.mktime(time.strptime(text, "%a %b %d %H:%M:%S %Y")))
    except (ValueError, OverflowError):
        return None


def _pid_only(pid: int) -> str | None:
    return "pid-only:%d" % pid if gatelib.pid_state(pid) == "live" else None


def process_identity(pid: int) -> str | None:
    """A PID-reuse-resistant identity for one process, or None when the OS hides it.

    "proc-start:<n>" on Darwin, Linux and Windows (creation time), "pid-only:<n>" elsewhere. Darwin reads ps lstart
    at second precision, so a PID reused inside the same second is the accepted residual risk.
    """
    pid = int(pid)
    if pid <= 0:
        return None
    if oscompat.WINDOWS:
        started = oscompat.process_started(pid)
        if started is None:
            return _pid_only(pid)
        # A process that exited keeps its creation time while any handle to it stays open (a
        # Popen's own, say): it has no identity any more, as a reaped POSIX process has none.
        return "proc-start:%d" % started if gatelib.pid_state(pid) != "gone" else None
    if sys.platform == "darwin":
        return _darwin_started(pid) or _pid_only(pid)
    try:
        # Linux field 22 is the start time; the comm field may hold spaces, so split after ')'.
        raw = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
        return "proc-start:%s" % raw[raw.rfind(")") + 2:].split()[19]
    except (OSError, IndexError):
        return _pid_only(pid)


def _smoke_temp(temp_root: str | os.PathLike[str] | None) -> Path | None:
    """The path when it is a smoke scratch directory under the system temp root, else None."""
    if not temp_root:
        return None
    try:
        candidate = Path(temp_root).resolve()
        base = Path(tempfile.gettempdir()).resolve()
    except OSError:
        return None
    if not candidate.name.startswith(TEMP_PREFIX):
        return None
    return candidate if base in candidate.parents else None


def _read(hub: str | Path) -> list[dict]:
    """The recorded children; an unreadable or malformed record reads as empty."""
    try:
        data = json.loads(record_path(hub).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [entry for entry in data if isinstance(entry, dict)]


def _write(hub: str | Path, entries: Sequence[Mapping]) -> None:
    path = record_path(hub)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([dict(entry) for entry in entries], indent=2) + "\n", encoding="utf-8")


def spawn(hub: str | Path, name: str, argv: Sequence[str], cwd: str | Path,
          env: Mapping[str, str], temp_root: str | os.PathLike[str]) -> subprocess.Popen:
    """Start one smoke child in its own group and record it before returning.

    The child heads its own group (oscompat.popen_group) so a Ctrl-C in the smoke run does not race the shutdown
    below. Its stdin is closed and its output goes to one log in its scratch root: a child
    holding this process's pipes open would hang the run, and a check that times out still has
    the child's own words.
    """
    root = _smoke_temp(temp_root)
    if root is None:
        raise ValueError(
            f"a smoke temp root must be a {TEMP_PREFIX}* directory under "
            f"{tempfile.gettempdir()}: {temp_root}")
    root.mkdir(parents=True, exist_ok=True)
    with log_path(root, name).open("ab") as stream:
        process = subprocess.Popen(
            [str(item) for item in argv], cwd=str(cwd), env=dict(env),
            stdin=subprocess.DEVNULL, stdout=stream, stderr=stream, **oscompat.popen_group())
    entry = {
        "name": str(name),
        "pid": process.pid,
        "process_identity": process_identity(process.pid),
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "temp_root": str(root),
    }
    with _record_lock(hub):
        entries = _read(hub)
        entries.append(entry)
        _write(hub, entries)
    _OWNED[process.pid] = (entry, process)
    return process


def _remove_temp(temp_root: str | os.PathLike[str] | None) -> str:
    """Remove one smoke scratch root and say what really happened.

    A just-terminated child still holds its log for a moment, so the removal retries for
    a bounded time. A root a living process still holds is reported, never claimed.
    """
    root = _smoke_temp(temp_root)
    if root is None:
        return "" if not temp_root else "; scratch left in place (not a smoke temp root)"
    deadline = time.monotonic() + REMOVE_SECONDS
    while True:
        shutil.rmtree(root, ignore_errors=True)
        if not root.exists():
            return "; scratch removed"
        if time.monotonic() >= deadline:
            return "; scratch still in use"
        time.sleep(0.1)


def _signal_owned(process: subprocess.Popen, force: bool) -> None:
    """Stop an owned child. Windows stops its whole tree: terminate and kill reach only the direct child,
    and a launcher stub's real worker would keep the scratch log open."""
    if oscompat.WINDOWS:
        try:
            oscompat.kill_tree(process.pid)
            return
        except OSError:
            pass
    process.kill() if force else process.terminate()


def _stop_owned(entry: Mapping, process: subprocess.Popen) -> str:
    name = entry.get("name") or "child"
    if process.poll() is None:
        _signal_owned(process, force=False)
        try:
            process.wait(timeout=STOP_SECONDS)
        except subprocess.TimeoutExpired:
            _signal_owned(process, force=True)
            try:
                process.wait(timeout=STOP_SECONDS)
            except subprocess.TimeoutExpired:
                return f"{name}: pid {process.pid} did not stop{_remove_temp(entry.get('temp_root'))}"
    return f"{name}: closed pid {process.pid} (exit {process.poll()}){_remove_temp(entry.get('temp_root'))}"


def close_all(hub: str | Path) -> list[str]:
    """Stop every child this process started, drop its record, and remove its scratch."""
    owned = list(_OWNED.values())
    _OWNED.clear()
    if not owned:
        return []
    notes = [_stop_owned(entry, process) for entry, process in owned]
    closed = {(entry.get("pid"), entry.get("started_at")) for entry, _ in owned}
    with _record_lock(hub):
        _write(hub, [entry for entry in _read(hub)
                     if (entry.get("pid"), entry.get("started_at")) not in closed])
    return notes


def _posix_compare(pid: int, identity: str) -> str:
    """TERMINATED-shaped facts for one read: the same process, a proven absence or change,
    or an identity the OS would not show (which never authorizes a kill).

    A live PID whose identity now reads as pid-only is uncertain, not a mismatch: the OS
    withheld the creation time this time, so nothing proves the recorded child is gone.
    """
    current = process_identity(pid)
    if current == identity:
        return "same"
    if current is None:
        return MISMATCH if gatelib.pid_state(pid) == "gone" else UNCERTAIN
    return MISMATCH if current.startswith(CREATION_IDENTITIES) else UNCERTAIN


def _reap(pid: int) -> None:
    """Reap the zombie when it is this process's own child; a stranger's belongs to its parent.

    A terminated child of this process keeps its PID and start time until it is waited for,
    so the identity would read "same" forever and a SIGKILL on its empty group fails.
    """
    if oscompat.WINDOWS:  # no zombies: a dead process is gone at once
        return
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


def _terminate_verified(pid: int, identity: str) -> str:
    """TERMINATED, MISMATCH (a proven exit or another process), or UNCERTAIN."""
    state = _posix_compare(pid, identity)
    if state != "same":
        return state
    try:
        # The child heads its own group (oscompat.popen_group); Windows stops the tree at once.
        oscompat.term_tree(pid)
    except OSError:
        return UNCERTAIN
    deadline = time.monotonic() + STOP_SECONDS
    while time.monotonic() < deadline:
        _reap(pid)
        if _posix_compare(pid, identity) == MISMATCH:
            return TERMINATED
        time.sleep(0.1)
    # Read the identity again right before the kill: the PID may have been recycled
    # between the last poll and this signal.
    _reap(pid)
    state = _posix_compare(pid, identity)
    if state != "same":
        return state
    try:
        oscompat.kill_tree(pid)
    except OSError:
        return UNCERTAIN
    return TERMINATED


def _reclaim_one(entry: Mapping) -> tuple[str, bool]:
    """One leftover: its note, and whether the record must stay for the next cleanup.

    The scratch belongs to the smoke run, not to whatever now holds the PID, so dropping
    the record without removing it would orphan the directory for good. An uncertain
    outcome keeps both: the record and its scratch wait for the next cleanup.
    """
    name = str(entry.get("name") or "child")
    pid = entry.get("pid")
    identity = entry.get("process_identity")
    scratch = entry.get("temp_root")
    if (not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0
            or not isinstance(identity, str) or not identity.startswith(CREATION_IDENTITIES)):
        return f"{name}: unverified: no creation identity{_remove_temp(scratch)}", False
    outcome = _terminate_verified(pid, identity)
    if outcome == UNCERTAIN:
        return f"{name}: uncertain, record kept for the next cleanup", True
    if outcome == MISMATCH:
        return f"{name}: pid reused or exited, untouched{_remove_temp(scratch)}", False
    return f"{name}: terminated pid {pid}{_remove_temp(scratch)}", False


def reclaim(hub: str | Path) -> list[str]:
    """Take down a previous run's leftovers, one note per recorded child.

    A record without a real creation identity is dropped and never terminated: a bare
    PID cannot tell the recorded child from whatever now holds that number.

    Each entry leaves the record only after its own reclaim, so a run interrupted halfway
    still names every child it has not tried yet. An uncertain outcome stays recorded.
    """
    notes = []
    with _record_lock(hub):
        entries = _read(hub)
        kept: list[Mapping] = []
        for index, entry in enumerate(entries):
            note, keep = _reclaim_one(entry)
            notes.append(note)
            if keep:
                kept.append(entry)
            _write(hub, [*kept, *entries[index + 1:]])
    return notes
