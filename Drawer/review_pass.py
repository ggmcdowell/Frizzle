"""review_pass -- the stored risky-review Pass and the task tag read that the risky land gate uses.

    <python> Drawer/review_pass.py record --hash <sha256> --head <sha> --diff <diff file> --round <n> --reviewer <model>

record writes <[project] state_root>/review/pass/<hash>.json {hash, head, verdict: Pass, round, reviewer, recorded_at}
only when the diff file exists and the SHA-256 of its bytes equals --hash, so the hash came from a
Drawer/review-diff.py diff. It prints one JSON line: {state: recorded, hash, path} exit 0, or {state: refused,
error} exit 2. risky-review.js runs it on each Pass. hub-land.py and product-push.py refuse a land of a task tagged risky
unless --reviewed <hash> is the current diff hash and stored() finds its Pass row. task_tags() reads a task's tags (the
tags field and the body's Tags: line) by Drawer/mem/mem.py get <task> --json.
The store proves the hash came from a review diff, not who wrote the row: the gate stops a forgotten review, not a
forged one.
Why: the risky review was prose only, and risky cards landed with no review.
Owner: Harness.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

DRAWER = Path(__file__).resolve().parent
HASH = re.compile(r"[0-9a-f]{64}")


class Refused(Exception):
    pass


class NoCard(Refused):
    """mem has no card for the task."""


def folder(state_root):
    return Path(state_root).expanduser() / "review" / "pass"


def record(state_root, digest, head, diff, round_, reviewer):
    """Write the Pass row of digest; Refused unless diff exists and its bytes hash to digest."""
    if not HASH.fullmatch(str(digest)):
        raise Refused("--hash takes the 64-hex SHA-256 from Drawer/review-diff.py")
    if not re.fullmatch(r"[0-9a-f]{40}", str(head)):
        raise Refused("--head takes a 40-hex commit sha")
    if not isinstance(round_, int) or round_ < 1:
        raise Refused("--round takes a whole number from 1")
    if not str(reviewer).strip():
        raise Refused("--reviewer takes the reviewer leg's model")
    try:
        data = Path(diff).read_bytes()
    except OSError as exc:
        raise Refused(f"no diff file {diff}: {exc.strerror or exc}") from None
    if hashlib.sha256(data).hexdigest() != digest:
        raise Refused(f"the diff file {diff} does not hash to {digest[:12]}")
    out = folder(state_root)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{digest}.json"
    row = {"hash": digest, "head": head, "verdict": "Pass", "round": round_, "reviewer": str(reviewer).strip(),
           "recorded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
    part = path.with_suffix(".part")
    part.write_text(json.dumps(row) + "\n", encoding="utf-8")
    os.replace(part, path)
    return path


def stored(state_root, digest):
    """The stored Pass row of digest, else None."""
    if not HASH.fullmatch(str(digest)):
        return None
    try:
        row = json.loads((folder(state_root) / f"{digest}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return row if isinstance(row, dict) and row.get("verdict") == "Pass" and row.get("hash") == digest else None


def task_tags(hub, task):
    """The task's tags, lower case: its tags field, then each word of a Tags: line in its body. NoCard when mem has
    no such card; Refused on any other mem fault."""
    try:
        run = subprocess.run([sys.executable, "-B", str(Path(hub) / "Drawer" / "mem" / "mem.py"), "get", task, "--json"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refused(f"mem get {task} did not run: {exc}") from None
    try:
        row = json.loads(run.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise Refused(f"mem get {task} exit {run.returncode}: {(run.stdout + run.stderr).strip()[-300:]}") from None
    if not isinstance(row, dict):
        raise Refused(f"mem get {task} gave no row")
    if "error" in row:
        error = str(row["error"])
        raise (NoCard if error.startswith("no card matches") else Refused)(f"mem get {task}: {error}")
    tags = [str(t).strip().lower() for t in row.get("tags") or []]
    line = re.search(r"(?mi)^tags:(.*)$", str(row.get("body") or ""))
    words = re.split(r"[\s,]+", line.group(1).strip().lower()) if line else []
    return [t for t in dict.fromkeys(tags + words) if t]


def gate(state_root, risky, reviewed, found, task):
    """Why the land of found (the review-diff args) waits for review, else None. Any task: --reviewed must be
    the current hash. A risky task: --reviewed is required and its Pass must be stored."""
    if reviewed and found["hash"] != reviewed:
        return "the diff to land is not the one reviewed; review args, then land again with the new hash"
    if risky and not reviewed:
        return f"task {task} is tagged risky: run risky-review on these args, then land with --reviewed <hash>"
    if risky and stored(state_root, found["hash"]) is None:
        return f"no stored risky-review Pass for {found['hash'][:12]}; run risky-review on these args, then land again"
    return None


def main(argv=None, state_root=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv in (["--help"], ["-h"]):
        print(__doc__)
        return 0
    parser = argparse.ArgumentParser(prog="review_pass.py")
    sub = parser.add_subparsers(dest="verb", required=True)
    rec = sub.add_parser("record")
    for flag in ("--hash", "--head", "--diff", "--reviewer"):
        rec.add_argument(flag, required=True)
    rec.add_argument("--round", type=int, required=True)
    args = parser.parse_args(argv)
    if state_root is None:
        sys.path.insert(0, str(DRAWER))
        import harness_config
        state_root = harness_config.path(harness_config.load(DRAWER.parent)["project"]["state_root"])
    try:
        path = record(state_root, args.hash.lower(), args.head.lower(), args.diff, args.round, args.reviewer)
    except Refused as exc:
        print(json.dumps({"state": "refused", "error": str(exc)}))
        return 2
    print(json.dumps({"state": "recorded", "hash": args.hash.lower(), "path": str(path)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
