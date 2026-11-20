#!/usr/bin/env python
"""Upgrade a stamped hub from its template: merge template/main into this task worktree.

    <python> Drawer/hub-upgrade.py [--remote template] [--branch main]

Run it in a clean task worktree of the stamped hub (git-flow skill, step 2), never in the live main checkout, then
land the commit as any other. It fetches the template remote (new-hub.py names it 'template'), merges its main with
no commit and settles the merge:

- a conflict in the generated settings only (.claude/settings.json: both sides re-rendered it, one with the template
  prefix and one with the stamp's) takes this side, and settings-sync.py --apply renders it again from the merged
  manifest and harness.toml;
- any other conflict stops: the merge is aborted, nothing changes, and it prints 'REFUSED conflict: <files>' (exit 1);
- the stamp's own [project] name, env_prefix and marker_file must survive the merge, else the same abort;
- it commits the merge and prints 'UPGRADED <sha>'.

It refuses with no write: the live main checkout, a dirty tree, a merge already running, no template remote, and a
hub whose [project] name is the template's (the template itself has nothing to take from itself).
"""
import argparse
import os
import subprocess
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import harness_config  # noqa: E402

GENERATED = (".claude/settings.json",)  # rendered whole by settings-sync.py
STAMP_KEYS = ("name", "env_prefix", "marker_file")


class Refused(Exception):
    pass


def git(cwd, *args, check=True):
    run = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=harness_config.git_env())
    if check and run.returncode:
        raise Refused(f"git {' '.join(args)}: {run.stderr.strip() or run.stdout.strip()}")
    return run


def stamp_values(text):
    project = tomllib.loads(text)["project"]
    return {key: project[key] for key in STAMP_KEYS}


def upgrade(hub, remote="template", branch="main", python=sys.executable):
    hub = Path(hub)
    dirs = git(hub, "rev-parse", "--absolute-git-dir", "--git-common-dir").stdout.splitlines()
    if len(dirs) != 2 or os.path.realpath(dirs[0]) == os.path.realpath(os.path.join(hub, dirs[1])):
        raise Refused("this is the live main checkout: run it in a task worktree (git-flow skill, step 2)")
    if git(hub, "status", "--porcelain").stdout.strip():
        raise Refused("the worktree is not clean: commit or remove the changes first")
    if git(hub, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode == 0:
        raise Refused("a merge is already running here")
    if git(hub, "remote", "get-url", remote, check=False).returncode:
        raise Refused(f"no remote '{remote}': this hub was not stamped by new-hub.py")
    git(hub, "fetch", "-q", remote, branch)
    theirs = f"{remote}/{branch}"
    ours = stamp_values((hub / "harness.toml").read_text(encoding="utf-8"))
    template = stamp_values(git(hub, "show", f"{theirs}:harness.toml").stdout)
    if ours["name"] == template["name"]:
        raise Refused(f"[project] name {ours['name']!r} is the template's: nothing to upgrade from")
    if git(hub, "merge-base", "--is-ancestor", theirs, "HEAD", check=False).returncode == 0:
        raise Refused(f"already up to date with {theirs}")
    merged = git(hub, "merge", "--no-commit", "--no-ff", theirs, check=False)
    if merged.returncode and not git(hub, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode == 0:
        raise Refused(f"git merge failed before it began: {merged.stderr.strip() or merged.stdout.strip()}")
    try:
        conflicts = git(hub, "diff", "--name-only", "--diff-filter=U").stdout.split()
        other = [f for f in conflicts if f not in GENERATED]
        if other:
            raise Refused("conflict: " + " ".join(other))
        for name in conflicts:
            git(hub, "checkout", "--ours", "--", name)
            print(f"re-rendered {name} (both sides changed it)")
        sync = subprocess.run([python, "-B", str(hub / "Drawer" / "settings-sync.py"), "--apply"], cwd=hub,
                              capture_output=True, text=True, env=harness_config.git_env())
        if sync.returncode:
            raise Refused(f"settings-sync --apply: {(sync.stdout + sync.stderr).strip()[-300:]}")
        git(hub, "add", "-u")
        kept = stamp_values((hub / "harness.toml").read_text(encoding="utf-8"))
        if kept != ours:
            raise Refused(f"conflict: harness.toml changed the stamp values {ours} to {kept}")
        git(hub, "commit", "-q", "-m", f"Upgrade from {theirs} {git(hub, 'rev-parse', '--short', theirs).stdout.strip()} "
            "(hub-upgrade.py).")
    except (Refused, OSError):
        git(hub, "merge", "--abort", check=False)
        raise
    return git(hub, "rev-parse", "HEAD").stdout.strip()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--remote", default="template")
    ap.add_argument("--branch", default="main")
    a = ap.parse_args(argv)
    try:
        sha = upgrade(harness_config.hub_root(Path.cwd()), a.remote, a.branch)
    except Refused as e:
        print(f"REFUSED {e}")
        return 1
    print(f"UPGRADED {sha}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
