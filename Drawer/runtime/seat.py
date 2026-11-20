r"""The seat command: configure this checkout, check, repair, clean up.

One seat lock covers every subcommand, so two sessions never check, configure or repair the same checkout at once.

Run it under the prepared interpreter, the hub root virtual environment; a seat being set up
has no py launcher (.venv/bin/python):

    <python> -B Drawer/runtime/seat.py init
    <python> -B Drawer/runtime/seat.py configure
    <python> -B Drawer/runtime/seat.py bind-git
    <python> -B Drawer/runtime/seat.py check --json
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

DRAWER = Path(__file__).resolve().parents[1]
if str(DRAWER) not in sys.path:
    sys.path.insert(0, str(DRAWER))
import harness_config  # noqa: E402
from runtime import children, components, filelock, profile  # noqa: E402

HUB = DRAWER.parent
SEAT_LOCK_SECONDS = 10.0
CONFIG_SECONDS = 300
SYNC_SECONDS = 900  # a lock sync may build a whole environment, as the smoke repairs do
SIGN_IN_SECONDS = 180
GITHUB_SIGN_IN_MISSING = (
    "bind-git: GitHub sign-in missing: Git on this computer is not signed in to GitHub for "
    "this hub. Sign in when the GitHub window opens, then tell the agent to go on. If no "
    "window opens (a Mac opens none), send this line to the human.")
NO_ORIGIN = (
    "bind-git: no origin: this checkout has no origin remote. Make your own copy (Use this template on GitHub) or create a repository, run "
    "git remote add origin <url>, then run this step again. A new hub: Drawer/new-hub.py --origin <url>.")
ORIGIN_NOT_WRITABLE = (
    "bind-git: origin not writable: you can reach origin but cannot push main to it (a clone of a public "
    "template is read-only). Make your own copy (Use this template on GitHub) or create a repository, point origin at it with git remote "
    "set-url origin <url>, then run this step again.")
ORIGIN_NO_MAIN = (
    "bind-git: origin has no main: origin is an empty repository and the push of main to it failed: {why}. "
    "Fix that cause, then run this step again.")
NO_MAIN_BRANCH = (
    "bind-git: no main branch: origin is empty and this checkout is not on main, so there is no main to bind. "
    "Run git switch -c main at the hub root, then run this step again.")
PINS_FILE = "seat-pins.json"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class SeatLock(filelock.FileLock):
    """The one lock every seat subcommand holds, beside this checkout's saved profile."""

    def __init__(self, hub, timeout: float | None = None):
        super().__init__(profile.default_profile_path(hub).parent / "seat.lock",
                         SEAT_LOCK_SECONDS if timeout is None else timeout)


def interpreter(hub: Path) -> Path:
    """The interpreter the locked sync prepared: the hub root .venv."""
    return hub / ".venv" / "bin" / "python"


def _extend_path() -> None:
    """A Dock-launched app keeps /usr/bin:/bin, so append the Homebrew and uv folders that exist."""
    have = os.environ.get("PATH", "").split(os.pathsep)
    extra = [d for d in ("/opt/homebrew/bin", "/usr/local/bin", os.path.expanduser("~/.local/bin"))
             if d not in have and os.path.isdir(d)]
    os.environ["PATH"] = os.pathsep.join([*filter(None, have), *extra])


def run_command(args, *, cwd, env=None, capture=False, timeout=None) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=str(cwd), env=env, text=True, capture_output=capture,
                          timeout=timeout, check=False)


def _last_line(done) -> str:
    # A failed command reports its reason on stderr; the setup skill tells the human
    # to quote that line, so read it first and fall back to stdout.
    streams = (getattr(done, "stdout", ""), getattr(done, "stderr", ""))
    for stream in (streams[::-1] if getattr(done, "returncode", 0) else streams):
        lines = [line.strip() for line in str(stream or "").splitlines() if line.strip()]
        if lines:
            return lines[-1]
    return "no output"


# Every lock whose environment configure syncs: name -> (hub-relative lock, its sync commands).
# harness-conform's pins check holds every tracked lock file to these rows or its exempt list.
LOCKS = {
    "hub-uv-lock": ("uv.lock", lambda hub: [
        ["uv", "sync", "--locked", "--link-mode", "copy", "--project", str(hub)]]),
}


def pins(hub: Path) -> dict[str, str]:
    """The local installs Git pins but cannot carry."""
    return {name: hashlib.sha256((hub / lock).read_bytes()).hexdigest() for name, (lock, _sync) in LOCKS.items()}


def _pins_path(hub: Path) -> Path:
    return profile.default_profile_path(hub).parent / PINS_FILE


