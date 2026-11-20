#!/usr/bin/env python
"""Product bootstrap: one command makes the product repo, binds it to the hub and runs its first gate.

    <python> Drawer/product-init.py --path <dir> (--origin <url> | --local-origin) [--pack godot] [--mcp-url <url>]

It does these steps in order; each is skipped when it is done already, so a second run changes nothing:
  1. --pack godot: find the Godot binary (GODOT, then godot on PATH, then /Applications/Godot.app) and print its
     version. With none, print the install command (brew install --cask godot) and exit with [smoke] blocked_exit,
     before any change. It downloads and installs nothing.
  2. Make <dir> and its Git repo on main. The origin is --origin <url>, or --local-origin: a bare repo <dir>-origin.git
     beside it. An existing origin stays; a different one is refused.
  3. --pack godot: copy the pack (examples/godot-pack) into <dir>, each file only when absent: tools/gate.sh,
     .githooks/pre-commit, .githooks/pre-push, GODOT-CONVENTIONS.md, project.godot, tests/smoke_test.gd and .gitignore
     lines. Commit them, with the first commit on a repo that has none, and push main to origin.
  4. Write the [product] lines into harness.local.toml of the main hub checkout: path and, with the pack, its
     gate_command, broad_roots and mcp_server (--mcp-url also writes mcp_url). A line already set stays; a different
     [product] path is refused.
  5. Bind the product hooks (core.hooksPath on <dir> to the main checkout's Drawer/product-hooks), then run
     settings-sync --mcp so the product task trees are known to the box settings.
  6. With the pack, run the first product gate ([product] gate_command in <dir>).

It prints one JSON line: state ready (the gate is green, or no pack set), red (the gate failed; gate_exit),
blocked (no Godot; exit [smoke] blocked_exit) or failed (step, output). With path, origin, godot, version, gdformat
(whether gdformat is on PATH) and changed (what this run did; empty on a second run). Notes go to stderr.
Exit codes: 0 ready, 1 red or failed, 2 bad arguments, [smoke] blocked_exit blocked.
"""
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import harness_config  # noqa: E402

PACK = HERE.parent / "examples" / "godot-pack"
MAC_APP = Path("/Applications/Godot.app/Contents/MacOS/Godot")
INSTALL = "brew install --cask godot"
GDFORMAT_INSTALL = "uv sync (in the hub; uv.lock pins gdtoolkit)"
# pack file -> product file; each is copied only when the product lacks it
PACK_FILES = {"gate.sh": "tools/gate.sh", "hooks/pre-commit": ".githooks/pre-commit",
              "hooks/pre-push": ".githooks/pre-push", "GODOT-CONVENTIONS.md": "GODOT-CONVENTIONS.md",
              "template/project.godot": "project.godot", "template/tests/smoke_test.gd": "tests/smoke_test.gd",
              "template/tests/smoke_test.gd.uid": "tests/smoke_test.gd.uid"}
IDENT = ["-c", "user.name=product-init", "-c", "user.email=product-init@example.invalid"]
GATE_SECONDS = 1800


class Failed(Exception):
    def __init__(self, step, output):
        super().__init__(f"{step}: {output}")
        self.step, self.output = step, str(output).strip()[-3000:]


