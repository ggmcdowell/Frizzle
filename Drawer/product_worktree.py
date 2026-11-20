#!/usr/bin/env python3
"""Per-task product worktrees: card-run adds them; the session-start hook and rows.py row sweep them.

  <python> Drawer/product_worktree.py add --task <task8> [--session S]
  <python> Drawer/product_worktree.py remove --task <task8>
  <python> Drawer/product_worktree.py sweep

add makes <product repo>_wt/<task8> on branch task/<task8> from origin/main (fetched first; HEAD when the repo
has no origin/main) and locks it 'task <task8> claim <claim8> session <session> <epoch>'. The task must
hold a mem claim; the session is the claim's session, and a --session (else CLAUDE_CODE_SESSION_ID) that
names another session is refused. A second add returns the same tree. A tree whose lock names another
claim than the card's live one holds a dead claim (a card holds one claim): add re-locks it to the live
claim, and its commits and files stay. A branch task/<task8> with no tree takes a new tree on that branch.

remove takes the task's tree when no claim on the card is live (no claim, or its session is dead) and the
tree holds no work but generated files; it deletes the branch only when origin/main holds its patches.

sweep removes each task/<task8> tree and branch only when the task is done or dropped, no claim on it is
live, the branch is patch-equal on origin/main (git cherry shows no + line) and the tree holds no work.
Every other tree stays, named with its reason. A mem fault holds the sweep: nothing is removed.
A removed tree loses its gitignored files (an import cache, local captures), as a session tree does.
The session-start hook (a subprocess under its sweep timeout) and Drawer/rows.py row call sweep.

Every line out is one JSON object (add, remove) or one 'PRODUCT SWEEP:' line. Exit 0 = PASS, 1 = refused.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_config  # noqa: E402

GENERATED = re.compile(r"(^|/)__pycache__/|\.uid$")  # regenerable: Python bytecode, engine uid stamps
TASK8_RX = re.compile(r"^[0-9a-f]{8}$")
BASE = "origin/main"
SWEEPABLE = ("done", "dropped")


class Refused(Exception):
    """A refusal: report it, change nothing more."""


def git(repo, *args, timeout=120):
    """One git run in repo; never prompts for a credential."""
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout,
                          env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))


def first_line(text):
    return (text.strip().splitlines() or [""])[0]


def product_repo(cfg):
    """(main checkout, project folder relative to the repo top) of [product] path, or None when it is
    unset or in no Git checkout."""
    value = str((cfg.get("product") or {}).get("path") or "").strip()
    project = harness_config.path(value).resolve() if value else None
    if project is None or not project.is_dir():
        return None
    top = git(project, "rev-parse", "--show-toplevel")
    if top.returncode:
        return None
    return harness_config.main_checkout(project), project.relative_to(Path(top.stdout.strip()).resolve())


def fetch(main):
    try:
        git(main, "fetch", "--quiet", timeout=60)  # best effort: offline, the last fetched upstream serves
    except subprocess.TimeoutExpired:
        pass


def fast_forward_main(main):
    """Fast-forward the main checkout's branch to its last fetched upstream when no tracked file has
    work; a report, empty when it was already there."""
    if git(main, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        return f"main checkout {main} kept: uncommitted work"
    before = git(main, "rev-parse", "HEAD").stdout.strip()
    merged = git(main, "merge", "--ff-only", "--quiet", "@{upstream}")
    if merged.returncode:
        return f"main checkout {main} kept: {first_line(merged.stderr)}"
    after = git(main, "rev-parse", "HEAD").stdout.strip()
    return "" if after == before else f"main checkout {main} fast-forwarded to {after[:7]}"


def work_in(root):
    """The first changed path in the tree that is not a generated untracked file, a git fault line, or ''."""
    status = git(root, "status", "--porcelain", "--untracked-files=all")
    if status.returncode:
        return first_line(status.stderr) or "git status failed"
    work = [line[3:] for line in status.stdout.splitlines()
            if not (line.startswith("?? ") and GENERATED.search(line[3:]))]
    return f"uncommitted {work[0]}" if work else ""


def force_remove(main, root):
    """Unlock and force-remove the worktree root of main; on a failed remove, re-lock it with its old lock
    reason, so a kept tree stays guarded. The git remove result."""
    held = next((t["locked"] for t in trees(main).values() if t["worktree"].resolve() == Path(root).resolve()), None)
    git(main, "worktree", "unlock", str(root))
    removed = git(main, "worktree", "remove", "--force", str(root))
    if removed.returncode and held is not None:
        git(main, "worktree", "lock", *(["--reason", held] if held else []), str(root))
    return removed


def remove_worktree(main, root, branch):
    """Remove the worktree root of main when its only untracked files are generated and nothing else
    changed, and branch when its upstream holds it; a report."""
    if why := work_in(root):
        return f"product worktree {root} kept: {why}"
    removed = force_remove(main, root)
    if removed.returncode:
        return f"product worktree {root} kept: {first_line(removed.stderr)}"
    deleted = git(main, "branch", "-d", branch)
    return (f"product worktree {root} removed" +
            ("" if deleted.returncode == 0 else f"; branch {branch} kept: {first_line(deleted.stderr)}"))


# ---------- task trees ----------

def open_mem():
    """The mem store the CLI uses: <env_prefix>_MEM_DB, a test run's scratch DB, else the live DB (read-only from a task worktree)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent / "mem"))
    import mem
    return mem.Mem(mem.db_path(mem.config()))