def stamped_pins(hub: Path) -> dict:
    """The pins the last configure brought this seat to; {} when it never stamped."""
    try:
        value = json.loads(_pins_path(hub).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def stale_pins(hub: Path) -> list[str]:
    """The pins whose stamp no longer matches Git, in pins() order; [] when the seat is current."""
    stamp = stamped_pins(hub)
    return [name for name, pin in pins(hub).items() if stamp.get(name) != pin]


def _configure_stale(hub: Path) -> list[str]:
    # A task worktree's .git is a file. Its venv and the local registrations belong to the main
    # checkout: a boot configure there would repoint them at a folder worktree-remove deletes.
    # A checkout with no seat profile is not set up yet: the setup skill configures it.
    if (hub / ".git").is_file() or not profile.default_profile_path(hub).exists():
        return []
    return stale_pins(hub)


# The boot runner's one registry: name -> (what is stale, the seat.py arguments that repair it).
# session-start runs each stale step after Git freshness, in this order, inside one budget.
BOOT_STEPS = {
    "configure": (_configure_stale, ["configure"]),
}
# Inside the session-start hook's 240 s, after freshness and its settings render.
BOOT_BUDGET_SECONDS = 90


def boot_steps(hub: Path, budget: float = BOOT_BUDGET_SECONDS) -> str:
    """Run every stale BOOT_STEPS row and return one SEAT STEPS line; '' when none was stale.

    A step runs as its own process with its output in a log beside the pin stamp, never on the
    hook's stdout. One still running at the budget keeps running and stamps when it ends: a sync
    killed halfway would leave a broken environment.
    """
    deadline = time.monotonic() + budget
    results = []
    for name, (stale, args) in BOOT_STEPS.items():
        reasons = stale(hub)
        if not reasons:
            continue
        ran = f"{name} ({', '.join(reasons)})"
        left = deadline - time.monotonic()
        if left <= 0:
            results.append(f"{ran} handed off: no boot budget left; the next boot runs it")
            continue
        log = _pins_path(hub).parent / f"boot-{name}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        # Append: a handed-off configure from an earlier boot may still write here.
        with log.open("a", encoding="utf-8") as out:
            out.write(f"== boot {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            out.flush()
            child = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), *args],
                                     cwd=str(hub), stdin=subprocess.DEVNULL, stdout=out,
                                     stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
        try:
            code = child.wait(timeout=left)
        except subprocess.TimeoutExpired:
            results.append(f"{ran} handed off: still running past the {budget:g} s boot budget; "
                           f"it stamps the seat when it ends (log {log})")
            continue
        if code == 3:
            results.append(f"{ran} skipped: another seat command holds the seat lock; the next boot runs it")
        elif code:
            lines = [line.strip() for line in log.read_text(encoding="utf-8", errors="replace").splitlines()
                     if line.strip()]
            results.append(f"{ran} failed (exit {code}): {lines[-1] if lines else 'no output'} (log {log}); "
                           "the next boot retries, tell the human if it repeats")
        else:
            results.append(f"{ran} ran ok")
    return f"SEAT STEPS — {'; '.join(results)}." if results else ""


def _sync_lock(hub: Path, run_command, stamp: dict, name: str, digest: str) -> int:
    """Run one LOCKS row's sync commands when its lock moved since the stamp, from the lock's folder."""
    lock, sync = LOCKS[name]
    if stamp.get(name) != digest:
        for argv in sync(hub):
            done = run_command(argv, cwd=(hub / lock).parent, capture=True, timeout=SYNC_SECONDS)
            if getattr(done, "returncode", 0):
                print(f"configure: {name} sync failed (exit {done.returncode}): {_last_line(done)}",
                      file=sys.stderr)
                return 1
        print(f"configure: {name} synced")
    stamp[name] = digest
    return 0


def init(hub: Path, member: str | None = None) -> int:
    """Write this checkout's seat profile once; an existing profile stays as it is.

    Defaults: member is [project] human in lower case and box is the login name. The role is always operator.
    """
    path = profile.default_profile_path(hub)
    moved = ""
    if path.exists():
        try:
            value = profile.load_runtime_profile(hub)
        except profile.ProfileError:
            # A saved profile that does not load, lacks a field or belongs to another checkout fails the doctor
            # and stops every seat command: keep it as .bad, start fresh.
            bad = path.with_name(path.name + ".bad")
            os.replace(path, bad)
            moved = f"init: saved profile was invalid; moved it to {bad.name} and wrote a fresh profile\n"
        else:
            print(f"init: profile exists box={value.get('box_name')} member={value.get('member_name')} "
                  f"role={value.get('role')}")
            return 0
    cfg = harness_config.load(hub)
    value = {"schema_version": profile.SCHEMA_VERSION, "hub_root": str(hub), "agent": "claude",
             "member_name": member or str(cfg["project"]["human"]).lower(),
             "box_name": getpass.getuser(), "role": "operator"}
    profile.save_profile(path, value)
    print(f"{moved}init: wrote profile box={value['box_name']} member={value['member_name']} role={value['role']}")
    return 0


