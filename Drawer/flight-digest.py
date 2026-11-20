"""Flight's daily digest: one chat-ready text of at most 15 lines for one local date. It sends nothing.

Flight sends it to the human by PushNotification or a doc link: the first Refresh after 17:00 local (flight
skill, section 6 Refresh).
It reads, all on this box (the main checkout, when run from a task worktree):
  - the commits that landed that date on hub main and on the product repo's origin/main (fetched first);
  - <Flight folder>/log.jsonl: reports and decisions;
  - the Next part of the State section of <Flight folder>/SCRATCHPAD.md.

    <python> Drawer/flight-digest.py [--date YYYY-MM-DD]
"""
import argparse
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import re
import subprocess
import sys

DRAWER = Path(__file__).resolve().parent
sys.path.insert(0, str(DRAWER))
import harness_config  # noqa: E402

HUB = harness_config.main_checkout(DRAWER.parent)
CFG = harness_config.load(HUB)
MAX_LINES = 15
WIDTH = 240  # characters per line, at most


def flight_dir():
    value = os.environ.get(harness_config.env_name("FLIGHT", CFG), "").strip()
    path = Path(value) if value else Path("Flight")
    return path if path.is_absolute() else HUB / path


def product_repo():
    _, value = harness_config.product_path(CFG)
    return harness_config.path(value) if value else None


def git(repo, *args):
    try:
        run = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60,
                             env=harness_config.git_env())
    except (OSError, subprocess.SubprocessError):
        return None
    return run.stdout if run.returncode == 0 else None


def commits(repo, ref, day):
    """[(short hash, subject lead)] of the first-parent commits on ref with a commit date on day; None when unread."""
    if repo is None:
        return None
    out = git(repo, "log", ref, "--first-parent", "--format=%h%x09%s",
              f"--since={day} 00:00", f"--until={day + timedelta(days=1)} 00:00")
    if out is None:
        return None
    return [(h, s.split(":")[0]) for h, s in (line.split("\t", 1) for line in out.splitlines() if "\t" in line)]


def commit_line(label, found):
    if found is None:
        return f"{label}: not read"
    heads = "; ".join(f"{h} {clip(s, 60)}" for h, s in found[:4])
    more = f"; +{len(found) - 4} more" if len(found) > 4 else ""
    return f"{label}: {len(found)}" + (f" ({heads}{more})" if found else "")


def clip(text, width):
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[:width - 1] + "…"


def rows(path, key, day):
    """The JSON rows of a .jsonl file whose `key` stamp falls on day."""
    found = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return found
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and str(row.get(key, "")).startswith(str(day)):
            found.append(row)
    return found


def log_lines(day):
    found = rows(flight_dir() / "log.jsonl", "t", day)
    lines = []
    for event, label in (("report", "Reports"), ("decision", "Decisions")):
        texts = [str(r.get("text") or "") for r in found if r.get("event") == event]
        if texts:
            shown = "; ".join(clip(t, 70) for t in texts[-3:])
            lines.append(f"{label}: {len(texts)}" + (f", last 3: {shown}" if len(texts) > 3 else f" ({shown})"))
        else:
            lines.append(f"{label}: 0")
    return lines


def next_lines():
    """Up to 3 numbered items of the State section's Next part."""
    try:
        text = (flight_dir() / "SCRATCHPAD.md").read_text(encoding="utf-8")
    except OSError:
        return ["Next: no pad"]
    part = re.search(r"^### Next\s*\n(.*?)(?=^#{2,3} |\Z)", text, re.M | re.S)
    items = re.findall(r"^\d+\.\s+(.*(?:\n(?!\d+\.)\s+.*)*)", part.group(1), re.M) if part else []
    if not items:
        return ["Next: none in State"]
    return [f"Next {i}: {clip(item, 150)}" for i, item in enumerate(items[:3], 1)]


def digest(day):
    product = product_repo()
    if product is not None:
        git(product, "fetch", "-q", "origin", "main")
    lines = [f"**Digest**: daily digest {day}",
             commit_line("Hub main commits", commits(HUB, "main", day)),
             commit_line("Product main commits", commits(product, "origin/main", day)),
             *log_lines(day), *next_lines()]
    return "\n".join(clip(line, WIDTH) for line in lines[:MAX_LINES])


def main(argv):
    p = argparse.ArgumentParser(prog="flight-digest.py", description="Print Flight's daily digest; send nothing.")
    p.add_argument("--date", type=date.fromisoformat, default=datetime.now().date(), help="YYYY-MM-DD, default today")
    a = p.parse_args(argv)
    print(digest(a.date))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
