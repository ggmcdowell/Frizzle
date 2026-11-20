"""Rules check: the root AGENTS.md must reach every session, and no CLAUDE.md may shadow it.

Claude Code loads AGENTS.md (2.1.277; 2.1.281 on every provider and with telemetry off; setting
instructionFiles, default "claude-md-or-agents-md") only when the working folder and its parents
hold no CLAUDE.md, .claude/CLAUDE.md or CLAUDE.local.md. Each session seats at the hub root, so one of these
files in the hub root or a parent folder turns the rules off with no warning: it SHADOWS.
~/.claude/CLAUDE.md is user scope and does not shadow. The same file in the product repo or a product
worktree only ADDS rules when a session reads a file there; the doc-gate refuses it all the same.

  notice()          the SessionStart lines: the sentinel ask, and a loud line per fault,
                    UPGRADE REQUIRED among them when claude is older than MIN_CLI.
  --index <repo>    pre-commit: exit 1 when the index holds a CLAUDE.md or CLAUDE.local.md, or a root
                    AGENTS.md over RULES_CEILING lines, or a staged AGENTS.md or SKILL.md with no rules
                    audit pass or with a rule-ID fault (Drawer/rules_audit.py refusals). A root AGENTS.md over RULES_CAP and up to
                    RULES_CEILING commits with a TRIM DUE note: the committer runs the trim next (harness
                    skill OPR-17). The ceiling is the last resort only.
  --check           print the faults, exit 1 when there is one.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HUB = HERE.parent
sys.path.insert(0, str(HERE))
import rules_audit  # noqa: E402

SENTINEL = "Rules sentinel:"
SHADOW_NAMES = ("CLAUDE.md", "CLAUDE.local.md")
MIN_CLI = (2, 1, 281)
RULES_CAP = rules_audit.CAPS["AGENTS.md"]  # one home: past it a trim is due (the TRIM DUE note)
RULES_CEILING = RULES_CAP + 10  # past it the commit is refused
TRIM = ".claude/workflows/rules-trim.js"
GIT_TIMEOUT = 10  # s per git call, so a hung git cannot stall the SessionStart hook


def user_file() -> Path:
    return Path(os.path.expanduser("~")) / ".claude" / "CLAUDE.md"


def candidates(folder: Path) -> list[Path]:
    return [folder / "CLAUDE.md", folder / "CLAUDE.local.md", folder / ".claude" / "CLAUDE.md"]


def is_shadow_name(path: str) -> bool:
    """A file name that Claude Code reads as project instructions in place of AGENTS.md."""
    return Path(path).name in SHADOW_NAMES


def shadowing(path: str, hub: Path = HUB) -> bool:
    """True when a file at path would turn AGENTS.md off for a session seated at hub."""
    p = Path(os.path.abspath(path))
    if not is_shadow_name(str(p)) or p == user_file():
        return False
    folder = p.parent.parent if p.parent.name == ".claude" else p.parent
    hub = Path(os.path.abspath(hub))
    return folder == hub or folder in hub.parents


def product_checkouts(cfg: dict | None = None) -> list[Path]:
    """The product repo's main checkout and its linked worktrees; none when [product] path is unset."""
    sys.path.insert(0, str(HERE))
    import harness_config
    cfg = cfg or harness_config.load(HUB)
    project = str((cfg.get("product") or {}).get("path") or "")
    if not project:
        return []
    out = subprocess.run(["git", "-C", str(harness_config.path(project)), "worktree", "list", "--porcelain"],
                         capture_output=True, text=True, env=harness_config.git_env(), timeout=GIT_TIMEOUT)
    return [Path(line[len("worktree "):]) for line in out.stdout.splitlines() if line.startswith("worktree ")]


