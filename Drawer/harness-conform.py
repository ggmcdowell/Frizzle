"""Harness conformance for the hub — one command, one exit code.

This is the thin aggregator. It runs the checks that already exist as their own tools,
then adds the classes a sweep keeps re-finding by hand: dead pointers in the root rules
and skills, a skill or agent name with no file, a hook path that does not resolve, and a
hiding-place filename doc-gate's regex misses.

Only a critical fault holds a commit (rule caps): hidden files, stale
cites, dead hook paths and a live MCP launch fault. A form finding prints WARN and the commit goes
on: dead pointers, missing skill or agent names, blob sizes, seat pins and a model id, port or hub
path that harness.toml names hard-coded in Drawer code (config, WRK-2). A settings drift is
fixed in place: the commit's own tree renders settings-sync --apply --tracked and the gate stages
the render. The secret scan, the name check and the rules caps and audit pass run in the hook
itself (Drawer/git-hooks/pre-commit) and always block.

    <python> Drawer/harness-conform.py           # all checks; exit 1 if any FAIL
    <python> Drawer/harness-conform.py -v        # ... plus every finding line
    <python> Drawer/harness-conform.py --list    # check names only
    <python> Drawer/harness-conform.py pointers skills  # run named checks only
    <python> Drawer/harness-conform.py config -v  # hard-coded harness.toml values in Drawer code
    <python> Drawer/harness-conform.py --apply   # regenerate every generated file, then the offline checks
    <python> Drawer/harness-conform.py --commit  # the pre-commit subset: offline, staged paths, the index
    <python> Drawer/harness-conform.py --commit -- <paths>  # precheck what git commit -- <paths> delivers

--apply is the one "regenerate everything" command: settings-sync, then the offline
checks (--no-check skips them, for setup). --commit is what Drawer/git-hooks/pre-commit
runs: no network, no seat. It judges the tree the commit delivers, not the shared
working tree: it exports the index (GIT_INDEX_FILE in a hook) to a temporary folder and runs
that tree's own copy of this file there (--commit-tree), so an unstaged draft never holds
the commit. There hiding, pointers and blobs judge the staged paths (pointers judge every
standing document when the commit deletes a file). A held check prints its own fix (FIXES).
It skips itself during a rebase, whose intermediate trees legitimately disagree with the
manifest. HARNESS_CONFORM=off skips the commit subset (the gate refuses a git command that sets it inline), =dry judges without logging; each held
commit appends one line to Drawer/commit-gate.log in the main checkout.

Precheck (the faster shared desk): Git holds the shared index lock
for the whole pre-commit hook, so a slow suite run makes every other session wait. Run
--commit before git commit, outside the lock: with `-- <paths>` it judges HEAD plus those
paths, the tree git commit -- <paths> delivers, in a private index. Clean tree checks record
that tree's hash in the Git dir (PASS_FILE); the hook then skips the tree checks for the same
tree and still runs the live MCP check. Any other tree, after another commit moves HEAD
included, runs the full subset in the hook as before. The precheck never runs the live MCP check;
the hook runs that check anyway.

Estate cost: one file. Maintainer: whoever changes the harness (rules, gates, hooks,
skills, lanes). A new mechanical rule adds a check here; a check that goes prose-only
is deleted here, not left passing vacuously.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import re
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

HUB = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HUB / "Drawer"))
import harness_config  # noqa: E402  (Drawer/ is on the path from here on)
import oscompat  # noqa: E402

# The hub's file folders: a backticked path under one is a hub pointer.
FOLDERS = ("Flight",)

PASS, WARN, FAIL, UNAVAILABLE = "PASS", "WARN", "FAIL", "UNAVAILABLE"  # WARN never holds a commit
PY = ".venv/bin/python -B"  # a new seat has no py launcher
FIX_LINE = f"{PY} Drawer/harness-conform.py --apply"


def _load(path: Path, name: str):
    """Import one Drawer module by path; hyphenated file names are not identifiers."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Result:
    def __init__(self, name: str, state: str, summary: str, findings=None, fixed=None):
        self.name = name
        self.state = state
        self.summary = summary
        self.findings = findings or []
        self.fixed = fixed or []  # hub-relative files the commit's tree rendered; the gate stages them


def _run(args: list[str], timeout: int = 600, env=None):
    env = dict(os.environ if env is None else env, PYTHONDONTWRITEBYTECODE="1")
    try:
        # Own process group, so a timeout reaches the grandchildren too.
        p = subprocess.Popen(args, cwd=HUB, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, env=env, **oscompat.popen_group())
    except FileNotFoundError as exc:
        return None, str(exc)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Kill the whole tree before draining: a grandchild holding the pipe open
        # would otherwise keep communicate() waiting past the bound.
        try:
            oscompat.kill_tree(p.pid)
        except OSError:  # ProcessLookupError: it ended just now
            pass
        p.kill()
        p.communicate()
        return None, f"timed out after {timeout}s"
    return p.returncode, (out or "") + (err or "")


# ---------------------------------------------------------------- wrapped tools

def check_settings(tracked=False, fix=False) -> Result:
    """The generated settings match the manifest. tracked: in --commit only the tracked
    settings files, because the exported tree has no box-local .mcp.json. settings-sync exits 0
    in sync, 1 on a drift, 2 on a manifest it cannot render. fix: render a drift here (the
    exported tree in --commit) and name each written file in fixed."""
    checker = HUB / "Drawer/settings-sync.py"
    if not checker.is_file():
        return Result("settings", FAIL, "required checker is missing",
                      [str(checker.relative_to(HUB))])
    scope = ["--tracked"] if tracked else []
    code, out = _run([sys.executable, "-B", str(checker), *scope])
    if code == 1 and fix:
        code, out = _run([sys.executable, "-B", str(checker), "--apply", *scope])
        # One "  wrote: <path>" line per file; the closing "wrote: N of M ..." line is no path.
        wrote = [line.split("wrote:", 1)[1].strip() for line in out.splitlines()
                 if line.startswith("  wrote: ")]
        if code == 0 and wrote:
            return Result("settings", PASS, f"settings drift fixed: {len(wrote)} file(s) rendered; "
                                            "the commit stages them", wrote, fixed=wrote)
    if code is None:
        return Result("settings", FAIL, f"required checker did not run: {out.strip()[:120]}")
    tail = [l for l in out.strip().splitlines() if l.strip()]
    return Result("settings", PASS if code == 0 else FAIL, tail[-1] if tail else "no output")


