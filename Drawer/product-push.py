"""product-push -- the product push as one call, from a product worktree.

    <python> Drawer/product-push.py <product worktree> [--session <id>] [--task <task8>] [--reviewed <hash>]

Run it after the commit, from the hub root. In the product worktree:

1. Refuse a main clone (a product worktree is a linked worktree under <repo>_wt/), a worktree left mid-rebase (step
   rebase-in-progress: finish or abort that rebase yourself; product-push never aborts it) and
   uncommitted tracked work.
2. git fetch origin main, then git rebase origin/main. A conflict aborts the rebase and stops.
3. HEAD already on origin/main: state already, nothing to push.
4. Ask the product repo's .githooks/pre-push (the push gate, PRD-2), with the push line on stdin, whether the
   verdict on disk covers HEAD. When it does not, run [product] gate_command in the worktree ({python} is the hub
   interpreter), then ask the hook again. A refusal after the gate stops. With no hook and an empty
   gate_command, no gate is set up and the push goes on; with gate_command set and no hook, the push stops.
5. Run the steer check (Drawer/steer_check.py, step push) for the session (--session, else
   CLAUDE_CODE_SESSION_ID) and the task8 (--task): a STOP on either pushes nothing: state need, exit 75. Flight
   holds one task's push by its task8, as legs share one session id. With no subject, the push stops.
   The check runs again before each retry after a remote-side fault, so a STOP set during the backoff holds it.
6. With --reviewed <hash> (the SHA-256 the review passed, from Drawer/review-diff.py): recompute the hash
   of HEAD against origin/main after each rebase. A different hash pushes nothing and runs no gate: state review,
   with the reviewed hash, the new hash and the new review args, exit 3. A head that changes no file against
   origin/main goes on.
   Risky: before the fetch it reads the task's tags (Drawer/review_pass.py task_tags) for --task, else for the product
   worktree folder name when it is 8 hex (product_worktree.py names it <task8>). With no such task there is no gate,
   as for a push that is not risky. A task tagged risky computes the hash with or without --reviewed and pushes
   nothing (state review, exit 3) unless --reviewed is that hash and risky-review stored its Pass (review_pass.py
   stored). A folder name mem has no card for (NoCard) is not risky; any other mem fault, for any task, is failed,
   step review.
7. Push HEAD to origin main through Drawer/pushretry.py (a remote-side fault retries). A non-fast-forward
   rejection goes back to step 2, at most 3 rounds; the gate runs again only when the hook finds HEAD uncovered.

It prints the gate's output to stderr and one JSON line to stdout: state pushed (head, gated, rounds, reviewed), already
(head), need (step push, why), review (reviewed, hash, head, args), or failed (step, exit, output). Exit 0 on pushed
or already, 1 on failed, 2 on bad arguments, 3 on review, 75 on need. A gate can pass 10 minutes: start the call through Drawer/long-job.py then.
Why: the product push was a hand retry loop with no steer check; a rebase after review could reach main unreviewed;
risky cards landed with no review.
"""
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

DRAWER = Path(__file__).resolve().parent
sys.path.insert(0, str(DRAWER))
import harness_config  # noqa: E402
import pushretry  # noqa: E402
import review_pass  # noqa: E402
import steer_check  # noqa: E402

_spec = importlib.util.spec_from_file_location("review_diff", DRAWER / "review-diff.py")
review_diff = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review_diff)

ROUNDS = 3
# git's own rejection line for a ref that origin moved past; a hook's output never starts with it.
REJECTED = re.compile(r"^ ! \[rejected\] .*\((non-fast-forward|fetch first)\)", re.MULTILINE)
USAGE = "usage: <python> Drawer/product-push.py <product worktree> [--session <id>] [--task <task8>] [--reviewed <hash>]\n"


def git(root, *args):
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def done(state, **fields):
    print(json.dumps({"state": state, **fields}))
    return {"pushed": 0, "already": 0, "review": 3, "need": steer_check.PAUSE}.get(state, 1)