def trust_dirs(hub: Path) -> list[str]:
    """The hub root and the task-worktree parents, under their written and their resolved spelling.

    The hub root is each session's cwd, so the first session of a stamped or new hub never stops at the
    folder trust prompt. The parents are the hub task worktrees (<hub>_wt) and the product worktrees
    (<product repo>_wt, when [product] path is set). Claude Code trusts a folder whose parent holds the
    trust flag, so the parents cover each task worktree."""
    cfg = harness_config.load(hub)
    dirs = [Path(hub), Path(f"{hub}_wt")]
    value = str((cfg.get("product") or {}).get("path") or "").strip()
    project = harness_config.path(value) if value else None
    if project is not None and project.is_dir():
        main = harness_config.main_checkout(project.resolve())
        if main:
            dirs.append(Path(f"{main}_wt"))
    out = []
    for folder in dirs:
        for spelling in (str(folder).rstrip("/") or "/", str(folder.resolve())):
            if spelling not in out:
                out.append(spelling)
    return out


def client_config_path() -> Path:
    """The Claude Code user config: $CLAUDE_CONFIG_DIR/.claude.json when set, else ~/.claude.json."""
    folder = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(folder).expanduser() if folder else Path.home()) / ".claude.json"


def trust_folders(folders: list[str], config: Path | None = None) -> list[str]:
    """Set projects[<folder>].hasTrustDialogAccepted in the client config for each folder, so a session
    started there never stops at the folder trust dialog. Every other key stays. Returns the
    folders it newly trusted; it writes nothing when each folder is trusted already."""
    config = config or client_config_path()
    try:
        value = json.loads(config.read_text(encoding="utf-8"))
    except FileNotFoundError:
        value = {}
    if not isinstance(value, dict):
        raise ValueError(f"{config} is not a JSON object")
    projects = value.setdefault("projects", {})
    added = []
    for folder in folders:
        entry = projects.setdefault(folder, {})
        if entry.get("hasTrustDialogAccepted") is not True:
            entry["hasTrustDialogAccepted"] = True
            added.append(folder)
    if added:
        mode = config.stat().st_mode & 0o777 if config.exists() else 0o600
        profile._atomic_write(config, (json.dumps(value, indent=2) + "\n").encode("utf-8"), mode)
    return added


def configure(hub: Path, run_command) -> int:
    """Refresh this checkout's local registrations, then bring every pin to this seat and stamp it.

    settings-sync --apply writes the local .mcp.json (and any drifted project settings). The bound product clone
    gets the product hooks (bind_product_hooks), so a pack applied after setup binds on the next configure.
    trust_folders marks the hub root and the task-worktree parents trusted in the client config. A
    LOCKS environment re-syncs whenever its lock moved, so a bump reaches the seat; the stamp
    gains only the pins this run brought the seat to, and stale_pins compares it.
    """
    profile.load_profile(profile.default_profile_path(hub))  # a seat without a readable profile stops here
    want = pins(hub)
    stamp = stamped_pins(hub)
    done = run_command([str(interpreter(hub)), "-B", str(hub / "Drawer" / "settings-sync.py"),
                        "--apply"], cwd=hub, capture=True, timeout=CONFIG_SECONDS)
    if getattr(done, "returncode", 0):
        print(f"configure: settings-sync.py failed (exit {done.returncode}): {_last_line(done)}",
              file=sys.stderr)
        return 1
    print(_last_line(done))
    added = trust_folders(trust_dirs(hub))
    if added:
        print(f"configure: trusted {len(added)} folders in {client_config_path()}")
    # Last: the registrations above stand even when an install fails.
    failed = bind_product_hooks(hub, run_command)  # a product added after setup is bound on the next configure
    for name in LOCKS:
        failed |= _sync_lock(hub, run_command, stamp, name, want[name])
    stamp_text = json.dumps(stamp, indent=1, sort_keys=True) + "\n"
    profile._atomic_write(_pins_path(hub), stamp_text.encode("utf-8"), 0o644)
    return failed


