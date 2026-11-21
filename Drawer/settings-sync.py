#!/usr/bin/env python
"""Canonical settings for the hub -- one source, every box.

Why this exists: Claude Code reads .claude/settings.json only from the session's primary
working directory, and every session seats at the hub root. Git carries the source and the one
generated project settings file. It also writes this box's .mcp.json (git-ignored), which
registers the memory server (Drawer/mem/mem.py mcp) and, when harness.toml [product] names one, the product MCP server ([product] mcp_server, an
HTTP endpoint at [product] mcp_url). With .mcp.json it writes this box's .claude/settings.local.json (git-ignored;
Claude Code reads it beside .claude/settings.json) from the manifest "local" key: its short permissions.allow list
plus this box's hub interpreter, the folders Flight's legs write as permissions.additionalDirectories (absolute, so
per box) and a lean SubagentStart boot for the implementer and workflow-leg types, and the editor MCP server in
enabledMcpjsonServers (the tracked settings.json never names it). This script owns that file: a hand edit is drift. Flight then runs headless with --permission-mode acceptEdits, never with permissions bypassed.
It also checks each model and effort pick in .claude/agents and .claude/workflows against harness.toml [routing.legs]:
a routed line that differs is drift (--apply rewrites it); a pick with no route exits 2.

    <python> Drawer/settings-sync.py            # check every generated file, exit 1 on drift
    <python> Drawer/settings-sync.py --apply    # write the drifted files
    <python> Drawer/settings-sync.py --mcp      # write only this box's git-ignored files: .mcp.json, settings.local.json
    <python> Drawer/settings-sync.py --tracked  # check only the tracked settings.json files (commit tree)

The one target is the hub root; its capability classes are the manifest's "root" list. A
settings.json in a hub subfolder is an orphan: no session seats there. This never reads or writes ~/.claude.
"""
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

HUB = Path(__file__).resolve().parent.parent
if str(HUB / "Drawer") not in sys.path:
    sys.path.insert(0, str(HUB / "Drawer"))
import product_worktree  # noqa: E402
import harness_config  # noqa: E402
PY = ".venv/bin/python -B"  # a new box has no py launcher

MANIFEST = HUB / "Drawer" / "settings-manifest.json"
MCP_JSON = ".mcp.json"
LOCAL_JSON = ".claude/settings.local.json"  # box-local: its folders are this box's absolute paths
LEG_SEAT = "{LEG_SEAT}"
GATES_MATCHER = "{GATES}"
# Commands that run another command: a local allow rule may name one only exactly (Bash(env)), never with a wildcard.
RUNNERS = ("env", "sudo", "xargs", "bash", "sh", "zsh", "eval", "exec", "command", "nohup", "timeout", "nice",
           "find")
REQUIRED_ENV = {
    "CLAUDE_CODE_SIMPLE_SYSTEM_PROMPT": "1",
    # Subagents and Workflow legs run at medium whatever the lead runs at (CLI 2.1.316).
    "CLAUDE_CODE_SUBAGENT_EFFORT": "medium",
    # Every hub Python runs in UTF-8 mode: a Windows pipe defaults to the ANSI code page.
    "PYTHONUTF8": "1",
}


# Leg model routing: harness.toml [routing.legs] is the one home of each model and effort pick in .claude/agents
# and .claude/workflows. A line tagged route:<role>, or a frontmatter under a route: <role> key, takes its picks from
# the row; a pick on any other line is an unrouted pick (exit 2), and a routed line that differs is drift.
ALIAS, LEVEL = "opus|sonnet|haiku", "low|medium|high|xhigh|max"
ROUTE_FIELDS = (
    ("model", re.compile(rf"(\bmodel(?::\s*|\s*=\s*)['\"]?)({ALIAS})\b")),
    ("effort", re.compile(rf"(\beffort(?::\s*|\s*=\s*)['\"]?)({LEVEL})\b")),
)
ROUTE_PICKS = (
    *(rx for _, rx in ROUTE_FIELDS),
    re.compile(rf"\b({ALIAS}) ({LEVEL})\b"),            # an "opus high" pair
    re.compile(r"claude-(?:opus|sonnet|haiku|fable)-\d"),  # a full model id
)
JS_ALIAS = re.compile(rf"(['\"`])({ALIAS})\1")  # a quoted alias in script code is a pick
JS_COMMENT = re.compile(r"(^|\s)//.*$")
ROUTE_TAG = re.compile(r"route:([a-z0-9-]+)")
ROUTE_KEY = re.compile(r"^route: ([a-z0-9-]+)$", re.M)
ROUTE_DIRS = (".claude/agents", ".claude/workflows")


