"""hub-land -- the hub close of the git-flow skill (section 2), as one call from a task worktree.

    <python> Drawer/hub-land.py --name <name> [--task <task8>] [--reviewed <hash>] [(--message <text> | --message-file <file>) -- <paths>...]

<name> is the task worktree's name (git-branch-policy.py worktree-add --name). Run it from the main checkout or
the task worktree; each step runs its own checkout's Drawer script: commit and smoke in the worktree, land and
worktree-remove in the main checkout. A copy outside the main checkout (a task worktree's) runs the main checkout's
copy with the same arguments and exits with its code, so a branch cut before a hub-land fix lands with the fixed
lander.

1. With paths: in the worktree, git add -- <paths> (a deleted path: git rm --cached), then git commit -- <paths>
   (the commit hooks run). A path with no staged change commits nothing. With no paths it lands the commits the
   worktree already holds.
   Relative paths are from the worktree root. A named path neither in the worktree nor tracked fails step stage.
2. Uncommitted work left in the worktree fails step stage. In the worktree: [smoke] land_command of the worktree's
   harness.toml, else [smoke] command ({python} is this interpreter; land_command runs only the hub tests the
   diff reaches), limited to
   [smoke] timeout_seconds, with its own temp folder (Drawer/run_tmp.py). Red, timed out or [smoke] blocked_exit: failed, step smoke, nothing pushed. An empty
   command runs no smoke and says smoke "unset". Freshness runs in the worktree first, so the smoke judges the
   tip land pushes: land --tip refuses any other (tip_moved), and the call smokes the new tip again, at most
   3 rounds.
   With --reviewed <hash> (the SHA-256 the review passed, from Drawer/review-diff.py), each round recomputes
   the hash of the tip against origin/main after freshness, before the smoke. A different hash smokes and pushes
   nothing: state review, with the reviewed hash, the new hash and the new review args, exit 3. A tip that changes
   no file against origin/main goes on.
   Risky: before the fetch it reads the task's tags (Drawer/review_pass.py task_tags) for --task, else for the
   trailing and the leading 8-hex part of <name> (<session8>-<task8> or <task8>-<slug>; a part mem has a card for
   is the task, a risky one first). When neither part is a card, there is no task and no gate, as for a land that
   is not risky. A task tagged risky computes the hash in each round with or without --reviewed, and smokes and
   pushes nothing (state review, exit 3) unless --reviewed is that hash and risky-review stored its Pass
   (review_pass.py stored). A derived id mem has no card for (NoCard) is not risky; any other mem fault, for any
   task, is failed, step review.
3. In the main checkout: git-branch-policy.py land --name <name> (freshness, probe, steer check, push, readback,
   worktree-land), with --task <task8> passed on: the steer check reads the session and the task8, so Flight
   can hold one task's push while legs share one session id. A steer STOP is state need,
   exit 75, nothing pushed.
4. In the main checkout: git-branch-policy.py worktree-remove --name <name>.

It prints one JSON line, state:
  landed   pushed and landed; smoke green, unset, or "pushed before" (a partial land: the tip was on origin/main
           before this call); reviewed (the --reviewed hash or null); removed true or false (remove_output says why not)
  already  origin/main and the main checkout held the task before this call: the branch tip is on both (the
           worktree goes; a refused remove is failed, step remove), or the branch is gone and
           state_root/worktree-remove.log logs its removal. A tip on origin/main only (a partial land) lands again
           with no smoke.
  need     the land paused on a steer STOP; send Flight NEED steer push
  review   the tip's diff is not the one reviewed, or a risky task has no stored Pass for it; review args, then land
           again with the new hash
  failed   step worktree|stage|commit|freshness|smoke|land|remove, with the step's exit and output
Exit 0 on landed or already, 75 on need, 1 on failed, 2 on bad arguments, 3 on review.
Why: the hub close was six hand steps across two checkouts; a rebase after review could reach main unreviewed;
risky cards landed with no review.
Owner: Harness.
"""
import argparse
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
import review_pass  # noqa: E402
import run_tmp  # noqa: E402