def _origin_env() -> dict[str, str]:
    """This process's environment with the credential manager allowed to open its sign-in window."""
    env = {key: value for key, value in os.environ.items()
           if key not in {"GCM_INTERACTIVE", "GH_PROMPT_DISABLED"}}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _github_sign_in(hub: Path, run_command) -> int:
    """Reach the private origin once with the credential manager allowed to open its window.

    The policy fetch never prompts, so an app-made clone with no command-line credential would
    fail there; a seat that already holds the credential answers here silently.
    Returns the exit code: 0 origin has main, 2 origin answered and has no main, else no sign-in.
    """
    try:
        done = run_command(["git", "-C", str(hub), "ls-remote", "--exit-code", "origin", "refs/heads/main"],
                           cwd=hub, env=_origin_env(), capture=True, timeout=SIGN_IN_SECONDS)
    except subprocess.TimeoutExpired:
        return -1
    return getattr(done, "returncode", 0)


def _has_origin(hub: Path, run_command) -> bool:
    done = run_command(["git", "-C", str(hub), "remote", "get-url", "origin"], cwd=hub, capture=True,
                       timeout=CONFIG_SECONDS)
    return not getattr(done, "returncode", 0)


def _origin_writable(hub: Path, run_command) -> bool:
    """A dry-run push of HEAD to main. A rejection that only says HEAD is behind or diverged still
    proves the push right: the remote answered, and install moves the checkout to main next."""
    try:
        # No local hooks: a booted checkout's pre-push gate refuses until bind-git installs the binding, and
        # that would hide the true answer. A dry run publishes nothing, so this is no gate bypass.
        done = run_command(["git", "-C", str(hub), "-c", f"core.hooksPath={os.devnull}", "push", "--dry-run",
                            "origin", "HEAD:refs/heads/main"],  # the full ref: a detached HEAD cannot name a new main
                           cwd=hub, env=_origin_env(), capture=True, timeout=SIGN_IN_SECONDS)
    except subprocess.TimeoutExpired:
        return False
    if not getattr(done, "returncode", 0):
        return True
    text = f"{getattr(done, 'stdout', '')}\n{getattr(done, 'stderr', '')}"
    return "non-fast-forward" in text or "fetch first" in text


def bind_git(hub: Path, run_command) -> int:
    """Bind this checkout to the Git branch policy: main in the hub root, task branches in worktrees."""
    if not _has_origin(hub, run_command):
        print(NO_ORIGIN, file=sys.stderr)
        return 1
    reached = _github_sign_in(hub, run_command)
    if reached not in (0, 2):  # 2: origin answered and has no main; the seed push below fills it
        print(GITHUB_SIGN_IN_MISSING, file=sys.stderr)
        return 1
    if not _origin_writable(hub, run_command):
        print(ORIGIN_NOT_WRITABLE, file=sys.stderr)
        return 1
    profile.load_profile(profile.default_profile_path(hub))  # a seat without a readable profile stops here
    argv = [str(interpreter(hub)), "-B", str(hub / "Drawer" / "git-branch-policy.py"), "install",
            "--repo", str(hub)]
    done = run_command(argv, cwd=hub, capture=True, timeout=CONFIG_SECONDS)
    if getattr(done, "returncode", 0):
        line = _last_line(done)
        if "accepted main could not be fetched" in line:
            # reached == 2: origin answered and holds no main, so the fetch failed for lack of main, not for sign-in.
            print(NO_MAIN_BRANCH if reached == 2 else GITHUB_SIGN_IN_MISSING, file=sys.stderr)
        else:
            print(f"bind-git: failed (exit {done.returncode}): {line}", file=sys.stderr)
        return 1
    result = json.loads(_last_line(done))
    if reached == 2:
        # A plain push, hooks on: pre-push and check-push gate it as any push of main.
        pushed = run_command(["git", "-C", str(hub), "push", "origin", "HEAD:main"], cwd=hub, env=_origin_env(),
                             capture=True, timeout=SIGN_IN_SECONDS)
        if getattr(pushed, "returncode", 0):
            # Git ends a refused push with its own "error: failed to push" line; the cause is the line before it.
            lines = [line.strip() for line in str(getattr(pushed, "stderr", "") or "").splitlines()
                     if line.strip() and not line.startswith("error: failed to push")]
            print(ORIGIN_NO_MAIN.format(why=lines[-1] if lines else _last_line(pushed)), file=sys.stderr)
            return 1
        print("bind-git: origin had no main; pushed main to it")
    print(f"bind-git: ok branch={result.get('branch') or 'main'}")
    return bind_product_hooks(hub, run_command)