def route_line(line, roles, js, front=None):
    """(line rendered from its role, error or None). front: the role of the frontmatter route: key."""
    tag = ROUTE_TAG.search(line)
    name = tag.group(1) if tag else front
    code = JS_COMMENT.sub("", line) if js else line
    picks = any(rx.search(code) for rx in ROUTE_PICKS) or (js and bool(JS_ALIAS.search(code)))
    if not name:
        return line, "unrouted model pick" if picks else None
    role = roles.get(name)
    if role is None:
        return line, f"route names unknown role {name!r}"
    if not picks:
        return line, f"route tag {name!r} picks nothing on its line" if tag else None
    for field, rx in ROUTE_FIELDS:
        if rx.search(line):
            if field not in role:
                return line, f"route role {name!r} has no {field}"
            line = rx.sub(lambda m, v=role[field]: m.group(1) + v, line)
    return line, None


def route_renders(cfg, hub=HUB):
    """(renders, errors): each agent and workflow file with its routed lines rendered from [routing.legs]."""
    roles = (cfg.get("routing") or {}).get("legs") or {}
    allowed = (cfg.get("gates") or {}).get("allowed_models") or ALIAS.split("|")
    errors = [f"[routing.legs] {name}: model must be in [gates] allowed_models, effort one of {LEVEL}"
              for name, role in roles.items()
              if not role or role.get("model", allowed[0]) not in allowed
              or role.get("effort", "medium") not in LEVEL.split("|")]
    renders = []
    for folder in ROUTE_DIRS:
        for path in sorted((hub / folder).glob("*")):
            if path.suffix not in (".md", ".js") or not path.is_file():
                continue
            have = path.read_text(encoding="utf-8")
            head = have.split("\n---", 1)[0] if have.startswith("---\n") else ""
            key, front_lines = ROUTE_KEY.search(head), head.count("\n") + 1
            out = []
            for number, line in enumerate(have.split("\n"), 1):
                front = key.group(1) if key and number <= front_lines else None
                line, error = route_line(line, roles, path.suffix == ".js", front)
                if error:
                    errors.append(f"{path.relative_to(hub).as_posix()}:{number}: {error}")
                out.append(line)
            renders.append((path, "\n".join(out)))
    return renders, errors


def config(hub=HUB):
    return harness_config.load(hub)


def editor_server(cfg):
    """The editor MCP server's name ([product] mcp_server), or "" when this hub registers none."""
    product = cfg.get("product", {})
    return str(product.get("mcp_server") or "") if product.get("mcp_url") else ""


def tracked_cfg(cfg):
    """cfg with no editor server: the tracked settings.json never names it, because [product] mcp_url is this box's
    choice (a git-ignored harness.local.toml). local_text enables the editor server in settings.local.json."""
    return {**cfg, "product": {**cfg.get("product", {}), "mcp_url": ""}}


def render(value, cfg):
    """Fill the manifest placeholders: {MEM_SERVER} and {MEM_TOOLS} name the memory server,
    {EDITOR_SERVER} the editor MCP server, {ENV_PREFIX} the session
    variable prefix, in keys and values.
    A server list drops an empty name."""
    if isinstance(value, dict):
        return {render(key, cfg): render(child, cfg) for key, child in value.items()}
    if isinstance(value, list):
        return [render(child, cfg) for child in value if child != "{EDITOR_SERVER}" or editor_server(cfg)]
    if isinstance(value, str):
        return (value.replace("{MEM_SERVER}", harness_config.server_name(cfg))
                .replace("{MEM_TOOLS}", cfg["derived"]["tool_prefix"])
                .replace("{EDITOR_SERVER}", editor_server(cfg))
                .replace("{ENV_PREFIX}", cfg["project"]["env_prefix"]))
    return value