def fail(step, output, code=1, **fields):
    return done("failed", step=step, exit=code, output=str(output).strip()[-3000:], **fields)


def tail(result):
    return (result.stdout or "") + (result.stderr or "")


def hook_argv(hook):
    """The product hook's runner, chosen by its shebang as Drawer/product-hooks/pre-push does."""
    first = hook.read_text(encoding="utf-8", errors="replace").split("\n", 1)[0]
    if first.startswith("#!") and "python" in first:
        return [sys.executable, "-B", str(hook)]
    return ["bash" if first.startswith("#!") and "bash" in first else "sh", str(hook)]


def covered(root, hook):
    """The product hook's own judgement: does the verdict on disk cover HEAD pushed onto origin/main?"""
    head = git(root, "rev-parse", "HEAD").stdout.strip()
    remote = git(root, "rev-parse", "refs/remotes/origin/main").stdout.strip()
    return subprocess.run([*hook_argv(hook), "origin"], cwd=str(root), capture_output=True, text=True,
                          input=f"HEAD {head} refs/heads/main {remote}\n", encoding="utf-8", errors="replace")


def gate_argv(cfg):
    """[product] gate_command split like a shell line, {python} as the hub interpreter; [] when empty."""
    command = str((cfg.get("product") or {}).get("gate_command") or "").strip()
    return [sys.executable if word == "{python}" else word for word in shlex.split(command)]


