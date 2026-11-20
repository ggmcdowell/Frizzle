"""Replay: the State section of Flight/SCRATCHPAD.md reached 86 lines past its 80-line cap (flight skill section 6)
with no code to stop it, and Flight/log.jsonl grew with no cap. Fix: flight-hooks.py Stop blocks once and the
bearings print STATE OVER CAP with the count past STATE_CAP lines (40); each log append keeps the newest LOG_KEEP rows.

The replay runs the hub's hooks on a fake Flight folder: an 86-line State must block Stop and name 86, and a
log past LOG_KEEP must keep only the newest rows.
"""
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from unittest import mock

INCIDENT = "the Flight State section passed its 80-line cap and the comms log grew with no cap"


def replay(hub, product):
    spec = importlib.util.spec_from_file_location("flight_hooks_replay", hub / "Drawer" / "flight-hooks.py")
    hooks = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hooks)
    now = datetime.now()
    # Hermetic: a card-run leg inherits the Flight session's env, and the live watcher state and rows are not
    # this replay's (incident befb286b).
    with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ), \
            mock.patch.object(hooks, "EVENTS_LOCK", Path(tmp) / "events-flight.lock"), \
            mock.patch.object(hooks, "HANDOFF", Path(tmp) / "flight-handoff"):
        os.environ.pop(hooks.ONESHOT_VAR, None)
        base = Path(tmp)
        rows = "".join(f"- row {n}\n" for n in range(84))
        (base / "SCRATCHPAD.md").write_text(f"## State\nSlack seen: 1 read {now:%Y-%m-%dT%H:%M}\n{rows}## Notes\n")
        out = hooks.stop(base, {"stop_hook_active": False}, now)
        if '"block"' not in out or "86 lines" not in out:
            return f"an 86-line State did not block Stop with its count: {out!r}"
        keep, hooks.LOG_KEEP = hooks.LOG_KEEP, 3
        try:
            for n in range(6):
                hooks.log(base, {"event": "note", "n": n}, now)
        finally:
            hooks.LOG_KEEP = keep
        kept = [json.loads(line)["n"] for line in (base / "log.jsonl").read_text().splitlines()]
        if kept != [3, 4, 5]:
            return f"the log kept {kept}, not the newest 3 rows"
    return None