_spec = importlib.util.spec_from_file_location("review_diff", DRAWER / "review-diff.py")
review_diff = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review_diff)

NEED = 75
ROUNDS = 3  # smoke-then-land rounds while origin/main moves under the smoke
TAIL = 3000


def run(argv, cwd, timeout=None, env=None):
    return subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, env=env)


def py(root, script, *args):
    """A hub script of the checkout root: each script acts on the checkout it sits in, not on cwd."""
    return [sys.executable, "-B", str(Path(root) / "Drawer" / script), *args]


def last_json(text):
    """The last stdout line that parses as a JSON object, else {}."""
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return {}


def output(result):
    return ((result.stdout or "") + (result.stderr or "")).strip()[-TAIL:]


def done(state, **fields):
    print(json.dumps({"state": state, **fields}))
    return {"landed": 0, "already": 0, "review": 3, "need": NEED}.get(state, 1)


def fail(step, result, **fields):
    return done("failed", step=step, exit=result.returncode, output=output(result), **fields)


def hub_root():
    common = run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], DRAWER)
    if common.returncode != 0:
        raise SystemExit(f"hub-land: no git repository at {DRAWER}: {common.stderr.strip()}")
    return Path(common.stdout.strip()).parent


def branch_tree(hub, branch):
    """The worktree that holds branch, else None."""
    path = current = None
    for line in run(["git", "worktree", "list", "--porcelain"], hub).stdout.splitlines():
        if line.startswith("worktree "):
            current = Path(line.removeprefix("worktree "))
        elif line == f"branch refs/heads/{branch}":
            path = current
    return path


def removal_logged(hub, branch):
    """True when state_root/worktree-remove.log (git-branch-policy.py worktree-remove) logs branch removed."""
    log = harness_config.path(harness_config.load(hub)["project"]["state_root"]) / "worktree-remove.log"
    try:
        return f" branch={branch} " in log.read_text(encoding="utf-8")
    except OSError:
        return False


def smoke_argv(wt):
    """[smoke] land_command (else command) of the worktree's config split like a shell line, {python} as this
    interpreter."""
    cfg = harness_config.load(wt).get("smoke") or {}
    command = str(cfg.get("land_command") or cfg.get("command") or "").strip()
    argv = [sys.executable if word == "{python}" else word for word in shlex.split(command)]
    return argv, float(cfg.get("timeout_seconds") or 1800), int(cfg.get("blocked_exit") or NEED)


def untracked(wt):
    """Uncommitted work in the worktree (untracked files or edits), past its interpreter link: worktree-remove
    refuses it, and smoke would judge it while the pushed tip lacks it."""
    return run(["git", "status", "--porcelain", "--untracked-files=normal", "--", ".", ":(exclude).venv"],
               wt).stdout.strip()


def remove(hub, name):
    os.chdir(hub)  # Windows refuses to delete a folder that is some process's working directory, ours included
    removed = run(py(hub, "git-branch-policy.py", "worktree-remove", "--name", name), hub)
    fields = {"removed": removed.returncode == 0}
    if removed.returncode != 0:
        fields["remove_output"] = output(removed)[-1500:]
    return fields


def main_copy():
    """The main checkout's hub-land.py when this file is another copy of it (a task worktree's), else None."""
    main = hub_root() / "Drawer" / "hub-land.py"
    if main.resolve() == Path(__file__).resolve() or not main.is_file():
        return None
    return main