def task_card(store, task8):
    """The task row for task8 (a uuid8 prefix); raises Refused for no card or a non-task."""
    try:
        row = store.find(task8)
    except Exception as error:  # MemError: no card or more than one
        raise Refused(f"task {task8}: {error}")
    if row["kind"] != "task":
        raise Refused(f"card {task8} is a {row['kind']}, not a task")
    return row


def claim_live(store, row):
    """'none', 'live', 'dead' or 'unknown' for the card's claim (mem claim_state; unknown counts live)."""
    return store.claim_state(row) if row["claim_id"] else "none"


def trees(main):
    """{branch: {'worktree': path, 'locked': reason or None}} for every worktree of main on a branch."""
    out = {}
    for block in git(main, "worktree", "list", "--porcelain").stdout.split("\n\n"):
        rows = dict(line.partition(" ")[::2] for line in block.splitlines())
        branch = rows.get("branch", "").removeprefix("refs/heads/")
        if branch and "worktree" in rows:
            out[branch] = {"worktree": Path(rows["worktree"]), "locked": rows.get("locked") if "locked" in rows else None}
    return out


def lock_fields(reason):
    """{'task', 'claim', 'session'} from a 'task <task8> claim <claim8> session <session> <epoch>' lock reason."""
    words = (reason or "").split()
    return {k: words[i + 1] for i, k in enumerate(words[:-1]) if k in ("task", "claim", "session")}


def lock(main, root, task8, row):
    reason = f"task {task8} claim {row['claim_id'][:8]} session {row['claim_session']} {int(time.time())}"
    git(main, "worktree", "unlock", str(root))
    locked = git(main, "worktree", "lock", "--reason", reason, str(root))
    if locked.returncode:
        raise Refused(f"product worktree {root} not locked: {first_line(locked.stderr)}")


