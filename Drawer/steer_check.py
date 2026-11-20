"""The steer check: an irreversible step reads Flight's STOP first, and pauses while one holds.

Run it right before an irreversible step (an upload, a release, a delete, a push to a shared
branch). Flight sets and lifts a STOP:

    <python> Drawer/steer_check.py check <session> --step <what>
    <python> Drawer/steer_check.py stop <session> <why> [--step <what>]
    <python> Drawer/steer_check.py go <session>
    <python> Drawer/steer_check.py status

check pauses (PAUSE line, exit 75) when Flight holds a STOP for the subject (a STOP with --step pauses only
a check whose --step names that step; one with no step pauses every step). The subject <session> is a session
(its uuid, uuid8 or name BOX-SKILL-<uuid8>) or a task (its uuid or task8): each matches on the first 8-hex token.
A task8 STOP holds one task's push while legs share one session id:
hub-land.py and product-push.py take --task <task8> and check it beside the session.
go lifts the subject's STOP. Each pause appends a row to state_root/steer/pauses.jsonl;
Drawer/flight-watchdog.py events prints it as PAUSE <uuid8> <step>: <why>.

Exit codes: 0 = CLEAR or done, 1 = usage error, 75 = PAUSE.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_config  # noqa: E402
import oscompat  # noqa: E402

HUB = Path(__file__).resolve().parents[1]
PAUSE = 75


def folder(cfg):
    """The steer state folder under [project] state_root."""
    return harness_config.path(cfg["project"]["state_root"]) / "steer"


def read_state(cfg):
    try:
        row = json.loads((folder(cfg) / "steer.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        row = {}
    row = row if isinstance(row, dict) else {}
    return {"stops": dict(row.get("stops") or {})}


def write_state(cfg, row):
    file = folder(cfg) / "steer.json"
    file.parent.mkdir(parents=True, exist_ok=True)
    part = file.with_name("%s.%d.tmp" % (file.name, os.getpid()))
    part.write_text(json.dumps(row) + "\n", encoding="utf-8", newline="\n")
    oscompat.replace(part, file)


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


UUID8_RE = re.compile(r"(?<![0-9a-f])[0-9a-f]{8}(?![0-9a-f])")


def uuid8(session):
    """The uuid8 of a session uuid, uuid8 or name (BOX-SKILL-<uuid8>); else its first 8 characters."""
    found = UUID8_RE.findall(str(session))
    return found[0] if found else str(session)[:8]


def check_all(cfg, subjects, step, now=None):
    """None when the step is clear for every subject (sessions and task8s), else the why of the first pause."""
    for subject in dict.fromkeys(uuid8(s) for s in subjects if s):
        why = check(cfg, subject, step, now)
        if why:
            return why
    return None


def check(cfg, session, step, now=None):
    """None when the step is clear, else the why of its pause (the pause row is written)."""
    s8 = uuid8(session)
    held = read_state(cfg)["stops"].get(s8)
    if held is None or held.get("step") not in (None, step):
        return None
    why = f"Flight holds a STOP for {s8}{' at ' + held['step'] if held.get('step') else ''}: {held.get('why', '')}"
    file = folder(cfg) / "pauses.jsonl"
    file.parent.mkdir(parents=True, exist_ok=True)
    with file.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"t": now or time.time(), "session": s8, "step": step, "why": why}) + "\n")
    return why


def pauses(cfg):
    """Every pause row, oldest first."""
    try:
        text = (folder(cfg) / "pauses.jsonl").read_text(encoding="utf-8")
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def stop(cfg, session, why, step=None, now=None):
    """Set a STOP for the session: for every step, or with step only for a check whose --step names it."""
    row = read_state(cfg)
    row["stops"][uuid8(session)] = {"why": why, "t": now or time.time(), **({"step": step} if step else {})}
    write_state(cfg, row)


def go(cfg, session):
    row = read_state(cfg)
    row["stops"].pop(uuid8(session), None)
    write_state(cfg, row)


def main(argv, cfg=None):
    if argv in (["--help"], ["-h"]):
        print(__doc__)
        return 0
    cfg = cfg or harness_config.load(HUB)
    if argv[:1] == ["check"] and len(argv) >= 2:
        args, step = argv[2:], "an irreversible step"
        while args:
            if args[0] == "--step" and len(args) > 1:
                step = args[1]
            else:
                print(__doc__)
                return 1
            args = args[2:]
        why = check(cfg, argv[1], step)
        if why:
            print(f"PAUSE {uuid8(argv[1])} {step}: {why}. Do not run the step; send Flight NEED steer {step}.")
            return PAUSE
        print(f"CLEAR {uuid8(argv[1])} {step}: no STOP.")
        return 0
    if argv[:1] == ["stop"] and len(argv) >= 3:
        words, step = argv[2:], None
        if "--step" in words:
            at = words.index("--step")
            if at + 1 >= len(words):
                print(__doc__)
                return 1
            step, words = words[at + 1], words[:at] + words[at + 2:]
        if not words:
            print(__doc__)
            return 1
        stop(cfg, argv[1], " ".join(words), step)
        print(f"STOP set for {uuid8(argv[1])}{' at ' + step if step else ''}.")
        return 0
    if argv[:1] == ["go"] and len(argv) == 2:
        go(cfg, argv[1])
        print(f"GO {uuid8(argv[1])}: no STOP.")
        return 0
    if argv == ["status"]:
        row = read_state(cfg)
        for s8, stop_row in sorted(row["stops"].items()):
            at = f" at {stop_row['step']}" if stop_row.get("step") else ""
            print(f"STOP {s8}{at} since {iso(float(stop_row.get('t', 0)))}: {stop_row.get('why', '')}")
        print(f"{len(row['stops'])} STOP(s).")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