def load_manifest(path=MANIFEST, cfg=None):
    """The manifest with the server placeholders filled from harness.toml."""
    return render(json.loads(Path(path).read_text(encoding="utf-8")), cfg or config())


def project_skills():
    return sorted(path.parent.name for path in (HUB / ".claude" / "skills").glob("*/SKILL.md"))


def validate_manifest(manifest, cfg=None):
    """Keep required hub behavior in project scope and personal choices out of it. With cfg, the model pin and the
    subagent model must be words of [gates] allowed_models."""
    common = manifest.get("common")
    if not isinstance(common, dict):
        raise ValueError("manifest common project settings must be an object")
    env = common.get("env")
    if not isinstance(env, dict):
        raise ValueError("project common must carry the required Claude environment")
    for key, value in REQUIRED_ENV.items():
        if env.get(key) != value:
            raise ValueError(f"project common must set {key}={value}")
    if not isinstance(env.get("CLAUDE_CODE_SUBAGENT_MODEL"), str) or not env["CLAUDE_CODE_SUBAGENT_MODEL"]:
        raise ValueError("project common must select the Claude subagent model")
    if cfg is not None:
        allowed = (cfg.get("gates") or {}).get("allowed_models") or ALIAS.split("|")
        for name, value in (("model", common.get("model")), ("CLAUDE_CODE_SUBAGENT_MODEL", env["CLAUDE_CODE_SUBAGENT_MODEL"])):
            if value not in allowed:
                raise ValueError(f"project common {name} {value!r} is not in [gates] allowed_models {allowed}")
    if common.get("autoMemoryEnabled") is not False:
        raise ValueError("project common must disable Claude auto memory")
    # A Flight that Flight.command starts runs Code Mode, which needs agent teams off; project scope never turns teams on.
    if env.get("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS", "0") != "0" or "teammateMode" in common:
        raise ValueError("project common must not set agent teams (Code Mode needs them off)")
    if common.get("crossSessionInbound") != "accept":
        raise ValueError("project common must accept cross-session messages")
    # The box-local settings add an allow list and hooks only: never a mode, an env or a blanket allow.
    local = manifest.get("local")
    if not isinstance(local, dict) or set(local) != {"permissions", "hooks"} or \
            not isinstance(local["permissions"], dict) or set(local["permissions"]) != {"allow"}:
        raise ValueError("local settings must hold permissions.allow and hooks only")
    allow = local["permissions"]["allow"]
    if not isinstance(allow, list) or not all(isinstance(rule, str) and rule for rule in allow):
        raise ValueError("local permissions.allow must be a list of rules")
    if blanket := [rule for rule in allow if rule.replace(" ", "") in ("Bash", "Bash(*)", "*")]:
        raise ValueError(f"local permissions.allow must not allow every command: {blanket}")
    # A runner with a wildcard (env -u *, xargs *, bash -c *) runs any command: a blanket allow by another name.
    if runner := [rule for rule in allow if "*" in rule and
                  re.match(rf"Bash\(\s*(?:{'|'.join(RUNNERS)})(?:\s|\*|\))", rule)]:
        raise ValueError(f"local permissions.allow must not allow every command through a runner: {runner}")

    # A hub copy with no skills on disk yet has nothing to check a skill name against.
    available = set(project_skills())
    configured = {
        skill
        for capability in manifest.get("capability_classes", {}).values()
        for skill in capability.get("skills", [])
    }
    if available and (unknown := configured - available):
        raise ValueError(f"manifest names unknown project skills: {sorted(unknown)}")
    if missing := available - configured:
        raise ValueError(f"manifest leaves project skills unscoped: {sorted(missing)}")


def validate_task_skills(manifest, cfg):
    """Each harness.toml [project] task_skills entry is a project skill the hub root can invoke."""
    available, visible = set(project_skills()), set(skills_for(manifest))
    if available and (hidden := [s for s in cfg["project"].get("task_skills", []) if s not in visible]):
        raise ValueError(f"task_skills not visible at the hub root: {hidden}")