def add_task(cfg, store, task8, session=None):
    """Add or re-enter the task's product worktree; a result dict. Raises Refused."""
    if not TASK8_RX.match(task8 or ""):
        raise Refused(f"--task takes a uuid8, not {task8!r}")
    found = product_repo(cfg)
    if found is None:
        raise Refused("no product repo: [product] path is unset or in no Git checkout")
    main, rel = found
    row = task_card(store, task8)
    if not row["claim_id"]:
        raise Refused(f"task {task8} holds no claim: claim it first (mem claim {task8})")
    if session and session != row["claim_session"]:
        raise Refused(f"task {task8} is claimed by session {row['claim_session']}, not {session}")
    root, branch = Path(f"{main}_wt") / task8, f"task/{task8}"
    listed = trees(main).get(branch)
    if listed and not listed["worktree"].exists():
        # A tree deleted by hand: drop its record (prune skips a locked one), then re-enter the branch.
        git(main, "worktree", "unlock", str(listed["worktree"]))
        git(main, "worktree", "prune")
        listed = trees(main).get(branch)
    if listed and listed["worktree"].resolve() != root.resolve():
        raise Refused(f"branch {branch} is checked out at {listed['worktree']}, not {root}")
    state = "kept"
    if listed is None:
        fetch(main)
        base = BASE if git(main, "rev-parse", "--verify", "--quiet", BASE).returncode == 0 else "HEAD"
        has_branch = git(main, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0
        args = [str(root), branch] if has_branch else ["-b", branch, str(root), base]
        # longpaths: a Godot import cache is deep, and Windows stops at 260 characters otherwise.
        added = git(main, "-c", "core.longpaths=true", "worktree", "add", "--quiet", *args)
        if added.returncode:
            raise Refused(f"product worktree {root} not added: {first_line(added.stderr)}")
        try:
            lock(main, root, task8, row)
        except Refused:
            if not has_branch:  # a fresh tree and branch hold nothing yet
                git(main, "worktree", "remove", "--force", str(root))
                git(main, "branch", "-D", branch)
            raise
        state = "re-entered" if has_branch else "added"
    elif lock_fields(listed["locked"]).get("claim") != row["claim_id"][:8]:
        lock(main, root, task8, row)  # the lock names a claim the card no longer holds: a dead claim
        state = "relocked"
    return {"result": "PASS", "state": state, "path": str(root), "project": str(root / rel), "branch": branch,
            "claim": row["claim_id"][:8], "session": row["claim_session"], "main": fast_forward_main(main)}


def patch_equal(main, branch):
    """True when origin/main holds every patch of branch (git cherry shows no + line)."""
    cherry = git(main, "cherry", BASE, branch)
    return cherry.returncode == 0 and not any(line.startswith("+") for line in cherry.stdout.splitlines())


def drop_tree(main, root, branch, landed):
    """Remove a clean tree (root None: no tree), then its branch when landed; (tree gone, report)."""
    parts = []
    if root is not None:
        if why := work_in(root):
            return False, f"product worktree {root} kept: {why}"
        removed = force_remove(main, root)
        if removed.returncode:
            return False, f"product worktree {root} kept: {first_line(removed.stderr)}"
        parts.append(f"product worktree {root} removed")
    if not landed:
        parts.append(f"branch {branch} kept: not on {BASE}")
    else:
        # Patch-equal on origin/main: a rebased push is no ancestor, so -d would refuse it.
        deleted = git(main, "branch", "-D", branch)
        parts.append(f"branch {branch} deleted" if deleted.returncode == 0
                     else f"branch {branch} kept: {first_line(deleted.stderr)}")
    return True, "; ".join(parts)


def remove_task(cfg, store, task8):
    """Remove the task's tree when no claim is live; a result dict. Raises Refused."""
    found = product_repo(cfg)
    if found is None:
        raise Refused("no product repo: [product] path is unset or in no Git checkout")
    main, _ = found
    row = task_card(store, task8)
    if (state := claim_live(store, row)) in ("live", "unknown"):
        raise Refused(f"task {task8} claim {row['claim_id'][:8]} is {state} (session {row['claim_session']}): tree kept")
    branch = f"task/{task8}"
    listed = trees(main).get(branch)
    gone, report = drop_tree(main, listed["worktree"] if listed else None, branch, patch_equal(main, branch))
    if not gone:
        raise Refused(report)
    return {"result": "PASS", "report": report}


def sweep(cfg, store_factory=open_mem):
    """Remove each task/<task8> tree and branch that is done or dropped, holds no live claim, is patch-equal
    on origin/main and holds no work; one report per tree or branch."""
    found = product_repo(cfg)
    if found is None:
        return []
    main = found[0]
    listed = trees(main)
    branches = git(main, "branch", "--list", "task/*", "--format=%(refname:short)").stdout.split()
    branches = [b for b in branches if TASK8_RX.match(b.removeprefix("task/"))]
    if not branches:
        return []
    try:
        store = store_factory()
    except Exception as error:  # a mem fault is no proof a task is done
        return [f"task trees held: mem unreadable ({first_line(str(error))})"]
    reports = []
    for branch in branches:
        task8, entry = branch.removeprefix("task/"), listed.get(branch)
        where = f"product worktree {entry['worktree']}" if entry else f"branch {branch}"
        try:
            row = task_card(store, task8)
            state = claim_live(store, row)
        except Exception as error:
            reports.append(f"{where} kept: {first_line(str(error))}")
            continue
        if row["status"] not in SWEEPABLE:
            reports.append(f"{where} kept: task {row['status']}")
        elif state in ("live", "unknown"):
            reports.append(f"{where} kept: claim {state}")
        elif not patch_equal(main, branch):
            reports.append(f"{where} kept: branch {branch} not on {BASE}")
        else:
            reports.append(drop_tree(main, entry["worktree"] if entry else None, branch, True)[1])
    return reports


def main(argv=None):
    ap = argparse.ArgumentParser(prog="Drawer/product_worktree.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("add"); s.add_argument("--task", required=True); s.add_argument("--session")
    s = sub.add_parser("remove"); s.add_argument("--task", required=True)
    sub.add_parser("sweep")
    a = ap.parse_args(argv)
    cfg = harness_config.load(Path(__file__).resolve().parents[1])
    if a.cmd == "sweep":
        reports = sweep(cfg)
        print("PRODUCT SWEEP: " + ("; ".join(reports) if reports else "nothing to remove") + ".")
        return 0
    try:
        store = open_mem()
        if a.cmd == "add":
            out = add_task(cfg, store, a.task, a.session or os.environ.get("CLAUDE_CODE_SESSION_ID") or None)
        else:
            out = remove_task(cfg, store, a.task)
    except Refused as error:
        print(json.dumps({"result": "REFUSED", "message": str(error)}))
        return 1
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
