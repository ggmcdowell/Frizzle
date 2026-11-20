#!/usr/bin/env python3
"""Install and enforce the hub's branch policy.

install --repo binds the checkout in its Git config: the tracked hooks (core.hooksPath), the push
defaults, hub.originUrl and hub.bound=true. Main is the one published line. A task branch lives
only in a linked worktree, is never tracked or pushed, and lands on main.

Task worktree (from the main checkout; skill .claude/skills/git-flow):
  <python> Drawer/git-branch-policy.py worktree-add --name <uuid8>-<slug>  # task/<name>, untracked, a seat;
                                                                     # <uuid8> is your session id's first 8,
                                                                     # or <task8>-<slug>: the task8 of a card
                                                                     # this session holds a live claim on
  <python> Drawer/git-branch-policy.py worktree-land --name <name>    # main fast-forwards, dirty work carried;
                                                                     # a red probe at the branch tip blocks it
  <python> Drawer/git-branch-policy.py land --name <name>             # one call: freshness in the worktree, the
                                                                     # probe at the tip, the steer check (STOP:
                                                                     # state need, exit 75, nothing pushed), the
                                                                     # tip to origin/main (rebase and retry up to
                                                                     # 3 times when origin moved), read back, then
                                                                     # worktree-land
  <python> Drawer/git-branch-policy.py worktree-remove --name <name>  # once origin/main holds it (or its
                                                                     # patches), no live peer session owns
                                                                     # the tree and no live claim holds its
                                                                     # <task8> card; logged under state_root
  <python> Drawer/git-branch-policy.py own-cleanup [--session-end]   # this session's own landed, clean hub
                                                                     # task trees and branches, and its uuid8
                                                                     # temp folders (to the trash); unlanded
                                                                     # and peer items stay; product session/
                                                                     # trees stay for the session-start sweep; also any
                                                                     # <task8>-* tree of a done or dropped
                                                                     # card with no claim and no worktree
                                                                     # column (task_sweep; the session-start sweep too)
The SessionEnd hook runs own-cleanup --session-end for the closing session (the payload's session_id) and
writes one line to own-cleanup.log under state_root. It never fails the close.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time


DRAWER_DIR = Path(__file__).resolve().parent
if str(DRAWER_DIR) not in sys.path:
    sys.path.insert(0, str(DRAWER_DIR))
import gatelib
import harness_config
import oscompat


# The hub that holds this file: its state_root and steer check read harness.toml.
CFG = harness_config.load(DRAWER_DIR.parent)


PY = ".venv/bin/python -B"  # a new seat has no py launcher
HOOKS_PATH = "Drawer/git-hooks"
# A checkout may commit on one unpushed task branch in a linked worktree; it merges to
# main and is deleted with its worktree. Main stays the only published line.
TASK_BRANCH = re.compile(r"task/[a-z0-9][a-z0-9._-]*")
# Task worktrees stand beside the hub as <hub parent>/<hub folder>_wt/<name>. The prepared
# interpreter is linked from the main checkout, never copied or rebuilt.
WORKTREE_SUFFIX = "_wt"
VENV = ".venv"
ZERO_SHA = "0" * 40
FRESHNESS_LOCK_SECONDS = 5
FETCH_TIMEOUT_SECONDS = 20
REBASE_TIMEOUT_SECONDS = 20
RECOVERY_TIMEOUT_SECONDS = 10
PUSH_TIMEOUT_SECONDS = 120
LAND_TRIES = 3  # push rounds a land takes when origin/main moves under it
PROBE_TIMEOUT_SECONDS = 600  # the probe at a branch tip (Drawer/probes/probe.py; about 4 s)
INTEGRATION_CHECK_TIMEOUT_SECONDS = 10
# One line per task worktree removal: what went, its tip and why that was safe.
REMOVE_LOG = harness_config.path(CFG["project"]["state_root"]) / "worktree-remove.log"
# One line per SessionEnd own-cleanup: what went, what stays and why.
OWN_CLEANUP_LOG = REMOVE_LOG.with_name("own-cleanup.log")
SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
CLEANUP_BUDGET_SECONDS = 80  # the hook timeout is 120 s; one more item (a fetch, a trash) fits in the rest
TRASH_TIMEOUT_SECONDS = 20
SESSION_END_STAYS = frozenset({"clear", "resume"})  # SessionEnd reasons whose process lives on


class PolicyError(RuntimeError):
    """The checkout or requested Git operation violates the branch policy."""


class GitTimeout(PolicyError):
    """A bounded Git subprocess exceeded its operation budget."""

    def __init__(self, message: str, *, quiesced: bool):
        super().__init__(message)
        self.quiesced = quiesced


class _FreshnessLock:
    """A process lock in this worktree's Git directory."""

    def __init__(self, repo: Path, timeout: float = FRESHNESS_LOCK_SECONDS):
        self.repo = repo
        self.timeout = timeout
        self.handle = None

    def __enter__(self):
        raw = _git(self.repo, "rev-parse", "--git-path", "hub-freshness.lock").stdout.strip()
        path = Path(raw)
        if not path.is_absolute():
            path = self.repo / path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = path.open("a+b")
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                oscompat.lock(self.handle)
                return self
            except OSError as exc:
                if time.monotonic() >= deadline:
                    self.handle.close()
                    self.handle = None
                    raise PolicyError(
                        "another session is already checking or updating this worktree"
                    ) from exc
                time.sleep(0.05)

    def __exit__(self, _kind, _value, _traceback):
        if self.handle is None:
            return
        oscompat.unlock(self.handle)
        self.handle.close()
        self.handle = None