def main(argv=None, cfg=None):
    args = sys.argv[1:] if argv is None else list(argv)
    if args in (["--help"], ["-h"]):
        print(__doc__)
        return 0
    session = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    reviewed = None
    flags = dict(zip(args[1::2], args[2::2]))
    if (not args or len(args) % 2 == 0 or len(flags) != (len(args) - 1) // 2
            or set(flags) - {"--session", "--task", "--reviewed"}
            or not all(v.strip() and not v.startswith("-") for v in flags.values())):
        sys.stderr.write(USAGE)
        return 2
    if "--session" in flags:
        session = flags["--session"].strip()
    if "--reviewed" in flags:
        reviewed = flags["--reviewed"].strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", reviewed):
            sys.stderr.write("product-push: --reviewed takes the 64-hex SHA-256 from Drawer/review-diff.py\n")
            return 2
    task = flags.get("--task", "").strip().lower()
    if task and not re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?", task):
        sys.stderr.write("product-push: --task takes a task8 or a task uuid\n")
        return 2
    task = task[:8]
    cfg = cfg or harness_config.load(DRAWER.parent)
    try:
        gate = gate_argv(cfg)
    except ValueError as exc:
        return fail("gate", f"[product] gate_command does not parse: {exc}")
    if not Path(args[0]).is_dir():
        return fail("worktree", f"{args[0]} is not a folder", 2)
    top = git(Path(args[0]), "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        return fail("worktree", tail(top), top.returncode)
    root = Path(top.stdout.strip())
    dirs = git(root, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir").stdout.split()
    if not root.parent.name.endswith("_wt") or len(dirs) != 2 or dirs[0] == dirs[1]:
        return fail("worktree", f"{root} is a main clone; push only from a product worktree under <repo>_wt/", 2)
    if any((Path(dirs[0]) / name).exists() for name in ("rebase-merge", "rebase-apply")):
        return fail("rebase-in-progress", f"{root} is mid-rebase; finish it (git rebase --continue) or abort it "
                    "(git rebase --abort) yourself, then run product-push again; nothing fetched or pushed")
    dirty = git(root, "status", "--porcelain", "--untracked-files=no")
    if dirty.returncode != 0:
        return fail("clean", tail(dirty), dirty.returncode)
    if dirty.stdout.strip():
        return fail("clean", "uncommitted tracked work; commit it first:\n" + dirty.stdout)
    hook = root / ".githooks" / "pre-push"
    if gate and not hook.is_file():
        return fail("gate", f"[product] gate_command is set, but the product push gate {hook} is missing", 2)
    if not session and not task:
        return fail("steer", "no subject for the steer check: pass --session <id> or --task <task8>, "
                    "or set CLAUDE_CODE_SESSION_ID", 2)
    # A task tagged risky pushes only with a stored risky-review Pass for the diff it pushes.
    subject, risky = task, False
    if not subject and re.fullmatch(r"[0-9a-f]{8}", root.name):
        try:
            risky = "risky" in review_pass.task_tags(DRAWER.parent, root.name)
            subject = root.name
        except review_pass.NoCard:
            pass  # no such card: not risky
        except review_pass.Refused as exc:  # a mem fault (locked, timeout) fails closed
            return fail("review", f"the tags of task {root.name} are unreadable: {exc}")
    if task:
        try:
            risky = "risky" in review_pass.task_tags(DRAWER.parent, subject)
        except review_pass.Refused as exc:
            return fail("review", f"the tags of task {subject} are unreadable: {exc}")
    state_root = harness_config.path(cfg["project"]["state_root"])

    def stopped():
        """The need exit code when Flight holds a STOP for this push, else None."""
        why = steer_check.check_all(cfg, [session, task], "push")
        return done("need", step="push", why=why) if why else None

    gated = False
    for round_ in range(1, ROUNDS + 1):
        fetched = git(root, "fetch", "origin", "main")
        if fetched.returncode != 0:
            return fail("fetch", tail(fetched), fetched.returncode)
        rebased = git(root, "rebase", "refs/remotes/origin/main")
        if rebased.returncode != 0:
            git(root, "rebase", "--abort")
            return fail("rebase", tail(rebased), rebased.returncode)
        head = git(root, "rev-parse", "HEAD").stdout.strip()
        if git(root, "rev-list", "--count", "refs/remotes/origin/main..HEAD").stdout.strip() == "0":
            return done("already", head=head)
        need = stopped()
        if need is not None:
            return need
        if reviewed or risky:
            try:
                found = review_diff.build(root, state_root)
            except review_diff.Refused as exc:
                return fail("review", exc)
            why = found and review_pass.gate(state_root, risky, reviewed, found, subject)
            if why:
                return done("review", reviewed=reviewed, hash=found["hash"], head=found["head"], args=found, output=why)
        if hook.is_file() and covered(root, hook).returncode != 0:
            if not gate:
                return fail("gate", "the product push gate finds HEAD uncovered and [product] gate_command is empty")
            try:
                ran = subprocess.run(gate, cwd=str(root), stdout=sys.stderr, stderr=sys.stderr)
            except OSError as exc:
                return fail("gate", f"[product] gate_command did not start: {exc}")
            gated = True
            check = covered(root, hook)
            if check.returncode != 0:
                return fail("gate", tail(check), check.returncode, gate_exit=ran.returncode)
        need = stopped()
        if need is not None:
            return need
        if git(root, "rev-parse", "HEAD").stdout.strip() != head:
            return fail("push", "HEAD moved during the gate; nothing pushed; run again")
        held = []
        pushed = pushretry.run(lambda: git(root, "push", "origin", "HEAD:main"),
                               hold=lambda: held.append(steer_check.check_all(cfg, [session, task], "push"))
                               or held[-1])
        if pushed is None:
            return done("need", step="push", why=held[-1])
        if pushed.returncode == 0:
            return done("pushed", head=head, gated=gated, rounds=round_, reviewed=reviewed,
                        output=(pushed.stderr or "").strip()[-1500:])
        if not REJECTED.search(pushed.stderr or ""):
            return fail("push", tail(pushed), pushed.returncode)
    return fail("push", f"origin/main moved under the push {ROUNDS} times; run again")


if __name__ == "__main__":
    sys.exit(main())