def absolute_message_file(argv):
    """argv with a relative --message-file made absolute, for a process about to leave its cwd."""
    out = []
    for position, arg in enumerate(argv):
        if arg == "--":
            return out + argv[position:]
        if arg.startswith("--message-file="):
            arg = "--message-file=" + str(Path(arg.split("=", 1)[1]).resolve())
        elif out and out[-1] == "--message-file":
            arg = str(Path(arg).resolve())
        out.append(arg)
    return out


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    lander = main_copy()
    if lander is not None:  # a stale branch copy smoked and landed with a fault main had fixed
        sys.stdout.flush()
        argv = absolute_message_file(argv)
        os.chdir(lander.parent.parent)  # as in remove(): this process must not hold the task worktree as its cwd
        return subprocess.run([sys.executable, "-B", str(lander), *argv], check=False).returncode
    parser = argparse.ArgumentParser(prog="hub-land.py")
    parser.add_argument("--name", required=True)
    parser.add_argument("--reviewed", type=str.lower)
    parser.add_argument("--task", default="", type=str.lower)
    message = parser.add_mutually_exclusive_group()
    message.add_argument("--message", "-m")
    message.add_argument("--message-file")
    parser.add_argument("paths", nargs="*")
    args = parser.parse_args(argv)
    has_message = args.message is not None or args.message_file is not None
    if bool(args.paths) != has_message:
        parser.error("a message and <paths> go together: both to commit, neither to land the commits already made")
    if args.reviewed is not None and not re.fullmatch(r"[0-9a-f]{64}", args.reviewed):
        parser.error("--reviewed takes the 64-hex SHA-256 from Drawer/review-diff.py")
    if args.task and not re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?", args.task):
        parser.error("--task takes a task8 or a task uuid")
    hub = hub_root()
    # A task tagged risky lands only with a stored risky-review Pass for the diff it pushes.
    # The trailing 8-hex part of the name (card-run <session8>-<task8>), then the leading one (git-flow <task8>-<slug>);
    # a part mem has a card for is known, and a risky known part gates the land.
    parts = args.name.split("-")
    derived = [] if args.task else [p for p in dict.fromkeys((parts[-1], parts[0])) if re.fullmatch(r"[0-9a-f]{8}", p)]
    task, risky = args.task, False
    for part in derived:
        try:
            tagged = "risky" in review_pass.task_tags(hub, part)
        except review_pass.NoCard:
            continue  # no such card: the other part may name the task
        except review_pass.Refused as exc:  # a mem fault (locked, timeout) fails closed
            return done("failed", step="review", exit=1, output=f"the tags of task {part} are unreadable: {exc}")
        if not task or (tagged and not risky):
            task = part
        risky = risky or tagged
    if args.task:
        try:
            risky = "risky" in review_pass.task_tags(hub, task)
        except review_pass.Refused as exc:
            return done("failed", step="review", exit=1, output=f"the tags of task {task} are unreadable: {exc}")
    branch = f"task/{args.name}"
    tip = run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], hub).stdout.strip()
    if not tip:
        if removal_logged(hub, branch):
            return done("already", removed=True)
        return done("failed", step="worktree", exit=2, output=f"{branch} does not exist and no removal of it is logged")
    wt = branch_tree(hub, branch)
    if wt is None:
        return done("failed", step="worktree", exit=2, output=f"{branch} has no worktree")
    try:
        fetched = run(["git", "fetch", "--quiet", "origin", "main"], hub, timeout=120)
    except subprocess.TimeoutExpired:
        return done("failed", step="worktree", exit=None, output="git fetch origin main passed 120 s")
    if fetched.returncode != 0:
        return fail("worktree", fetched)
    def holds(ref):
        return run(["git", "merge-base", "--is-ancestor", tip, ref], hub).returncode == 0
    pushed_before = holds("refs/remotes/origin/main")

    if args.paths:
        if args.message_file is not None:
            text_file = Path(args.message_file).resolve()
            if not text_file.is_file():
                return done("failed", step="commit", exit=2, output=f"no message file {text_file}")
            message_args = ["-F", str(text_file)]
        else:
            message_args = ["-m", args.message]
        paths = []
        for raw in args.paths:
            path = Path(raw)
            if path.is_absolute():
                try:
                    path = path.resolve().relative_to(wt.resolve())
                except ValueError:
                    return done("failed", step="stage", exit=2, output=f"{raw} is not under the task worktree {wt}")
            paths.append(path.as_posix())
        present = [path for path in paths if (wt / path).exists()]
        gone = [path for path in paths if not (wt / path).exists()]
        unknown = [path for path in gone  # a path removed by an earlier run of this call is gone from the index
                   if not run(["git", "--literal-pathspecs", "ls-files", "--", path], wt).stdout.strip()
                   and not run(["git", "--literal-pathspecs", "ls-tree", "-r", "--name-only", "HEAD~", "--", path],
                               wt).stdout.strip()]
        if unknown:
            return done("failed", step="stage", exit=2,
                        output=f"neither in the task worktree nor tracked: {', '.join(unknown)}")
        git = ["git", "--literal-pathspecs"]
        for stage in ([[*git, "add", "--", *present]] if present else []) + (
                [[*git, "rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", *gone]] if gone else []):
            result = run(stage, wt)
            if result.returncode != 0:
                return fail("stage", result)
        if run([*git, "diff", "--cached", "--quiet", "--", *paths], wt).returncode != 0:
            result = run([*git, "commit", *message_args, "--", *paths], wt)
            if result.returncode != 0:
                return fail("commit", result)
            tip = run(["git", "rev-parse", "HEAD"], wt).stdout.strip()
            pushed_before = False

    if pushed_before and holds("HEAD"):
        removed = remove(hub, args.name)
        if not removed["removed"]:
            return done("failed", step="remove", exit=1, output=removed["remove_output"], tip=tip)
        return done("already", tip=tip, **removed)

    loose = untracked(wt)
    if loose:
        return done("failed", step="stage", exit=2, tip=tip, output=(
            f"uncommitted work in {wt}; name it in <paths>, commit or delete it:\n{loose}"))
    command, timeout, blocked = smoke_argv(wt)
    for _round in range(ROUNDS):
        # Freshness first, so the smoke judges the tip that land pushes; land refuses any other.
        fresh = run(py(wt, "git-branch-policy.py", "freshness", "--source", "resume"), wt)
        if last_json(fresh.stdout).get("state") not in {"current", "rebased"}:
            return fail("freshness", fresh, tip=tip)
        tip = run(["git", "rev-parse", "HEAD"], wt).stdout.strip()
        if args.reviewed or risky:
            state_root = harness_config.path(harness_config.load(wt)["project"]["state_root"])
            try:
                found = review_diff.build(wt, state_root)
            except review_diff.Refused as exc:
                return done("failed", step="review", exit=1, output=str(exc), tip=tip)
            why = found and review_pass.gate(state_root, risky, args.reviewed, found, task[:8])
            if why:
                return done("review", reviewed=args.reviewed, hash=found["hash"], head=found["head"], args=found,
                            tip=tip, output=why)
        smoke = "unset"
        if pushed_before:
            smoke = "pushed before"  # a land that pushed and stopped short (partial): it smoked then; finish the land
        elif command:
            try:
                # The smoke's test processes keep their temp folders in one run folder.
                with run_tmp.run_folder(harness_config.load(wt)) as env:
                    result = run(command, wt, timeout=timeout, env=env)
            except subprocess.TimeoutExpired:
                return done("failed", step="smoke", exit=None, output=f"[smoke] command passed {timeout:g} s", tip=tip)
            except OSError as exc:
                return done("failed", step="smoke", exit=None, output=f"[smoke] command did not start: {exc}", tip=tip)
            if result.returncode != 0:
                return fail("smoke", result, blocked=result.returncode == blocked, tip=tip)
            smoke = "green"
        expect = ([] if pushed_before else ["--tip", tip]) + (["--task", args.task[:8]] if args.task else [])
        result = run(py(hub, "git-branch-policy.py", "land", "--name", args.name, *expect), hub)
        land = last_json(result.stdout)
        if land.get("reason") == "tip_moved":
            continue  # origin/main moved during the smoke: smoke the rebased tip, then land it
        break
    else:
        return fail("land", result, land_state=land.get("state"), reason="tip_moved", pushed=False, rounds=ROUNDS)
    if land.get("state") == "need":
        return done("need", step="land", reason=land.get("reason"), why=land.get("why"), tip=land.get("tip"),
                    smoke=smoke)
    if land.get("state") != "landed":
        return fail("land", result, land_state=land.get("state"), reason=land.get("reason"),
                    pushed=bool(land.get("pushed")))
    return done("landed", tip=land.get("tip"), smoke=smoke, reviewed=args.reviewed, **remove(hub, args.name))


if __name__ == "__main__":
    sys.exit(main())