def faults(hub: Path = HUB, products: list[Path] | None = None) -> list[str]:
    """One line per fault: a missing sentinel, a shadowing file, a product-repo instruction file."""
    found = []
    agents = hub / "AGENTS.md"
    try:
        lines = agents.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = None
    if lines is None:
        found.append(f"RULES MISSING: {agents} is not readable.")
    elif not any(SENTINEL in line for line in lines):
        found.append(f"RULES SENTINEL MISSING: {agents} has no '{SENTINEL}' line.")
    for folder in [hub, *hub.parents]:
        for p in candidates(folder):
            if p.is_file() and p != user_file():
                found.append(f"RULES SHADOWED: {p} turns AGENTS.md off for every hub session. Delete it.")
    if products is None:
        try:
            products = product_checkouts()
        except subprocess.TimeoutExpired as exc:
            products = []
            found.append(f"RULES CHECK FAULT: git worktree list in {exc.cmd[2]} timed out after {GIT_TIMEOUT} s; "
                         f"the product repo instruction files went unchecked.")
    for root in products:
        # The checkout root and each top folder: a product folder (the [product] path) sits one level down.
        tops = [d for d in sorted(root.iterdir()) if d.is_dir() and d.name != ".git"] if root.is_dir() else []
        for p in [p for folder in (root, *tops) for p in candidates(folder)]:
            if p.is_file():
                found.append(f"RULES ADDED: {p} adds rules when a session reads the product repo. Delete it.")
    return found


def cli_version() -> str:
    try:
        return subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def notice(hub: Path = HUB, products: list[Path] | None = None) -> str:
    try:
        bad = faults(hub, products)
        version = cli_version()
        if cli_too_old(version):
            bad.append(f"UPGRADE REQUIRED: claude {version.strip()} is older than "
                       f"{'.'.join(map(str, MIN_CLI))} and may not load AGENTS.md. Run claude update.")
    except Exception as exc:
        bad = [f"RULES CHECK FAILED: {exc}"]
    ask = (f"RULES CHECK: the root AGENTS.md line that starts '{SENTINEL}' must be in your context; this "
           f"hook hides its code. If you cannot read it, stop all work and send NEED rules missing.")
    if bad:
        ask += " Stop all work and send NEED rules fault with each line below.\n" + "\n".join(bad)
    return ask


def cli_too_old(version_text: str) -> bool:
    """True when `claude --version` names a version under MIN_CLI (no native AGENTS.md load)."""
    head = version_text.strip().split(" ")[0]
    try:
        return tuple(int(x) for x in head.split(".")[:3]) < MIN_CLI
    except ValueError:
        return False


def index_shadows(repo: str) -> list[str]:
    out = subprocess.run(["git", "-C", repo, "ls-files", "--cached"], capture_output=True, text=True, check=True)
    return [f for f in out.stdout.splitlines() if is_shadow_name(f)]


def staged_names(repo: str) -> list[str] | None:
    """Each path the index adds, changes or renames to, unquoted (-z); None when git cannot say."""
    out = subprocess.run(["git", "-C", repo, "diff", "--cached", "--name-only", "-z", "--diff-filter=AMR"],
                         capture_output=True, text=True)
    return [f for f in out.stdout.split("\0") if f] if out.returncode == 0 else None


def index_rules_lines(repo: str) -> int:
    """Lines of the root AGENTS.md in the index; 0 when the index holds none."""
    out = subprocess.run(["git", "-C", repo, "show", ":AGENTS.md"], capture_output=True, text=True)
    return len(out.stdout.splitlines()) if out.returncode == 0 else 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["--index"]:
        repo = argv[1] if len(argv) > 1 else "."
        bad = index_shadows(repo)
        for f in bad:
            print(f"pre-commit: {f} is a Claude instruction file. AGENTS.md is the only rules file: "
                  f"a CLAUDE.md turns it off. Remove it from the index.", file=sys.stderr)
        n = index_rules_lines(repo)
        if n > RULES_CEILING:
            print(f"pre-commit: AGENTS.md has {n} lines, over the {RULES_CEILING}-line ceiling. "
                  f"Trim it now: run the rules-trim workflow ({TRIM}) with mode 'propose', cut or move "
                  f"each line that fails the removal test, then run it with mode 'review' (harness "
                  f"skill, OPR-17).", file=sys.stderr)
        elif n > RULES_CAP:
            print(f"pre-commit: TRIM DUE: AGENTS.md has {n} lines, over the {RULES_CAP}-line cap. "
                  f"The commit passes. Trim it next to the cap or below: the rules-trim workflow ({TRIM}), "
                  f"mode 'propose', then mode 'review' (harness skill, OPR-17).", file=sys.stderr)
        audit = []
        names = staged_names(repo)
        if names is None or any(f == "AGENTS.md" or f.endswith("/SKILL.md") for f in names):
            audit = rules_audit.refusals(repo)
        for line in audit:
            print(line, file=sys.stderr)
        return 1 if bad or audit or n > RULES_CEILING else 0
    bad = faults()
    print("\n".join(bad) or "rules check: AGENTS.md has its sentinel and nothing shadows it.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