def gates_matcher() -> str:
    """The PreToolUse matcher rendered from the GATES table in Drawer/project-gate.py."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("project_gate_table", HUB / "Drawer" / "project-gate.py")
    table = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(table)
    return table.claude_matcher()


# Hub paths are relative to the session's project dir, so they survive any box or copy.
HUB_PREFIX = "$CLAUDE_PROJECT_DIR"


def hooks_for(manifest):
    out = {}
    for event, entries in manifest.get("hooks", {}).items():
        # Events without tool matching (UserPromptSubmit, Stop, ...) omit "matcher".
        rendered = []
        for entry in entries:
            timeout = entry.get("timeout")
            if timeout is not None and (
                not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 1
            ):
                raise ValueError(f"{event} hook timeout must be a positive integer")
            hook = {"type": "command", "command": entry["command"].replace("{HUB}", HUB_PREFIX)}
            if timeout is not None:
                hook["timeout"] = timeout
            for flag in ("async", "asyncRewake"):
                if entry.get(flag) is True:
                    hook[flag] = True
            matcher = entry.get("matcher")
            if matcher == GATES_MATCHER:
                matcher = gates_matcher()
            rendered.append({
                **({"matcher": matcher} if matcher else {}),
                "hooks": [hook],
            })
        out[event] = rendered
    return out


def servers_for(manifest):
    enabled = []
    for capability in manifest["root"]:
        enabled.extend(manifest["capability_classes"][capability]["servers"])
    return list(dict.fromkeys(enabled))


def skills_for(manifest):
    enabled = []
    for capability in manifest["root"]:
        enabled.extend(manifest["capability_classes"][capability].get("skills", []))
    return list(dict.fromkeys(enabled))


def body_for(manifest):
    enabled = servers_for(manifest)
    disabled = [s for s in manifest["all_servers"] if s not in enabled]
    body = dict(manifest["common"])
    visible_skills = set(skills_for(manifest))
    overrides = dict(body.get("skillOverrides", {}))
    for skill in project_skills():
        if skill in visible_skills:
            overrides.pop(skill, None)
        else:
            overrides[skill] = "user-invocable-only"
    body["skillOverrides"] = dict(sorted(overrides.items()))
    if "statusLine" in body:
        status = dict(body["statusLine"])
        status["command"] = status["command"].replace("{HUB}", HUB_PREFIX)
        body["statusLine"] = status
    body["enabledMcpjsonServers"] = sorted(enabled)
    if disabled:
        body["disabledMcpjsonServers"] = sorted(disabled)
    hooks = hooks_for(manifest)
    if hooks:
        body["hooks"] = hooks
    return json.dumps(body, indent=2) + "\n"


def product_main(cfg):
    """The product repo's main checkout, or None: no project_path, no Git checkout, or Git missing or hung."""
    try:
        found = product_worktree.product_repo(cfg)
    except (OSError, subprocess.SubprocessError):
        return None
    return found[0] if found else None


