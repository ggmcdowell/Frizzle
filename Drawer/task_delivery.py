"""Task delivery check: each repo=sha a task close cites is on origin/main and holds its named paths.

A close cites one or more `repo=sha` commits and the task's repo-relative paths. For each commit, after a
fetch of the remote branch, the sha must be an ancestor of origin/main. Each named path must be changed by a
cited commit, and no named path may hold uncommitted work in that repo. A close that changed no file names
`--no-file-changes '<why>'` instead. No receipt token and no watcher verdict.
Ported from an earlier harness. It never commits or pushes.

    .venv/bin/python -B Drawer/task_delivery.py check --commit <repo>=<sha> [--commit ...] [--path <p> ...]
    .venv/bin/python -B Drawer/task_delivery.py check --no-file-changes '<why>'

It prints one JSON line: the deliveries on a pass (exit 0), {"error": ...} on a refusal (exit 1).
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

PROG = ".venv/bin/python -B Drawer/task_delivery.py"
SHA = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")


class DeliveryError(RuntimeError):
    pass


def git(repo, *args):
    try:
        result = subprocess.run(["git", "--literal-pathspecs", "-C", str(repo), *args], capture_output=True,
                                text=True, timeout=120, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DeliveryError(f"git {args[0]} unavailable: {type(exc).__name__}") from exc
    return result


def must(repo, *args, why):
    result = git(repo, *args)
    if result.returncode:
        raise DeliveryError(f"{why} ({result.stderr.strip()[:200] or 'git ' + args[0] + ' failed'})")
    return result.stdout.strip()


def check_path(p):
    if (not p or any(c in p for c in "\x00\r\n") or p.startswith(("/", "-")) or "\\" in p
            or any(part in (".", "..", "") for part in p.split("/"))):
        raise DeliveryError(f"name an exact repo-relative file path: {p!r}")
    return p


def parse_commit(cite):
    repo, sep, sha = cite.rpartition("=")
    if not sep or not repo or not SHA.fullmatch(sha):
        raise DeliveryError(f"cite a commit as <repo>=<full sha>: {cite!r}")
    return repo, sha


def verify_commit(repo, sha, remote, branch):
    try:
        root = Path(repo).expanduser().resolve(strict=True)
    except OSError as exc:
        raise DeliveryError(f"no repo at {repo}") from exc
    top = must(root, "rev-parse", "--show-toplevel", why=f"{repo} is not a Git repo")
    root = Path(top).resolve()
    sha = must(root, "rev-parse", "--verify", "--quiet", sha + "^{commit}", why=f"no commit {sha[:12]} in {root}")
    must(root, "fetch", "--no-tags", "--quiet", remote, f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}",
         why=f"fetch {remote} {branch} failed in {root}")
    if git(root, "merge-base", "--is-ancestor", sha, f"refs/remotes/{remote}/{branch}").returncode:
        raise DeliveryError(f"commit {sha[:12]} is not on {remote}/{branch} in {root}: push it first")
    changed = must(root, "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "-m", "-z", sha,
                   why=f"cannot list the files of {sha[:12]}")
    return {"repo": str(root), "commit": sha, "changed": set(filter(None, changed.split("\x00")))}


def verify(commits=(), paths=(), no_file_changes=None, remote="origin", branch="main"):
    """Return the delivery dict, or raise DeliveryError with the reason the close is refused."""
    commits, paths = list(commits), [check_path(p) for p in paths]
    if no_file_changes is not None:
        if commits or paths:
            raise DeliveryError("cite commits and paths, or --no-file-changes; not both")
        if not no_file_changes.strip():
            raise DeliveryError("--no-file-changes needs the reason no file changed")
        return {"delivered": True, "no_file_changes": no_file_changes.strip()}
    if not commits:
        raise DeliveryError("cite each repo=sha the task delivered, or --no-file-changes '<why>'")
    if len(set(paths)) != len(paths):
        raise DeliveryError("name each path once")
    checked = [verify_commit(*parse_commit(c), remote, branch) for c in commits]
    for p in paths:
        homes = sorted({c["repo"] for c in checked if p in c["changed"]})
        if not homes:
            raise DeliveryError(f"no cited commit changes {p}: commit it, or cite the commit that does")
        for repo in homes:
            if must(repo, "status", "--porcelain=v1", "--untracked-files=all", "--", p, why=f"status of {p} failed"):
                raise DeliveryError(f"{p} has uncommitted work in {repo}: commit and push it")
    return {"delivered": True, "remote": f"{remote}/{branch}", "paths": paths,
            "commits": [{"repo": c["repo"], "commit": c["commit"]} for c in checked]}


def main(argv=None):
    ap = argparse.ArgumentParser(prog=PROG, description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    ck = sub.add_parser("check", help="check a task close's delivery")
    ck.add_argument("--commit", action="append", default=[], metavar="REPO=SHA")
    ck.add_argument("--path", action="append", default=[], metavar="PATH")
    ck.add_argument("--no-file-changes", metavar="WHY")
    ck.add_argument("--remote", default="origin")
    ck.add_argument("--branch", default="main")
    args = ap.parse_args(argv)
    try:
        out = verify(args.commit, args.path, args.no_file_changes, args.remote, args.branch)
    except DeliveryError as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