def git(repo, *args):
    env = {**harness_config.git_env(), "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env)


def git_ok(repo, step, *args):
    done = git(repo, *args)
    if done.returncode:
        raise Failed(step, done.stdout + done.stderr)
    return done.stdout.strip()


def find_godot(env=None, app=None):
    """The Godot binary: GODOT, then godot on PATH, then the macOS app; None when there is none."""
    env = os.environ if env is None else env
    app = MAC_APP if app is None else app
    if env.get("GODOT"):
        return shutil.which(env["GODOT"], path=env.get("PATH"))
    return shutil.which("godot", path=env.get("PATH")) or (str(app) if app.is_file() and os.access(app, os.X_OK) else None)


def godot_version(godot):
    done = subprocess.run([godot, "--version"], capture_output=True, text=True, timeout=60)
    return (done.stdout.strip().splitlines() or [""])[-1]


def toml_value(value):
    return "[" + ", ".join(json.dumps(v) for v in value) + "]" if isinstance(value, list) else json.dumps(value)


def set_product_lines(text, wanted):
    """text with each wanted [product] key set when it is absent or empty; a non-empty key stays. -> (text, keys set)."""
    have = tomllib.loads(text).get("product", {}) if text.strip() else {}
    missing = {k: v for k, v in wanted.items() if have.get(k) in (None, "", [])}
    if not missing:
        return text, []
    lines = text.splitlines()
    head = next((i for i, line in enumerate(lines) if re.match(r"^\[product\]\s*(#.*)?$", line)), None)
    if head is None:
        body = [f"{k} = {toml_value(v)}" for k, v in missing.items()]
        return text.rstrip("\n") + ("\n\n" if text.strip() else "") + "\n".join(["[product]", *body]) + "\n", list(missing)
    end = next((i for i in range(head + 1, len(lines)) if lines[i].startswith("[")), len(lines))
    for key, value in missing.items():
        row = next((i for i in range(head + 1, end) if re.match(rf"^{re.escape(key)}\s*=", lines[i])), None)
        if row is None:
            lines.insert(head + 1, f"{key} = {toml_value(value)}")
            end += 1
        else:
            lines[row] = f"{key} = {toml_value(value)}"
    return "\n".join(lines) + "\n", list(missing)


def ensure_repo(project, origin, local_origin, changed):
    """The product folder as a Git repo on main with its origin; notes each change."""
    project.mkdir(parents=True, exist_ok=True)
    top = git(project, "rev-parse", "--show-toplevel")
    if top.returncode:
        git_ok(project, "git init", "init", "-q", "-b", "main")
        changed.append("git init")
    elif Path(top.stdout.strip()).resolve() != project:
        raise Failed("repo", f"{project} is inside the Git repo {top.stdout.strip()}; pass that repo's own folder")
    branch = git(project, "symbolic-ref", "--short", "HEAD").stdout.strip()
    if branch != "main":
        if git(project, "rev-parse", "--verify", "-q", "HEAD").returncode == 0:
            raise Failed("repo", f"{project} is on branch {branch}; the product gate pushes main")
        git_ok(project, "branch", "symbolic-ref", "HEAD", "refs/heads/main")
    current = git(project, "remote", "get-url", "origin").stdout.strip()
    want = origin
    if local_origin:
        want = str(project.with_name(project.name + "-origin.git"))
        if not Path(want).exists():
            git_ok(project.parent, "origin", "init", "-q", "--bare", "-b", "main", want)
            changed.append("local origin")
    if current and want and current != want:
        raise Failed("origin", f"origin is {current}, not {want}")
    if not current and not want:
        raise Failed("origin", "the repo has no origin: pass --origin <url> or --local-origin")
    if not current:
        git_ok(project, "origin", "remote", "add", "origin", want)
        changed.append("origin")
    return current or want


def ensure_ignore(project):
    """The pack's .gitignore lines, each added when absent; true when it wrote the file."""
    path = project / ".gitignore"
    have = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    add = [line for line in (PACK / "template" / "gitignore").read_text(encoding="utf-8").splitlines() if line not in have]
    if add:
        path.write_text("".join(f"{line}\n" for line in [*have, *add]), encoding="utf-8")
    return bool(add)


def copy_pack(project, changed):
    """Copy each missing pack file into the product; the paths it wrote."""
    made = []
    for source, dest in PACK_FILES.items():
        target = project / dest
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(PACK / source, target)
            made.append(dest)
    if ensure_ignore(project):
        made.append(".gitignore")
    changed.extend(made)
    return made


def commit_and_push(project, paths, message, changed):
    """Commit paths (the first commit of a repo with none takes whatever is staged), then push main to origin."""
    if paths:
        git_ok(project, "add", "add", "--", *paths)
    empty = git(project, "rev-parse", "--verify", "-q", "HEAD").returncode != 0
    if empty or git(project, "diff", "--cached", "--quiet").returncode:
        ident = [] if git(project, "config", "user.email").stdout.strip() else IDENT
        git_ok(project, "commit", *ident, "commit", "-q", "--allow-empty", "-m", message)
        changed.append("commit")
    head = git_ok(project, "rev-parse", "rev-parse", "HEAD")
    remote = git(project, "ls-remote", "--heads", "origin", "main").stdout.split()
    if remote[:1] != [head]:
        git_ok(project, "push", "push", "-q", "-u", "origin", "main")
        changed.append("push")


def write_product_lines(main, project, pack, mcp_url, changed):
    """[product] lines of the main checkout's harness.local.toml; the gate_command in force afterwards."""
    path = main / harness_config.LOCAL
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    try:
        have = tomllib.loads(text).get("product", {})
    except tomllib.TOMLDecodeError as exc:
        raise Failed("toml", f"{path} does not parse: {exc}")
    if have.get("path") and harness_config.path(have["path"]).resolve() != project:
        raise Failed("toml", f"{path} [product] path is {have['path']}; edit it first, or pass that folder")
    wanted = {"path": str(project)}
    if pack:
        lines = tomllib.loads((PACK / "harness.pack.toml").read_text(encoding="utf-8"))["product"]
        wanted.update({k: lines[k] for k in ("gate_command", "broad_roots", "mcp_server")})
        if mcp_url:
            wanted["mcp_url"] = mcp_url
    new, keys = set_product_lines(text, wanted)
    if keys:
        path.write_text(new, encoding="utf-8")
        changed.append("harness.local.toml")
    return tomllib.loads(new).get("product", {}).get("gate_command", "")


def bind(main, project, changed, run):
    hooks = str(main / "Drawer" / "product-hooks")
    if git(project, "config", "--get", "core.hooksPath").stdout.strip() != hooks:
        git_ok(project, "hooks", "config", "core.hooksPath", hooks)
        changed.append("core.hooksPath")
    done = run([sys.executable, "-B", str(main / "Drawer" / "settings-sync.py"), "--mcp"], cwd=str(main),
               capture_output=True, text=True, timeout=300)
    if done.returncode:
        raise Failed("settings-sync", done.stdout + done.stderr)
    if "wrote:" in done.stdout:
        changed.append("settings-sync")


def run_gate(project, command, godot, blocked, run):
    """The first product gate: [product] gate_command in the checkout. -> (state, exit code)."""
    argv = [sys.executable if word == "{python}" else word for word in shlex.split(command)]
    env = {**os.environ, **({"GODOT": godot} if godot else {})}
    done = run(argv, cwd=str(project), env=env, capture_output=True, text=True, timeout=GATE_SECONDS)
    sys.stderr.write((done.stdout or "") + (done.stderr or ""))
    return ("ready" if done.returncode == 0 else "blocked" if done.returncode == blocked else "red"), done.returncode


def main(argv=None, hub=None, run=subprocess.run):
    ap = argparse.ArgumentParser(prog="product-init.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--path", required=True, help="the product folder: its Git repo root")
    where = ap.add_mutually_exclusive_group()
    where.add_argument("--origin", help="the origin URL or path the product pushes main to")
    where.add_argument("--local-origin", action="store_true", help="a bare origin <path>-origin.git beside the folder")
    ap.add_argument("--pack", choices=["godot"], help="apply the Godot pack")
    ap.add_argument("--mcp-url", help="the editor MCP server's URL ([product] mcp_url)")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    hub = Path(hub) if hub else harness_config.hub_root(HERE)
    main_hub = harness_config.main_checkout(hub)
    blocked = int(harness_config.load(hub).get("smoke", {}).get("blocked_exit") or 75)
    project = Path(args.path).expanduser().resolve()
    row = {"path": str(project)}
    changed = []
    godot = None
    try:
        if args.pack:
            godot = find_godot()
            if not godot:
                sys.stderr.write(f"product-init: BLOCKED: no Godot found (GODOT, PATH, {MAC_APP}). Install it: {INSTALL}\n")
                print(json.dumps({"state": "blocked", "reason": "no Godot", "install": INSTALL, "path": str(project)}))
                return blocked
            row.update(godot=godot, version=godot_version(godot), gdformat=bool(shutil.which("gdformat", path=os.pathsep.join(
                [str(main_hub / ".venv" / "bin"), os.environ.get("PATH", "")]))))
            sys.stderr.write(f"product-init: godot {godot} {row['version']}\n")
            if not row["gdformat"]:
                sys.stderr.write(f"product-init: gdformat is not in the hub environment or on PATH; GDScript style is not fixed on commit. Install it: {GDFORMAT_INSTALL}\n")
        row["origin"] = ensure_repo(project, args.origin, args.local_origin, changed)
        made = copy_pack(project, changed) if args.pack else []
        commit_and_push(project, made, "Godot pack: gate, hooks, conventions and smoke test." if made else "Start the product.", changed)
        command = write_product_lines(main_hub, project, args.pack, args.mcp_url, changed)
        bind(main_hub, project, changed, run)
        state, code = ("ready", None)
        if args.pack and command:
            state, code = run_gate(project, command, godot, blocked, run)
        row.update(state=state, gate_exit=code, changed=changed)
    except Failed as exc:
        print(json.dumps({**row, "state": "failed", "step": exc.step, "output": exc.output, "changed": changed}))
        return 1
    print(json.dumps(row))
    return {"ready": 0, "blocked": blocked}.get(state, 1)


if __name__ == "__main__":
    sys.exit(main())