def user_temp_dir():
    """The box's temp folder, not this process's TMPDIR, so a sandboxed shell renders the same file. A Mac
    answers its per-user folder by getconf; elsewhere, or on a failure, the process temp folder."""
    if sys.platform == "darwin":
        try:
            found = subprocess.run(["getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True, text=True,
                                   timeout=10, check=True).stdout.strip()
            if found:
                return Path(found)
        except (OSError, subprocess.SubprocessError):
            pass
    return Path(tempfile.gettempdir())


def local_dirs(cfg, hub=HUB):
    """The folders Flight's legs write beyond the hub root, each under its written and its resolved
    spelling: the product task trees (<product repo>_wt, when [product] path names a Git checkout), the hub task
    worktrees (<main hub>_wt), state_root and the temp folders."""
    product = product_main(cfg)
    dirs = [Path(f"{product}_wt")] if product else []
    dirs += [Path(f"{harness_config.main_checkout(hub)}_wt"), harness_config.path(cfg["project"]["state_root"])]
    dirs += ([] if os.name == "nt" else [Path("/tmp")]) + [user_temp_dir()]
    out = []
    for folder in dirs:
        for spelling in (str(folder).rstrip("/") or "/", str(folder.resolve())):
            if spelling not in out:
                out.append(spelling)
    return out


def leg_seat_command(cfg, hub=HUB):
    """The lean SubagentStart boot: a printf of one SEAT line, so a leg starts with no Python and no door."""
    # Forward slashes: sh -c strips the backslashes of a Windows path out of the JSON, and Windows reads both.
    product = product_main(cfg)
    main = harness_config.main_checkout(hub).as_posix()
    parts = [f"hub={main}", f"hub_trees={main}_wt"]
    if product:
        parts += [f"product={product.as_posix()}", f"product_trees={product.as_posix()}_wt"]
    parts.append(f"state_root={harness_config.path(cfg['project']['state_root']).as_posix()}")
    answer = {"hookSpecificOutput": {"hookEventName": "SubagentStart",
                                     "additionalContext": "SEAT " + " ".join(parts)}}
    return "printf '%s\\n' " + shlex.quote(json.dumps(answer))


def local_text(manifest, cfg, hub=HUB):
    """The box-local .claude/settings.local.json: the local allow list plus this box's hub interpreter, the
    add-dirs and the local hooks (the lean leg boot), and the editor MCP server when [product] names one."""
    allow = list(manifest["local"]["permissions"]["allow"])
    # The rule matches the command text Git Bash runs, so it names the .venv/bin/python spelling (a shim on Windows)
    # and a forward-slash path, whatever the box.
    python = f"Bash({harness_config.main_checkout(hub).as_posix()}/.venv/bin/python *)"
    if python not in allow:
        allow.append(python)
    hooks = hooks_for(manifest["local"])
    seat = leg_seat_command(cfg, hub)
    for entries in hooks.values():
        for entry in entries:
            for hook in entry["hooks"]:
                hook["command"] = hook["command"].replace(LEG_SEAT, seat)
    body = {"permissions": {"allow": allow, "additionalDirectories": local_dirs(cfg, hub)}, "hooks": hooks}
    if editor_server(cfg):
        body["enabledMcpjsonServers"] = [editor_server(cfg)]
    return json.dumps(body, indent=2) + "\n"


def runtime_args(server, cfg):
    """Portable argv that finds the hub from the cwd first, then from <PREFIX>_HUB.

    A session in a task worktree or a subfolder starts the server from there, so the walk goes up
    to the folder that holds the marker file. The env fallback counts only when it holds it.
    """
    marker = cfg["project"]["marker_file"]
    entry = (
        "from pathlib import Path;import os,runpy,sys;"
        f"m={marker!r};"
        "h=next((p for p in (Path.cwd(),*Path.cwd().parents) if (p/m).is_file()),None);"
        f"e=os.environ.get({harness_config.env_name('HUB', cfg)!r});"
        "h=h or (Path(e) if e and (Path(e)/m).is_file() else None);"
        "assert h is not None,'hub root not found';"
        "s=h/'Drawer'/'mem'/'mem.py';"
        "sys.argv=[str(s),'mcp'];"
        "runpy.run_path(str(s),run_name='__main__')"
    )
    return ["-B", "-c", entry]


def mcp_json_text(manifest, cfg):
    """This box's Claude MCP registration: every manifest server, launched by the hub venv.

    Claude's tool search defers every server's schemas until a session asks for them;
    no server here sets alwaysLoad, because a server-level true loads every tool upfront.
    """
    editor = editor_server(cfg)
    # A task worktree has no .venv of its own: it borrows the interpreter that renders it.
    python = cfg["derived"]["python"] if Path(cfg["derived"]["python"]).is_file() else sys.executable
    registrations = {
        server: ({"type": "http", "url": cfg["product"]["mcp_url"]} if server == editor else
                 {"command": python, "args": runtime_args(server, cfg), "env": {"PYTHONUTF8": "1"}})
        for server in manifest["all_servers"]
    }
    return json.dumps({"mcpServers": registrations}, indent=2) + "\n"


def sync_projects(apply_, hub=HUB, mcp=True):
    cfg = config(hub)
    try:
        manifest = load_manifest(MANIFEST, cfg)
        validate_manifest(manifest, cfg)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 2

    unknown_capabilities = {c for c in manifest.get("root", []) if c not in manifest["capability_classes"]}
    if unknown_capabilities:
        print(f"ERROR: root names unknown capability classes: {sorted(unknown_capabilities)}")
        return 2
    try:
        validate_task_skills(manifest, cfg)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 2
    unknown = {
        server
        for capability in manifest["capability_classes"].values()
        for server in capability["servers"]
        if server not in manifest["all_servers"]
    }
    if unknown:
        print(f"ERROR: manifest names servers not in all_servers: {sorted(unknown)}")
        return 2

    routed, route_errors = route_renders(cfg, hub)
    if route_errors:
        print("ERROR: model routing ([routing.legs] in harness.toml):\n  " + "\n  ".join(route_errors))
        return 2

    rel = lambda p: p.relative_to(hub).as_posix()
    drift, dropped, written = [], [], 0
    wanted = [(hub / ".claude" / "settings.json", body_for(load_manifest(MANIFEST, tracked_cfg(cfg))))]
    if mcp:  # box-local, git-ignored: the commit tree (--tracked) holds neither
        wanted.append((hub / MCP_JSON, mcp_json_text(manifest, cfg)))
        wanted.append((hub / LOCAL_JSON, local_text(manifest, cfg, hub)))
    wanted.extend(routed)

    for path, want in wanted:
        have = path.read_text(encoding="utf-8") if path.exists() else None
        written += 1
        if have == want:
            continue
        drift.append(path)
        if have is not None and path.suffix == ".json":
            try:
                lost = set(json.loads(have)) - set(json.loads(want))
            except json.JSONDecodeError:
                lost = {"<unparseable>"}
            if lost:
                dropped.append((path, sorted(lost)))
        if apply_:
            path.parent.mkdir(exist_ok=True)
            path.write_text(want, encoding="utf-8", newline="\n")

    # A subfolder settings.json is read by a session started in that folder, which no session here does.
    # Surface it rather than leave it silent.
    for stale in sorted(hub.glob("*/.claude/settings.json")):
        print(f"  ORPHAN: {rel(stale)} - every session seats at the hub root; delete it")

    for path, lost in dropped:
        print(f"  note: {rel(path)} had keys the manifest does not produce: {lost}")
    if not drift:
        print(f"in sync: {written} generated files match the manifest")
        return 0
    verb = "wrote" if apply_ else "DRIFT"
    for path in drift:
        print(f"  {verb}: {rel(path)}")
    print(f"{verb}: {len(drift)} of {written} generated files")
    return 0 if apply_ else 1


def write_mcp_json(hub=HUB):
    """Write only this box's git-ignored renders, .mcp.json and .claude/settings.local.json (no check mode).
    Git-branch-policy runs it after each freshness, land and worktree-add, so both follow the checked-out tree."""
    cfg = config(hub)
    try:
        manifest = load_manifest(MANIFEST, cfg)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 2
    for rel, want in ((MCP_JSON, mcp_json_text(manifest, cfg)),
                      (LOCAL_JSON, local_text(manifest, cfg, hub))):
        path = hub / rel
        if path.exists() and path.read_text(encoding="utf-8") == want:
            print(f"in sync: {rel}")
            continue
        path.parent.mkdir(exist_ok=True)
        path.write_text(want, encoding="utf-8", newline="\n")
        print(f"wrote: {rel}")
    return 0


def main(argv=None):
    args = set(sys.argv[1:] if argv is None else argv)
    if args - {"--apply", "--mcp", "--tracked"} or ("--mcp" in args and len(args) > 1):
        print(__doc__.replace("<python>", PY))
        return 2
    if "--mcp" in args:
        return write_mcp_json()
    return sync_projects("--apply" in args, mcp="--tracked" not in args)


if __name__ == "__main__":
    sys.exit(main())
