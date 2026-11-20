"""Rules audit: the removal test on the hub's instruction files (harness skill OPR-17).

The root AGENTS.md loads into every session and a skill stays in context once loaded, so every line costs
attention on every task. The guidance and its sources: .claude/skills/harness/instruction-files.md.
The pre-commit trigger runs the audit on each edit:

  staged <repo>    pre-commit (through Drawer/rules_check.py --index): exit 1 when the index changes AGENTS.md or a .claude/skills/*/SKILL.md whose
                   staged text has no pass row (run the audit on it, then `pass`), or holds a rule-ID fault: a
                   staged AGENTS.md takes the full ID check below and fails closed when the history is unreadable;
                   a staged skill alone takes the duplicate check.

  check            print one line per instruction file: AGENTS.md (cap 40) and each SKILL.md (cap 500),
                   OVER or OK with its line count, then one ID line per rule-ID fault; exit 1 when one is
                   over or a fault stands.

The rule-ID check (harness skill OPR-10): a root AGENTS.md rule line (a `- ` or `<n>. ` line outside
`## Words`) with no `<PFX>-<n> <slug> — ` head; a root ID used twice over AGENTS.md and the skills; an
AGENTS.md ID that a commit since ID_BASE cut (seen in one snapshot of AGENTS.md and the skills, absent later).
  log --file F --action cut|move --line TEXT [--home H] [--why W]
                   append one cut or move to state_root/rules-audit/log.jsonl.
  pass --file F    after the cold review leg passes: record F's text as audited (passed.jsonl).

The audit: the rules-trim workflow (.claude/workflows/rules-trim.js), mode 'propose', then each cut or move
and its log row, then mode 'review' on a second model.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys

HERE = Path(__file__).resolve().parent
HUB = HERE.parent
CAPS = {"AGENTS.md": 40, "SKILL.md": 500}  # rules_check.RULES_CAP reads the AGENTS.md cap here
WORKFLOW = ".claude/workflows/rules-trim.js"
GUIDE = ".claude/skills/harness/instruction-files.md"


def state_dir() -> Path:
    sys.path.insert(0, str(HERE))
    import harness_config
    cfg = harness_config.load(harness_config.hub_root(HERE))
    return harness_config.path(cfg["project"]["state_root"]) / "rules-audit"


def files(hub: Path = HUB) -> list[Path]:
    return [hub / "AGENTS.md", *sorted((hub / ".claude" / "skills").glob("*/SKILL.md"))]


def counts(hub: Path = HUB) -> list[tuple[str, int, int]]:
    """(hub-relative path, lines, cap) for each instruction file."""
    out = []
    for p in files(hub):
        if p.is_file():
            out.append((p.relative_to(hub).as_posix(), len(p.read_text(encoding="utf-8").splitlines()), CAPS[p.name]))
    return out


def git(repo, *args) -> str:
    """stdout of one git call; '' on any failure, git missing too, so the watchdog loop never dies here."""
    try:
        out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    except OSError:
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def is_rule_file(path: str) -> bool:
    parts = path.split("/")
    return path == "AGENTS.md" or (len(parts) == 4 and parts[:2] == [".claude", "skills"] and parts[3] == "SKILL.md")


def passed(folder: Path) -> set[tuple[str, str]]:
    try:
        rows = (folder / "passed.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    out = set()
    for r in rows:
        try:
            d = json.loads(r)
            out.add((d["file"], d["blob"]))
        except (ValueError, KeyError, TypeError):
            continue
    return out


def staged_files(repo: str) -> list[str] | None:
    """Each path the index adds, changes or renames to, unquoted (-z); None when git cannot say."""
    try:
        out = subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--name-only", "-z", "--diff-filter=AMR"],
                             capture_output=True, text=True)
    except OSError:
        return None
    return [f for f in out.stdout.split("\0") if f] if out.returncode == 0 else None


def unaudited(repo: str, folder: Path) -> list[str]:
    """Each rule file the index adds or changes whose staged text has no pass row."""
    names = [f for f in staged_files(repo) or [] if is_rule_file(f)]
    ok = passed(folder) if names else set()
    return [f for f in names if (f, git(repo, "rev-parse", f":{f}")) not in ok]


def staged_unaudited(repo: str) -> list[str]:
    """unaudited() that reads the state folder only when a rule file is staged: a commit of other files never
    needs the hub config."""
    names = [f for f in staged_files(repo) or [] if is_rule_file(f)]
    return unaudited(repo, state_dir()) if names else []

# The root rule-ID law (harness skill OPR-10): each root AGENTS.md rule line starts `- <PFX>-<n> <slug> — `;
# never renumber or reuse an ID. ID_BASE is the commit that brought the IDs in: the replay of
# retired IDs starts there. Empty: the replay starts at the root commit of rev.
PREFIXES = ("MSN", "SKL", "RTE", "INC", "MEM", "WRK", "SHP", "SAF", "CMP")
ID_BASE = ""
_PFX = "|".join(PREFIXES)
HEAD = re.compile(rf"^\s*- ({_PFX})-([0-9]+) [a-z0-9]+(?:-[a-z0-9]+)* — ")
ROOT_ID = re.compile(rf"^\s*- ((?:{_PFX})-[0-9]+) ")
RULE_LINE = re.compile(r"^\s*(- |[0-9]+\. )")
ID_LAW = "(harness skill OPR-10: never renumber or reuse; a new line takes the next free number)"
SKILL_GLOB = ".claude/skills/*/SKILL.md"


def id_faults(agents: str, skills: dict[str, str], retired: set[str] | None) -> list[str]:
    """Each rule-ID fault: a root rule line with no head, an ID used twice over AGENTS.md and the skills, and an
    AGENTS.md ID that an earlier commit retired (skipped when retired is None)."""
    out, places, words = [], {}, False
    for n, line in enumerate(agents.splitlines(), 1):
        if line.startswith("## "):
            words = line.strip() == "## Words"
        if not words and RULE_LINE.match(line) and not HEAD.match(line):
            out.append(f"AGENTS.md:{n} has no `<PFX>-<n> <slug> — ` head {ID_LAW}")
        m = ROOT_ID.match(line)
        if m:
            places.setdefault(m[1], []).append(f"AGENTS.md:{n}")
            if retired and m[1] in retired:
                out.append(f"AGENTS.md:{n} reuses {m[1]}, retired by an earlier commit {ID_LAW}")
    for path in sorted(skills):
        for n, line in enumerate(skills[path].splitlines(), 1):
            m = ROOT_ID.match(line)
            if m:
                places.setdefault(m[1], []).append(f"{path}:{n}")
    out += [f"{i} is used more than once: {', '.join(p)} {ID_LAW}" for i, p in places.items() if len(p) > 1]
    return out


FAIL = subprocess.CompletedProcess([], 1, "", "")


def run_git(repo, *args) -> subprocess.CompletedProcess:
    """One git call; FAIL when git cannot run."""
    try:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    except OSError:
        return FAIL


def retired_ids(repo, rev: str = "HEAD") -> set[str] | None:
    """Each root ID that a commit from ID_BASE to rev cut: seen in one snapshot of AGENTS.md and the skills,
    absent from a later one. A moved line keeps its ID, so it is not retired. None when git cannot say."""
    base = ID_BASE
    if not base:
        roots = run_git(repo, "rev-list", "--max-parents=0", rev)
        if roots.returncode or not roots.stdout.split():
            return None
        base = roots.stdout.split()[-1]
    if run_git(repo, "cat-file", "-e", f"{base}^{{commit}}").returncode:
        return None
    revs = run_git(repo, "rev-list", "--reverse", "--first-parent", f"{base}..{rev}", "--", "AGENTS.md",
                   ".claude/skills")
    if revs.returncode:
        return None
    seen, retired = set(), set()
    for sha in [base, *revs.stdout.split()]:
        grep = run_git(repo, "grep", "-h", "-E", rf"^[[:space:]]*- ({_PFX})-[0-9]+ ", sha, "--", "AGENTS.md",
                       SKILL_GLOB)
        if grep.returncode not in (0, 1):  # 1: no match
            return None
        present = {m[1] for m in map(ROOT_ID.match, grep.stdout.splitlines()) if m}
        seen |= present
        retired |= seen - present
    return retired


def history_fault(repo) -> str:
    return (f"rule-ID history unreadable: base {(ID_BASE or 'root')[:8]} is not in {repo}, so a reused retired ID cannot be "
            f"found {ID_LAW}")


def id_report(hub: Path) -> list[str]:
    """The rule-ID faults of the work-tree AGENTS.md and skills under hub (`check`)."""
    agents = hub / "AGENTS.md"
    skills = {p.relative_to(hub).as_posix(): p.read_text(encoding="utf-8")
              for p in sorted((hub / ".claude" / "skills").glob("*/SKILL.md"))}
    retired = retired_ids(hub)
    text = agents.read_text(encoding="utf-8") if agents.is_file() else ""
    return ([] if retired is not None else [history_fault(hub)]) + id_faults(text, skills, retired or set())


def staged_id_faults(repo: str, names: list[str]) -> list[str]:
    """The rule-ID faults of the index texts. A staged AGENTS.md takes the full check, and fails closed when the
    history cannot be read; a staged skill alone takes the duplicate check."""
    if not any(is_rule_file(f) for f in names):
        return []
    paths = run_git(repo, "ls-files", "-z", "--", SKILL_GLOB).stdout.split("\0")
    skills = {p: run_git(repo, "show", f":{p}").stdout for p in paths if is_rule_file(p)}
    agents = run_git(repo, "show", ":AGENTS.md").stdout
    if "AGENTS.md" not in names:
        return [f for f in id_faults(agents, skills, None) if " is used more than once: " in f]
    retired = retired_ids(repo)
    return ([] if retired is not None else [history_fault(repo)]) + id_faults(agents, skills, retired or set())


def refusals(repo: str) -> list[str]:
    """The pre-commit lines for each staged rule file with no pass; Drawer/rules_check.py --index prints them."""
    names = staged_files(repo)
    if names is None:
        return ["pre-commit: git cannot list the staged files, so the rules audit check cannot run. Fix git, "
                "then commit again."]
    return [f"pre-commit: {f} changed and its staged text has no rules audit pass. Run the rules audit on it "
            f"(harness skill OPR-17: the rules-trim workflow, propose then a cold review), then "
            f"Drawer/rules_audit.py pass --file {f}." for f in staged_unaudited(repo)] + \
        [f"pre-commit: {f}" for f in staged_id_faults(repo, names)]


def repo_path(file: str) -> str:
    """The repo-relative POSIX path of file as the index names it, from any folder; '' outside the repo."""
    top = git(".", "rev-parse", "--show-toplevel")
    if not top:
        return ""
    rel = os.path.relpath(os.path.realpath(file), os.path.realpath(top))
    return "" if rel.startswith("..") else Path(rel).as_posix()


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="rules_audit.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    st = sub.add_parser("staged")
    st.add_argument("repo", nargs="?", default=".")
    ps = sub.add_parser("pass")
    ps.add_argument("--file", required=True)
    lg = sub.add_parser("log")
    lg.add_argument("--file", required=True)
    lg.add_argument("--action", required=True, choices=("cut", "move"))
    lg.add_argument("--line", required=True)
    lg.add_argument("--home", default="")
    lg.add_argument("--why", default="")
    a = p.parse_args(argv)
    now = datetime.now(timezone.utc)
    if a.cmd == "check":
        over = 0
        for path, n, cap in counts(HUB):
            over += n > cap
            print(f"{'OVER' if n > cap else 'OK'} {path} {n}/{cap}")
        ids = id_report(HUB)
        for fault in ids:
            print(f"ID {fault}")
        print(f"rules audit: {over} over the cap, {len(ids)} rule-ID faults. Removal test on each rule line: "
              f"{WORKFLOW}; guidance: {GUIDE}")
        return 1 if over or ids else 0
    if a.cmd == "staged":
        bad = refusals(a.repo)
        for line in bad:
            print(line, file=sys.stderr)
        return 1 if bad else 0
    folder = state_dir()
    folder.mkdir(parents=True, exist_ok=True)
    if a.cmd == "log":
        if a.action == "move" and not a.home:
            p.error("a move names its new home: --home")
        row = {"time": now.isoformat(), "file": a.file, "action": a.action, "line": a.line, "home": a.home,
               "why": a.why}
        with open(folder / "log.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        print(f"rules audit: logged {a.action} in {a.file}")
        return 0
    if a.cmd == "pass":
        rel, blob = repo_path(a.file), git(".", "hash-object", "--", a.file)
        if not is_rule_file(rel) or not blob:
            p.error(f"{a.file} is not a readable AGENTS.md or .claude/skills/<name>/SKILL.md of this repo")
        with open(folder / "passed.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"time": now.isoformat(), "file": rel, "blob": blob}) + "\n")
        print(f"rules audit: {rel} {blob[:12]} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