def check_seat() -> Result:
    """This box's setup state; user application preferences are outside conformance."""
    problems = []
    doctor = HUB / "Drawer/seat-doctor.py"
    if not doctor.is_file():
        problems.append("required seat doctor is missing: Drawer/seat-doctor.py")
    else:
        code, out = _run([sys.executable, "-B", str(doctor)])
        if code != 0:
            problems.append(out.strip().splitlines()[-1] if out.strip() else "seat doctor did not run")
    return Result("seat", FAIL if problems else PASS,
                  "seat environment inspected; user application preferences are not required", problems)


# ---------------------------------------------------------------- new classes

def _settings_files() -> list[Path]:
    """Active tracked project settings only; manifests are sources, not registrations."""
    return [path for path in (HUB / ".claude/settings.json",) if path.is_file()]


HOOK_PATH = re.compile(
    r"\$CLAUDE_PROJECT_DIR((?:[\\/]\.\.)*[\\/](?:Drawer|\.claude)[^\"'\s]*?\.(?:py|mjs|sh))"
)


def _hook_commands(settings_file: Path):
    try:
        settings = json.loads(settings_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, str(exc)
    commands = []
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return commands, None
    for event_entries in hooks.values():
        if not isinstance(event_entries, list):
            continue
        for entry in event_entries:
            for hook in entry.get("hooks", []) if isinstance(entry, dict) else []:
                if isinstance(hook, dict) and hook.get("type") == "command":
                    if isinstance(hook.get("command"), str):
                        commands.append(hook["command"])
    return commands, None


def _hook_candidates(settings_file: Path, commands):
    project = settings_file.parent.parent
    for command in commands:
        for suffix in HOOK_PATH.findall(command):
            yield Path(os.path.normpath(str(project) + suffix.replace("/", os.sep)))


def check_hooks() -> Result:
    """Every hook command naming a hub-relative script must resolve on disk."""
    dead, seen = [], set()
    malformed = []
    for sf in _settings_files():
        commands, error = _hook_commands(sf)
        if error:
            malformed.append(f"{sf.relative_to(HUB)}: {error}")
            continue
        for candidate in _hook_candidates(sf, commands):
            key = (str(sf), str(candidate))
            if key in seen:
                continue
            seen.add(key)
            if not candidate.is_file():
                dead.append(f"{sf.relative_to(HUB)} -> {candidate}")
    if malformed:
        return Result("hooks", FAIL, f"{len(malformed)} active settings file(s) malformed", malformed)
    if dead:
        return Result("hooks", FAIL, f"{len(dead)} hook path(s) do not resolve", dead)
    return Result("hooks", PASS, f"{len(seen)} hook path reference(s) resolve")


DOC_GLOBS = [
    "AGENTS.md",
    "PRODUCT-CONVENTIONS.md",
    ".claude/skills/*/SKILL.md", ".claude/agents/*.md", ".claude/output-styles/*.md",
]
FOLDER_ALT = "|".join(re.escape(folder) for folder in FOLDERS)
PATH_REF = re.compile(r"`([^`\n]+)`")
HUBBISH = re.compile(rf"^(Drawer|\.claude|{FOLDER_ALT})[\\/]")
LOOKS_LIKE_FILE = re.compile(r"\.[A-Za-z0-9]{1,6}$")
# A template, not a pointer: a glob, an angle-bracket slot, or a date/time placeholder.
PLACEHOLDER = re.compile(r"[*?<>${}]|YYYY|MM-DD|HHmmss|\bstamp\b|\bslug\b|\bdate\b", re.I)
# Declared outputs and per-seat gitignored files may be absent
# before setup, a first run, or in a fresh task worktree. These are runtime contracts, not
# source files that must ship in Git.
RUNTIME_POINTER = re.compile(
    r"^((?:Flight/)?SCRATCHPAD\.md|Flight/log\.jsonl|Flight/compactions/)$",
    re.I,
)


def _glob(pattern: str) -> list[Path]:
    return sorted(HUB.glob(pattern))


def _docs() -> list[Path]:
    out = []
    for g in DOC_GLOBS:
        out.extend(_glob(g))
    return [p for p in out if p.is_file()]


def check_pointers(staged=None) -> Result:
    """Backticked hub-relative paths inside standing documents must exist.

    staged: in --commit, only the staged documents are judged. A bare run also fails on a
    DOC_GLOBS entry that matches nothing, so a retired document cannot pass vacuously."""
    dead = []
    docs = _docs()
    if staged is not None:
        docs = [doc for doc in docs if doc.relative_to(HUB).as_posix() in set(staged)]
    else:
        empty = [g for g in DOC_GLOBS if not _glob(g)]
        if empty:
            return Result("pointers", WARN, f"{len(empty)} DOC_GLOBS pattern(s) match no document", empty)
    for doc in docs:
        rel_doc = str(doc.relative_to(HUB)).replace("\\", "/")
        for n, line in enumerate(doc.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            dead += [f"{rel_doc}:{n} -> {ref}" for ref in _dead_refs(line, HUB)]
    if dead:
        return Result("pointers", WARN, f"{len(dead)} dead path reference(s)", dead)
    return Result("pointers", PASS, f"every backticked hub path in {len(docs)} document(s) resolves")


def _dead_refs(line: str, root: Path) -> list[str]:
    """Each backticked hub path on line that names no file under root."""
    dead = []
    for ref in PATH_REF.findall(line):
        ref = ref.strip().rstrip(".,;:)")
        if " " in ref or PLACEHOLDER.search(ref):
            continue
        if RUNTIME_POINTER.match(ref.replace("\\", "/")):
            continue
        if not HUBBISH.match(ref) or (root / ref.replace("\\", "/")).exists():
            continue
        if not LOOKS_LIKE_FILE.search(ref) and not ref.endswith("/"):
            continue
        dead.append(ref)
    return dead


# cites (the pre-pivot sweep): a standing line must not cite a superseded mem card as current.
# The tracked documents are judged in every run; the live Flight pad's State section (untracked,
# in the main checkout) only in a bare run, since state outside the commit must not hold an
# unrelated commit. Those live lines also fail on a backticked hub path that names no file: a
# removed file cited as current.
CITE_GLOBS = DOC_GLOBS + [".claude/rules/*.md"]
MEM_ID = re.compile(r"(?<![0-9A-Za-z])([0-9a-f]{8})(?![0-9A-Za-z])")
# A line that names the change is history, not a current citation.
HISTORY_WORDS = re.compile(r"supersed|retire|replace", re.I)
STATE_HEADING = "## State"


def _mem_db():
    """The local mem database path, or None when mem has no database yet."""
    try:
        cfg = harness_config.load(harness_config.main_checkout(HUB)).get("mem", {})
    except (OSError, ValueError):  # a hub with no readable harness.toml has no mem
        return None
    db = Path(os.path.expanduser(cfg.get("db", "")))
    return db if cfg.get("db") and db.is_file() else None


def _superseded(db: Path) -> dict:
    """uuid8 -> uuid8 of its successor, for each superseded card whose uuid8 no live card shares."""
    import sqlite3
    con = sqlite3.connect(Path(db).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT substr(id,1,8), substr(superseded_by,1,8) FROM cards").fetchall()
    finally:
        con.close()
    live = {old for old, new in rows if not new}
    return {old: new for old, new in rows if new and old not in live}


def _state_lines(pad: Path) -> list[tuple[int, str]]:
    """(line number, text) of the pad's State section: its heading up to the next level-2 heading."""
    out, inside = [], False
    for n, line in enumerate(pad.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if line.startswith("## "):
            inside = line.strip() == STATE_HEADING
            continue
        if inside:
            out.append((n, line))
    return out


def _live_sources(root: Path) -> list[tuple[str, list[tuple[int, str]]]]:
    """The untracked standing lines: the Flight pad's State section."""
    sources = []
    pad = root / "Flight" / "SCRATCHPAD.md"
    if pad.is_file():
        sources.append(("Flight/SCRATCHPAD.md", _state_lines(pad)))
    return sources


def _stale_ids(line: str, superseded: dict) -> list[str]:
    if HISTORY_WORDS.search(line):
        return []
    return [f"{old} (superseded by {superseded[old]})" for old in MEM_ID.findall(line)
            if old in superseded and superseded[old] not in line]


def check_cites(staged=None) -> Result:
    """No standing line cites a superseded mem card, or (live lines) a removed hub file, as current.
    staged: in --commit, only the staged tracked documents, and no live lines."""
    db = _mem_db()
    if db is None:
        return Result("cites", PASS, "no local mem database; cites are not judged")
    try:
        superseded = _superseded(db)
    except Exception as exc:
        return Result("cites", UNAVAILABLE, "the mem database could not be read", [f"{type(exc).__name__}: {exc}"])
    docs = sorted({p for g in CITE_GLOBS for p in _glob(g) if p.is_file()})
    if staged is not None:
        docs = [doc for doc in docs if doc.relative_to(HUB).as_posix() in set(staged)]
    stale = []
    for doc in docs:
        rel = doc.relative_to(HUB).as_posix()
        for n, line in enumerate(doc.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            stale += [f"{rel}:{n} cites {cite}" for cite in _stale_ids(line, superseded)]
    live = [] if staged is not None else _live_sources(harness_config.main_checkout(HUB))
    root = harness_config.main_checkout(HUB)
    for rel, lines in live:
        for n, line in lines:
            stale += [f"{rel}:{n} cites {cite}" for cite in _stale_ids(line, superseded)]
            stale += [f"{rel}:{n} cites removed file {ref}" for ref in _dead_refs(line, root)]
    scope = f"{len(docs)} document(s)" + ("" if staged is not None else f" and {len(live)} live source(s)")
    if stale:
        return Result("cites", FAIL, f"{len(stale)} stale citation(s) in {scope}", stale)
    return Result("cites", PASS, f"no superseded card or removed file cited in {scope}")


BLOB_MAX_BYTES = 1024 * 1024
IMAGE_SUFFIX = re.compile(r"\.(png|jpe?g|gif|webp|bmp|tga|tiff?|exr|psd)$", re.I)
# Where evidence lands: a hub folder's reports/ or the Drawer. Product images elsewhere
# are source, not evidence. Art and build binaries live in the media share, not in Git.
EVIDENCE_HOME = re.compile(r"^([^/]+/reports/|Drawer/)", re.I)


def _index_sizes(paths=None) -> dict:
    """Byte size of each index blob under paths (the whole index when paths is None).
    A staged deletion has no index entry, so it is never judged. The whole index is listed and
    filtered here: staged paths on one command line can pass the OS argument limit."""
    listing = subprocess.run(["git", "ls-files", "-s", "-z"],
                             cwd=HUB, capture_output=True, text=True)
    wanted = None if paths is None else set(paths)
    blobs = {}
    for record in listing.stdout.split("\0"):
        if record and "\t" in record:
            meta, path = record.split("\t", 1)
            if wanted is None or path in wanted:
                blobs[path] = meta.split()[1]
    if not blobs:
        return {}
    probe = subprocess.run(["git", "cat-file", "--batch-check=%(objectsize)"], cwd=HUB,
                           input=("\n".join(blobs.values()) + "\n").encode(), capture_output=True)  # bytes: no CRLF
    sizes = probe.stdout.decode("utf-8", "replace").splitlines()
    return {path: int(size) for path, size in zip(blobs, sizes) if size.strip().isdigit()}


def check_blobs(staged=None) -> Result:
    """No blob over 1 MB, and no image under an evidence home."""
    refused = []
    for path, size in _index_sizes(staged).items():
        if size > BLOB_MAX_BYTES:
            refused.append(f"{path} ({size // 1024} KB, over 1 MB)")
        elif IMAGE_SUFFIX.search(path) and EVIDENCE_HOME.match(path):
            refused.append(f"{path} (image under an evidence home)")
    if refused:
        return Result("blobs", WARN, f"{len(refused)} blob(s) over a limit", refused)
    return Result("blobs", PASS, "blob limits pass")


# Tracked lock files the seat pin stamp leaves out, each with why (operator pick); the
# stamped ones are LOCKS in Drawer/runtime/seat.py.
UNSTAMPED_LOCKS: dict[str, str] = {}
LOCK_NAMES = {"uv.lock", "requirements.lock", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
              "poetry.lock", "Pipfile.lock"}
UV_TOOL_PIN = "uv tool install "


def _stamp_rows() -> tuple[set, set]:
    """The seat pin stamp's own rows: its uv tool pins and its hub-relative lock paths.
    The base stamps no uv tool, so a seat without uv_tools has no pin rows."""
    drawer = str(HUB / "Drawer")
    if drawer not in sys.path:
        sys.path.insert(0, drawer)
    from runtime import seat
    tools = seat.uv_tools(HUB) if hasattr(seat, "uv_tools") else {}
    return {row[0] for row in tools.values()}, {row[0] for row in seat.LOCKS.values()}


def _git_paths(*args) -> list[str]:
    done = subprocess.run(["git", args[0], "-z", *args[1:]], cwd=HUB, capture_output=True, text=True,
                          encoding="utf-8")
    if done.returncode > 1:  # git grep answers 1 for no match
        raise OSError(done.stderr.strip() or f"git {args[0]} exit {done.returncode}")
    return [path for path in done.stdout.split(chr(0)) if path]


def check_pins() -> Result:
    """Every per-box install reaches the seat pin stamp or says why not (an operator
    ruling): a `uv tool install` pin in hub Python is a row of seat.uv_tools (the base has
    none), and a tracked
    lock file is a row of seat.LOCKS or of UNSTAMPED_LOCKS. Else its bump reaches no seat."""
    try:
        pins, locks = _stamp_rows()
        tracked = _git_paths("ls-files")
        # Python string constants only; a uv tool install in a shell script passes unseen.
        sources = [path for path in _git_paths("grep", "-l", "-F", UV_TOOL_PIN, "--", "*.py")
                   if not Path(path).name.startswith("test_") and "/tests/" not in f"/{path}"]
    except Exception as exc:
        return Result("pins", FAIL, "the seat pin stamp or the Git index could not be read",
                      [f"{type(exc).__name__}: {exc}"])
    findings = [f"{path}: a lock file outside the seat pin stamp; add it to LOCKS in Drawer/runtime/seat.py "
                "or to UNSTAMPED_LOCKS in Drawer/harness-conform.py with the reason"
                for path in tracked
                if Path(path).name in LOCK_NAMES and path not in locks and path not in UNSTAMPED_LOCKS]
    findings += [f"{path}: UNSTAMPED_LOCKS names a lock Git does not track"
                 for path in UNSTAMPED_LOCKS if path not in tracked]
    for path in sources:
        try:
            tree = ast.parse((HUB / path).read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError) as exc:
            findings.append(f"{path}: unreadable ({type(exc).__name__})")
            continue
        # A pin is the prefix plus a spec; the bare prefix (UV_TOOL_PIN above) names no tool.
        findings += [f"{path}:{node.lineno}: {node.value!r} is not a uv tool pin of the seat stamp (Drawer/runtime/seat.py)"
                     for node in ast.walk(tree)
                     if isinstance(node, ast.Constant) and isinstance(node.value, str)
                     and node.value.startswith(UV_TOOL_PIN) and node.value.strip() != UV_TOOL_PIN.strip()
                     and node.value not in pins]
    if findings:
        return Result("pins", WARN, f"{len(findings)} per-box install(s) outside the seat pin stamp", findings)
    return Result("pins", PASS, f"{len(pins)} uv tool pin(s) and {len(locks)} lock(s) stamped; "
                                f"{len(UNSTAMPED_LOCKS)} lock(s) exempt with a reason")


SKILL_REF = re.compile(r"(?<![\w/-])/([a-z][a-z0-9-]{2,})(?![\w/-])")


def check_skills() -> Result:
    """A slash-command name named in a standing document needs a SKILL.md."""
    have = {p.parent.name for p in HUB.glob(".claude/skills/*/SKILL.md")}
    builtin = {"security-review", "init", "run", "loop", "schedule",
               "simplify", "clear", "compact", "config", "help", "fast", "dataviz",
               "update-config", "claude-api",
               "workflow-authoring", "keybindings-help",
               "fewer-permission-prompts", "skill-doctor", "install-slack-app", "doctor"}
    dead = []
    for doc in _docs():
        rel_doc = str(doc.relative_to(HUB)).replace("\\", "/")
        for n, line in enumerate(doc.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            for name in SKILL_REF.findall(line):
                if name in have or name in builtin:
                    continue
                if f"/{name}" in line and ("skill" in line.lower() or line.strip().startswith("- ")):
                    dead.append(f"{rel_doc}:{n} -> /{name}")
    if dead:
        return Result("skills", WARN, f"{len(dead)} skill name(s) with no SKILL.md", sorted(set(dead)))
    return Result("skills", PASS, f"{len(have)} skill(s) on disk; no dangling skill name")


AGENT_REF = re.compile(r"subagent_type[\"'\s:=]+[\"']?([a-z][a-z0-9-]+)")


def check_agents() -> Result:
    have = {p.stem for p in HUB.glob(".claude/agents/*.md")}
    builtin = {"general-purpose", "fork", "claude", "explore", "plan", "statusline-setup",
               "claude-code-guide", "code-reviewer"}
    dead = []
    for doc in _docs():
        rel_doc = str(doc.relative_to(HUB)).replace("\\", "/")
        for n, line in enumerate(doc.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            for name in AGENT_REF.findall(line):
                if name.lower() not in have and name.lower() not in builtin:
                    dead.append(f"{rel_doc}:{n} -> subagent_type={name}")
    if dead:
        return Result("agents", WARN, f"{len(dead)} agent type(s) with no agent file", sorted(set(dead)))
    return Result("agents", PASS, f"{len(have)} agent file(s); no dangling subagent_type")


HIDE_SEGMENT = re.compile(
    r"(^|[\\/])(attic|archives?|_archive|backups?|_backup|rejected|old|copy|copies)([\\/]|$)", re.I)
HIDE_NAME = re.compile(
    r"[-_.](bak|orig|old|backup|copy|rejected)\d*([-_.]\d{2,})*(\.[A-Za-z0-9]+)?$", re.I)
# Exempt from the FOLDER rule only. A backup FILE NAME is never exempt: the hub keeps
# no second version of a file, wherever the lane writes it.
HIDE_SEGMENT_EXEMPT = []
HIDE_EXEMPT = [
    re.compile(r"[\\/](node_modules|__pycache__|\.git|\.pytest_cache|\.stfolder|\.stversions)[\\/]", re.I),
    re.compile(r"^Drawer[\\/](doc-gate|test_doc_gate|harness-conform)\.py$", re.I),
    re.compile(r"^Drawer[\\/]never-copy\.txt$", re.I),  # the copy exclude list, not a copy
]
SKIP_DIRS = {"node_modules", "__pycache__", ".git", ".pytest_cache", ".stfolder",
             ".stversions", ".venv", "dumps"}


def _walk_hub():
    stack = [HUB]
    while stack:
        d = stack.pop()
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for e in entries:
            if e.is_dir():
                if e.name not in SKIP_DIRS:
                    stack.append(e)
            else:
                yield e


def check_hiding(staged=None) -> Result:
    """No hiding place on disk: the hub keeps no backups and no archives.

    staged: in --commit, only the staged paths are judged, so an untracked stray in
    a gitignored tree cannot block every commit."""
    hits = []
    files = _walk_hub() if staged is None else [HUB / path for path in staged if (HUB / path).is_file()]
    for f in files:
        rel = str(f.relative_to(HUB)).replace("/", "\\")
        if any(x.search(rel) for x in HIDE_EXEMPT):
            continue
        if HIDE_NAME.search(f.name):
            hits.append(rel.replace("\\", "/"))
        elif HIDE_SEGMENT.search(rel) and not any(x.search(rel) for x in HIDE_SEGMENT_EXEMPT):
            hits.append(rel.replace("\\", "/"))
    if hits:
        return Result("hiding", FAIL, f"{len(hits)} hiding-place path(s) on disk", sorted(hits))
    return Result("hiding", PASS, "no backup, archive, or copy path in the hub tree")


CONFIG_PATH_KEY = re.compile(r"(^|_)(root|dir|path|file|hub|db)$")
CONFIG_MIN_BODY = 8  # a shorter path body would match too many unrelated strings


def _config_values(cfg: dict) -> tuple[set, set, set]:
    """(models, ports, paths): the values of harness.toml a Drawer file must read, not repeat."""
    models, ports, paths = set(), set(), set()
    for key, value in harness_config.flat(cfg):
        leaf = key.rsplit(".", 1)[-1]
        if isinstance(value, str) and value:
            if leaf == "model" or leaf.endswith("_model"):
                models.add(value)
            if value.startswith(("~/", "/")) or CONFIG_PATH_KEY.search(leaf):
                paths.add(value)
            if "://" in value:
                try:
                    port = urlparse(value).port
                except ValueError:
                    port = None
                if port:
                    ports.add(port)
        elif isinstance(value, int) and not isinstance(value, bool) and (leaf == "port" or leaf.endswith("_port")):
            ports.add(value)
        elif leaf == "allowed_models" and isinstance(value, list):
            models.update(item for item in value if isinstance(item, str) and item)
    return models, ports, paths


def _docstrings(tree) -> set:
    """The ids of the docstring nodes of a module, a class and each function."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                found.add(id(first.value))
    return found


def _port_names(tree) -> dict:
    """The id of each int constant assigned to a name, or passed as a keyword, that holds 'port'."""
    named = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        elif isinstance(node, ast.keyword) and node.arg:
            names = [node.arg]
        else:
            continue
        if node.value is not None and any("port" in name.lower() for name in names):
            named[id(node.value)] = True
    return named


def check_config(staged=None) -> Result:
    """WRK-2: no model id, port or hub path that harness.toml names is a literal in Drawer code.

    Test files hold fixtures and docstrings name values, so neither is judged; models match whole.
    staged: in --commit, only the staged Drawer .py files, or every one when harness.toml is staged."""
    if not (HUB / harness_config.CONFIG).is_file():
        return Result("config", PASS, "no harness.toml")
    cfg = harness_config.load(HUB)
    cfg.pop("derived", None)
    models, ports, paths = _config_values(cfg)
    port_res = [(port, re.compile(rf":{port}(?!\d)")) for port in sorted(ports)]
    bodies = [(path, path[2:] if path.startswith("~/") else path) for path in sorted(paths)]
    files = [f for f in sorted((HUB / "Drawer").rglob("*.py"))
             if not f.name.startswith("test_") and not SKIP_DIRS.intersection(f.relative_to(HUB).parts)]
    if staged is not None and harness_config.CONFIG not in staged:
        files = [f for f in files if f.relative_to(HUB).as_posix() in set(staged)]
    findings = set()
    for f in files:
        rel = f.relative_to(HUB).as_posix()
        try:
            tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        docs, port_named = _docstrings(tree), _port_names(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or id(node) in docs:
                continue
            value = node.value
            if isinstance(value, str):
                if value in models:
                    findings.add((rel, node.lineno, f"model {value!r}"))
                findings.update((rel, node.lineno, f"port {port}") for port, rx in port_res if rx.search(value))
                findings.update((rel, node.lineno, f"path {path!r}") for path, body in bodies
                                if value == path or (len(body) >= CONFIG_MIN_BODY and body in value))
            elif isinstance(value, int) and not isinstance(value, bool) and value in ports \
                    and id(node) in port_named:
                findings.add((rel, node.lineno, f"port {value}"))
    if findings:
        return Result("config", WARN, f"{len(findings)} hard-coded harness.toml value(s) in Drawer code",
                      [f"{rel}:{line} {what}" for rel, line, what in sorted(findings)])
    return Result("config", PASS, f"{len(files)} Drawer file(s); no hard-coded model, port or hub path")


def check_mcp_live(staged=None) -> Result:
    """The real Claude client serves every expected MCP server with tools.
    In --commit it runs only when a staged path changes an MCP launch (mcp-live-check.gate)."""
    probe = HUB / "Drawer" / "mcp-live-check.py"
    if not probe.is_file():
        return Result("mcp-live", FAIL, "required live MCP gate is missing", ["Drawer/mcp-live-check.py"])
    live = _load(probe, "conform_mcp_live_check")
    if staged is not None:
        state, summary, findings = live.gate(staged)
        return Result("mcp-live", state, summary, findings)
    report = live.run()
    findings = [f for probe in report["probes"] for f in probe["findings"]]
    return Result("mcp-live", report["state"],
                  f"{len(report['probes'])} live client probes in {report['seconds']} s", findings)


CHECKS = {
    "settings": check_settings,
    "seat": check_seat,
    "hooks": check_hooks,
    "pointers": check_pointers,
    "skills": check_skills,
    "agents": check_agents,
    "hiding": check_hiding,
    "blobs": check_blobs,
    "pins": check_pins,
    "cites": check_cites,
    "config": check_config,
    "mcp-live": check_mcp_live,
}


GENERATORS = [
    ["Drawer/settings-sync.py", "--apply"],
]
# Offline, fast: what a commit may be held on.
OFFLINE = ["settings", "skills", "hooks", "pointers", "agents", "hiding", "blobs", "pins", "cites", "config"]


def apply_generators() -> int:
    """Regenerate every generated file from its source. Exit 1 on the first generator failure."""
    for name, *flags in GENERATORS:
        generator = HUB / name
        if not generator.is_file():
            print(f"ERROR: required generator is missing: {name}")
            return 1
        code, out = _run([sys.executable, "-B", str(generator), *flags])
        tail = [line for line in out.strip().splitlines() if line.strip()]
        print(f"[{'ok' if code == 0 else 'ERR':4}] {Path(name).name:22} {tail[-1] if tail else 'no output'}")
        if code != 0:
            for line in tail[:-1][-20:]:
                print(f"         {line}")
            return 1
    return 0


def _git_policy():
    return _load(HUB / "Drawer" / "git-branch-policy.py", "conform_git_branch_policy")


VENV = ".venv"  # untracked; the exported tree links it
TREE_TIMEOUT = 600  # the exported tree's offline checks


def _export_tree(dest: Path) -> None:
    """Write the index the commit delivers to dest as a working tree. A .git file names
    this repository, so Git in dest sees its history; the interpreter is linked, not copied."""
    subprocess.run(["git", "checkout-index", "--all", "--ignore-skip-worktree-bits",
                    f"--prefix={dest.as_posix()}/"], cwd=HUB, check=True, capture_output=True, text=True)
    git_dir = subprocess.run(["git", "rev-parse", "--absolute-git-dir"], cwd=HUB, check=True,
                             capture_output=True, text=True).stdout.strip()
    (dest / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8", newline="\n")
    venv = (HUB / VENV).resolve()  # a linked worktree's own .venv is itself a link
    if venv.is_dir():
        if oscompat.WINDOWS:  # a symlink needs Developer Mode or an elevated shell; a junction needs neither
            subprocess.run(["cmd", "/c", "mklink", "/J", str(dest / VENV), str(venv)], check=True,
                           capture_output=True, text=True)
        else:
            (dest / VENV).symlink_to(venv, target_is_directory=True)


def tree_checks(staged) -> list[Result]:
    """The checks a commit is held on, run by the exported tree's own copy of this file."""
    # A staged path absent on disk is a deletion (or a rename source): judge every standing
    # document then, because the dangling pointer may live in an unstaged one.
    wide = any(not (HUB / path).exists() for path in staged)
    checks = {
        "settings": lambda: check_settings(tracked=True, fix=True),
        "pointers": lambda: check_pointers(staged=None if wide else staged),
        "hiding": lambda: check_hiding(staged=staged),
        "blobs": lambda: check_blobs(staged=staged),
        "cites": lambda: check_cites(staged=staged),
        "config": lambda: check_config(staged=staged),
    }
    return [checks.get(name, CHECKS[name])() for name in OFFLINE]


def _git_index(index, *args: str) -> str:
    """git in HUB; index: another index file to act on (None: the one this process sees)."""
    env = dict(os.environ, GIT_INDEX_FILE=str(index)) if index else None
    return subprocess.run(["git", *args], cwd=HUB, env=env, check=True, capture_output=True,
                          text=True).stdout.strip()


def _stage_render(tree: Path, paths: list[str]) -> list[str]:
    """Stage each file the commit's tree rendered into the index this commit builds. The working
    tree takes the render when its copy is the old indexed one. A git commit -- <paths> builds a
    temporary index; the real index Git writes back after it (index.lock) takes the render too, or
    the next commit would revert it. Returns one note per step that did not run."""
    index = os.environ.get("GIT_INDEX_FILE")
    others = []
    if index and Path(index).name.startswith("next-index-"):  # git commit -- <paths>; else a lock is a peer's
        real = Path(_git_out("rev-parse", "--absolute-git-dir")) / "index.lock"
        if real.is_file():
            others.append(real)
    notes = []
    for rel in paths:
        new = _git_out("hash-object", "-w", f"--path={rel}", str(tree / rel))
        entry = _git_index(None, "ls-files", "-s", "--", rel).split()
        mode, old = (entry[0], entry[1]) if entry else ("100644", "")
        _git_index(None, "update-index", "--add", "--cacheinfo", f"{mode},{new},{rel}")
        for other in others:
            try:
                _git_index(other, "update-index", "--add", "--cacheinfo", f"{mode},{new},{rel}")
            except subprocess.CalledProcessError as exc:
                notes.append(f"{rel}: the real index keeps the old render ({(exc.stderr or '').strip()[:120]}); git add it")
        work = HUB / rel
        have = _git_out("hash-object", f"--path={rel}", str(work)) if work.is_file() else old
        if have == old:
            work.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(tree / rel, work)
        elif have != new:
            notes.append(f"{rel}: the working tree copy is someone's edit; left as is")
    return notes


def _judge_the_tree(staged, stage: bool = True) -> list[Result]:
    """tree_checks on the exported index, fail closed; HUB stays this checkout. stage: a settings
    drift the tree rendered is staged into this commit (_stage_render); a precheck stages nothing."""
    root = Path(tempfile.mkdtemp(prefix="conform-commit-"))
    try:
        try:
            _export_tree(root / "tree")
        except (OSError, subprocess.CalledProcessError) as exc:
            detail = getattr(exc, "stderr", "") or str(exc)
            return [Result("tree", FAIL, "the commit's tree could not be exported", [detail.strip()[:300]])]
        (root / "staged.json").write_text(json.dumps(staged), encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        if os.environ.get("GIT_INDEX_FILE"):  # a hook's index may be relative to this checkout
            env["GIT_INDEX_FILE"] = str(HUB / os.environ["GIT_INDEX_FILE"])
        code, out = _run([sys.executable, "-B", str(root / "tree/Drawer/harness-conform.py"),
                          "--commit-tree", str(root)], timeout=TREE_TIMEOUT, env=env)
        try:
            rows = json.loads((root / "results.json").read_text(encoding="utf-8"))
            results = [Result(row["name"], row["state"], row["summary"], row["findings"], row.get("fixed"))
                       for row in rows]
        except (OSError, ValueError, KeyError, TypeError):
            tail = [line for line in out.strip().splitlines() if line.strip()]
            return [Result("tree", FAIL, f"the commit's tree was not judged (exit {code})", tail[-20:])]
        for r in results:
            if r.fixed and stage:
                try:
                    notes = _stage_render(root / "tree", r.fixed)
                except (OSError, subprocess.CalledProcessError) as exc:
                    detail = getattr(exc, "stderr", "") or str(exc)
                    r.state, r.summary = FAIL, "the settings render could not be staged"
                    notes = [f"{detail.strip()[:300]}"]
                r.findings += notes
                if notes and r.state == PASS:
                    r.state = WARN  # a render step that did not run must show
        return results
    finally:
        shutil.rmtree(root, ignore_errors=True)  # removes the interpreter link, never follows it


PASS_FILE = "harness-conform-pass"  # in the Git dir: tree hashes a precheck judged clean
PASS_KEEP = 50


def _git_out(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=HUB, check=True, capture_output=True, text=True).stdout.strip()


def _pass_file() -> Path:
    return Path(_git_out("rev-parse", "--absolute-git-dir")) / PASS_FILE


def _prechecked(tree: str) -> bool:
    try:
        return tree in _pass_file().read_text(encoding="utf-8").split()
    except OSError:
        return False


def _record_pass(tree: str) -> None:
    path = _pass_file()
    try:
        trees = path.read_text(encoding="utf-8").split()
    except OSError:
        trees = []
    trees = [t for t in trees if t != tree][-(PASS_KEEP - 1):] + [tree]
    path.write_text("\n".join(trees) + "\n", encoding="utf-8")


def precheck(paths: list[str]) -> int:
    """commit_gate on HEAD plus paths, in a private index: the tree git commit -- <paths> delivers."""
    fd, index = tempfile.mkstemp(prefix="conform-precheck-")
    os.close(fd)
    os.remove(index)  # git read-tree writes a fresh index; an empty file is not one
    os.environ["GIT_INDEX_FILE"] = index
    try:
        try:
            _git_out("read-tree", "HEAD")
            _git_out("add", "-A", "--", *paths)
        except subprocess.CalledProcessError as exc:
            print(f"harness-conform: cannot build the precheck index ({(exc.stderr or '').strip()[:300]})")
            return 1
        return commit_gate(precheck=True)
    finally:
        os.environ.pop("GIT_INDEX_FILE", None)
        try:
            os.remove(index)
        except OSError:
            pass


def gate_drawer() -> str:
    """The commit gate's home: the main checkout's Drawer. A task worktree logs as the box, so
    worktree-remove never deletes a log line."""
    return str(harness_config.main_checkout(HUB) / "Drawer")


def _causes(results) -> str:
    """One log line's causes: each failed check, its summary and its first named cases."""
    def flat(text):
        return " ".join(str(text).split())
    causes = []
    for r in results:
        if r.state == PASS:
            continue
        cases = [flat(f) for f in r.findings[:5]] + ([f"+{len(r.findings) - 5} more"] if len(r.findings) > 5 else [])
        causes.append(f"{r.name} {r.state} {flat(r.summary)}" + (f" [{'; '.join(cases)}]" if cases else ""))
    return " | ".join(causes)


def commit_gate(precheck: bool = False) -> int:
    """The pre-commit subset. It regenerates only a settings drift, from the commit's own tree, and
    stages that render (_stage_render); a precheck stages nothing."""
    mode = os.environ.get("HARNESS_CONFORM", "").lower()  # off: skip; dry: judge, log nothing
    if mode == "off":
        return 0
    policy = _git_policy()
    try:
        if policy._rebase_branch(HUB):
            print("harness-conform: rebase in progress; the commit subset is skipped")
            return 0
        staged = policy._staged_paths(HUB)
    except policy.PolicyError as exc:
        print(f"harness-conform: cannot read the Git state ({exc}); fail closed")
        return 1
    try:
        tree_hash = _git_out("write-tree")
    except subprocess.CalledProcessError:
        tree_hash = ""
    if tree_hash and _prechecked(tree_hash):
        print(f"harness-conform: tree {tree_hash[:12]} was prechecked clean; the tree checks are skipped")
        tree = []
    else:
        tree = _judge_the_tree(staged, stage=not precheck and mode != "dry")
    # a staged render can change an MCP launch key: the live check judges it with the staged paths
    rendered = [path for r in tree for path in r.fixed if path not in staged]
    results = tree + ([] if precheck else [check_mcp_live(staged=staged + rendered)])
    if tree and tree_hash and all(r.state in (PASS, WARN) and not r.fixed for r in tree):
        _record_pass(tree_hash)  # the live MCP check runs only in the hook
    if report(results, verbose=False) == 0:
        return 0
    if precheck:
        print("harness-conform: precheck failed; each failed check above names its fix")
        return 1
    if mode != "dry":
        import gatelib
        gatelib.append(os.path.join(gate_drawer(), COMMIT_LOG), "deny %d staged path(s) tree %s: %s"
                       % (len(staged), tree_hash[:12] or "unknown", _causes(results)))
    print("harness-conform: commit held; each failed check above names its fix")
    return 1


RECHECK = f"{PY} Drawer/harness-conform.py --commit"
COMMIT_LOG = "commit-gate.log"  # in gate_drawer(): one line per held commit
# The fix a failed check prints. pins and mcp-live name theirs in their own lines.
FIXES = {
    "settings": f"{FIX_LINE}, then git add the regenerated files",
    "skills": "add the named skill's SKILL.md, or correct the name in the document",
    "agents": "add the named .claude/agents file, or correct the subagent_type",
    "hooks": "restore the named script, or correct the hook command",
    "pointers": "correct or remove each named dead path",
    "hiding": "delete or rename each named hiding-place path",
    "blobs": "unstage each named blob; art and build binaries belong in the media share",
    "cites": "cite the successor card (mem get <id>) or drop the line; name a removed file no more",
    "config": "read the value from harness.toml (harness_config.load) in place of the literal (WRK-2)",
    "tree": f"repair the error named above, then recheck: {RECHECK}",
}


def report(results, verbose) -> int:
    for r in results:
        print(f"[{r.state:4}] {r.name:18} {r.summary}")
        if r.findings and (verbose or r.state != PASS):
            for f in r.findings[:60]:
                print(f"         {f}")
            if len(r.findings) > 60:
                print(f"         ... {len(r.findings) - 60} more")
        if r.state in (FAIL, WARN) and r.name in FIXES:
            print(f"         fix: {FIXES[r.name]}")

    failed = [r.name for r in results if r.state == FAIL]
    unavailable = [r.name for r in results if r.state == UNAVAILABLE]
    print()
    if failed:
        print(f"RESULT: FAIL - {len(failed)} of {len(results)} checks failed: {', '.join(failed)}")
        return 1
    if unavailable:
        print(f"RESULT: INCOMPLETE - {len(results) - len(unavailable)} checks clean, "
              f"{len(unavailable)} unavailable: {', '.join(unavailable)}")
        return 2
    warned = [r.name for r in results if r.state == WARN]
    if warned:
        print(f"RESULT: PASS - {len(results)} checks, {len(warned)} warning(s): {', '.join(warned)}")
        return 0
    print(f"RESULT: PASS - all {len(results)} checks clean")
    return 0


FLAGS = ("-v", "--verbose", "--list", "--commit", "--commit-tree", "--apply", "--no-check")


def main(argv: list[str]) -> int:
    if "-h" in argv or "--help" in argv:
        print(__doc__)
        return 0
    paths = argv[argv.index("--") + 1:] if "--" in argv else []  # --commit -- <paths>: the precheck
    argv = argv[:argv.index("--")] if "--" in argv else argv
    if any(a in ("-h", "--help") for a in argv):
        print(__doc__.strip())
        return 0
    bad = [a for a in argv if a.startswith("-") and a not in FLAGS]
    if bad:
        print(f"unknown flag(s): {', '.join(bad)}; known: {', '.join(FLAGS)}", file=sys.stderr)
        return 2
    verbose = "-v" in argv or "--verbose" in argv
    argv = [a for a in argv if a not in ("-v", "--verbose")]
    if "--list" in argv:
        print("\n".join(CHECKS))
        return 0
    if "--commit" in argv:
        return precheck(paths) if paths else commit_gate()
    if "--commit-tree" in argv:  # internal: _judge_the_tree runs it inside the exported tree
        io = Path(argv[argv.index("--commit-tree") + 1])
        staged = json.loads((io / "staged.json").read_text(encoding="utf-8"))
        rows = [vars(r) for r in tree_checks(staged)]
        (io / "results.json").write_text(json.dumps(rows), encoding="utf-8")
        return 0
    if "--apply" in argv:
        code = apply_generators()
        if code != 0 or "--no-check" in argv:
            return code
        print()
        return report([CHECKS[n]() for n in OFFLINE], verbose)
    names = [a for a in argv if not a.startswith("-")] or list(CHECKS)
    unknown = [n for n in names if n not in CHECKS]
    if unknown:
        print(f"unknown check(s): {', '.join(unknown)}", file=sys.stderr)
        print(f"known: {', '.join(CHECKS)}", file=sys.stderr)
        return 2
    return report([CHECKS[n]() for n in names], verbose)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