def _git(repo: Path, *arguments: str, input_text: str | None = None,
         check: bool = True, env: dict[str, str] | None = None,
         timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    command = ["git", "-C", str(repo), *arguments]
    if timeout is None:
        result = subprocess.run(
            # Never inherit stdin: a Git child holding a stdio MCP server's pipe hangs or ends
            # that server.
            command, input=input_text, stdin=subprocess.DEVNULL if input_text is None else None,
            text=True, capture_output=True, check=False, env=env, timeout=timeout,
        )
    else:
        process = subprocess.Popen(
            command,
            stdin=(subprocess.PIPE if input_text is not None else None),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            **oscompat.popen_group(),
        )
        try:
            stdout, stderr = process.communicate(input=input_text, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise GitTimeout(
                f"Git {arguments[0]} timed out while checking accepted main",
                quiesced=_terminate_process_tree(process),
            ) from exc
        result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    if check and result.returncode:
        raise PolicyError(f"Git {arguments[0]} failed while checking the Hub branch policy")
    return result


def _terminate_process_tree(process: subprocess.Popen) -> bool:
    """Stop a timed Git command and every descendant before recovery begins."""
    quiesced = True
    try:
        oscompat.kill_tree(process.pid)
    except ProcessLookupError:
        pass
    except OSError:
        quiesced = False
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        quiesced = False
        try:
            process.kill()
            process.communicate(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass
    return quiesced and process.poll() is not None


def _failure_detail(result: subprocess.CompletedProcess[str]) -> str:
    # The cause, not Git's closing 'failed to push some refs' line: the rejected ref,
    # the remote's or a hook's words.
    lines = [line.strip() for line in (result.stderr or result.stdout).splitlines() if line.strip()]
    why = [line for line in lines if not line.startswith(("hint:", "To ", "error: failed to push some refs"))]
    return (" | ".join(why[-3:]) if why else lines[-1] if lines else f"Git exited {result.returncode}")[:600]


OPERATION_MARKERS = ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD",
                     "REVERT_HEAD", "BISECT_LOG", "sequencer")


def _operation_in_progress(repo: Path) -> str | None:
    # One rev-parse for every marker: it prints one path per --git-path.
    raws = _git(repo, "rev-parse", *(arg for marker in OPERATION_MARKERS
                                     for arg in ("--git-path", marker))).stdout.splitlines()
    if len(raws) != len(OPERATION_MARKERS):
        raise PolicyError("Git did not return a path for each operation marker")
    for marker, raw in zip(OPERATION_MARKERS, raws):
        path = Path(raw.strip())
        if not path.is_absolute():
            path = repo / path
        if path.exists():
            return marker
    return None


def _distance(repo: Path) -> tuple[int, int]:
    result = _git(
        repo, "rev-list", "--left-right", "--count",
        "HEAD...refs/remotes/origin/main",
    )
    fields = result.stdout.split()
    if len(fields) != 2:
        raise PolicyError("Git did not return an accepted-main distance")
    return int(fields[0]), int(fields[1])


def _remote_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never", "GH_PROMPT_DISABLED": "1"})
    return env


def _fetch_main(repo: Path, timeout: float) -> subprocess.CompletedProcess[str]:
    return _git(
        repo, "fetch", "--no-tags", "origin", "+refs/heads/main:refs/remotes/origin/main",
        check=False, env=_remote_env(), timeout=timeout,
    )


def _repo_root(raw: str | os.PathLike[str]) -> Path:
    candidate = Path(raw).resolve()
    result = _git(candidate, "rev-parse", "--show-toplevel")
    root = Path(result.stdout.strip()).resolve()
    if root != candidate:
        raise PolicyError("the policy must target the Hub checkout root")
    return root


def _config(repo: Path, key: str) -> str:
    result = _git(repo, "config", "--get", key, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def _config_scope(repo: Path) -> str:
    worktrees = sum(
        1 for line in _git(repo, "worktree", "list", "--porcelain").stdout.splitlines()
        if line.startswith("worktree ")
    )
    worktree_config = _config(repo, "extensions.worktreeConfig").casefold() == "true"
    if worktrees > 1 and not worktree_config:
        raise PolicyError(
            "multiple worktrees share local Git config; enable extensions.worktreeConfig before installation"
        )
    return "--worktree" if worktree_config else "--local"


def _set_config(repo: Path, scope: str, key: str, value: str) -> None:
    _git(repo, "config", scope, key, value)


def _config_all(repo: Path) -> dict[str, list[str]]:
    """Every Git config value in one call, keyed as git config --list names it:
    section and key lower-case, a subsection (a branch name) as written; _config_key builds that key."""
    result = _git(repo, "config", "-z", "--list", check=False)
    values: dict[str, list[str]] = {}
    for entry in result.stdout.split("\0") if result.returncode == 0 else ():
        key, _, value = entry.partition("\n")
        if key:
            values.setdefault(key, []).append(value.strip())
    return values


def _config_key(key: str) -> str:
    """key as git config --list names it: hub.originUrl -> hub.originurl, branch.Main.merge -> branch.Main.merge."""
    section, _, rest = key.partition(".")
    subsection, _, name = rest.rpartition(".")
    return ".".join(part for part in (section.casefold(), subsection, name.casefold()) if part)


def _last(values: dict[str, list[str]], key: str) -> str:
    """The value git config --get gives: the last one, or "" when unset."""
    found = values.get(_config_key(key))
    return found[-1] if found else ""


def _require_bound(repo: Path, values: dict[str, list[str]] | None = None) -> None:
    """Refuse unless install bound this checkout (hub.bound=true)."""
    values = _config_all(repo) if values is None else values
    if _last(values, "hub.bound").casefold() != "true":
        raise PolicyError("the checkout has no verified branch-policy binding; run install --repo first")


def _current_branch(repo: Path) -> str:
    result = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def _rebase_branch(repo: Path) -> str:
    operation = _operation_in_progress(repo)
    if operation not in {"rebase-merge", "rebase-apply"}:
        return ""
    raw = _git(repo, "rev-parse", "--git-path", f"{operation}/head-name").stdout.strip()
    path = Path(raw)
    if not path.is_absolute():
        path = repo / path
    try:
        head_name = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise PolicyError("the active rebase does not identify its owning branch") from exc
    prefix = "refs/heads/"
    return head_name[len(prefix):] if head_name.startswith(prefix) else ""


def _require_branch(branch: str, operation: str) -> None:
    if not branch:
        raise PolicyError(f"{operation} is refused from detached HEAD")
    if branch != "main":
        raise PolicyError(f"{operation} are allowed only on main; current target is {branch}")


def _common_dir(repo: Path) -> tuple[Path, Path]:
    git_dir, common_dir = _git(
        repo, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir",
    ).stdout.splitlines()
    return Path(git_dir).resolve(), Path(common_dir).resolve()


def _linked(repo: Path) -> bool:
    git_dir, common_dir = _common_dir(repo)
    return git_dir != common_dir


def _task_branch(branch: str) -> bool:
    return bool(TASK_BRANCH.fullmatch(branch))


def _require_commit_branch(repo: Path, branch: str, operation: str) -> None:
    """Main, or a task/* branch in a linked worktree that is never tracked or pushed."""
    if not _task_branch(branch):
        _require_branch(branch, operation)
        return
    if not _linked(repo):
        raise PolicyError(f"{operation} on task branch {branch} are allowed only in a linked worktree")
    if _config_values(repo, f"branch.{branch}.remote") or _config_values(repo, f"branch.{branch}.merge"):
        raise PolicyError(f"{operation} on task branch {branch} are refused: a task branch is never tracked; "
                          f"run git branch --unset-upstream {branch}")
    if _git(repo, "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{branch}",
            check=False).returncode == 0:
        raise PolicyError(f"{operation} on task branch {branch} are refused: a task branch is never pushed")


def _set_branch_destination(repo: Path, scope: str, branch: str) -> None:
    for key, value in ((f"branch.{branch}.remote", "origin"),
                       (f"branch.{branch}.merge", f"refs/heads/{branch}")):
        if scope == "--worktree":
            # The clone's own local entry would stand beside the worktree one, and Git
            # refuses a bare push when a branch has two upstream entries.
            _git(repo, "config", "--local", "--unset-all", key, check=False)
        _set_config(repo, scope, key, value)


def _config_values(repo: Path, key: str) -> list[str]:
    result = _git(repo, "config", "--get-all", key, check=False)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()] if result.returncode == 0 else []


def _staged_paths(repo: Path) -> list[str]:
    result = _git(
        repo, "diff", "--cached", "--name-only", "--no-renames",
        "--diff-filter=ACDMRTUXB", "-z",
    )
    return [path for path in result.stdout.split("\0") if path]


def _switch(repo: Path, *switch: str) -> None:
    switched = _git(repo, "switch", *switch, check=False)
    if switched.returncode:
        # Git names the colliding paths above its last line; keep the whole refusal.
        raise PolicyError("Git switch failed: " + (
            " ".join((switched.stderr or switched.stdout).split())[:600] or _failure_detail(switched)))


def install(repo: Path) -> dict[str, object]:
    """Bind the checkout: tracked hooks, push defaults, the origin URL and hub.bound=true, on main."""
    for hook in ("pre-commit", "pre-merge-commit", "pre-push"):
        if not (repo / HOOKS_PATH / hook).is_file():
            raise PolicyError(f"tracked Git hook is missing: {hook}")

    scope = _config_scope(repo)
    current = _current_branch(repo)
    if current != "main":
        if current:
            raise PolicyError(f"checkout is on {current}; preserve its work and move it to main explicitly")
        local = _git(repo, "show-ref", "--verify", "refs/heads/main", check=False)
        remote = _git(repo, "show-ref", "--verify", "refs/remotes/origin/main", check=False)
        dirty = bool(_git(repo, "status", "--porcelain").stdout.strip())
        if local.returncode == 0:
            if dirty:
                raise PolicyError("main already exists and tracked work is dirty; no switch was attempted")
            _switch(repo, "main")
        elif remote.returncode == 0:
            if dirty:
                raise PolicyError("origin/main already exists and tracked work is dirty; no switch was attempted")
            _switch(repo, "--track", "-c", "main", "origin/main")
        else:
            # A new main starts at accepted main.
            if _fetch_main(repo, FETCH_TIMEOUT_SECONDS).returncode:
                raise PolicyError("accepted main could not be fetched; no main branch was created")
            _switch(repo, "-c", "main", "refs/remotes/origin/main")
        current = "main"

    _set_config(repo, scope, "core.hooksPath", HOOKS_PATH)
    _set_config(repo, scope, "remote.pushDefault", "origin")
    _set_config(repo, scope, "push.default", "upstream")
    if oscompat.WINDOWS:  # a task worktree path under a deep hub folder passes 260 characters
        _set_config(repo, scope, "core.longpaths", "true")
    _set_config(repo, scope, "hub.originUrl", _git(repo, "remote", "get-url", "origin").stdout.strip())
    _set_config(repo, scope, "hub.bound", "true")
    _set_branch_destination(repo, scope, current)
    return status(repo)


def status(repo: Path) -> dict[str, object]:
    values = _config_all(repo)  # one config read for the binding, the hooks path and the upstream
    _require_bound(repo, values)
    branch = _current_branch(repo)
    _require_commit_branch(repo, branch, "Hub commits")
    hooks = _last(values, "core.hooksPath").replace("\\", "/").rstrip("/")
    # Claude Code's worktree isolation rewrites the relative path as this checkout's absolute one.
    if hooks != HOOKS_PATH and not (
            Path(hooks).is_absolute() and Path(hooks).resolve() == (repo / HOOKS_PATH).resolve()):
        raise PolicyError("the checkout does not use the tracked hub Git hooks")
    common = {"result": "PASS", "branch": branch, "writable_branch": branch, "hooks_path": HOOKS_PATH}
    if _task_branch(branch):
        # A task branch pushes nowhere: it merges into main in the main checkout.
        return {**common, "push_remote": "", "push_ref": ""}
    expected_merge = f"refs/heads/{branch}"
    remotes = values.get(_config_key(f"branch.{branch}.remote"), [])
    merges = values.get(_config_key(f"branch.{branch}.merge"), [])
    if _last(values, f"branch.{branch}.remote") != "origin" or _last(values, f"branch.{branch}.merge") != expected_merge:
        raise PolicyError("the current branch does not push to its same-named origin branch")
    if len(merges) > 1 or len(remotes) > 1:
        raise PolicyError("the current branch has two upstream entries; run install again to keep one")
    return {**common, "push_remote": "origin", "push_ref": expected_merge}


def check_commit(repo: Path) -> dict[str, object]:
    _require_bound(repo)
    branch = _current_branch(repo)
    rebasing = False
    if not branch:
        branch = _rebase_branch(repo)
        rebasing = bool(branch)
    _require_commit_branch(repo, branch, "Hub rebase commits" if rebasing else "Hub commits")
    return {"result": "PASS", "branch": branch, "rebasing": rebasing}


def _branch_tip_in_accepted_main(repo: Path, remote_sha: str) -> str | None:
    accepted = "refs/remotes/origin/main"
    ancestor = _git(
        repo, "merge-base", "--is-ancestor", remote_sha, accepted,
        check=False, timeout=INTEGRATION_CHECK_TIMEOUT_SECONDS,
    )
    if ancestor.returncode == 0:
        return "ancestor"
    merges = _git(
        repo, "rev-list", "--merges", f"{accepted}..{remote_sha}",
        check=False, timeout=INTEGRATION_CHECK_TIMEOUT_SECONDS,
    )
    if merges.returncode or merges.stdout.strip():
        return None
    cherry = _git(
        repo, "cherry", accepted, remote_sha,
        check=False, timeout=INTEGRATION_CHECK_TIMEOUT_SECONDS,
    )
    if cherry.returncode:
        return None
    rows = [line.split() for line in cherry.stdout.splitlines() if line.strip()]
    if rows and all(len(row) == 2 and row[0] == "-" for row in rows):
        return "patch-equivalent"
    return None


def check_push(repo: Path, remote: str, url: str, updates: str) -> dict[str, object]:
    _require_bound(repo)
    if remote != "origin":
        raise PolicyError("Hub pushes are allowed only to origin")
    # Git passes the configured target URL as argv[2] to pre-push. Pin that exact
    # target so changing origin alone cannot redirect a protected operation.
    expected_url = _config(repo, "hub.originUrl")
    if url != expected_url:
        raise PolicyError("the origin URL disagrees with the verified checkout binding")
    checked: list[str] = []
    for line in updates.splitlines():
        fields = line.split()
        if len(fields) != 4:
            raise PolicyError("Git supplied a malformed pre-push update")
        _local_ref, local_sha, remote_ref, remote_sha = fields
        if not remote_ref.startswith("refs/heads/"):
            raise PolicyError("Hub pushes may update branches only; tags and other refs are refused")
        branch = remote_ref.removeprefix("refs/heads/")
        if local_sha == ZERO_SHA:
            if branch == "main":
                raise PolicyError("Hub main branch deletion is refused")
            if remote_sha == ZERO_SHA or not re.fullmatch(r"[0-9a-fA-F]{40,64}", remote_sha):
                raise PolicyError("Git supplied no valid remote branch tip for deletion")
            proof = _branch_tip_in_accepted_main(repo, remote_sha)
            if proof is None:
                raise PolicyError(
                    f"Hub branch deletion is refused because {remote_ref} tip {remote_sha[:12]} "
                    "is not integrated into accepted main"
                )
            checked.append(remote_ref)
            continue
        _require_branch(branch, "Hub pushes")
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", local_sha):
            raise PolicyError("Git supplied a malformed object id")
        if remote_sha != ZERO_SHA and not re.fullmatch(r"[0-9a-fA-F]{40,64}", remote_sha):
            raise PolicyError("Git supplied a malformed remote object id")
        # Creating main on an origin that has none: there is no accepted main to contain (bind-git's seed push).
        if branch == "main" and remote_sha == ZERO_SHA:
            checked.append(remote_ref)
            continue
        if _git(
            repo, "merge-base", "--is-ancestor", "refs/remotes/origin/main", local_sha,
            check=False, timeout=INTEGRATION_CHECK_TIMEOUT_SECONDS,
        ).returncode:
            raise PolicyError(
                f"Hub push to {branch} is refused because the tip does not contain accepted main; "
                f"run {PY} Drawer/git-branch-policy.py publish instead of git push"
            )
        checked.append(remote_ref)
    return {"result": "PASS", "remote": remote, "refs": checked}


def _fast_forward(repo: Path, common: dict[str, object], timeout: float) -> dict[str, object] | str:
    """Fast-forward a dirty branch in place. Return the final result, or the commit to carry
    the uncommitted work onto when Git refuses the move."""
    head, accepted = str(common["head_before"]), str(common["accepted_main"])
    try:
        merged = _git(repo, "merge", "--ff-only", "--no-stat", "refs/remotes/origin/main",
                      check=False, timeout=timeout)
    except GitTimeout as exc:
        if not exc.quiesced:
            return {
                **common, "state": "blocked", "reason": "fast_forward_process_tree_unverified",
                "head_after": None,
                "message": (
                    "GIT FRESHNESS BLOCKED — the bounded fast-forward timed out and its process "
                    "tree could not be proven stopped. Wait for owning agent recovery before "
                    "touching this worktree."
                ),
            }
        merged = subprocess.CompletedProcess(
            ["git", "merge", "--ff-only"], 124, stdout="",
            stderr="the fast-forward exceeded its bounded timeout",
        )
    head_after = _git(repo, "rev-parse", "HEAD").stdout.strip()
    if head_after not in {head, accepted}:
        return {
            **common, "state": "blocked", "reason": "rebase_recovery_required",
            "head_after": head_after,
            "message": (
                f"GIT FRESHNESS BLOCKED — the fast-forward left HEAD at {head_after[:12]}, "
                "neither the original nor accepted main. Repair the branch before new code work."
            ),
        }
    if head_after == head:
        # Git names the colliding paths above its last line; keep the whole refusal.
        common["fast_forward_refused"] = " ".join(
            (merged.stderr or merged.stdout).split())[:600] or _failure_detail(merged)
        return accepted
    return {
        **common, "state": "rebased", "head_after": head_after, "fast_forward": True,
        "ahead": 0, "behind": 0,
        "message": (
            f"GIT FRESHNESS REBASED — {common['branch']} fast-forwarded to accepted main "
            f"{accepted[:12]}; uncommitted work stayed in place, nothing was committed."
        ),
    }


def _replay_in_place(repo: Path, common: dict[str, object], timeout: float) -> dict[str, object] | str:
    """Rebase a dirty branch's local commits without touching its uncommitted work.

    Each commit is replayed onto accepted main in the object store (merge-tree,
    commit-tree: no hook, no index, no worktree), then git read-tree -m -u moves the
    worktree and update-ref the branch; Git refuses when a dirty path would change.
    Return the final result, or the replayed tip to carry the uncommitted work onto
    when Git refuses the move.
    """
    head, accepted, branch = str(common["head_before"]), str(common["accepted_main"]), common["branch"]
    # A local merge commit may carry a conflict resolution a linear replay would drop.
    if _git(repo, "rev-list", "--merges", "-1", f"{accepted}..{head}").stdout.strip():
        return {
            **common, "state": "blocked", "reason": "merge_history_dirty", "head_after": head,
            "message": (
                f"GIT FRESHNESS BLOCKED — {branch} holds a local merge commit and uncommitted "
                "work; nothing moved. Run freshness again once that work is committed or "
                "reverted. Never stash, reset or check out another session's work."
            ),
        }
    commits = _git(
        repo, "rev-list", "--reverse", "--no-merges", "--right-only", "--cherry-pick",
        f"{accepted}...{head}",
    ).stdout.split()
    tip, replayed = accepted, 0
    for commit in commits:
        tree, conflicts = _merge_onto(repo, f"{commit}^", tip, commit)
        if conflicts:
            return {
                **common, "state": "blocked", "reason": "replay_conflict", "conflicts": conflicts,
                "head_after": head,
                "message": (
                    f"GIT FRESHNESS BLOCKED — local commit {commit[:12]} conflicts with accepted "
                    f"main {accepted[:12]} in {', '.join(conflicts[:5])} and the worktree holds "
                    "uncommitted work; nothing moved. Once that work is committed or reverted, run "
                    "git rebase origin/main, resolve each conflict, finish the rebase, and run "
                    f"{PY} Drawer/git-branch-policy.py publish. Never stash, reset or check out "
                    "another session's work; never ask the user."
                ),
            }
        tip_tree, own_tree, parent_tree = _git(
            repo, "rev-parse", f"{tip}^{{tree}}", f"{commit}^{{tree}}", f"{commit}^^{{tree}}",
        ).stdout.split()
        if tree == tip_tree and own_tree != parent_tree:
            continue  # its change is already upstream; git rebase drops it too
        # Bytes, not text: the message and author must survive any locale unchanged.
        raw = subprocess.run(["git", "-C", str(repo), "cat-file", "commit", commit],
                             capture_output=True, check=False)
        if raw.returncode:
            raise PolicyError(f"Git cat-file failed while replaying {commit[:12]}")
        header, _, message = raw.stdout.partition(b"\n\n")
        author = next(line for line in header.split(b"\n") if line.startswith(b"author "))
        name, _, rest = author[7:].decode().partition(" <")
        email, _, date = rest.rpartition("> ")
        written = subprocess.run(
            ["git", "-C", str(repo), "commit-tree", tree, "-p", tip],
            input=message, capture_output=True, check=False,
            env={**os.environ, "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
                 "GIT_AUTHOR_DATE": date},
        )
        if written.returncode:
            raise PolicyError(f"Git commit-tree failed while replaying {commit[:12]}")
        tip, replayed = written.stdout.decode().strip(), replayed + 1

    moved_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    if moved_head != head:
        return {
            **common, "state": "blocked", "reason": "peer_commit_race", "head_after": moved_head,
            "message": (
                f"GIT FRESHNESS BLOCKED — HEAD of {branch} moved to {moved_head[:12]} while "
                "freshness ran. Nothing was moved; run freshness again."
            ),
        }
    # A two-way read-tree is checkout's own move: it keeps every staged entry and edit
    # on a path the rebase does not change, and refuses, changing nothing, on any other.
    # It trusts stat data, so refresh it first.
    _git(repo, "update-index", "-q", "--refresh", check=False)
    try:
        moved = _git(repo, "read-tree", "-m", "-u", head, tip, check=False, timeout=timeout)
    except GitTimeout as exc:
        return {
            **common, "state": "blocked",
            "reason": "replay_timeout" if exc.quiesced else "replay_process_tree_unverified",
            "head_after": head,
            "message": (
                f"GIT FRESHNESS BLOCKED — the bounded in-place move to {tip[:12]} timed out"
                + ("" if exc.quiesced else " and its process tree could not be proven stopped")
                + ". The worktree may be partly moved; repair it before new code work."
            ),
        }
    # The compare-and-swap refuses when a peer commit landed after the HEAD check.
    swapped = not moved.returncode and not _git(
        repo, "update-ref", "-m", "freshness: in-place rebase onto accepted main",
        "HEAD", tip, head, check=False,
    ).returncode
    if moved.returncode:
        # Git names each dirty path the rebase would change; keep the whole refusal.
        common["replay_refused"] = " ".join(
            (moved.stderr or moved.stdout).split())[:600] or _failure_detail(moved)
        common["replayed"] = replayed
        return tip
    if not swapped:
        peer = _git(repo, "rev-parse", "HEAD").stdout.strip()
        return {
            **common, "state": "blocked", "reason": "peer_commit_race", "head_after": peer,
            "message": (
                f"GIT FRESHNESS BLOCKED — commit {peer} landed on {branch} while freshness moved "
                f"the worktree to {tip}. HEAD stays on {peer[:12]}. Run git read-tree -m -u "
                f"{tip} {peer} to return the worktree to it, then run freshness again."
            ),
        }
    final_ahead, final_behind = _distance(repo)
    return {
        **common, "state": "rebased", "head_after": tip, "replayed": replayed,
        "ahead": final_ahead, "behind": final_behind,
        "message": (
            f"GIT FRESHNESS REBASED — {branch} replayed {replayed} local commits onto accepted "
            f"main {accepted[:12]} in place; uncommitted work stayed in place, nothing was committed."
        ),
    }


def _merge_onto(repo: Path, head: str, tip: str, tree: str,
                timeout: float | None = None) -> tuple[str, list[str]]:
    """Carry tree's change from head onto tip in the object store: (tree, conflicted paths)."""
    merged = _git(repo, "merge-tree", "--write-tree", "--name-only", "--no-messages",
                  "--merge-base", head, tip, tree, check=False, timeout=timeout)
    lines = merged.stdout.splitlines()
    if merged.returncode == 1:
        return "", [line for line in lines[1:] if line]
    if merged.returncode or not lines:
        raise PolicyError(f"Git merge-tree failed while carrying uncommitted work: {_failure_detail(merged)}")
    return lines[0], []


# The carry's words for its two callers; a land's messages stay as worktree-land always printed them.
CARRY_WORDS = {
    "land": {"banner": "GIT LAND BLOCKED", "run": "the land", "again": "land again",
             "Again": "Land again", "reflog": "worktree-land: {onto}"},
    "freshness": {"banner": "GIT FRESHNESS BLOCKED", "run": "freshness", "again": "run freshness again",
                  "Again": "Run freshness again", "reflog": "freshness: carry onto {onto}"},
}


def _carry_onto(repo: Path, common: dict[str, object], head: str, tip: str, deadline: float,
                *, by: str, onto: str) -> dict[str, object] | None:
    """Move a dirty checkout from head to tip, keeping every staged and unstaged edit as it was.

    The index tree (I) and the worktree tree (W, from a private copy of the index after add -A)
    are each merged onto tip in the object store. A private-index read-tree -m -u moves the
    worktree from W to W'; update-ref swaps HEAD by compare-and-swap; a plain read-tree moves
    the real index from I to I'. No commit, no hook. A conflict, a failure, the deadline (one
    time.monotonic() bound shared by the bounded steps), or a peer's commit or staging during
    the move puts everything back and blocks. Return None on success, else the blocked result.
    by picks the words (CARRY_WORDS); onto names tip in them.
    """
    words = CARRY_WORDS[by]
    banner, where = words["banner"], common["branch"]
    index_tree = _git(repo, "write-tree", check=False)
    if index_tree.returncode:
        return {**common, "state": "blocked", "reason": "unmerged_index", "head_after": head,
                "message": f"{banner} — the index holds unmerged paths; nothing moved."}
    index_tree = index_tree.stdout.strip()
    # write-tree drops intent-to-add entries; the new index gets them back.
    intent = [line[3:] for line in _git(repo, "status", "--porcelain", "--untracked-files=no").stdout.splitlines()
              if line.startswith(" A ")]

    def git_path(name: str) -> Path:
        path = Path(_git(repo, "rev-parse", "--git-path", name).stdout.strip())
        return path if path.is_absolute() else repo / path

    def left() -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GitTimeout("the carry exceeded its bounded deadline", quiesced=True)
        return remaining

    private: Path | None = None
    env: dict[str, str] = {}
    work_tree = new_work = new_index = ""
    moving = moved_tree = swapped = index_done = unverified = False

    def blocked_as(reason: str, message: str) -> dict[str, object]:
        return {**common, "state": "blocked", "reason": reason, "head_after":
                _git(repo, "rev-parse", "HEAD").stdout.strip(), "message": f"{banner} — {message}"}

    def put_back(reason: str, message: str, *, moved: bool = False) -> dict[str, object]:
        if moved:
            _git(repo, "read-tree", "-m", "-u", new_work, work_tree, check=False, env=env)
        return blocked_as(reason, message)

    try:
        # A private copy of the index takes add -A, so the real index never holds the worktree tree.
        private = git_path("hub-land.index")
        shutil.copyfile(git_path("index"), private)
        env = {**os.environ, "GIT_INDEX_FILE": str(private)}
        _git(repo, "add", "-A", env=env, timeout=left())
        work_tree = _git(repo, "write-tree", env=env).stdout.strip()
        new_index, index_conflicts = _merge_onto(repo, head, tip, index_tree, timeout=left())
        new_work, work_conflicts = _merge_onto(repo, head, tip, work_tree, timeout=left())
        conflicts = sorted(set(index_conflicts) | set(work_conflicts))
        if conflicts:
            return put_back("carry_conflict", (
                f"uncommitted edits in {', '.join(conflicts)} conflict with {onto}; nothing moved, "
                f"{where} stays at {head[:12]}. {words['Again']} once that work is committed or "
                "reverted. Never stash, reset or check out another session's work."
            ))
        _git(repo, "update-index", "-q", "--refresh", check=False, env=env)
        moving = True
        moved = _git(repo, "read-tree", "-m", "-u", work_tree, new_work, check=False,
                     env=env, timeout=left())
        moving = False
        moved_tree = not moved.returncode
        if moved.returncode:
            return put_back("carry_refused", (
                f"Git refused to move the worktree to {onto}: "
                f"{' '.join((moved.stderr or moved.stdout).split())[:600]}. Nothing moved."
            ))
        if _git(repo, "update-ref", "-m", words["reflog"].format(onto=onto), "HEAD", tip, head,
                check=False).returncode:
            return put_back("peer_commit_race", (
                f"a commit landed on {where} while {words['run']} ran. The worktree was "
                f"moved back; {words['again']}."
            ), moved=True)
        swapped = True
        if _git(repo, "write-tree", check=False).stdout.strip() != index_tree:
            _git(repo, "update-ref", "HEAD", head, tip, check=False)
            swapped = False
            return put_back("peer_index_race", (
                f"another session staged work while {words['run']} ran. HEAD and the "
                f"worktree were moved back; {words['again']}."
            ), moved=True)
        # A plain read-tree: a two-way one refuses any entry whose worktree differs from the
        # index, which is exactly the unstaged work being carried. It drops stat data (refresh
        # rebuilds it).
        _git(repo, "read-tree", new_index)
        index_done = True
        _git(repo, "update-index", "-q", "--refresh", check=False)
        intent = [path for path in intent if (repo / path).exists()]
        if intent:
            _git(repo, "add", "-N", "--", *intent)
    except GitTimeout as exc:
        if not exc.quiesced:
            unverified = True
            return {
                **common, "state": "blocked", "reason": "carry_process_tree_unverified",
                "head_after": head,
                "message": (
                    f"{banner} — a bounded carry step timed out and its process tree could not be "
                    "proven stopped. Wait for owning agent recovery before touching this worktree."
                ),
            }
        return put_back("carry_timeout", (
            f"carrying uncommitted work onto {onto} exceeded its bounded deadline. "
            + (f"The worktree may be partly moved; tree {work_tree} holds its uncommitted bytes. "
               "Repair it before new work." if moving else f"Nothing moved; {words['again']}.")
        ))
    except (PolicyError, OSError) as exc:
        detail = f"carrying uncommitted work onto {onto} failed: {str(exc)[:300]}."
        if not index_done:
            # The index still holds I: move HEAD and the worktree back to it.
            if swapped:
                _git(repo, "update-ref", "HEAD", head, tip, check=False)
            return put_back("carry_failed", f"{detail} " + (
                f"HEAD and the worktree were moved back; {words['again']}." if moved_tree
                else f"Nothing moved; {words['again']}."), moved=moved_tree)
        return blocked_as("carry_failed", (
            f"{detail} HEAD, the worktree and the index moved; an intent-to-add "
            f"entry of {', '.join(sorted(intent))[:300] or 'none'} may be lost. "
            "Check git status and restore it before new work."
        ))
    finally:
        if private is not None:
            try:
                private.unlink(missing_ok=True)
            except OSError:
                if not unverified:  # a Git that could not be stopped may still hold it open
                    raise
    return None


def freshness(
    repo: Path,
    source: str,
    *,
    lock_timeout: float = FRESHNESS_LOCK_SECONDS,
    fetch_timeout: float = FETCH_TIMEOUT_SECONDS,
    rebase_timeout: float = REBASE_TIMEOUT_SECONDS,
    recovery_timeout: float = RECOVERY_TIMEOUT_SECONDS,
) -> dict[str, object]:
    """Fetch accepted main and safely refresh one bound checkout or task worktree.

    Startup and resume take the same bounded path: a branch behind accepted main moves
    onto it before work. A clean branch is rebased. A dirty branch with no local commits
    is fast-forwarded in place; one with local commits replays them in the object store
    and moves like a checkout. When Git refuses that move because a dirty path collides,
    the uncommitted work is carried onto the new HEAD as worktree-land carries it (merge-tree,
    private-index move, compare-and-swap): staged stays staged, unstaged stays unstaged,
    no hook runs and nothing is committed. A true conflict, a local merge commit or a
    replay conflict blocks with nothing moved. Main and a task branch take the same path.
    The check is "unconfigured" until install sets hub.bound.
    """
    source = source.strip().casefold()
    if source not in {"startup", "resume", "clear"}:
        raise PolicyError("freshness source must be startup, resume, or clear")
    # A private index (harness-review lanes inherit one) would take the fast-forward
    # while the checkout's own index kept the old tree: upstream then reads as staged reverts.
    if os.environ.get("GIT_INDEX_FILE"):
        return {
            "state": "blocked", "reason": "private_index", "source": source,
            "message": (
                "GIT FRESHNESS BLOCKED — GIT_INDEX_FILE names a private index, so moving HEAD "
                "would leave the checkout's index on the old tree. Nothing was fetched or moved; "
                "unset GIT_INDEX_FILE and run freshness again."
            ),
        }
    repo = _repo_root(repo)
    if _config(repo, "hub.bound").casefold() != "true":
        return {
            "state": "unconfigured", "source": source,
            "message": (
                "GIT FRESHNESS SETUP REQUIRED — this Hub checkout has no verified branch-policy "
                "binding. Run the setup skill's branch binding before code work; do not commit or push."
            ),
        }

    with _FreshnessLock(repo, lock_timeout):
        # Recovery remains discoverable on a detached rebase HEAD.
        operation = _operation_in_progress(repo)
        recovery_branch = _current_branch(repo) or _rebase_branch(repo) or "detached HEAD"
        if operation:
            return {
                "state": "blocked", "reason": "operation_in_progress", "source": source,
                "branch": recovery_branch,
                "message": (
                    f"GIT FRESHNESS BLOCKED — {operation} is already in progress on "
                    f"{recovery_branch}. Resolve or abort it before new code work; accepted main "
                    "was not fetched."
                ),
            }
        branch = str(status(repo)["branch"])
        expected_url = _config(repo, "hub.originUrl")
        actual_url = _git(repo, "remote", "get-url", "origin").stdout.strip()
        if not expected_url or actual_url != expected_url:
            raise PolicyError("the origin URL disagrees with the verified checkout binding")
        try:
            fetched = _fetch_main(repo, fetch_timeout)
        except GitTimeout as exc:
            return {
                "state": "blocked",
                "reason": ("fetch_timeout" if exc.quiesced else "fetch_process_tree_unverified"),
                "source": source,
                "branch": branch,
                "message": (
                    "GIT FRESHNESS BLOCKED — fetching accepted main exceeded its bounded timeout. "
                    + (
                        "The Git process tree stopped cleanly; authentication or network repair is required."
                        if exc.quiesced else
                        "The Git process tree could not be proven stopped; the owning agent must "
                        "verify and recover it before continuing."
                    )
                    + " Do not claim this checkout is current."
                ),
            }
        if fetched.returncode:
            return {
                "state": "blocked", "reason": "fetch_failed", "source": source,
                "branch": branch,
                "message": (
                    "GIT FRESHNESS BLOCKED — accepted main could not be fetched: "
                    f"{_failure_detail(fetched)}. Authentication or network repair is required; "
                    "do not claim this checkout is current."
                ),
            }

        head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()
        accepted = _git(repo, "rev-parse", "refs/remotes/origin/main").stdout.strip()
        ahead, behind = _distance(repo)
        dirty = bool(_git(repo, "status", "--porcelain", "--untracked-files=normal").stdout.strip())
        contains_main = _git(
            repo, "merge-base", "--is-ancestor", "refs/remotes/origin/main", "HEAD",
            check=False,
        ).returncode == 0
        common = {
            "source": source, "branch": branch, "head_before": head_before, "accepted_main": accepted,
            "ahead": ahead, "behind": behind, "dirty": dirty,
        }
        if contains_main:
            return {
                **common, "state": "current", "head_after": head_before,
                "message": (
                    f"GIT FRESHNESS CURRENT — {branch} contains accepted main {accepted[:12]} "
                    f"(ahead {ahead}); no rebase was needed."
                ),
            }
        if dirty:
            # The move keeps every staged entry and edit it does not collide with; on a
            # collision it returns the commit to carry the uncommitted work onto.
            moved = (_replay_in_place if ahead else _fast_forward)(repo, common, rebase_timeout)
            if isinstance(moved, dict):
                return moved
            blocked = _carry_onto(repo, common, head_before, moved, time.monotonic() + rebase_timeout,
                                  by="freshness", onto=f"accepted main {accepted[:12]}")
            if blocked:
                return blocked
            carried = _git(repo, "status", "--porcelain", "--untracked-files=normal").stdout.splitlines()
            final_ahead, final_behind = _distance(repo)
            return {
                **common, "state": "rebased", "head_after": moved, "carried": len(carried),
                "ahead": final_ahead, "behind": final_behind,
                "message": (
                    f"GIT FRESHNESS REBASED — {branch} moved onto accepted main {accepted[:12]} "
                    f"(HEAD {moved[:12]}, ahead {final_ahead}); uncommitted work was carried, staged "
                    "and unstaged, nothing was committed."
                ),
            }

        timed_out = False
        try:
            rebased = _git(
                repo, "rebase", "refs/remotes/origin/main", check=False,
                timeout=rebase_timeout,
            )
        except GitTimeout as exc:
            if not exc.quiesced:
                result = {
                    **common, "state": "blocked", "reason": "rebase_process_tree_unverified",
                    "head_after": None,
                    "message": (
                        "GIT FRESHNESS BLOCKED — the bounded rebase timed out and its process tree "
                        "could not be proven stopped. No recovery command was started; wait for "
                        "owning agent recovery before touching this worktree."
                    ),
                }
                return result
            timed_out = True
            rebased = subprocess.CompletedProcess(
                ["git", "rebase", "refs/remotes/origin/main"], 124,
                stdout="", stderr="rebase exceeded its bounded timeout",
            )
        if rebased.returncode:
            conflict_result = _git(
                repo, "diff", "--name-only", "--diff-filter=U", check=False,
            )
            conflicts = [line for line in conflict_result.stdout.splitlines() if line.strip()]
            active_rebase = _operation_in_progress(repo)
            if active_rebase not in {"rebase-merge", "rebase-apply"}:
                head_after_failure = _git(
                    repo, "rev-parse", "HEAD", check=False,
                ).stdout.strip()
                failure_dirty = bool(_git(repo, "status", "--porcelain").stdout.strip())
                if head_after_failure == head_before and not failure_dirty:
                    return {
                        **common, "state": "blocked",
                        "reason": ("rebase_timeout" if timed_out else "rebase_failed"),
                        "head_after": head_after_failure,
                        "message": (
                            f"GIT FRESHNESS BLOCKED — Git could not start or complete the "
                            f"{'bounded ' if timed_out else ''}rebase: "
                            f"{_failure_detail(rebased)}. The original clean branch was unchanged; "
                            "repair the Git configuration or upstream condition before new code work."
                        ),
                    }
                return {
                    **common, "state": "blocked", "reason": "rebase_recovery_required",
                    "head_after": head_after_failure,
                    "message": (
                        "GIT FRESHNESS BLOCKED — rebase failed outside a recoverable rebase state "
                        "and the worktree changed. Stop work and repair this worktree before continuing."
                    ),
                }
            try:
                aborted = _git(
                    repo, "rebase", "--abort", check=False, timeout=recovery_timeout,
                )
            except GitTimeout as exc:
                if not exc.quiesced:
                    result = {
                        **common, "state": "blocked", "reason": "abort_process_tree_unverified",
                        "conflicts": conflicts, "head_after": None,
                        "message": (
                            "GIT FRESHNESS BLOCKED — the bounded rebase abort timed out and its "
                            "process tree could not be proven stopped. Recovery is unverified; stop "
                            "work and repair this worktree."
                        ),
                    }
                    return result
                aborted = subprocess.CompletedProcess(
                    ["git", "rebase", "--abort"], 124,
                    stdout="", stderr="rebase abort exceeded its bounded timeout",
                )
            head_after_abort = _git(repo, "rev-parse", "HEAD", check=False).stdout.strip()
            dirty_after_abort = bool(_git(repo, "status", "--porcelain").stdout.strip())
            if aborted.returncode:
                result = {
                    **common, "state": "blocked", "reason": "abort_failed",
                    "conflicts": conflicts, "head_after": head_after_abort,
                    "message": (
                        "GIT FRESHNESS BLOCKED — rebase failed and automatic abort did not restore "
                        "the original HEAD. Stop work and repair this worktree before continuing."
                    ),
                }
                return result
            if head_after_abort != head_before or dirty_after_abort:
                return {
                    **common, "state": "blocked", "reason": "abort_failed",
                    "conflicts": conflicts, "head_after": head_after_abort,
                    "message": (
                        "GIT FRESHNESS BLOCKED — rebase failed and automatic abort did not restore "
                        "the original clean branch. Stop work and repair this worktree before continuing."
                    ),
                }
            names = ", ".join(conflicts[:5]) if conflicts else "conflicting commits"
            return {
                **common, "state": "blocked",
                "reason": ("rebase_timeout" if timed_out else "rebase_conflict"),
                "conflicts": conflicts, "head_after": head_after_abort,
                "message": (
                    f"GIT FRESHNESS BLOCKED — rebasing {branch} onto accepted main "
                    f"{'timed out while resolving' if timed_out else 'conflicted in'} "
                    f"{names}. The rebase was aborted and HEAD {head_after_abort[:12]} restored. Run "
                    "git rebase origin/main, resolve each conflict, finish the rebase, and run "
                    f"{PY} Drawer/git-branch-policy.py publish. Never ask the user."
                ),
            }

        head_after = _git(repo, "rev-parse", "HEAD").stdout.strip()
        if _git(
            repo, "merge-base", "--is-ancestor", "refs/remotes/origin/main", "HEAD",
            check=False,
        ).returncode:
            return {
                **common, "state": "blocked", "reason": "rebase_recovery_required",
                "head_after": head_after,
                "message": (
                    "GIT FRESHNESS BLOCKED — the completed rebase does not contain accepted main. "
                    "The owning agent must repair the branch before new code work."
                ),
            }
        final_ahead, final_behind = _distance(repo)
        return {
            **common, "state": "rebased", "head_after": head_after,
            "ahead": final_ahead, "behind": final_behind,
            "message": (
                f"GIT FRESHNESS REBASED — {branch} now contains accepted main {accepted[:12]} "
                f"(HEAD {head_after[:12]}, ahead {final_ahead})."
            ),
        }


def _hook_refused(result: subprocess.CompletedProcess[str]) -> bool:
    """True when a hook refused the push: origin's '[remote rejected]' (pre-receive, update) or a local
    pre-push hook. A refusal is never an origin move, so land reports it in the first round."""
    text = f"{result.stderr or ''}\n{result.stdout or ''}"
    return "[remote rejected]" in text or "hook declined" in text or "pre-push hook" in text


def _push(repo: Path, push: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    """git <push> through Drawer/pushretry.py: a remote-side fault retries after its backoff; any other
    refusal returns at once. A push past its bounded timeout is exit 124."""
    import pushretry  # here, not at the top: the hooks run this file where pushretry may be absent

    def once() -> subprocess.CompletedProcess[str]:
        try:
            return _git(repo, *push, check=False, env=_remote_env(), timeout=timeout)
        except GitTimeout:
            return subprocess.CompletedProcess(push, 124, stdout="", stderr="the push exceeded its bounded timeout")

    return pushretry.run(once)


def publish(repo: Path, *, push_timeout: float = PUSH_TIMEOUT_SECONDS) -> dict[str, object]:
    """The one close command: refresh on accepted main, then push main."""
    repo = _repo_root(repo)
    fresh = freshness(repo, "resume")
    if fresh.get("state") not in {"current", "rebased"}:
        return fresh
    branch = str(fresh["branch"])
    if TASK_BRANCH.fullmatch(branch):
        return {**fresh, "source": "publish", "state": "blocked", "reason": "task_branch", "message": (
            f"GIT PUBLISH BLOCKED — {branch} is never pushed. In the main checkout run "
            f"land --name {branch.removeprefix('task/')}."
        )}
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    common = {"source": "publish", "branch": branch, "head": head, "freshness": fresh["state"]}
    pushed = _push(repo, ["push", "origin", "HEAD:refs/heads/main"], push_timeout)
    if pushed.returncode:
        return {**common, "state": "blocked", "reason": "push_failed", "message": (
            f"GIT PUBLISH BLOCKED — the push of {branch} failed: {_failure_detail(pushed)}. "
            "Run publish again; fix the named cause first when it repeats."
        )}
    return {**common, "state": "published", "pushed_ref": "refs/heads/main",
            "message": f"GIT PUBLISHED — {head[:12]} is on origin/main."}


def _task_name(repo: Path, name: str) -> str:
    branch = f"task/{name}"
    if not TASK_BRANCH.fullmatch(branch):
        raise PolicyError("a task name is lower-case letters, digits, '.', '_' and '-', starting with a letter or digit")
    if _linked(repo):
        raise PolicyError("run task worktree commands from the main checkout")
    return branch


def _venv_link(path: Path) -> bool:
    link = path / VENV
    return os.path.islink(link) or getattr(os.path, "isjunction", lambda _: False)(link)


def _link_venv(target: Path, link: Path) -> None:
    """Link the main checkout's interpreter folder into a task worktree: a symlink, on Windows a junction
    (it needs no privilege; _unlink_venv drops it with rmdir)."""
    if oscompat.WINDOWS:
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def _unlink_venv(path: Path) -> None:
    """Remove the interpreter link first, so no removal can reach the main checkout's interpreter."""
    link = path / VENV
    if os.path.islink(link):
        os.unlink(link)
    elif _venv_link(path):
        os.rmdir(link)  # a junction: rmdir drops the link and keeps its target
    elif os.path.lexists(link):
        raise PolicyError(f"{link} is not a link to the main checkout's interpreter; remove it by hand")


def _drop_seat(path: Path) -> None:
    from runtime import profile
    _unlink_venv(path)
    shutil.rmtree(profile.default_profile_path(path).parent, ignore_errors=True)


SWEEPABLE = ("done", "dropped")  # task states whose <task8>-* hub tree task_sweep may remove


def _open_mem():
    """The mem store the CLI uses (product_worktree.open_mem): <env_prefix>_MEM_DB, a test run's scratch DB, else the live DB."""
    import product_worktree
    return product_worktree.open_mem()


def _task8(name: str) -> str:
    """The first '-' part of a task worktree name when it is a uuid8 (a task8 or a session uuid8), else ''."""
    part = name.lower().split("-")[0]
    return part if re.fullmatch(r"[0-9a-f]{8}", part) else ""


def _task_card(store, task8: str):
    """The task row whose id starts with task8, or None when no card matches or the card is no task."""
    try:
        row = store.find(task8)
    except Exception as exc:  # MemError: no card, or more than one
        if str(exc).startswith("no card"):
            return None
        raise PolicyError(f"task {task8}: {exc}")
    return row if row["kind"] == "task" else None


def _mem_or_refuse(why: str):
    try:
        return _open_mem()
    except Exception as exc:  # a mem fault is no proof of a claim either way
        raise PolicyError(f"mem unreadable ({str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__}); {why}")


def _own_name(name: str) -> None:
    """A new task worktree name carries this session's uuid8 as one '-' part, or starts
    with the task8 of a card this session holds a live claim on: legs share one session id, so the task8
    names whose tree it is."""
    own = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip().lower()[:8]
    if not re.fullmatch(r"[0-9a-f]{8}", own):
        raise PolicyError("CLAUDE_CODE_SESSION_ID is not set; a task worktree name is <uuid8>-<slug>, "
                          "the uuid8 your session id's first 8 characters")
    if own in name.split("-"):
        return
    task8 = _task8(name)
    if task8:
        store = _mem_or_refuse(f"no proof this session claims task {task8}")
        row = _task_card(store, task8)
        if row is not None and row["claim_id"] and (row["claim_session"] or "").strip().lower()[:8] == own \
                and store.claim_state(row) == "live":
            return
    raise PolicyError(f"a task worktree name carries your session uuid8 as one '-' part: "
                      f"<uuid8>-<slug>, as {own}-{name}; or starts with the task8 of a card your session "
                      f"holds a live claim on: <task8>-<slug>")


def worktree_add(repo: Path, name: str) -> dict[str, object]:
    """Add task/<name> in its own linked worktree, untracked, and make it a seat of this checkout.

    The worktree gets the main checkout's interpreter as a link, a copy of this checkout's seat
    profile naming the worktree, and its own generated MCP renders, so hooks, mcp-live and the
    policy run there as in the main checkout. Git copies the worktree-scoped policy config.
    """
    from runtime import profile
    branch = _task_name(repo, name)
    _own_name(name)
    _require_bound(repo)
    path = repo.parent / (repo.name + WORKTREE_SUFFIX) / name
    if os.path.lexists(path):
        raise PolicyError(f"{path} already exists")
    if _git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0:
        raise PolicyError(f"{branch} already exists; finish or remove it first")
    fetched = _fetch_main(repo, FETCH_TIMEOUT_SECONDS)
    if fetched.returncode:
        raise PolicyError(f"accepted main could not be fetched: {_failure_detail(fetched)}")
    _git(repo, "worktree", "add", "--no-track", "-b", branch, str(path), "refs/remotes/origin/main")
    try:
        if (repo / VENV).is_dir():
            _link_venv(repo / VENV, path / VENV)
        source = profile.default_profile_path(repo)
        if source.exists():
            seat = profile.load_profile(source)
            seat["hub_root"] = str(path)
            profile.save_profile(profile.default_profile_path(path), seat)
        rendered = subprocess.run(
            [sys.executable, "-B", str(path / "Drawer" / "settings-sync.py"), "--mcp"],
            cwd=path, capture_output=True, text=True, check=False,
        )
        if rendered.returncode:
            raise PolicyError(f"the worktree MCP render failed: {_failure_detail(rendered)}")
        checked = status(path)  # the worktree reads back bound, on its task branch
    except BaseException:
        _drop_seat(path)
        _git(repo, "worktree", "remove", "--force", str(path), check=False)
        _git(repo, "branch", "-D", branch, check=False)
        raise
    return {"result": "PASS", "branch": checked["branch"], "path": str(path),
            "message": f"TASK WORKTREE READY — {branch} at {path}, no upstream; commit there."}


def _refuse_live(name: str, own: str = "") -> None:
    """Refuse the tree of another live session: a uuid8 '-' part of name that a running Claude Code
    session carries (gatelib.live_sessions), other than the caller's own (own, else the session
    variable). A session row of a running pid that cannot be read refuses too: it is no proof the tree
    is free."""
    own = (own or os.environ.get("CLAUDE_CODE_SESSION_ID", "")).strip().lower()[:8]
    live, unread = gatelib.live_sessions(Path.home(), own)
    if unread:
        raise PolicyError(f"{name}: session row {unread[0]} is unreadable; no proof the tree is free, it stays")
    peers = sorted({part for part in name.lower().split("-") if re.fullmatch(r"[0-9a-f]{8}", part)} & live - {own})
    if peers:
        raise PolicyError(f"{name} is the worktree of live session {peers[0]}; it stays")


def _refuse_claimed(name: str, own: str = "") -> None:
    """Refuse a <task8>-* tree whose task card holds a live claim (or one of unknown liveness): a leg of
    that task may still work in it. A name led by the caller's own uuid8 is a session tree."""
    task8 = _task8(name)
    own = (own or os.environ.get("CLAUDE_CODE_SESSION_ID", "")).strip().lower()[:8]
    if not task8 or task8 == own:
        return
    store = _mem_or_refuse(f"no proof task {task8} holds no live claim; {name} stays")
    row = _task_card(store, task8)
    if row is not None and row["claim_id"] and (state := store.claim_state(row)) in ("live", "unknown"):
        raise PolicyError(f"{name}: task {task8} claim {row['claim_id'][:8]} is {state} "
                          f"(session {row['claim_session']}); it stays")


def task_sweep(repo: Path, own: str = "", deadline: float | None = None) -> tuple[list[str], list[dict[str, str]]]:
    """Remove each <task8>-* hub task tree and branch whose card is done or dropped, holds no claim and
    no worktree column, through worktree_remove (patch-equal on origin/main, clean, no live peer). A name
    that carries own's uuid8 is own-cleanup's; a name whose task8 matches no task card is a session tree
    and is not read. (removed branches, held [{item, reason}]); a mem fault holds every tree."""
    own8 = own.strip().lower()[:8]
    names = [b.removeprefix("task/") for b in sorted(
        _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/task/").stdout.split())]
    names = [n for n in names if _task8(n) and not (own8 and own8 in n.split("-"))]
    removed: list[str] = []
    held: list[dict[str, str]] = []
    if not names:
        return removed, held
    try:
        store = _open_mem()
    except Exception as exc:
        return removed, [{"item": "task trees", "reason": f"held: mem unreadable ({str(exc).strip()[:200]})"}]
    for name in names:
        branch = f"task/{name}"
        try:
            row = _task_card(store, _task8(name))
        except PolicyError as exc:
            held.append({"item": branch, "reason": str(exc)})
            continue
        if row is None:
            continue
        if row["status"] not in SWEEPABLE:
            held.append({"item": branch, "reason": f"task {row['status']}"})
        elif row["claim_id"]:
            held.append({"item": branch, "reason": f"card holds claim {row['claim_id'][:8]}"})
        elif row["worktree"]:
            held.append({"item": branch, "reason": f"card names worktree {row['worktree']}"})
        elif deadline is not None and time.monotonic() > deadline:
            held.append({"item": branch, "reason": "out of the own-cleanup time budget"})
        else:
            try:
                worktree_remove(repo, name, own=own)
                removed.append(branch)
            except (PolicyError, OSError) as exc:
                held.append({"item": branch, "reason": str(exc).splitlines()[0]})
    return removed, held


def _log_removal(path: Path | None, branch: str, tip: str, basis: str) -> None:
    """Best effort, as gatelib.append: the removal already happened, so a log fault never fails it."""
    try:
        REMOVE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with REMOVE_LOG.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} worktree-remove branch={branch} "
                         f"path={path or ''} tip={tip} basis={basis}\n")
    except OSError:
        pass


def worktree_remove(repo: Path, name: str, own: str = "") -> dict[str, object]:
    """Delete task/<name> and its worktree once accepted main holds every commit on it, as an ancestor or
    patch-equal after a rebased land, and no live peer session owns the tree. Each removal is logged."""
    branch = _task_name(repo, name)
    tip = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False).stdout.strip()
    if not tip:
        raise PolicyError(f"{branch} does not exist")
    _refuse_live(name, own)
    _refuse_claimed(name, own)
    fetched = _fetch_main(repo, FETCH_TIMEOUT_SECONDS)
    if fetched.returncode:
        raise PolicyError(f"accepted main could not be fetched: {_failure_detail(fetched)}")
    basis = _branch_tip_in_accepted_main(repo, tip)
    if basis is None:
        raise PolicyError(f"{branch} tip {tip[:12]} is not on origin/main; merge it and publish first")
    path = None
    current = None
    for line in _git(repo, "worktree", "list", "--porcelain").stdout.splitlines():
        if line.startswith("worktree "):
            current = Path(line.removeprefix("worktree "))
        elif line == f"branch refs/heads/{branch}":
            path = current
    if path is not None:
        # The interpreter link is this command's own; a POSIX symlink escapes a "dir/" ignore rule.
        dirty = _git(path, "status", "--porcelain", "--untracked-files=normal", "--",
                     ".", f":(exclude){VENV}").stdout.strip()
        if dirty:
            raise PolicyError(f"{path} has uncommitted work; commit or discard it first:\n{dirty}")
        _drop_seat(path)
        _git(repo, "worktree", "remove", str(path))
        if path.parent.name.endswith(WORKTREE_SUFFIX):
            with contextlib.suppress(OSError):  # the last tree gone: no empty <hub>_wt folder stays beside the hub
                path.parent.rmdir()
    _git(repo, "branch", "-D", branch)  # proved on origin/main above
    _log_removal(path, branch, tip, basis)
    return {"result": "PASS", "branch": branch, "path": str(path) if path else "",
            "message": f"TASK WORKTREE REMOVED — {branch} is on origin/main; branch and worktree deleted."}


def _temp_roots() -> list[Path]:
    """The temp folders a session writes scratch to: the system one (TMPDIR) and /tmp, once each."""
    roots: list[Path] = []
    for raw in (tempfile.gettempdir(), "/tmp"):
        root = Path(raw)
        if root.is_dir() and root.resolve() not in {r.resolve() for r in roots}:
            roots.append(root)
    return roots


def _trash(path: Path) -> None:
    """Move path to the trash (root AGENTS.md: trash, not rm). No trash command keeps it."""
    tool = shutil.which("trash") or next((t for t in ("/usr/bin/trash", "/opt/homebrew/bin/trash")
                                          if os.access(t, os.X_OK)), None)
    if not tool and oscompat.WINDOWS:
        try:
            oscompat.recycle(path)
        except OSError as exc:
            raise PolicyError(f"trash failed: {exc}") from exc
        return
    if not tool:
        raise PolicyError("no trash command on this box; it stays")
    ran = subprocess.run([tool, str(path)], capture_output=True, text=True, check=False, timeout=TRASH_TIMEOUT_SECONDS)
    if ran.returncode or os.path.lexists(path):
        raise PolicyError(f"trash failed: {_failure_detail(ran)}")


def own_cleanup(repo: Path, session: str = "") -> dict[str, object]:
    """Remove what the closing session made and count what stays.

    Own = a name that carries the session's uuid8 as one part. A hub task branch, with its worktree,
    goes only through worktree_remove: landed on origin/main, clean and no live peer's. A refused one is
    kept, with its reason. A folder directly under a temp root goes to the trash; a linked worktree
    there (a product session/<uuid8> tree), and any folder with a .git in it or one level down (a scratch
    clone may hold unpushed work), is left, untouched. Product repo trees and branches are not read at
    all: the session-start sweep owns them. Past CLEANUP_BUDGET_SECONDS the rest is kept, inside the hook's 120 s.
    A box with no trash command (and no Recycle Bin) keeps every temp folder, listed as leftover.
    Then task_sweep: a done or dropped card's <task8>-* tree goes; one it holds is listed in left.
    """
    me = (session or os.environ.get("CLAUDE_CODE_SESSION_ID", "")).strip().lower()
    if not SESSION_ID.fullmatch(me):
        raise PolicyError("own-cleanup needs the session id (CLAUDE_CODE_SESSION_ID or the hook payload)")
    if _linked(repo):
        repo = _common_dir(repo)[1].parent  # a task worktree's hook: the removal runs from main
    own = me[:8]
    deadline = time.monotonic() + CLEANUP_BUDGET_SECONDS
    removed: list[str] = []
    kept: list[dict[str, str]] = []
    left: list[dict[str, str]] = []
    branches = _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/task/").stdout.split()
    for branch in sorted(branches):
        name = branch.removeprefix("task/")
        if own not in name.split("-"):
            continue
        if time.monotonic() > deadline:
            kept.append({"item": branch, "reason": "out of the own-cleanup time budget"})
            continue
        try:
            worktree_remove(repo, name, own=me)
            removed.append(branch)
        except (PolicyError, OSError) as exc:
            kept.append({"item": branch, "reason": str(exc).splitlines()[0]})
    # <task8>-* trees carry no session uuid8: done ones go, the rest are counted as left, not kept.
    swept, held = task_sweep(repo, me, deadline)
    removed += swept
    left += held
    for root in _temp_roots():
        for entry in sorted(root.iterdir()):
            if own not in re.split(r"[^0-9a-z]+", entry.name.lower()):
                continue
            if entry.is_symlink() or not entry.is_dir():
                continue  # own temp folders only; a file or a link is no tree
            if _holds_git(entry):
                left.append({"item": str(entry), "reason": "a git tree; the session-start sweep or worktree-remove owns it"})
                continue
            if time.monotonic() > deadline:
                kept.append({"item": str(entry), "reason": "out of the own-cleanup time budget"})
                continue
            try:
                _trash(entry)
                removed.append(str(entry))
            except (PolicyError, OSError, subprocess.SubprocessError) as exc:
                kept.append({"item": str(entry), "reason": str(exc)})
    return {"result": "PASS" if not kept else "LEFTOVER", "session": own, "removed": removed,
            "kept": kept, "leftover": len(kept), "left": left}


def _holds_git(entry: Path) -> bool:
    """True when entry or a folder directly in it holds a .git (a clone or a worktree)."""
    if os.path.lexists(entry / ".git"):
        return True
    try:
        return any(os.path.lexists(child / ".git") for child in entry.iterdir()
                   if child.is_dir() and not child.is_symlink())
    except OSError:
        return True  # unreadable: no proof it holds no work


def own_cleanup_at_session_end(repo_arg: str, payload: str) -> str:
    """The SessionEnd hook: own-cleanup for the payload's session_id, one line to OWN_CLEANUP_LOG.
    Never fails the close: every fault becomes the log line. A session variable that names another
    session is a forged call: nothing goes. A /clear or a resume ends the session id but not the
    process, whose shell may still sit in its tree: nothing goes then either."""
    try:
        data = json.loads(payload or "{}")
        data = data if isinstance(data, dict) else {}
        session = str(data.get("session_id", "")).strip().lower()
        reason = str(data.get("reason", ""))
        held = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip().lower()
        if held and held != session:
            raise PolicyError("the payload session id is not this environment's; nothing removed")
        if reason in SESSION_END_STAYS:
            line = f"session={session[:8]} SKIPPED reason={reason}: the process lives on"
        else:
            result = own_cleanup(_repo_root(repo_arg), session)
            line = (f"session={result['session']} {result['result']} removed={result['removed']} "
                    f"leftover={result['leftover']} kept={[k['item'] + ': ' + k['reason'] for k in result['kept']]} "
                    f"left={[i['item'] for i in result['left']]}")
    except Exception as exc:  # an unattended close hook: a fault is logged, never raised
        kind = "" if isinstance(exc, PolicyError) else f"{type(exc).__name__}: "
        line = f"session=? REFUSED {kind}{exc}"
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} own-cleanup {line}"
    try:
        OWN_CLEANUP_LOG.parent.mkdir(parents=True, exist_ok=True)
        with OWN_CLEANUP_LOG.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass
    return line


def _branch_worktree(repo: Path, branch: str) -> Path | None:
    """The worktree that holds branch, else None."""
    path = current = None
    for line in _git(repo, "worktree", "list", "--porcelain").stdout.splitlines():
        if line.startswith("worktree "):
            current = Path(line.removeprefix("worktree "))
        elif line == f"branch refs/heads/{branch}":
            path = current
    return path


def _probe_tip(repo: Path, branch: str, tip: str) -> tuple[str, str] | None:
    """(reason, message) when the probe (Drawer/probes/probe.py) is not green at the branch tip, else None.

    It runs in the branch's own worktree, so it judges the committed tip: a worktree with tracked
    edits, or none, blocks too. A skipped probe proves nothing.
    """
    path = _branch_worktree(repo, branch)
    if path is None:
        return "no_worktree", f"{branch} has no worktree to run the probe in"
    if _git(path, "rev-parse", "HEAD").stdout.strip() != tip or _git(
            path, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        return "worktree_dirty", f"{path} has tracked edits past the tip; commit or drop them, then land again"
    try:
        run = subprocess.run([sys.executable, "-B", str(path / "Drawer" / "probes" / "probe.py")], cwd=path,
                             capture_output=True, text=True, check=False, timeout=PROBE_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as error:
        return "probe_red", f"the probe did not run: {error}"
    if run.returncode:
        red = [line for line in run.stdout.splitlines() if line.startswith("FAIL ")] or [_failure_detail(run)]
        return "probe_red", "the probe is red at the tip: " + "; ".join(red)[:1500]
    return None


def worktree_land(repo: Path, name: str, *, timeout: float = REBASE_TIMEOUT_SECONDS) -> dict[str, object]:
    """Fast-forward main to task/<name> in the main checkout, carrying any uncommitted work.

    Another session's staged and unstaged edits in the main checkout stay as they were, also on
    paths the branch changes; only a true conflict with them blocks, and then nothing moves. The
    probe must be green at the branch tip (_probe_tip): a hub commit never lands on a red probe.
    """
    branch = _task_name(repo, name)
    if os.environ.get("GIT_INDEX_FILE"):
        raise PolicyError("GIT_INDEX_FILE names a private index; unset it and land again")
    _require_bound(repo)
    tip = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False).stdout.strip()
    if not tip:
        raise PolicyError(f"{branch} does not exist")
    if _config(repo, f"branch.{branch}.remote") or _git(
            repo, "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{branch}", check=False).returncode == 0:
        raise PolicyError(f"{branch} is tracked or pushed; a task branch never is")
    fresh = freshness(repo, "resume")
    if fresh.get("state") not in {"current", "rebased"}:
        return fresh
    if fresh.get("branch") != "main":
        raise PolicyError("land a task branch from the main checkout on main")
    with _FreshnessLock(repo):
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        common = {"source": "worktree-land", "branch": "main", "task_branch": branch,
                  "head_before": head, "tip": tip}
        if head != fresh.get("head_after"):
            return {**common, "state": "blocked", "reason": "peer_commit_race", "head_after": head,
                    "message": "GIT LAND BLOCKED — main moved after freshness; nothing moved. Land again."}
        if head == tip:
            return {**common, "state": "current", "head_after": head,
                    "message": f"GIT LAND CURRENT — main already holds {branch}."}
        if _git(repo, "merge-base", "--is-ancestor", "refs/remotes/origin/main", tip, check=False).returncode:
            return {**common, "state": "blocked", "reason": "branch_behind_main", "head_after": head,
                    "message": (f"GIT LAND BLOCKED — {branch} lacks accepted main. In its worktree run "
                                "freshness --source resume, then land again; nothing moved.")}
        if _git(repo, "merge-base", "--is-ancestor", head, tip, check=False).returncode:
            return {**common, "state": "blocked", "reason": "not_fast_forward", "head_after": head,
                    "message": (f"GIT LAND BLOCKED — main holds commits {branch} lacks; publish main, "
                                "rebase the branch with freshness in its worktree, then land again.")}
        probed = _probe_tip(repo, branch, tip)
        if probed is not None:
            return {**common, "state": "blocked", "reason": probed[0], "head_after": head,
                    "message": f"GIT LAND BLOCKED — {probed[1]}; nothing moved."}
        result = _carry_onto(repo, common, head, tip, time.monotonic() + timeout, by="land", onto=branch)
        if result is None:
            carried = _git(repo, "status", "--porcelain", "--untracked-files=normal").stdout.splitlines()
            result = {
                **common, "state": "landed", "head_after": tip, "carried": len(carried),
                "message": (
                    f"GIT LANDED LOCALLY — {branch} fast-forwarded main to {tip[:12]}; uncommitted work "
                    "stayed in place, staged and unstaged, nothing was committed. Run publish, then "
                    f"worktree-remove --name {branch.removeprefix('task/')}."
                ),
            }
    if result["state"] == "landed":
        result["renders"] = _render_mcp(repo)
    return result


def _render_mcp(repo: Path) -> bool:
    """Render this checkout's MCP config from the tree now checked out."""
    return subprocess.run(
        [sys.executable, "-B", str(repo / "Drawer" / "settings-sync.py"), "--mcp"],
        cwd=repo, capture_output=True, text=True, check=False,
    ).returncode == 0


def _task8_arg(value: str) -> str:
    """argparse type of land --task: a task8 or a task uuid, as its task8."""
    found = re.fullmatch(r"([0-9a-f]{8})(-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?", value.strip().lower())
    if not found:
        raise argparse.ArgumentTypeError(f"--task takes a task8 or a task uuid, not {value!r}")
    return found.group(1)


def _steer_hold(name: str, task: str = "") -> str | None:
    """The why of a Flight STOP at step push (Drawer/steer_check.py) for the calling session, the uuid8 the
    task name carries or the task8 (--task), else None. With none, the land stops: no push runs
    unchecked."""
    import steer_check  # here, not at the top: the hooks run this file where steer_check may be absent
    subjects = [s for s in (os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip().lower()[:8],
                            steer_check.uuid8(name), task) if re.fullmatch(r"[0-9a-f]{8}", s)]
    if not subjects:
        return "no session for the steer check: set CLAUDE_CODE_SESSION_ID or name the task <uuid8>-<slug>"
    for subject in dict.fromkeys(subjects):
        why = steer_check.check(CFG, subject, "push")
        if why:
            return why
    return None


def land(repo: Path, name: str, *, tries: int = LAND_TRIES,
         push_timeout: float = PUSH_TIMEOUT_SECONDS, expect_tip: str = "", task: str = "") -> dict[str, object]:
    """Land task/<name> on origin/main and in the main checkout, in one call.

    Each round: freshness in the task worktree, the probe at the tip, the steer check, then the tip
    itself goes to origin/main through pushretry, so no commit gets a new id after the push. When
    origin/main moved under the push, the next round rebases the branch onto it, at most `tries`
    rounds. It reports landed only when origin/main, read back, holds the tip and worktree-land has
    moved the main checkout onto it. A probe_red, a not_fast_forward (main holds commits the branch
    lacks), a steer STOP (state need), a push_failed or an origin_moved pushes nothing; a refusal that
    names a hook (pre-receive, pre-push) is push_failed at once, never read as an origin move. A push that
    exits non-zero when the next fetch fails too is state partial, reason landing_unknown, pushed null:
    the push may have reached origin, and a rerun is idempotent. A fault after the push is state partial
    with pushed true: readback_failed, branch_moved (the branch moved during the land; origin/main holds
    the probed tip, the main checkout did not move) or a local_land_failed.
    """
    branch = _task_name(repo, name)
    if os.environ.get("GIT_INDEX_FILE"):
        raise PolicyError("GIT_INDEX_FILE names a private index; unset it and land again")
    _require_bound(repo)
    if _current_branch(repo) != "main":
        raise PolicyError("land a task branch from the main checkout on main")
    if not _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False).stdout.strip():
        raise PolicyError(f"{branch} does not exist")
    if _config(repo, f"branch.{branch}.remote") or _git(
            repo, "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{branch}", check=False).returncode == 0:
        raise PolicyError(f"{branch} is tracked or pushed; a task branch never is")
    wt = _branch_worktree(repo, branch)
    if wt is None:
        raise PolicyError(f"{branch} has no worktree; land it from the one that made it")
    common: dict[str, object] = {"source": "land", "task_branch": branch, "pushed": False}

    def fetch() -> subprocess.CompletedProcess[str]:
        try:
            return _fetch_main(repo, FETCH_TIMEOUT_SECONDS)
        except GitTimeout as exc:
            return subprocess.CompletedProcess(["fetch"], 124, stdout="", stderr=str(exc))

    def on_origin(sha: str) -> bool:
        return _git(repo, "merge-base", "--is-ancestor", sha, "refs/remotes/origin/main", check=False).returncode == 0

    probed = ""
    for attempt in range(1, tries + 1):
        fresh = freshness(wt, "resume")
        if fresh.get("state") not in {"current", "rebased"}:
            return {**fresh, **common, "attempts": attempt}
        tip = _git(repo, "rev-parse", "--verify", f"refs/heads/{branch}").stdout.strip()
        common.update(tip=tip, attempts=attempt)
        if expect_tip and tip != expect_tip:
            # The caller tested expect_tip (hub-land's smoke); a rebased tip is untested.
            return {**common, "state": "blocked", "reason": "tip_moved", "expected": expect_tip, "message": (
                f"GIT LAND BLOCKED — {branch} is at {tip[:12]}, not the tested {expect_tip[:12]} (freshness rebased "
                "it); nothing was pushed. Test the new tip, then land again.")}
        if tip != probed:
            red = _probe_tip(repo, branch, tip)
            if red is not None:
                return {**common, "state": "blocked", "reason": red[0],
                        "message": f"GIT LAND BLOCKED — {red[1]}; nothing was pushed."}
            probed = tip
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        if _git(repo, "merge-base", "--is-ancestor", head, tip, check=False).returncode:
            return {**common, "state": "blocked", "reason": "not_fast_forward", "message": (
                f"GIT LAND BLOCKED — main holds commits {branch} lacks; publish main, then land again. "
                "Nothing was pushed.")}
        why = _steer_hold(name, task)
        if why:
            return {**common, "state": "need", "reason": "steer", "step": "push", "why": why, "message": (
                f"GIT LAND PAUSED — {why}. Nothing was pushed; send Flight NEED steer push.")}
        # The probed sha itself, never the branch name: a commit made after the probe stays off origin.
        pushed = _push(repo, ["push", "origin", f"{tip}:refs/heads/main"], push_timeout)
        if not pushed.returncode:
            break
        fetched = fetch()
        if fetched.returncode == 0 and on_origin(tip):
            break  # the ref moved and the answer was lost (a timeout, a dropped link): the readback decides
        if fetched.returncode:
            return {**common, "state": "partial", "reason": "landing_unknown", "pushed": None, "message": (
                f"GIT LAND PARTIAL — the push of {branch} failed ({_failure_detail(pushed)}) and origin/main "
                f"could not be read back ({_failure_detail(fetched)}); landing state unknown; land again "
                "(a rerun is idempotent).")}
        moved = not _hook_refused(pushed) and _git(
            repo, "merge-base", "--is-ancestor", "refs/remotes/origin/main", tip, check=False).returncode != 0
        if not moved:
            return {**common, "state": "blocked", "reason": "push_failed", "message": (
                f"GIT LAND BLOCKED — the push of {branch} failed: {_failure_detail(pushed)}. "
                "Nothing landed; fix the named cause, then land again.")}
    else:
        return {**common, "state": "blocked", "reason": "origin_moved", "message": (
            f"GIT LAND BLOCKED — origin/main moved under the push {tries} times; nothing landed. Land again.")}
    fetched = fetch()
    if fetched.returncode or not on_origin(tip):
        return {**common, "state": "partial", "reason": "readback_failed", "message": (
            f"GIT LAND PARTIAL — the push of {branch} ran, but origin/main could not be read back to hold tip "
            f"{tip[:12]}: {_failure_detail(fetched) if fetched.returncode else 'not an ancestor'}. "
            "Do not claim it landed; land again.")}
    common["pushed"] = True
    moved = _git(repo, "rev-parse", "--verify", f"refs/heads/{branch}").stdout.strip()
    if moved != tip:
        return {**common, "state": "partial", "reason": "branch_moved", "message": (
            f"GIT LAND PARTIAL — origin/main holds {branch} tip {tip[:12]}, but the branch moved to {moved[:12]} "
            "during the land; the main checkout did not move. Land again.")}
    try:
        local = worktree_land(repo, name)
    except PolicyError as exc:
        local = {"reason": "local_land_failed", "message": str(exc)}
    if local.get("state") not in {"landed", "current"}:
        return {**local, **common, "state": "partial", "reason": local.get("reason") or "local_land_failed",
                "message": (f"GIT LAND PARTIAL — origin/main holds {branch} tip {tip[:12]}, but the main checkout "
                            f"did not move onto it: {local.get('message')} Fix that, then land again.")}
    # The push moved origin/main first, so worktree-land mostly reports current: render the landed tree here.
    return {**common, "state": "landed", "pushed_ref": "refs/heads/main", "head_after": tip,
            "renders": _render_mcp(repo), "message": (
                f"GIT LANDED — origin/main holds {branch} tip {tip[:12]} and the main checkout moved onto it. "
                f"Run worktree-remove --name {branch.removeprefix('task/')}.")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.replace("<python>", PY))
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in (
        "install", "status", "check-commit", "check-push", "freshness",
        "publish", "worktree-add", "worktree-land", "worktree-remove", "land", "own-cleanup",
    ):
        command = subcommands.add_parser(name)
        command.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
        if name == "check-push":
            command.add_argument("--remote", required=True)
            command.add_argument("--url", required=True)
        if name in {"worktree-add", "worktree-land", "worktree-remove", "land"}:
            command.add_argument("--name", required=True)
        if name == "land":
            command.add_argument("--tip", default="", help="push only this tip; a rebase to another is tip_moved")
            command.add_argument("--task", type=_task8_arg,
                                 help="the task8 (or task uuid) the steer check also reads")
        if name == "freshness":
            command.add_argument("--source", required=True, choices=("startup", "resume", "clear"))
        if name == "own-cleanup":
            command.add_argument("--session-end", action="store_true",
                                 help="the SessionEnd hook: reads the payload on stdin, logs, exits 0")
    args = parser.parse_args(argv)
    if args.command == "own-cleanup" and args.session_end:
        try:
            stream = getattr(sys.stdin, "buffer", None)
            payload = stream.read().decode("utf-8", "replace") if stream is not None else sys.stdin.read()
        except Exception:  # no stdin at all (None) or a broken pipe: an empty payload, logged as refused
            payload = ""
        own_cleanup_at_session_end(args.repo, payload)
        return 0
    try:
        repo = _repo_root(args.repo)
        if args.command == "install":
            result = install(repo)
        elif args.command == "status":
            result = status(repo)
        elif args.command == "check-commit":
            result = check_commit(repo)
        elif args.command == "freshness":
            result = freshness(repo, args.source)
        elif args.command == "publish":
            result = publish(repo)
            print(json.dumps(result, sort_keys=True))
            return 0 if result.get("state") == "published" else 1
        elif args.command == "worktree-add":
            result = worktree_add(repo, args.name)
        elif args.command == "worktree-remove":
            result = worktree_remove(repo, args.name)
        elif args.command == "own-cleanup":
            result = own_cleanup(repo)
            print(json.dumps(result, sort_keys=True))
            return 0 if result["result"] == "PASS" else 1
        elif args.command == "worktree-land":
            result = worktree_land(repo, args.name)
            print(json.dumps(result, sort_keys=True))
            return 0 if result.get("state") in {"landed", "current"} else 1
        elif args.command == "land":
            result = land(repo, args.name, expect_tip=args.tip, task=args.task or "")
            print(json.dumps(result, sort_keys=True))
            return {"landed": 0, "need": 75}.get(str(result.get("state")), 1)
        else:
            result = check_push(repo, args.remote, args.url, sys.stdin.read())
        if args.command in {"install", "status", "freshness",
                            "worktree-add", "worktree-remove"}:
            print(json.dumps(result, sort_keys=True))
        return 0
    except PolicyError as exc:
        print(f"Git policy: {exc}", file=sys.stderr)
        if args.command not in {"check-commit", "check-push"}:
            # A JSON verb refuses in JSON too, so a caller's .json() reads the refusal.
            print(json.dumps({"error": str(exc)}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
