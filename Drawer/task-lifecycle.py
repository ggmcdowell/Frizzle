#!/usr/bin/env python3
"""Task lifecycle: one checked path to begin, close and steer a mem task.

Each verb runs Drawer/mem/mem.py with --json, so a task worktree reaches the live hub as mem
does. It prints one JSON line; a refusal prints {"error": ...} and exits non-zero.

    .venv/bin/python -B Drawer/task-lifecycle.py begin <id> [--session S] [--model M] [--effort E] [--worktree W]
    .venv/bin/python -B Drawer/task-lifecycle.py complete <id> --claim-id C --evidence '<4+ words naming the sha>'
        (--commit <repo>=<full sha> ... [--path <repo-relative path> ...] | --no-file-changes '<why>') [--lesson L]
    <repo> is a checkout path, or a name: hub (the hub checkout this command runs in) or product (the product repo of
    [product] path in harness.toml, or the session's own product worktree).
    .venv/bin/python -B Drawer/task-lifecycle.py release <id> --claim-id C [--reason R]
    .venv/bin/python -B Drawer/task-lifecycle.py interrupt <id> [--reason R]
    .venv/bin/python -B Drawer/task-lifecycle.py note <id> '<text>' [--stop] [--session S]
    .venv/bin/python -B Drawer/task-lifecycle.py check-close <id>

begin claims the task and prints its claim_id, card and Done-when. complete is mem complete: the live claim_id,
evidence that names a cited sha and labels each critical claim ([device], [sim], [test] or [unverified]; WRK-4),
a task_delivery pass (each sha on origin/main); --lesson adds a lesson row with no commit id. check-close prints the task state, exit 0: CLOSED for a done task with delivered=1 or a dropped one, NO-CLAIM for
an open task no session holds; {error} with the state (exit 1) while a claim is held or a done task has delivered=0.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_config  # noqa: E402

MEM = Path(__file__).resolve().parent / "mem" / "mem.py"
PROG = ".venv/bin/python -B Drawer/task-lifecycle.py"
DONE_WHEN = re.compile(r"\bDone when:\s*(.+?)(?:\n|$)", re.I)
STATE_KEYS = ("id", "title", "status", "claim_id", "claim_session", "worktree", "evidence", "commits", "closed_at")


class Refused(Exception):
    def __init__(self, value: dict, code: int):
        super().__init__(value.get("error", ""))
        self.value, self.code = value, code


def mem(*argv: str) -> dict | list:
    """Run one mem verb with --json; a refusal raises Refused with mem's {error} and exit code. The timeout leaves
    each cited commit its 120 s delivery fetch."""
    try:
        run = subprocess.run([sys.executable, "-B", str(MEM), *argv, "--json"], capture_output=True, text=True,
                             timeout=120 + 150 * max(1, argv.count("--commit")))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refused({"error": f"mem {argv[0]} did not run: {type(exc).__name__}"}, 1) from exc
    lines = run.stdout.strip().splitlines()
    try:
        value = json.loads(lines[-1]) if lines else None
    except ValueError:
        value = None
    if value is None:
        why = (run.stderr.strip() or run.stdout.strip())[-300:] or f"exit {run.returncode}"
        raise Refused({"error": f"mem {argv[0]} gave no JSON line: {why}"}, run.returncode or 1)
    if run.returncode or (isinstance(value, dict) and "error" in value):
        raise Refused(value if isinstance(value, dict) and "error" in value
                      else {"error": f"mem {argv[0]} exit {run.returncode}"}, run.returncode or 1)
    return value


REPO_NAMES = ("hub", "product")


def repo_root(name: str) -> str:
    """The checkout a repo name stands for: hub is the hub root above the cwd, product is the product's repo."""
    if name == "hub":
        try:
            return str(harness_config.hub_root())
        except SystemExit as exc:
            raise Refused({"error": f"repo name hub: {exc}"}, 1) from exc
    _, project = harness_config.product_path(harness_config.load())
    if not project:
        raise Refused({"error": "repo name product: no product repo is set ([product] path is empty)"}, 1)
    run = subprocess.run(["git", "-C", str(harness_config.path(project)), "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True, env=harness_config.git_env())
    if run.returncode:
        raise Refused({"error": f"repo name product: {project} is not in a Git repo"}, 1)
    return run.stdout.strip()


def resolve_commit(cite: str) -> str:
    """<repo>=<sha> with a repo name swapped for its checkout path; a path passes as given. A bare word that is
    no repo name is refused, so a typo never reads as a relative path."""
    repo, sep, sha = cite.rpartition("=")
    if not sep or any(c in repo for c in "/\\~") or repo in (".", ".."):
        return cite
    if repo not in REPO_NAMES:
        raise Refused({"error": f"unknown repo name {repo!r} in --commit: use {' or '.join(REPO_NAMES)}, "
                                "or a checkout path"}, 1)
    return f"{repo_root(repo)}={sha}"


def opt(flag: str, value) -> list[str]:
    return [flag, value] if value else []


def state(card: dict) -> dict:
    return {**{k: card[k] for k in STATE_KEYS if k in card}, "delivered": bool(card.get("delivered"))}


def begin(a) -> dict:
    card = mem("claim", a.id, *opt("--session", a.session), *opt("--model", a.model), *opt("--effort", a.effort),
               *opt("--worktree", a.worktree))
    match = DONE_WHEN.search(card.get("body", ""))
    return {"result": "CLAIMED", "id": card["id"], "claim_id": card["claim_id"],
            "claim_session": card.get("claim_session"), "title": card.get("title"), "card": card.get("body"),
            "done_when": match.group(1).strip() if match else None,
            "next": "complete with --claim-id, evidence and each pushed repo=sha; or release"}


def check_close(a) -> dict:
    card = mem("get", a.id)
    if card.get("kind") != "task":
        raise Refused({"error": f"{card['id']} is a {card.get('kind')}, not a task"}, 1)
    s = state(card)
    if card.get("claim_id"):
        raise Refused({"error": f"task {card['id']} still holds claim {card['claim_id']}: complete it with "
                                "evidence or release it before DONE", **s}, 1)
    if card.get("status") == "done" and not card.get("delivered"):
        raise Refused({"error": f"task {card['id']} is done with no delivery check (delivered=0): "
                                "it was closed outside complete", **s}, 1)
    return {"result": "CLOSED" if card.get("status") in ("done", "dropped") else "NO-CLAIM", **s}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog=PROG, description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("begin", help="claim a task: prints claim_id, card and Done-when")
    s.add_argument("id"); s.add_argument("--session"); s.add_argument("--model"); s.add_argument("--effort")
    s.add_argument("--worktree")
    s = sub.add_parser("complete", help="the checked close")
    s.add_argument("id"); s.add_argument("--claim-id", required=True); s.add_argument("--evidence", required=True)
    s.add_argument("--commit", action="append", default=[], metavar="REPO=SHA",
                   help="REPO is hub, product or a checkout path; SHA is the full pushed sha")
    s.add_argument("--path", action="append", default=[], metavar="PATH")
    s.add_argument("--no-file-changes", metavar="WHY"); s.add_argument("--lesson")
    s = sub.add_parser("release", help="give the claim back: the task goes to todo")
    s.add_argument("id"); s.add_argument("--claim-id", required=True); s.add_argument("--reason")
    s = sub.add_parser("interrupt", help="park a task whose claim session is dead")
    s.add_argument("id"); s.add_argument("--reason")
    s = sub.add_parser("note", help="write a task_log note; --stop is the steer to halt")
    s.add_argument("id"); s.add_argument("text"); s.add_argument("--stop", action="store_true")
    s.add_argument("--session")
    s = sub.add_parser("check-close", help="print the task state; refuse a held claim or an unchecked done")
    s.add_argument("id")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "begin":
            out = begin(a)
        elif a.cmd == "complete":
            card = mem("complete", a.id, "--claim-id", a.claim_id, "--evidence", a.evidence,
                       *[x for c in a.commit for x in ("--commit", resolve_commit(c))], *[x for p in a.path for x in ("--path", p)],
                       *opt("--no-file-changes", a.no_file_changes), *opt("--lesson", a.lesson))
            out = {"result": "COMPLETED", **state(card), **({"lesson": card["lesson"]} if card.get("lesson") else {})}
        elif a.cmd == "release":
            out = {"result": "RELEASED", **state(mem("release", a.id, "--claim-id", a.claim_id,
                                                     *opt("--reason", a.reason)))}
        elif a.cmd == "interrupt":
            out = {"result": "INTERRUPTED", **state(mem("interrupt", a.id, *opt("--reason", a.reason)))}
        elif a.cmd == "note":
            out = {"result": "NOTED", **mem("note", a.id, a.text, *(["--stop"] if a.stop else []),
                                            *opt("--session", a.session))}
        else:
            out = check_close(a)
    except Refused as exc:
        print(json.dumps(exc.value, ensure_ascii=False, separators=(",", ":")))
        return exc.code
    print(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