def bind_product_hooks(hub: Path, run_command) -> int:
    """Point the bound product clone's core.hooksPath at the main hub's Drawer/product-hooks (PRODUCT-CONVENTIONS.md PRD-2, Product
    hooks), so its push runs the product push gate (PRD-2). No product bound ([product] path empty or no
    folder): nothing to do."""
    value = str((harness_config.load(hub).get("product") or {}).get("path") or "").strip()
    project = harness_config.path(value) if value else None
    if project is None or not project.is_dir():
        return 0
    hooks = harness_config.main_checkout(hub) / "Drawer" / "product-hooks"
    done = run_command(["git", "-C", str(project.resolve()), "config", "core.hooksPath", str(hooks)], cwd=hub,
                       capture=True, timeout=CONFIG_SECONDS)
    if getattr(done, "returncode", 0):
        print(f"bind-git: product hooks failed (exit {done.returncode}): {_last_line(done)}", file=sys.stderr)
        return 1
    print(f"bind-git: product hooks {hooks}")
    return 0


def _records(hub: Path, only: str | list[str] | None) -> list[dict]:
    """Every component record, with the children the checks started closed again."""
    machine = profile.machine(hub)
    try:
        return components.check_all(hub, machine, only=only)
    finally:
        children.close_all(hub)


def check(hub: Path, as_json: bool, only: str | None) -> int:
    if only is not None:
        names = [item.name for item in components.inventory(hub)]
        # A family name selects every <family>:<part> row; an unmatched name is an error, never an
        # empty clean result the caller would read as a passing re-check.
        only = [name for name in names if name == only or name.startswith(f"{only}:")]
        if not only:
            print(f"check: --only names no component; choose from {', '.join(names)}", file=sys.stderr)
            return 1
    records = _records(hub, only)
    if as_json:
        print(json.dumps({"records": list(records), "summary": components.summary(records)},
                         indent=1, sort_keys=True))
    else:
        for record in records:
            print(f"{record['status']} {record['name']}: {record['reason']}")
    return 0


def repair(hub: Path, name: str) -> int:
    record = next((item for item in _records(hub, name) if item["name"] == name), None)
    if record is None:
        print(f"repair: {name} is not a checked component")
        return 1
    if record["status"] in ("PASS", "SKIPPED"):
        print(f"repair: {name} needs no repair")
        return 0
    try:
        result = components.repair(hub, name, record)
    except components.RepeatRefused as exc:
        print(f"repair: {name} repeat refused (same failure after a repair). {exc}")
        return 3
    print(f"{result['status']} {result['name']}: {result['reason']}")
    if result["status"] != "PASS" and result.get("detail"):
        # The repair's own note lives only here: the next check builds a fresh record.
        print(result["detail"])
    return 0


def cleanup(hub: Path) -> int:
    for note in children.reclaim(hub):
        print(note)
    return 0


def main(argv=None, *, run_command=run_command) -> int:
    parser = argparse.ArgumentParser(prog="seat", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init_parser = commands.add_parser("init", help="write this checkout's seat profile once")
    init_parser.add_argument("--member")
    commands.add_parser("configure", help="refresh registrations and install and stamp every pin")
    commands.add_parser("bind-git", help="bind this checkout to the Git branch policy")
    check_parser = commands.add_parser("check", help="check every component of this seat")
    check_parser.add_argument("--json", action="store_true", help="print the records as JSON")
    check_parser.add_argument("--only", help="check one named component")
    repair_parser = commands.add_parser("repair", help="repair one failed component")
    repair_parser.add_argument("name")
    repair_parser.add_argument("--clear", action="store_true",
                               help="end today's refusal of this component's repair; repair nothing")
    commands.add_parser("cleanup", help="reclaim the children a check left behind")
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    args = parser.parse_args(argv)
    hub = HUB
    _extend_path()
    try:
        with SeatLock(hub):
            if args.command == "init":
                return init(hub, args.member)
            if args.command == "configure":
                return configure(hub, run_command)
            if args.command == "bind-git":
                return bind_git(hub, run_command)
            if args.command == "check":
                return check(hub, args.json, args.only)
            if args.command == "repair" and args.clear:
                count = components.clear_refusals(hub, args.name)
                print(f"repair: {args.name} refusal cleared ({count} entries)")
                return 0
            if args.command == "repair":
                return repair(hub, args.name)
            return cleanup(hub)
    except filelock.LockBusy as exc:
        print(f"seat: another seat command holds {exc.path}", file=sys.stderr)
        return 3
    except (profile.ProfileError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"seat: {args.command} failed ({type(exc).__name__}: {exc})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
