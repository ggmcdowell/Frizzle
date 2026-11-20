"""mem: the hub's memory. One local SQLite file (harness.toml [mem]), WAL.

Rows are typed cards: design, decision, taste, lesson, fact, task. A body is never edited: a change
is a new row that supersedes the old one, in one transaction. Search fuses FTS5 (porter) and a flat
sqlite-vec KNN over OpenAI embeddings by reciprocal rank; with no key or no network it runs on
keywords alone.
Run it as `.venv/bin/python -B Drawer/mem/mem.py <command>` from the hub root (`mem` below).

    mem search "<query>" [-k 10] [--kind K] [--all]
    mem get <id> [--follow]
    mem add --kind K --body "<text>" [--title T] [--topic T ...] [--source S] [--supersedes ID] [--new]
            [--priority p0-p4] [--status S] [--depends-on ID ...] [--tag T ...] [--pinned] [--closes ID]
            [--model M] [--effort E] [--area A]
    mem list [--kind K] [--status S] [--topic T] [--tag T] [--limit N] [--all] | mem list --ready [--tag T]
    mem status <id> <todo|doing|parked|done|dropped> [--expect S] [--reason R]
    mem claim <id> [--session S] [--model M] [--effort E] [--worktree W]
    mem release <id> --claim-id C [--reason R]
    mem complete <id> --claim-id C --evidence E (--commit REPO=SHA ... [--path P ...] | --no-file-changes WHY)
                 [--lesson L]
    mem interrupt <id> [--reason R] | mem reap [--dry-run]
    mem note <id> "<text>" [--stop] [--session S] | mem notes <id> [--since N] | mem log <id>
    mem digest | mem export [path] | mem snapshot | mem nightly | mem import <path> | mem mcp
    mem doctor | mem reembed | mem seed [path]

Setup: doctor claims the DB for this live hub in <db>.hub (a DB another hub that still exists owns: FAIL, give this
hub its own roots in harness.local.toml), makes the DB, loads sqlite-vec, names the search mode (keyword-only with no OpenAI key) and prints
one OK or FAIL line (exit 0 or 1). seed imports Drawer/mem/seed/starter.jsonl (or path) only into an empty DB.
reembed embeds each card that has no vector (a card added with no key or no network); it needs a key.

Ready list and route fields: get --follow walks superseded_by to the live card. list --ready returns
the live tasks that are todo or parked, hold no claim and whose depends_on rows are all closed (the claim check),
each with chain (the open tasks that wait on it at any depth), ranked by chain, then priority. add takes model,
effort and area, checked against harness.toml [gates] allowed_models, [routing] rungs and [project] task_skills.

Incident: a task card with the tag incident is an incident record (what broke, impact, suspected
cause). A session opens one with Drawer/incident.py; the events watchdog (Drawer/flight-watchdog.py
events) opens one from an INCIDENT text line that names no record id. A lesson added with --closes <incident id> closes it: in one transaction the lesson takes the
tag closes:<incident id> and the incident goes to done. An incident whose text or tags name git history or a
delete (filter-branch, filter-repo, a force push, reset --hard, branch -D, update-ref -d, refs/original, a
delete, trash, rm, prune, gc) closes only when the record or the lesson holds a line
'LOST-WORK CHECK: <command or audit> -> <result>'; else the close is refused.

Claims: mem claim takes one open task for one session in a single UPDATE under BEGIN
IMMEDIATE where claim_id is null, so of two racing claims exactly one gets a claim_id; the loser exits 3. It
refuses a non-task, a closed or superseded card (naming the live id) and a task with an open depends_on. A claim
sets the task doing; mem release needs that claim_id and sets it back to todo. status refuses done while a claim
is held, a closed task reopened with no --reason, and a task whose status is not --expect; the MCP set_status
shares the guard. Each claim, release and status move writes one task_log row; a refused claim on a held task
writes one 'claim_refused' row (the asking session; the holder in detail) and still exits 3. export carries the claim
columns and each card's task_log rows (key log). An open-time migration adds the columns to an older DB once.

Liveness: a claim is live while its claim_session has a live
~/.claude/sessions row on this machine (runlib.session_state). mem interrupt parks a task whose claim is dead: the
claim clears, doing goes to parked, the worktree stays, one task_log 'interrupt' row; a live claim is refused.
A claim's task_log row names the host that took it.
mem complete is the checked close: the live claim_id, evidence of 4+ words that names a cited
sha (or --no-file-changes) and gives each critical claim (crash, save loss, blocker, data loss, gate fault) its
evidence label (WRK-4), and a task_delivery pass (each cited sha on origin/main). It sets done, evidence,
commits, delivered=1 and closed_at, clears the claim and writes one task_log 'complete' row; --lesson adds a lesson
row (topic task:<id8>) that names no commit id (a card id8 or full card uuid is a fair cite) and no prefix of a
cited sha, whatever its letters, in the same transaction. Drawer/task-lifecycle.py wraps it.
mem reap interrupts each dead claim (--dry-run lists them and changes nothing). Liveness fails closed: no session
string, no sessions folder or an unreadable sessions row is state unknown, listed by reap and never parked. mem note writes a task_log 'note'
row (--stop: a 'stop' row, the steer to halt); notes lists both with n (the task_log rowid; --since N returns
rows after n) and stop; log prints every task_log row of a task.

Tests: under a hub test run (a python -m unittest or pytest process, or a test_*.py
script, as this process or an ancestor) the default store is a scratch DB per run, never the live one, and
opening the live DB raises LiveUnderTest, so a test that reaches it fails.
Live checkout guard: mem.py outside the live hub (the main worktree of the Git checkout that holds this mem.py, when
it holds the marker file: so mem.py in a task worktree) runs the live hub's mem.py for a live-DB command; an in-process open there is read-only. MEM_LIVE_WRITE=1
lifts it on purpose. A schema change to the live DB takes a mem-snapshots/premigrate-hub-<stamp>.db first.

Nightly (`mem nightly`; session start runs `mem nightly --if-stale`): a VACUUM INTO snapshot, then cards.jsonl committed
in its own Git repo ([mem] export_dir), pushed to [mem] export_remote when one is set (optional).
`--if-stale` does nothing while the last export is under 24 h old. `mem doctor` warns on a stale export.
An export or nightly of a DB with no cards, or only the starter cards (SEED; setup seeds a lost DB at once), refuses to
overwrite a cards.jsonl that holds cards (for a starter-only DB: a card outside the starter set), and names the restore.
`mem digest` of such a DB, while an export or snapshot holds those cards, opens with one MEMORY DB HOLDS NO CARDS (or
MEMORY DB HOLDS ONLY THE STARTER CARDS) line that names the restore; a corrupt DB exits 2 with one line that names it too.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid as uuidlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import harness_config  # noqa: E402
import pushretry  # noqa: E402

KINDS = ("design", "decision", "taste", "lesson", "fact", "task")
STATUSES = ("todo", "doing", "parked", "done", "dropped")
OPEN = ("todo", "doing", "parked")
INCIDENT = "incident"  # the tag of an incident record
# The text lint on add. Old rows are not checked; an incident-tagged task skips
# it, so a fault record is never lost. A ~ path passes: it is portable across seats.
TASK_FORM = "'<name>: <what>. Why: <gap>. Done when: <end state>'"
TASK_FORM_RE = re.compile(r"^[^:\n]{1,120}:\s*\S.*?\bWhy:\s*\S.*?\bDone when:\s*\S", re.I | re.S)
BODY_CAP = {"task": 1800}
OTHER_CAP = 600
LINT = (("an ISO date", re.compile(r"\b\d{4}-\d{2}-\d{2}\b")),
        ("a user-home or volume path", re.compile(r"(?<![\w~.])/(?:Users|home|Volumes)/\w|\b[A-Za-z]:\\(?:Users\\)?\w")),
        ("a File:123 cite", re.compile(r"\b[\w./-]*\w\.[A-Za-z]\w{0,5}:\d+\b")))
# An incident record whose text or tags name git history or a delete closes only with a lost-work check line.
LOST_WORK_RISK = re.compile(
    r"filter-branch|filter-repo|force[- ]push|push (?:--force|-f\b)|reset --hard|branch -D\b|update-ref -d\b"
    r"|refs/original|\bdelet|\btrash|\brm\b|\bprune|\bgc\b|git[- ]history", re.I)
LOST_WORK_CHECK = re.compile(r"LOST-WORK CHECK:\s*\S.*?->\s*\S")
# The checked close: evidence of 4+ words naming a cited sha; a lesson names no commit id.
EVIDENCE_WORDS = 4
COMMIT_ID = re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{7,64}\b", re.I)  # evidence: a cited sha prefix
LESSON_SHA = re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,64}\b", re.I)  # a lesson: not a card id
HEX_WORD = re.compile(r"\b[0-9a-f]{7,64}\b", re.I)  # a lesson never names a cited sha prefix, all digits too
CARD_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)  # a lesson may cite a card
# WRK-4: a critical claim carries its evidence label, one per sentence. mem complete refuses
# evidence with an unlabeled one; flight-watchdog flags a DONE close with one. Never bare gate, hook or incident.
CRITICAL_CLAIM = re.compile(
    r"\b(?:crash(?:es|ed|ing)?|save[- ]loss|lost saves?|data[- ]loss|lost data|blockers?"
    r"|block(?:s|ed|ing)? (?:play|the ship|ship(?:ping)?)|(?:gate|hook|tool) (?:fault|failure)s?"
    r"|broken (?:gate|hook|tool)s?)\b", re.I)
EVIDENCE_LABEL = re.compile(r"\[(?:device|sim|test|unverified)\]", re.I)
CLAIM_SPLIT = re.compile(r"(?<=[.!?])\s+|\s\|\s|\n")
PRIORITIES = ("p0", "p1", "p2", "p3", "p4")
RRF_K = 60
POOL = 40
NEAR_DUP = 0.92
DIGEST_CHARS = 6000  # ~1,500 tokens
SEED = HERE / "seed" / "starter.jsonl"  # the starter cards mem seed loads into an empty DB
LIVE_WRITE_ENV = "MEM_LIVE_WRITE"  # =1: code outside the live hub may write and migrate the live DB
# A hub test run: python -m unittest or -m pytest, a test_*.py script, or pytest itself.
TEST_RUN_RE = re.compile(r"^\S*python[\d.]*\s+(?:-\S+\s+)*?(?:-m\s+(?:unittest|pytest)\b|\S*\btest_\w*\.py\b)"
                         r"|^\S*\bpytest\b", re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards(
  rowid INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL CHECK (kind IN ('design','decision','taste','lesson','fact','task')),
  area TEXT, title TEXT, body TEXT NOT NULL, status TEXT, priority TEXT, depends_on TEXT,
  effort TEXT, model TEXT, tags TEXT, created_at TEXT NOT NULL, source TEXT, box TEXT,
  pinned INTEGER NOT NULL DEFAULT 0, supersedes TEXT, superseded_by TEXT, sha256 TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS cards_kind ON cards(kind, superseded_by);
CREATE TABLE IF NOT EXISTS card_topics(card_id TEXT NOT NULL, topic TEXT NOT NULL, PRIMARY KEY(card_id, topic));
CREATE INDEX IF NOT EXISTS card_topics_topic ON card_topics(topic);
CREATE VIRTUAL TABLE IF NOT EXISTS cards_fts USING fts5(title, body, topics, tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS task_log(
  rowid INTEGER PRIMARY KEY, task_id TEXT NOT NULL, at TEXT NOT NULL, event TEXT NOT NULL,
  claim_id TEXT, session TEXT, detail TEXT, box TEXT);
CREATE INDEX IF NOT EXISTS task_log_task ON task_log(task_id, rowid);
"""
# Claim and close columns, added to an older cards table at open; commits is a JSON list.
CLAIM_COLUMNS = (("claim_id", "TEXT"), ("claim_session", "TEXT"), ("claimed_at", "TEXT"), ("run_model", "TEXT"),
                 ("run_effort", "TEXT"), ("worktree", "TEXT"), ("evidence", "TEXT"), ("commits", "TEXT"),
                 ("delivered", "INTEGER NOT NULL DEFAULT 0"), ("closed_at", "TEXT"))
LOG_KEYS = ("at", "event", "claim_id", "session", "detail", "box")


def unlabeled(text: str) -> list[str]:
    """The distinct critical terms (lower case) of each sentence of text that has no evidence label (WRK-4)."""
    terms = []
    for sentence in CLAIM_SPLIT.split(text or ""):
        if not EVIDENCE_LABEL.search(sentence):
            terms += [t.lower() for t in CRITICAL_CLAIM.findall(sentence) if t.lower() not in terms]
    return terms


class MemError(Exception):
    pass


def lint_body(kind: str, body: str, tags=()) -> None:
    """Refuse a new row's text that breaks the lint: a task not in TASK_FORM, a body over its cap, an ISO date,
    a user-home or volume path, a File:123 cite. An incident-tagged task passes as it is."""
    if kind == "task" and INCIDENT in tags:
        return
    cap = BODY_CAP.get(kind, OTHER_CAP)
    if len(body) > cap:
        raise MemError(f"a {kind} body holds at most {cap} chars, this one {len(body)}: cut it")
    if kind == "task" and not TASK_FORM_RE.match(body):
        raise MemError(f"a task body takes the form {TASK_FORM}")
    for what, rule in LINT:
        if hit := rule.search(body):
            raise MemError(f"the body holds {what} ({hit.group(0)!r}): cut it; a ~ path passes")


# Restate lint (ported from an earlier harness): a fact, taste or design
# body whose 6-word runs mostly repeat a rule file's runs restates a rule; the rule file is its one home.
RESTATE_KINDS = ("fact", "taste", "design")
RULE_FILE_GLOBS = ("AGENTS.md", "*/AGENTS.md", "PRODUCT-CONVENTIONS.md", ".claude/skills/**/*.md")
SHINGLE = 6
RESTATE_MIN_RUNS = 8  # a shorter body is too small to measure
RESTATE_SHARE = 0.5   # this share of its word runs found in the rule files refuses it
_WORD_RE = re.compile(r"[a-z0-9']+")
_rule_runs_cache: dict[str, tuple] = {}


def _runs(text: str) -> list[tuple]:
    words = _WORD_RE.findall(text.lower())
    return [tuple(words[i:i + SHINGLE]) for i in range(len(words) - SHINGLE + 1)]


def rule_runs(root: Path) -> dict[tuple, str]:
    """Each word run of the hub's rule files, mapped to the first file that holds it; cached until a file changes."""
    files = sorted({p for g in RULE_FILE_GLOBS for p in root.glob(g) if p.is_file()})
    stamp = tuple((str(p), p.stat().st_mtime_ns) for p in files)
    cached = _rule_runs_cache.get(str(root))
    if cached and cached[0] == stamp:
        return cached[1]
    runs: dict[tuple, str] = {}
    for p in files:
        for run in _runs(p.read_text(encoding="utf-8", errors="replace")):
            runs.setdefault(run, p.relative_to(root).as_posix())
    _rule_runs_cache[str(root)] = (stamp, runs)
    return runs


def lint_restate(kind: str, body: str, root: Path) -> None:
    """Refuse a fact, taste or design body that restates a rule file line; lessons, decisions and tasks pass."""
    if kind not in RESTATE_KINDS:
        return
    runs = _runs(body)
    if len(runs) < RESTATE_MIN_RUNS:
        return
    known = rule_runs(root)
    homes = [known[r] for r in runs if r in known]
    if len(homes) / len(runs) < RESTATE_SHARE:
        return
    home = max(set(homes), key=homes.count)
    raise MemError(f"{len(homes)} of its {len(runs)} word runs repeat a line {home} already holds: a rule lives "
                   "in its file, so add nothing or a pointer ('<topic>: <file>'); a lesson or decision passes")


class Claimed(MemError):
    """The task already holds a claim: the losing claim of a race (exit 3)."""


class LiveUnderTest(MemError):
    """A hub test run opened the live mem database."""


class LiveReadOnly(MemError):
    """A write to the live mem database from code outside the live hub."""


_test_run: list[int | None] = []


def test_run() -> int | None:
    """The pid of the hub test run this process runs in (itself or an ancestor), else None."""
    if not _test_run:
        try:
            ps = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,command="], capture_output=True, text=True,
                                timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            ps = ""
        procs = {}
        for row in ps.splitlines():
            pid, ppid, command = (row.split(None, 2) + ["", "", ""])[:3]
            if pid.isdigit() and ppid.isdigit():
                procs[int(pid)] = (int(ppid), command)
        pid, seen, found = os.getpid(), set(), None
        while pid in procs and pid not in seen:
            seen.add(pid)
            ppid, command = procs[pid]
            if TEST_RUN_RE.match(command):
                found = pid
            pid = ppid
        _test_run.append(found)
    return _test_run[0]


def state_root() -> Path:
    """[project] state_root: the default home of the live DB and the export."""
    return harness_config.path(harness_config.load(harness_config.hub_root(HERE))["project"]["state_root"])


_PREFIX: list[str] = []


def env_value(suffix: str) -> str:
    """The value of <env_prefix>_<suffix> ('' when unset): MEM_DB and MEM_EMBED take the hub's prefix, so two hubs on one
    box share no setting. The bare name alone (an old setter) stops mem: it would otherwise fall through to the live DB."""
    if not _PREFIX:
        _PREFIX.append(harness_config.load(harness_config.hub_root(HERE))["project"]["env_prefix"])
    name = f"{_PREFIX[0]}_{suffix}"
    if name not in os.environ and os.environ.get(suffix):
        raise SystemExit(f"{suffix} is no longer read; set {name}")
    return os.environ.get(name, "")


def live_db(cfg: dict | None = None) -> Path:
    """[mem] db, else <[project] state_root>/hub.db."""
    db = (config() if cfg is None else cfg).get("db")
    return Path(harness_config.path(db)) if db else state_root() / "hub.db"


def real_live_db() -> Path:
    """The live DB under the real user home: a test run moves HOME, so live_db() names a scratch one there."""
    return harness_config.real_roots(harness_config.load(harness_config.hub_root(HERE)))[2]


def live_hub(hub: Path | None = None) -> Path:
    """The live hub: the main worktree of the Git checkout that holds hub (default: this mem.py's checkout), when it
    holds the marker file; else hub itself. A task worktree names its main clone; a clone in any folder names itself."""
    hub = Path(hub or this_hub())
    main = harness_config.main_checkout(hub)
    marker = harness_config.load(harness_config.hub_root(HERE))["project"]["marker_file"]
    return main if (main / marker).is_file() else hub


def this_hub() -> Path:
    """The checkout this mem.py runs from: the live hub, a task worktree or a temp clone."""
    return HERE.parent.parent


def same_path(a: str | Path, b: str | Path) -> bool:
    return Path(a).expanduser().resolve() == Path(b).expanduser().resolve()


def foreign_live(db: str | Path, cfg: dict | None = None) -> bool:
    """True when db is the live DB and this code is not the live hub's: code in a task
    worktree can run ahead of the live schema, and its open would migrate the live DB before land.
    MEM_LIVE_WRITE=1 names the live DB on purpose. With no live hub mem.py (a fixture HOME) there is no live
    code to fall behind, and the open runs as before."""
    return (os.environ.get(LIVE_WRITE_ENV) != "1" and same_path(db, live_db(cfg))
            and not same_path(this_hub(), live_hub())
            and (live_hub() / "Drawer" / "mem" / "mem.py").is_file())


def check_route(model=None, effort=None, area=None) -> None:
    """Refuse a model, effort or area that harness.toml does not name."""
    if not (model or effort or area):
        return
    cfg = harness_config.load(harness_config.hub_root(HERE))
    for name, value, allowed, home in (
            ("model", model, cfg.get("gates", {}).get("allowed_models", []), "[gates] allowed_models"),
            ("effort", effort, cfg.get("routing", {}).get("rungs", []), "[routing] rungs"),
            ("area", area, cfg.get("project", {}).get("task_skills", []), "[project] task_skills")):
        if value and value not in allowed:
            raise MemError(f"{name} {value!r} is not in harness.toml {home}: {', '.join(allowed)}")


def config() -> dict:
    return harness_config.load(harness_config.hub_root(HERE)).get("mem", {})


def this_box() -> str:
    """The host this process runs on. A card and a task_log row name it."""
    return socket.gethostname().split(".")[0]


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# ---------- embeddings ----------

class Embedder:
    """OpenAI embeddings; None when no key or the call fails, so search falls back to keywords."""

    def __init__(self, cfg: dict):
        self.model = cfg["embed_model"]
        self.dim = int(cfg.get("embed_dim", 1536))
        self.key = os.environ.get("OPENAI_API_KEY") or self._key_from(cfg.get("env_file"))
        self.client = None
        self.auth_failed = False  # the last embedding call got a 401: the key is set but invalid

    @staticmethod
    def _key_from(env_file: str | None) -> str | None:
        if not env_file or not (p := Path(harness_config.path(env_file))).is_file():
            return None
        for line in p.read_text().splitlines():
            if line.startswith("OPENAI_API_KEY="):
                return line.split("=", 1)[1].strip().strip("'\"") or None
        return None

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        if not self.key or env_value("MEM_EMBED") == "off":
            return None
        try:
            if self.client is None:
                from openai import OpenAI
                self.client = OpenAI(api_key=self.key, timeout=20, max_retries=2)
            out: list[list[float]] = []
            for i in range(0, len(texts), 100):
                res = self.client.embeddings.create(model=self.model, input=texts[i:i + 100])
                out += [d.embedding for d in res.data]
            return out
        except Exception as exc:  # network or key fault: keyword-only
            self.auth_failed = getattr(exc, "status_code", None) == 401
            print(f"mem: embedding off ({type(exc).__name__}: {exc})", file=sys.stderr)
            return None


# ---------- store ----------

class Mem:
    rules_root: Path | None = None  # the hub whose rule files the restate lint reads; None: this hub

    def __init__(self, db: str | Path, embedder: Embedder | None = None):
        if test_run() is not None and Path(db).expanduser().resolve() in (
                live_db().expanduser().resolve(), real_live_db().resolve()):
            raise LiveUnderTest(f"a hub test run opened the live mem database {db}: give the test a scratch "
                                "<env_prefix>_MEM_DB")
        self.is_live_db = same_path(db, live_db())
        self.read_only = foreign_live(db)
        self.premigrate_snapshot: Path | None = None
        if self.read_only:
            # Code outside the live hub reads the live DB as it is: no schema step, no write.
            uri = Path(db).expanduser().resolve().as_uri() + "?mode=ro"
            self.db = sqlite3.connect(uri, uri=True, timeout=10, isolation_level=None)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA busy_timeout=10000")
            self.embedder = embedder or Embedder(config())
            self.vec = self._load_vec()
            return
        self.db = sqlite3.connect(str(db), timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=10000")
        # Concurrent first opens race the switch to WAL, and SQLite can refuse it with no busy wait:
        # retry it for up to 10 seconds.
        for wait in range(100):
            try:
                self.db.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) or wait == 99:
                    raise
                time.sleep(0.1)
        # One write lock for the schema: an autocommit CREATE that reads, then writes after
        # another first open committed, fails at once with 'database is locked' and skips the busy wait.
        # A schema change to the live DB takes a snapshot first: the first pass rolls the
        # change back, the snapshot runs outside the transaction, and the second pass applies it.
        for first in (True, False):
            self._begin()
            try:
                version = self.db.execute("PRAGMA schema_version").fetchone()[0]
                had = self.db.execute("SELECT 1 FROM sqlite_master WHERE name='cards'").fetchone() is not None
                for stmt in SCHEMA.split(";"):
                    if stmt.strip():
                        self.db.execute(stmt)
                self._migrate()
                hold = (first and self.is_live_db and had
                        and self.db.execute("PRAGMA schema_version").fetchone()[0] != version)
                self.db.execute("ROLLBACK" if hold else "COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
            if not hold:
                break
            stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            self.premigrate_snapshot = self._vacuum_into(
                Path(db).expanduser().parent / "mem-snapshots" / f"premigrate-hub-{stamp}.db")
        self.embedder = embedder or Embedder(config())
        self.vec = self._load_vec()

    def _begin(self) -> None:
        if self.read_only:
            raise LiveReadOnly(f"code outside the live hub ({this_hub()}) may not write the live mem database: "
                               f"run mem from {live_hub()}, or set {LIVE_WRITE_ENV}=1 on purpose")
        self.db.execute("BEGIN IMMEDIATE")

    def _migrate(self) -> None:
        """Add each missing claim column and the claim index; runs under the open's write lock, so once."""
        have = {r[1] for r in self.db.execute("PRAGMA table_info(cards)")}
        for name, decl in CLAIM_COLUMNS:
            if name not in have:
                self.db.execute(f"ALTER TABLE cards ADD COLUMN {name} {decl}")
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS cards_claim ON cards(claim_id) WHERE claim_id IS NOT NULL")

    def _load_vec(self) -> bool:
        try:
            import sqlite_vec
            self.db.enable_load_extension(True)
            sqlite_vec.load(self.db)
            self.db.enable_load_extension(False)
            if self.read_only:  # no CREATE on a read-only open: vector search runs only when the table exists
                return self.db.execute("SELECT 1 FROM sqlite_master WHERE name='vec_cards'").fetchone() is not None
            self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS vec_cards USING vec0("
                            f"embedding float[{self.embedder.dim}] distance_metric=cosine)")
            return True
        except Exception as exc:
            print(f"mem: vector search off ({exc})", file=sys.stderr)
            return False

    # rows
    def row(self, r: sqlite3.Row) -> dict:
        d = {k: r[k] for k in r.keys() if k not in ("rowid", "sha256") and r[k] not in (None, "", 0)}
        for k in ("tags", "depends_on", "commits"):
            if k in d:
                d[k] = json.loads(d[k])
        d["topics"] = [t for (t,) in self.db.execute(
            "SELECT topic FROM card_topics WHERE card_id=? ORDER BY rowid", (r["id"],))]
        return d

    def find(self, ident: str) -> sqlite3.Row:
        rows = self.db.execute("SELECT * FROM cards WHERE id=? OR id LIKE ? LIMIT 2",
                               (ident, ident + "%")).fetchall()
        if len(rows) != 1:
            raise MemError(f"{'no' if not rows else 'more than one'} card matches {ident!r}")
        return rows[0]

    def get(self, ident: str, follow: bool = False) -> dict:
        r = self.find(ident)
        return self.row(self.live(r) if follow else r)

    def _insert(self, c: dict, vector: list[float] | None) -> None:
        body = c["body"]
        cur = self.db.execute(
            "INSERT INTO cards(id,kind,area,title,body,status,priority,depends_on,effort,model,tags,"
            "created_at,source,box,pinned,supersedes,superseded_by,sha256,claim_id,claim_session,claimed_at,"
            "run_model,run_effort,worktree,evidence,commits,delivered,closed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (c["id"], c["kind"], c.get("area"), c.get("title"), body, c.get("status"), c.get("priority"),
             json.dumps(c["depends_on"]) if c.get("depends_on") else None, c.get("effort"), c.get("model"),
             json.dumps(c["tags"]) if c.get("tags") else None, c["created_at"], c.get("source"),
             c.get("box"), int(bool(c.get("pinned"))), c.get("supersedes"), c.get("superseded_by"),
             hashlib.sha256(body.encode()).hexdigest(), c.get("claim_id"), c.get("claim_session"),
             c.get("claimed_at"), c.get("run_model"), c.get("run_effort"), c.get("worktree"), c.get("evidence"),
             json.dumps(c["commits"]) if c.get("commits") else None, int(bool(c.get("delivered"))),
             c.get("closed_at")))
        for e in c.get("log", []):
            self.db.execute("INSERT INTO task_log(task_id,at,event,claim_id,session,detail,box) VALUES(?,?,?,?,?,?,?)",
                            (c["id"], *(e.get(k) for k in LOG_KEYS)))
        topics = list(dict.fromkeys(t.strip() for t in c.get("topics", []) if t and t.strip()))
        self.db.executemany("INSERT OR IGNORE INTO card_topics(card_id,topic) VALUES(?,?)",
                            [(c["id"], t) for t in topics])
        self.db.execute("INSERT INTO cards_fts(rowid,title,body,topics) VALUES(?,?,?,?)",
                        (cur.lastrowid, c.get("title") or "", body, " | ".join(topics)))
        if vector is not None and self.vec:
            self.db.execute("INSERT INTO vec_cards(rowid, embedding) VALUES(?,?)",
                            (cur.lastrowid, json.dumps(vector)))

    @staticmethod
    def embed_text(c: dict) -> str:
        return "\n".join(x for x in (c.get("title"), " | ".join(c.get("topics", [])), c["body"]) if x)

    def add(self, kind: str, body: str, *, title=None, topics=(), source=None, supersedes=None,
            new=False, area=None, status=None, priority=None, depends_on=(), effort=None, model=None,
            tags=(), pinned=False, box=None, closes=None) -> dict:
        body = (body or "").strip()
        if kind not in KINDS:
            raise MemError(f"kind must be one of {', '.join(KINDS)}")
        if not body:
            raise MemError("body is empty")
        lint_body(kind, body, tags)
        lint_restate(kind, body, self.rules_root or harness_config.hub_root(HERE))
        check_route(model, effort, area)
        if kind == "design" and not (source or "").startswith("feedback:"):
            raise MemError("a design row changes only from the human's feedback: give --source feedback:<note>")
        if kind == "task":
            status = status or "todo"
            priority = priority or "p2"
            if not title:
                title = body.split(":", 1)[0].strip()[:120]
        elif status or priority or depends_on:
            raise MemError("status, priority and depends_on are for tasks only")
        if status and status not in STATUSES:
            raise MemError(f"status must be one of {', '.join(STATUSES)}")
        if priority and priority not in PRIORITIES:
            raise MemError(f"priority must be one of {', '.join(PRIORITIES)}")
        old = self.find(supersedes) if supersedes else None
        if old is not None and old["superseded_by"]:
            raise MemError(f"{old['id']} is already superseded by {old['superseded_by']}")
        for dep in depends_on:
            self.find(dep)
        incident = self.find(closes) if closes else None
        if incident is not None:
            if kind != "lesson":
                raise MemError("only a lesson closes an incident")
            if incident["kind"] != "task" or INCIDENT not in json.loads(incident["tags"] or "[]"):
                raise MemError(f"{incident['id']} is no incident record (a task with the tag {INCIDENT})")
            if incident["status"] not in OPEN:
                raise MemError(f"incident {incident['id']} is already {incident['status']}")
            if incident["claim_id"]:
                raise MemError(f"incident {incident['id']} is claimed by session {incident['claim_session']}: "
                               "release the claim first")
            risk = LOST_WORK_RISK.search(" ".join([incident["title"] or "", incident["body"],
                                                   *json.loads(incident["tags"] or "[]")]))
            if risk and not (LOST_WORK_CHECK.search(incident["body"]) or LOST_WORK_CHECK.search(body)):
                raise MemError(f"incident {incident['id']} names {risk.group(0)!r} (git history or a delete): "
                               "put the line 'LOST-WORK CHECK: <command or audit> -> <result>' in the lesson")
            tags = [*tags, f"closes:{incident['id']}"]
        card = dict(id=str(uuidlib.uuid4()), kind=kind, area=area, title=title, body=body,
                    topics=list(topics), source=source, status=status, priority=priority,
                    depends_on=[self.find(d)["id"] for d in depends_on], effort=effort, model=model,
                    tags=list(tags), pinned=pinned, created_at=now(),
                    box=box or this_box(),
                    supersedes=old["id"] if old is not None else None)
        vec = (self.embedder.embed([self.embed_text(card)]) or [None])[0]
        if old is None and not new:
            dup = self.near_dup(vec, kind, body)
            if dup:
                raise MemError(f"near-duplicate of {dup['id']} ({dup['why']}): {dup['body'][:160]!r}; "
                               "pass --supersedes <id> to replace it or --new to keep both")
        self._begin()
        try:
            self._insert(card, vec)
            if old is not None:
                self.db.execute("UPDATE cards SET superseded_by=? WHERE id=?", (card["id"], old["id"]))
            if incident is not None:
                # Re-checked under the write lock: a claim that landed after the read above refuses the close.
                if self.db.execute("UPDATE cards SET status='done', closed_at=? WHERE id=? AND claim_id IS NULL "
                                   "AND status IN ('todo','doing','parked')", (now(), incident["id"])).rowcount != 1:
                    raise MemError(f"incident {incident['id']} was claimed or closed meanwhile: close refused")
                self._log(incident["id"], "status", detail=f"{incident['status']} -> done: closed by lesson {card['id']}")
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return self.get(card["id"])

    def near_dup(self, vec, kind: str, body: str) -> dict | None:
        same = self.db.execute("SELECT id, body FROM cards WHERE sha256=? AND superseded_by IS NULL",
                               (hashlib.sha256(body.encode()).hexdigest(),)).fetchone()
        if same:
            return dict(id=same["id"], body=same["body"], why="same text")
        if vec is None or not self.vec:
            return None
        for r in self.db.execute(
                "SELECT c.id, c.body, v.distance FROM (SELECT rowid, distance FROM vec_cards "
                "WHERE embedding MATCH ? AND k = 10) v JOIN cards c ON c.rowid = v.rowid "
                "WHERE c.kind=? AND c.superseded_by IS NULL ORDER BY v.distance", (json.dumps(vec), kind)):
            if 1 - r["distance"] >= NEAR_DUP:
                return dict(id=r["id"], body=r["body"], why=f"cosine {1 - r['distance']:.2f}")
        return None

    # task status, claims and the task log
    def _write(self, fn):
        """Run fn under one write lock (BEGIN IMMEDIATE): its reads and writes see no other writer."""
        self._begin()
        try:
            out = fn()
            self.db.execute("COMMIT")
            return out
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def _log(self, task_id: str, event: str, claim_id=None, session=None, detail=None, box=None) -> None:
        self.db.execute("INSERT INTO task_log(task_id,at,event,claim_id,session,detail,box) VALUES(?,?,?,?,?,?,?)",
                        (task_id, now(), event, claim_id, session, detail, box or this_box()))

    def task_log(self, ident: str) -> list[dict]:
        rows = self.db.execute(f"SELECT {','.join(LOG_KEYS)} FROM task_log WHERE task_id=? ORDER BY rowid",
                               (self.find(ident)["id"],))
        return [{k: r[k] for k in LOG_KEYS if r[k] is not None} for r in rows]

    def live(self, r: sqlite3.Row) -> sqlite3.Row:
        """The live card at the end of r's supersede chain."""
        seen = {r["id"]}
        while r["superseded_by"] and r["superseded_by"] not in seen:
            r = self.find(r["superseded_by"])
            seen.add(r["id"])
        return r

    def _task(self, ident: str, verb: str) -> sqlite3.Row:
        r = self.find(ident)
        if r["kind"] != "task":
            raise MemError(f"only a task {verb}: {r['id']} is a {r['kind']}")
        return r

    def set_status(self, ident: str, status: str, expect: str | None = None, reason: str | None = None) -> dict:
        """Move a task. Refuses done while a claim is held, a closed task reopened with no reason, and a
        task whose status is not expect. One task_log row per move."""
        for s in (status, expect):
            if s is not None and s not in STATUSES:
                raise MemError(f"status must be one of {', '.join(STATUSES)}")
        reason = (reason or "").strip() or None

        def move():
            r = self._task(ident, "has a status")
            old = r["status"]
            if expect is not None and old != expect:
                raise MemError(f"task {r['id']} is {old}, not {expect}")
            if status == "done" and r["claim_id"]:
                raise MemError(f"task {r['id']} is claimed by session {r['claim_session']} (claim {r['claim_id']}): "
                               "a claimed task is not set done by status; release the claim first")
            if old in ("done", "dropped") and status in OPEN and not reason:
                raise MemError(f"task {r['id']} is {old}: reopening it to {status} needs --reason")
            closed_at = (r["closed_at"] if old == status else now()) if status in ("done", "dropped") else None
            # A reopen drops the old close's proof: a later done must be checked again.
            proof = ", delivered=0, evidence=NULL, commits=NULL" if status in OPEN else ""
            self.db.execute(f"UPDATE cards SET status=?, closed_at=?{proof} WHERE id=?", (status, closed_at, r["id"]))
            if old != status or reason:
                self._log(r["id"], "status", r["claim_id"], r["claim_session"],
                          f"{old} -> {status}" + (f": {reason}" if reason else ""))
            return r["id"]
        return self.get(self._write(move))

    def claim(self, ident: str, session: str, model: str | None = None, effort: str | None = None,
              worktree: str | None = None) -> dict:
        """Claim one open task for session: one UPDATE where claim_id is null, under BEGIN IMMEDIATE.
        Returns the card with its new claim_id. A claim on a held task (the loser of a race) writes one
        'claim_refused' task_log row (the asking session; the holder in detail), then raises Claimed."""
        session = (session or "").strip()
        if not session:
            raise MemError("a claim needs a session: pass --session or set CLAUDE_CODE_SESSION_ID")

        def take():
            r = self._task(ident, "takes a claim")
            if r["superseded_by"]:
                tip = self.live(r)
                raise MemError(f"task {r['id']} is superseded; the live card is {tip['id']}: claim {tip['id']}")
            if r["status"] not in OPEN:
                raise MemError(f"task {r['id']} is {r['status']}: only an open task takes a claim")
            for dep in json.loads(r["depends_on"] or "[]"):
                d = self.live(self.find(dep))
                if d["status"] in OPEN:
                    raise MemError(f"task {r['id']} depends on open task {d['id']} ({d['status']})")
            claim_id = str(uuidlib.uuid4())
            took = self.db.execute(
                "UPDATE cards SET claim_id=?, claim_session=?, claimed_at=?, run_model=?, run_effort=?, "
                "worktree=COALESCE(?, worktree), status='doing' WHERE id=? AND claim_id IS NULL",
                (claim_id, session, now(), model, effort, worktree, r["id"])).rowcount
            if took != 1:
                # Logged and returned, not raised: _write rolls back on a raise, and the row must persist.
                self._log(r["id"], "claim_refused", None, session,
                          f"held by session {r['claim_session']} claim {r['claim_id']} since {r['claimed_at']}")
                return Claimed(f"task {r['id']} is already claimed by session {r['claim_session']} "
                               f"since {r['claimed_at']}")
            self._log(r["id"], "claim", claim_id, session,
                      " ".join(f"{k}={v}" for k, v in (("model", model), ("effort", effort),
                                                       ("worktree", worktree)) if v) or None)
            return r["id"]
        out = self._write(take)
        if isinstance(out, Claimed):
            raise out
        return self.get(out)

    def release(self, ident: str, claim_id: str, reason: str | None = None) -> dict:
        """Give a claim back with its claim_id: the claim clears, doing goes back to todo, the worktree stays."""
        def give():
            r = self._task(ident, "holds a claim")
            if not r["claim_id"]:
                raise MemError(f"task {r['id']} holds no claim")
            if (claim_id or "").strip() != r["claim_id"]:
                raise MemError(f"claim_id {claim_id!r} is not the live claim on task {r['id']}")
            self.db.execute("UPDATE cards SET claim_id=NULL, claim_session=NULL, claimed_at=NULL, "
                            "status=CASE WHEN status='doing' THEN 'todo' ELSE status END WHERE id=?", (r["id"],))
            self._log(r["id"], "release", r["claim_id"], r["claim_session"], (reason or "").strip() or None)
            return r["id"]
        return self.get(self._write(give))

    def complete(self, ident: str, claim_id: str, evidence: str, commits=(), paths=(),
                 no_file_changes: str | None = None, lesson: str | None = None) -> dict:
        """The checked close: claim_id must be the live claim, evidence 4+ words naming a cited sha (or
        no_file_changes) with each critical claim labeled (WRK-4), and task_delivery must pass. Sets done, evidence, commits, delivered=1 and closed_at,
        clears the claim; a lesson row (topic task:<id8>, no commit id and no prefix of a cited sha; a card id8 or
        full card uuid is a fair cite) lands in the same transaction.
        Every refusal leaves the task as it was."""
        evidence = " ".join((evidence or "").split())
        if len(evidence.split()) < EVIDENCE_WORDS:
            raise MemError(f"evidence takes {EVIDENCE_WORDS} or more words that name the proof and its sha")
        if bad := [s for s in CLAIM_SPLIT.split(evidence) if unlabeled(s)]:
            raise MemError(f"a critical claim carries its evidence label ([device], [sim], [test] or [unverified]): "
                           f"{bad[0][:120]!r}")
        lesson = (lesson or "").strip() or None
        import task_delivery  # here, not at the top: a copied mem.py without its Drawer neighbours still opens
        shas = []
        if no_file_changes is None:
            try:
                shas = [task_delivery.parse_commit(c)[1].lower() for c in commits]
            except task_delivery.DeliveryError as exc:
                raise MemError(str(exc)) from exc
        if lesson:
            def card(m):  # a full card uuid is a fair cite; a uuid of no card stays in the scan
                hit = self.db.execute("SELECT 1 FROM cards WHERE id = ?", (m.group(0).lower(),)).fetchone()
                return " " if hit else m.group(0)
            scan = CARD_UUID.sub(card, lesson)
            for word in HEX_WORD.findall(scan):  # a cited sha prefix, whatever its letters
                if any(s.startswith(word.lower()) for s in shas):
                    raise MemError(f"a lesson names no commit id ({word!r}): the evidence holds the sha")
            for hit in LESSON_SHA.findall(scan):
                if len(hit) == 8 and self.db.execute("SELECT 1 FROM cards WHERE id LIKE ?", (hit.lower() + "-%",)).fetchone():
                    continue  # a card id8 is a fair cite
                raise MemError(f"a lesson names no commit id ({hit!r}): the evidence holds the sha")
            lint_body("lesson", lesson)
        r = self._task(ident, "completes")
        self._held(r, claim_id)
        if no_file_changes is None:
            named = [w.lower() for w in COMMIT_ID.findall(evidence)]
            if not any(s.startswith(w) for w in named for s in shas):
                raise MemError("evidence names no cited sha: put a cited commit id (7+ chars) in it")
        try:
            delivery = task_delivery.verify(commits, paths, no_file_changes)
        except task_delivery.DeliveryError as exc:
            raise MemError(f"delivery check refused: {exc}; task {r['id']} stays {r['status']}") from exc
        card = None
        if lesson:
            card = dict(id=str(uuidlib.uuid4()), kind="lesson", body=lesson, topics=[f"task:{r['id'][:8]}"],
                        source=f"task:{r['id']}", tags=[], created_at=now(), box=this_box())
            vec = (self.embedder.embed([self.embed_text(card)]) or [None])[0]
            if dup := self.near_dup(vec, "lesson", lesson):
                raise MemError(f"the lesson is a near-duplicate of {dup['id']} ({dup['why']}): drop --lesson "
                               "or write the new part only")

        def close():
            # Re-checked under the write lock: the claim may have moved during the git fetch.
            now_r = self._task(r["id"], "completes")
            self._held(now_r, claim_id)
            stamp = now()
            done = delivery.get("commits") or []
            self.db.execute("UPDATE cards SET status='done', evidence=?, commits=?, delivered=1, closed_at=?, "
                            "claim_id=NULL, claim_session=NULL, claimed_at=NULL WHERE id=? AND claim_id=?",
                            (evidence, json.dumps(done) if done else None, stamp, r["id"], now_r["claim_id"]))
            detail = (f"no file changes: {delivery['no_file_changes']}" if "no_file_changes" in delivery
                      else " ".join(f"{c['repo']}={c['commit']}" for c in done))
            self._log(r["id"], "complete", now_r["claim_id"], now_r["claim_session"], detail)
            if card is not None:
                self._insert(card, vec)
        self._write(close)
        out = self.get(r["id"])
        if card is not None:
            out["lesson"] = card["id"]
        return out

    def _held(self, r: sqlite3.Row, claim_id: str) -> None:
        if r["status"] != "doing" or not r["claim_id"]:
            raise MemError(f"task {r['id']} is {r['status']} and holds no claim: begin it first")
        if (claim_id or "").strip() != r["claim_id"]:
            raise MemError(f"claim_id {claim_id!r} is not the live claim on task {r['id']}")

    def claim_state(self, r: sqlite3.Row) -> str:
        """live, dead or unknown: r's claim session in this machine's ~/.claude/sessions. It fails closed: no
        session string, no sessions folder or an unreadable row is unknown, and only dead is parked."""
        import runlib  # here, not at the top: a scratch hub may copy mem.py without it
        return runlib.session_state((r["claim_session"] or "").strip(), Path.home())  # reads CLAUDE_CONFIG_DIR

    def interrupt(self, ident: str, reason: str | None = None) -> dict:
        """Park a task whose claim session is dead: the claim clears, doing goes to parked, the worktree stays.
        Refuses a task with no claim and a claim whose session still runs."""
        def park():
            r = self._task(ident, "takes an interrupt")
            if not r["claim_id"]:
                raise MemError(f"task {r['id']} holds no claim")
            state = self.claim_state(r)
            if state == "unknown":
                raise MemError(f"task {r['id']} claim session {r['claim_session']!r}: liveness unknown (no session, "
                               "no ~/.claude/sessions folder or an unreadable row); only a dead claim is interrupted")
            if state == "live":
                raise MemError(f"task {r['id']} is claimed by live session {r['claim_session']}: "
                               "only a dead claim is interrupted")
            self.db.execute("UPDATE cards SET claim_id=NULL, claim_session=NULL, claimed_at=NULL, "
                            "status=CASE WHEN status='doing' THEN 'parked' ELSE status END WHERE id=?", (r["id"],))
            self._log(r["id"], "interrupt", r["claim_id"], r["claim_session"],
                      (reason or "").strip() or "claim session has no live sessions row")
            return r["id"]
        return self.get(self._write(park))

    def reap(self, dry_run: bool = False) -> list[dict]:
        """Interrupt each claim whose session is dead; list an unknown one (state unknown) and never park it.
        dry_run lists them and changes nothing."""
        rows = [{"id": r["id"], "claim_id": r["claim_id"], "claim_session": r["claim_session"],
                 "claimed_at": r["claimed_at"], "worktree": r["worktree"], "state": state}
                for r in self.db.execute("SELECT * FROM cards WHERE claim_id IS NOT NULL ORDER BY claimed_at, id")
                if (state := self.claim_state(r)) != "live"]
        if dry_run:
            return rows
        reaped = []
        for d in rows:
            if d["state"] != "dead":
                reaped.append(d)
                continue
            try:
                self.interrupt(d["id"], "reaped: claim session has no live sessions row")
            except MemError as exc:  # the claim moved or its session came back since the list
                d["skipped"] = str(exc)
            reaped.append(d)
        return reaped

    def note(self, ident: str, text: str, stop: bool = False, session: str | None = None) -> dict:
        """Write one task_log note on a task; stop marks it the steer to halt."""
        text = (text or "").strip()
        if not text:
            raise MemError("a note needs text")

        def put():
            r = self._task(ident, "takes a note")
            self._log(r["id"], "stop" if stop else "note", r["claim_id"], (session or "").strip() or None, text)
            return self.db.execute("SELECT last_insert_rowid()").fetchone()[0]
        n = self._write(put)
        return self.notes(ident, n - 1)[0]

    def notes(self, ident: str, since: int = 0) -> list[dict]:
        """The note and stop rows of a task after task_log rowid since, oldest first, each with n and stop."""
        rows = self.db.execute("SELECT rowid AS n, * FROM task_log WHERE task_id=? AND event IN ('note','stop') "
                               "AND rowid>? ORDER BY rowid", (self.find(ident)["id"], since))
        return [{"n": r["n"], "at": r["at"], "text": r["detail"], "stop": r["event"] == "stop",
                 **{k: r[k] for k in ("session", "claim_id") if r[k] is not None}} for r in rows]

    # search
    @staticmethod
    def fts_query(q: str) -> str:
        words = ["".join(ch for ch in w if ch.isalnum() or ch == "_") for w in q.split()]
        return " OR ".join(f'"{w}"' for w in words if w)

    def search(self, query: str, k: int = 10, kind: str | None = None, all_rows: bool = False) -> list[dict]:
        where = "1=1" if all_rows else "c.superseded_by IS NULL AND (c.status IS NULL OR c.status IN ('todo','doing','parked'))"
        if kind:
            where += f" AND c.kind = '{kind}'" if kind in KINDS else " AND 0"
        ranks: dict[int, float] = {}
        if fq := self.fts_query(query):
            for i, (rowid,) in enumerate(self.db.execute(
                    f"SELECT c.rowid FROM cards_fts f JOIN cards c ON c.rowid=f.rowid "
                    f"WHERE cards_fts MATCH ? AND {where} ORDER BY bm25(cards_fts, 2.0, 1.0, 2.0) LIMIT ?",
                    (fq, POOL))):
                ranks[rowid] = ranks.get(rowid, 0) + 1 / (RRF_K + i + 1)
        vec = (self.embedder.embed([query]) or [None])[0] if self.vec else None
        if vec is not None:
            hits = self.db.execute(
                f"SELECT c.rowid FROM (SELECT rowid, distance FROM vec_cards WHERE embedding MATCH ? "
                f"AND k = ?) v JOIN cards c ON c.rowid=v.rowid WHERE {where} ORDER BY v.distance",
                (json.dumps(vec), POOL * 3)).fetchall()
            for i, (rowid,) in enumerate(hits[:POOL]):
                ranks[rowid] = ranks.get(rowid, 0) + 1 / (RRF_K + i + 1)
        needle = query.strip().lower()
        out = []
        for rowid, score in ranks.items():
            r = self.db.execute("SELECT * FROM cards WHERE rowid=?", (rowid,)).fetchone()
            hay = " ".join(filter(None, (r["title"], r["body"]))).lower()
            topics = [t.lower() for (t,) in self.db.execute(
                "SELECT topic FROM card_topics WHERE card_id=?", (r["id"],))]
            if needle and (needle in hay or needle in topics):
                score += 1 / RRF_K  # exact phrase or topic: one first-place vote more
            out.append((score, r))
        out.sort(key=lambda x: -x[0])
        return [dict(self.row(r), score=round(s, 4)) for s, r in out[:k]]

    def list(self, kind=None, status=None, topic=None, limit=50, all_rows=False, tag=None) -> list[dict]:
        sql, args = "SELECT c.* FROM cards c WHERE 1=1", []
        if not all_rows:
            sql += " AND c.superseded_by IS NULL"
        if kind:
            sql += " AND c.kind=?"; args.append(kind)
        if status:
            sql += " AND c.status=?"; args.append(status)
        if topic:
            sql += " AND c.id IN (SELECT card_id FROM card_topics WHERE topic=?)"; args.append(topic)
        if tag:
            sql += " AND EXISTS (SELECT 1 FROM json_each(c.tags) WHERE value=?)"; args.append(tag)
        sql += " ORDER BY CASE WHEN c.kind='task' THEN c.priority END, c.created_at DESC LIMIT ?"
        return [self.row(r) for r in self.db.execute(sql, args + [limit])]

    def ready(self, tag=None) -> list[dict]:
        """Open, unclaimed live tasks whose depends_on rows are all closed, each with chain (the open tasks that
        wait on it at any depth); ranked by chain, then priority, then age."""
        rows = {r["id"]: r for r in self.db.execute(
            "SELECT * FROM cards WHERE kind='task' AND superseded_by IS NULL")}
        deps = {i: {self.live(self.find(d))["id"] for d in json.loads(r["depends_on"] or "[]")}
                for i, r in rows.items() if r["status"] in OPEN}
        waiters: dict[str, set] = {}
        for i, ds in deps.items():
            for d in ds:
                waiters.setdefault(d, set()).add(i)

        def chain(i: str) -> int:
            seen, todo = set(), [i]
            while todo:
                for w in waiters.get(todo.pop(), ()):
                    if w not in seen and w != i:
                        seen.add(w); todo.append(w)
            return len(seen)
        out = []
        for i, ds in deps.items():
            r = rows[i]
            if r["status"] == "doing" or r["claim_id"] or any(
                    (rows[d]["status"] if d in rows else self.find(d)["status"]) in OPEN for d in ds):
                continue
            if tag and tag not in json.loads(r["tags"] or "[]"):
                continue
            out.append(dict(self.row(r), chain=chain(i)))
        return sorted(out, key=lambda c: (-c["chain"], c.get("priority") or "p9", c["created_at"]))

    def counts(self) -> dict:
        return {k: n for k, n in self.db.execute(
            "SELECT kind, count(*) FROM cards WHERE superseded_by IS NULL GROUP BY kind")}

    # digest, export, import
    def digest(self) -> str:
        def line(r, n=200):
            text = r.get("title") or r["body"]
            return f"- {r['id'][:8]} {text[:n]}"
        parts = ["MEMORY (Drawer/mem/mem.py; MCP server mem). Search before you decide: "
                 "`mem search \"<words>\"`. Write a change as a new row: `mem add --supersedes <id>`."]
        tasks = [r for r in self.list(kind="task", limit=200) if r.get("status") in OPEN]
        parts.append(f"\nOPEN TASKS ({len(tasks)}):")
        parts += [f"- {r['id'][:8]} {r.get('priority', '')} {r.get('status')} | {r.get('title') or r['body'][:100]}"
                  for r in tasks[:25]]
        if taste := [r for r in self.list(kind="taste", limit=200) if r.get("pinned")] or self.list(kind="taste", limit=15):
            parts.append("\nTASTE:")
            parts += [line(r) for r in taste]
        parts.append("\nNEWEST DECISIONS:")
        parts += [line(r, 160) for r in self.list(kind="decision", limit=10)]
        if play := self.list(topic="playtest", limit=5):
            parts.append("\nPLAYTEST NOTES:")
            parts += [line(r, 160) for r in play]
        counts = self.counts()
        parts.append("\nROWS: " + ", ".join(f"{k} {counts.get(k, 0)}" for k in KINDS))
        top = self.db.execute("SELECT t.topic, count(*) n FROM card_topics t JOIN cards c ON c.id=t.card_id "
                              "WHERE c.superseded_by IS NULL GROUP BY t.topic ORDER BY n DESC LIMIT 20").fetchall()
        parts.append("TOPICS: " + ", ".join(f"{t} {n}" for t, n in top))
        text = "\n".join(parts)
        return text if len(text) <= DIGEST_CHARS else text[:DIGEST_CHARS - 4] + "\n..."

    def export(self, path: Path) -> int:
        rows = []
        for r in self.db.execute("SELECT * FROM cards ORDER BY created_at, id").fetchall():
            row = self.row(r)
            if log := self.task_log(r["id"]):
                row["log"] = log
            rows.append(row)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
        tmp.replace(path)
        return len(rows)

    def import_jsonl(self, path: Path) -> int:
        cards = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        known = {i for (i,) in self.db.execute("SELECT id FROM cards")}
        cards = [c for c in cards if c["id"] not in known]
        vectors = self.embedder.embed([self.embed_text(c) for c in cards]) if cards else None
        self._begin()
        try:
            for i, c in enumerate(cards):
                self._insert(c, vectors[i] if vectors else None)
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return len(cards)

    def seed(self, path: Path | None = None) -> dict:
        """Import the starter cards (SEED, or path) only into an empty DB; a DB that holds a card is left as it is."""
        if held := self.db.execute("SELECT count(*) FROM cards").fetchone()[0]:
            return {"imported": 0, "skipped": f"the DB holds {held} cards: the seed loads only into an empty DB"}
        return {"imported": self.import_jsonl(Path(path or SEED))}

    def unembedded(self) -> list[sqlite3.Row]:
        """The cards that have no vector: added or imported with no key, no network or <env_prefix>_MEM_EMBED=off."""
        if not self.vec:
            return []
        return self.db.execute("SELECT * FROM cards WHERE rowid NOT IN (SELECT rowid FROM vec_cards) "
                               "ORDER BY rowid").fetchall()

    def reembed(self) -> int:
        """Embed each card that has no vector; returns the count. Refused with vector search off or no embedding."""
        if not self.vec:
            raise MemError("vector search is off (sqlite-vec did not load): there is no vector table to fill")
        rows = self.unembedded()
        if not rows:
            return 0
        vectors = self.embedder.embed([self.embed_text(self.row(r)) for r in rows])
        if not vectors:
            raise MemError(f"{len(rows)} cards have no vector and embedding is off (no OPENAI_API_KEY, "
                           "<env_prefix>_MEM_EMBED=off or a network fault): set the key in [mem] env_file and rerun")
        self._begin()
        try:
            self.db.executemany("INSERT INTO vec_cards(rowid, embedding) VALUES(?,?)",
                                [(r["rowid"], json.dumps(v)) for r, v in zip(rows, vectors)])
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return len(rows)

    def _vacuum_into(self, dest: Path) -> Path:
        """A whole copy at dest; an earlier file there goes only when the new one is whole."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_suffix(".part")
        part.unlink(missing_ok=True)
        self.db.execute("VACUUM INTO ?", (str(part),))
        check = sqlite3.connect(part)
        try:
            verdict = check.execute("PRAGMA integrity_check").fetchall()
        except sqlite3.DatabaseError as e:
            verdict = [(str(e),)]
        finally:
            check.close()
        if verdict != [("ok",)]:  # a corrupt copy never replaces a whole one
            part.unlink(missing_ok=True)
            raise sqlite3.DatabaseError(f"snapshot {part} failed integrity_check: {verdict[:3]}")
        part.replace(dest)
        return dest

    def snapshot(self, folder: Path, keep: int) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        dest = self._vacuum_into(folder / f"hub-{dt.date.today().isoformat()}.db")
        for pattern in ("hub-*.db", "premigrate-hub-*.db"):
            for old in sorted(folder.glob(pattern))[:-keep]:
                old.unlink()
        return dest


OWNER_SUFFIX = ".hub"  # <db>.hub names the live hub that made the DB


def claim_db(path: Path, hub: Path) -> str | None:
    """Record hub as the DB's owner in <db>.hub; the FAIL line when another hub that still exists owns it.
    Two hubs with one slug on one box share the default roots: the second must not take the first one's DB."""
    owner = Path(str(path) + OWNER_SUFFIX)
    try:
        held = Path(owner.read_text(encoding="utf-8").strip())
    except OSError:
        held = None
    if held and held != hub and (held / harness_config.CONFIG).is_file():
        return (f"FAIL mem doctor: db {path} belongs to the hub at {held}, not {hub}. Give this hub its own roots "
                f"in {hub / harness_config.LOCAL} ([project] config_root and state_root, [mem] db, env_file and "
                "export_dir, [names] denylist), then rerun")
    if held != hub:
        owner.parent.mkdir(parents=True, exist_ok=True)
        owner.write_text(f"{hub}\n", encoding="utf-8")
    return None


EXPORT_STALE_SECONDS = 24 * 3600


def export_age(export_dir: Path) -> float | None:
    """Seconds since the last nightly export wrote cards.jsonl, None when it never ran."""
    try:
        return max(0.0, time.time() - (Path(export_dir) / "cards.jsonl").stat().st_mtime)
    except OSError:
        return None


def doctor(path: Path, hub: Path | None = None, export_dir: Path | None = None) -> tuple[bool, str]:
    """Claim the DB for this live hub (claim_db), open (and so create) it, check sqlite-vec and the search mode;
    one OK, WARN or FAIL line. WARN (still exit 0): a key the API rejected with a 401, or an export_dir whose
    last export is over 24 h old (or missing, once the DB holds cards)."""
    refused = claim_db(Path(path), Path(hub or live_hub()).resolve())
    if refused:
        return False, refused
    try:
        m = Mem(path)
    except Exception as exc:
        return False, f"FAIL mem doctor: cannot open {path} ({type(exc).__name__}: {exc})"
    warns: list[str] = []
    try:
        cards = m.db.execute("SELECT count(*) FROM cards").fetchone()[0]
        if not m.vec:
            return False, f"FAIL mem doctor: db {path} opens, but sqlite-vec did not load (run uv sync)"
        if env_value("MEM_EMBED") == "off":
            mode = "keyword-only (<env_prefix>_MEM_EMBED=off)"
        elif not getattr(m.embedder, "key", True):
            mode = "keyword-only (no OPENAI_API_KEY: put it in [mem] env_file for embeddings)"
        elif m.embedder.embed(["mem doctor"]) is None:
            if m.embedder.auth_failed:
                warns.append("OPENAI_API_KEY is set but the API refused it (401): fix the key in [mem] env_file")
                mode = "keyword-only (the key was refused)"
            else:
                mode = "keyword-only (the embedding call failed: check the key and the network)"
        else:
            missing = len(m.unembedded())
            mode = "keywords + embeddings" + (f"; {missing} cards have no vector: run mem reembed" if missing else "")
        age = export_age(export_dir) if export_dir is not None else None
        if export_dir is not None and (age is None and cards or age is not None and age > EXPORT_STALE_SECONDS):
            warns.append("the nightly export is " + ("missing" if age is None else f"{age / 3600:.0f} h old")
                         + f" in {export_dir}: run mem nightly (session start does it)")
        line = f"mem doctor: db {path}, {cards} cards, sqlite-vec loaded, search {mode}"
        return True, (f"WARN {line}; WARN: " + "; ".join(warns)) if warns else f"OK {line}"
    finally:
        m.db.close()


# ---------- nightly: export to its own Git repo ----------

def starter_ids() -> frozenset[str]:
    """The ids of the starter cards (SEED): mem seed loads them into an empty DB, so a lost DB holds only these."""
    try:
        with open(SEED, encoding="utf-8") as f:
            return frozenset(json.loads(line)["id"] for line in f if line.strip())
    except (OSError, ValueError, KeyError):
        return frozenset()


def _outside_starter(db: sqlite3.Connection) -> int:
    ids = sorted(starter_ids())
    return db.execute(f"SELECT count(*) FROM cards WHERE id NOT IN ({','.join('?' * len(ids))})", ids).fetchone()[0]


def lost_kind(db: sqlite3.Connection) -> str:
    """"empty" for a DB with no card, "starter" for one that holds only the starter cards, else "" (it holds its own)."""
    if not db.execute("SELECT count(*) FROM cards").fetchone()[0]:
        return "empty"
    return "" if _outside_starter(db) else "starter"


def holds_cards(export: Path, beyond_starter: bool = False) -> bool:
    """True when the export file has a card line; beyond_starter: a card outside the starter set."""
    starter = starter_ids() if beyond_starter else frozenset()
    try:
        with open(export, encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    card_id = json.loads(line).get("id")
                except (ValueError, AttributeError):
                    card_id = None  # a line that is no card row still counts as held
                if card_id not in starter:
                    return True
    except OSError:
        pass
    return False


def snapshot_with_cards(folder: Path, beyond_starter: bool = False) -> Path | None:
    """The newest snapshot in folder that holds a card (beyond_starter: outside the starter set), else None."""
    for snap in sorted(Path(folder).glob("hub-*.db"), reverse=True):
        try:
            db = sqlite3.connect(f"{snap.resolve().as_uri()}?mode=ro", uri=True)
            try:
                if (_outside_starter(db) if beyond_starter else db.execute("SELECT count(*) FROM cards").fetchone()[0]):
                    return snap
            finally:
                db.close()
        except sqlite3.Error:
            continue
    return None


def restore_hint(db: Path, export: Path, snap_dir: Path, corrupt: bool = False,
                 beyond_starter: bool = False) -> str | None:
    """The restore command for this hub when the export or a snapshot still holds cards (README, MANUAL: Restore),
    else None. A corrupt DB, or a snapshot copy over an existing file, moves the old file aside first.
    beyond_starter: the DB holds the starter cards, so only a card outside that set is worth restoring."""
    aside = f"mv {db} {db}.bad; "
    if holds_cards(export, beyond_starter):
        return ("Restore: " + (aside if corrupt else "") + f".venv/bin/python -B Drawer/mem/mem.py import {export}")
    if snap := snapshot_with_cards(snap_dir, beyond_starter):
        return f"Restore: {aside if corrupt or Path(db).exists() else ''}cp {snap} {db}"
    return None


def refuse_empty_export(m: "Mem", dest: Path, db: Path, snap_dir: Path) -> None:
    """An export of a DB with no cards, or only the starter cards, must not replace a dest that holds cards (for a
    starter-only DB: a card outside the starter set): a lost DB would erase its backup."""
    kind = lost_kind(m.db)
    if kind and holds_cards(dest, kind == "starter"):
        raise MemError(f"refusing to overwrite {dest}: the DB {db} holds "
                       + ("no cards" if kind == "empty" else "only the starter cards") + ", the export "
                       + ("does" if kind == "empty" else "holds more") + ". "
                       + restore_hint(db, dest, snap_dir, beyond_starter=kind == "starter"))


def publish_export(folder: Path, remote: str | None) -> str:
    """Commit cards.jsonl in the export repo and push it to [mem] export_remote when one is set.
    The export holds names the hub repo's name check refuses, so it never goes to the hub repo."""
    git = lambda *a: subprocess.run(["git", "-C", str(folder), *a], check=True, text=True,  # noqa: E731
                                    capture_output=True).stdout.strip()
    if not (folder / ".git").exists():
        git("init", "-q", "-b", "main")
    git("add", "cards.jsonl")
    if not git("status", "--porcelain"):
        state = "unchanged"
    else:
        git("commit", "-q", "-m", "Nightly export of hub.db")
        state = git("rev-parse", "--short=12", "HEAD")
    if remote:
        push = ["git", "-C", str(folder), "push", "-q", remote, "HEAD:refs/heads/main"]
        done = pushretry.run(lambda: subprocess.run(push, text=True, capture_output=True))
        if done.returncode:
            raise subprocess.CalledProcessError(done.returncode, push, done.stdout, done.stderr)
        state += " pushed"
    return state


# ---------- MCP ----------

def serve_mcp(mem_factory) -> None:
    from mcp.server.fastmcp import FastMCP
    server = FastMCP("mem")

    def call(fn):
        try:
            return fn(mem_factory())
        except MemError as exc:
            return {"error": str(exc)}

    @server.tool()
    def search(query: str, k: int = 10, kind: str | None = None, include_old: bool = False) -> list | dict:
        """Search memory (keywords + meaning). kind: design|decision|taste|lesson|fact|task.
        include_old also returns superseded rows and closed tasks."""
        return call(lambda m: m.search(query, k, kind, include_old))

    @server.tool()
    def get(id: str, follow: bool = False) -> dict:
        """One card by id or unique id prefix. follow: the live card at the end of its supersede chain."""
        return call(lambda m: m.get(id, follow))

    @server.tool()
    def add(kind: str, body: str, title: str | None = None, topics: list[str] | None = None,
            source: str | None = None, supersedes: str | None = None, new: bool = False,
            priority: str | None = None, status: str | None = None, depends_on: list[str] | None = None,
            pinned: bool = False, tags: list[str] | None = None, closes: str | None = None,
            model: str | None = None, effort: str | None = None, area: str | None = None) -> dict:
        """Write one card. kind: design|decision|taste|lesson|fact|task. A change supersedes the old
        card (supersedes=<id>). A near-duplicate is refused unless supersedes or new is set. A design
        card needs source "feedback:<the human's note>". A lesson with closes=<incident id> closes that
        incident record (a task tagged incident). The text lint (lint_body) refuses a task body not in the
        form '<name>: <what>. Why: <gap>. Done when: <end state>', a body over its cap, an ISO date, a
        user-home or volume path and a File:123 cite; an incident record skips it. model, effort and area
        must be in harness.toml [gates] allowed_models, [routing] rungs and [project] task_skills."""
        return call(lambda m: m.add(kind, body, title=title, topics=topics or [], source=source,
                                    supersedes=supersedes, new=new, priority=priority, status=status,
                                    depends_on=depends_on or [], pinned=pinned, tags=tags or [], closes=closes,
                                    model=model, effort=effort, area=area))

    @server.tool()
    def list_cards(kind: str | None = None, status: str | None = None, topic: str | None = None,
                   limit: int = 50, include_old: bool = False, tag: str | None = None,
                   ready: bool = False) -> list | dict:
        """Current cards by exact filter, newest first (tasks by priority). tag: one tag, e.g. incident.
        ready: only open, unclaimed tasks with every depends_on closed, each with chain, chain-first."""
        return call(lambda m: m.ready(tag) if ready else m.list(kind, status, topic, limit, include_old, tag))

    @server.tool()
    def set_status(id: str, status: str, expect: str | None = None, reason: str | None = None) -> dict:
        """Move a task: todo|doing|parked|done|dropped. Refused: done while a claim is held, a done or
        dropped task reopened with no reason, a task whose status is not expect."""
        return call(lambda m: m.set_status(id, status, expect, reason))

    server.run()


# ---------- CLI ----------

def db_path(cfg: dict) -> Path:
    """<env_prefix>_MEM_DB, else a scratch DB per hub test run, else the live DB."""
    env_value("MEM_EMBED")  # a bare MEM_EMBED stops mem too, key or no key
    given = env_value("MEM_DB")
    run = None if given else test_run()
    p = (Path(given) if given else
         Path(tempfile.gettempdir()) / "hub-test-mem" / f"{run}.db" if run is not None else live_db(cfg))
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def show(rows) -> None:
    if isinstance(rows, dict):
        rows = [rows]
    for r in rows:
        head = f"{r['id'][:8]} {r['kind']}"
        if r["kind"] == "task":
            head += f" {r.get('priority')} {r.get('status')}"
        if "chain" in r:
            head += f" chain={r['chain']}"
        if r.get("superseded_by"):
            head += f" (superseded by {r['superseded_by'][:8]})"
        if r.get("topics"):
            head += " [" + ", ".join(r["topics"][:4]) + "]"
        print(head)
        print("  " + r["body"].replace("\n", "\n  "))


def note_argv(argv: list[str]) -> list[str]:
    """Put each note positional after '--', so a text that starts with '-' (a list line) is text, not a flag."""
    if "--" in argv:
        return argv
    flags, pos, rest = [], [], argv[1:]
    while rest:
        a = rest.pop(0)
        if a in ("--stop", "--json", "-h", "--help") or a.startswith("--session="):
            flags.append(a)
        elif a == "--session":
            flags += [a, *rest[:1]]; rest = rest[1:]
        else:
            pos.append(a)
    return ["note", *flags, "--", *pos]


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] in (["claim"], ["note"]) and not any(a == "--session" or a.startswith("--session=") for a in argv) \
            and os.environ.get("CLAUDE_CODE_SESSION_ID"):
        argv = [*argv, "--session", os.environ["CLAUDE_CODE_SESSION_ID"]]  # resolved here, before a hand-off to the live hub
    if argv[:1] == ["note"]:
        argv = note_argv(argv)
    cfg = config()
    if not env_value("MEM_DB") and test_run() is None and foreign_live(live_db(cfg), cfg):
        # A task worktree or temp clone runs the live hub's mem.py on the live DB.
        cli = live_hub() / "Drawer" / "mem" / "mem.py"
        return subprocess.run([sys.executable, "-B", str(cli), *argv]).returncode
    ap = argparse.ArgumentParser(prog=".venv/bin/python -B Drawer/mem/mem.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search"); s.add_argument("query"); s.add_argument("-k", type=int, default=10)
    s.add_argument("--kind"); s.add_argument("--all", action="store_true")
    s = sub.add_parser("get"); s.add_argument("id"); s.add_argument("--follow", action="store_true")
    s = sub.add_parser("add")
    s.add_argument("--kind", required=True, choices=KINDS); s.add_argument("--body", required=True)
    s.add_argument("--title"); s.add_argument("--topic", action="append", default=[])
    s.add_argument("--source"); s.add_argument("--supersedes"); s.add_argument("--new", action="store_true")
    s.add_argument("--area"); s.add_argument("--priority"); s.add_argument("--status")
    s.add_argument("--depends-on", action="append", default=[]); s.add_argument("--tag", action="append", default=[])
    s.add_argument("--pinned", action="store_true"); s.add_argument("--closes")
    s.add_argument("--model"); s.add_argument("--effort")
    s = sub.add_parser("list"); s.add_argument("--kind"); s.add_argument("--status"); s.add_argument("--topic")
    s.add_argument("--tag"); s.add_argument("--ready", action="store_true")
    s.add_argument("--limit", type=int, default=50); s.add_argument("--all", action="store_true")
    s = sub.add_parser("status"); s.add_argument("id"); s.add_argument("status", choices=STATUSES)
    s.add_argument("--expect", choices=STATUSES); s.add_argument("--reason")
    s = sub.add_parser("claim"); s.add_argument("id"); s.add_argument("--session")
    s.add_argument("--model"); s.add_argument("--effort"); s.add_argument("--worktree")
    s = sub.add_parser("release"); s.add_argument("id"); s.add_argument("--claim-id", required=True)
    s.add_argument("--reason")
    s = sub.add_parser("complete"); s.add_argument("id"); s.add_argument("--claim-id", required=True)
    s.add_argument("--evidence", required=True); s.add_argument("--commit", action="append", default=[])
    s.add_argument("--path", action="append", default=[]); s.add_argument("--no-file-changes")
    s.add_argument("--lesson")
    s = sub.add_parser("interrupt"); s.add_argument("id"); s.add_argument("--reason")
    s = sub.add_parser("reap"); s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("note"); s.add_argument("id"); s.add_argument("text"); s.add_argument("--stop", action="store_true")
    s.add_argument("--session")
    s = sub.add_parser("notes"); s.add_argument("id"); s.add_argument("--since", type=int, default=0)
    s = sub.add_parser("log"); s.add_argument("id")
    sub.add_parser("digest"); sub.add_parser("counts"); sub.add_parser("snapshot")
    s = sub.add_parser("nightly"); s.add_argument("--if-stale", action="store_true")
    sub.add_parser("mcp"); sub.add_parser("doctor"); sub.add_parser("reembed")
    s = sub.add_parser("seed"); s.add_argument("path", nargs="?")
    s = sub.add_parser("export"); s.add_argument("path", nargs="?")
    s = sub.add_parser("import"); s.add_argument("path")
    # The JSON contract: one compact line out; a MemError is {"error"} on stdout, exit 2 (a lost claim race: exit 3).
    for s in sub.choices.values():
        s.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    def out(value, text=None) -> None:
        if a.json:
            print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        elif text is not None:
            print(text)
        else:
            show(value)

    path = db_path(cfg)
    if a.cmd == "mcp":
        cache: list[Mem] = []
        serve_mcp(lambda: cache[0] if cache else cache.append(Mem(path)) or cache[0])
        return 0
    export_dir = harness_config.path(cfg["export_dir"]) if cfg.get("export_dir") else state_root() / "mem-export"
    if test_run() is not None and not env_value("MEM_DB"):
        export_dir = path.parent / "mem-export"  # a hub test run never writes the live export
    if a.cmd == "doctor":
        ok, line = doctor(path, export_dir=export_dir)
        out({"ok": ok, "line": line}, line)
        return 0 if ok else 1
    snap_dir = path.parent / "mem-snapshots"
    try:
        m = Mem(path)
    except sqlite3.DatabaseError as exc:
        if isinstance(exc, sqlite3.OperationalError):  # locked, busy: not a lost file
            raise
        hint = restore_hint(path, export_dir / "cards.jsonl", snap_dir, corrupt=True)
        text = f"the DB {path} is unreadable ({exc})" + (f". {hint}" if hint else "")
        print(json.dumps({"error": text}, ensure_ascii=False) if a.json else f"mem: {text}", file=sys.stderr)
        return 2
    try:
        if a.cmd == "search":
            out(m.search(a.query, a.k, a.kind, a.all))
        elif a.cmd == "get":
            out(m.get(a.id, a.follow))
        elif a.cmd == "add":
            out(m.add(a.kind, a.body, title=a.title, topics=a.topic, source=a.source, supersedes=a.supersedes,
                       new=a.new, area=a.area, priority=a.priority, status=a.status, depends_on=a.depends_on,
                       tags=a.tag, pinned=a.pinned, closes=a.closes, model=a.model, effort=a.effort))
        elif a.cmd == "list":
            out(m.ready(a.tag) if a.ready else m.list(a.kind, a.status, a.topic, a.limit, a.all, a.tag))
        elif a.cmd == "status":
            out(m.set_status(a.id, a.status, a.expect, a.reason))
        elif a.cmd == "claim":
            c = m.claim(a.id, a.session, a.model, a.effort, a.worktree)
            out(c, f"{c['claim_id']} claims task {c['id']}")
        elif a.cmd == "release":
            c = m.release(a.id, a.claim_id, a.reason)
            out(c, f"released task {c['id']}; status {c['status']}")
        elif a.cmd == "complete":
            c = m.complete(a.id, a.claim_id, a.evidence, a.commit, a.path, a.no_file_changes, a.lesson)
            out(c, f"completed task {c['id']}; delivered")
        elif a.cmd == "interrupt":
            c = m.interrupt(a.id, a.reason)
            out(c, f"interrupted task {c['id']}; status {c['status']}")
        elif a.cmd == "reap":
            rows = m.reap(a.dry_run)
            verb = "would interrupt" if a.dry_run else "interrupted"
            out(rows, "\n".join((f"unknown {d['id'][:8]} (session {d['claim_session']}): kept, liveness unknown"
                                 if d["state"] == "unknown" else f"{verb} {d['id'][:8]} (session {d['claim_session']})")
                                + (f" skipped: {d['skipped']}" if d.get("skipped") else "") for d in rows)
                or "no dead claim")
        elif a.cmd == "note":
            n = m.note(a.id, a.text, a.stop, a.session)
            out(n, f"note {n['n']} on task {m.get(a.id)['id'][:8]}" + (" (stop)" if n["stop"] else ""))
        elif a.cmd == "notes":
            rows = m.notes(a.id, a.since)
            out(rows, "\n".join(f"{n['n']} {n['at']}{' STOP' if n['stop'] else ''} {n['text']}" for n in rows))
        elif a.cmd == "log":
            rows = m.task_log(a.id)
            out(rows, "\n".join(" ".join(str(e[k]) for k in LOG_KEYS if k in e) for e in rows))
        elif a.cmd == "digest":
            d = m.digest()
            kind = lost_kind(m.db)
            if kind and (hint := restore_hint(path, export_dir / "cards.jsonl", snap_dir,
                                              beyond_starter=kind == "starter")):
                d = ("MEMORY DB HOLDS NO CARDS, but a backup does." if kind == "empty"
                     else "MEMORY DB HOLDS ONLY THE STARTER CARDS, but a backup holds more.") + f" {hint}\n" + d
            out({"digest": d}, d)
        elif a.cmd == "counts":
            c = m.counts()
            out(c, json.dumps(c, sort_keys=True))
        elif a.cmd == "export":
            dest = Path(a.path) if a.path else export_dir / "cards.jsonl"
            dest.parent.mkdir(parents=True, exist_ok=True)
            refuse_empty_export(m, dest, path, snap_dir)
            n = m.export(dest)
            out({"exported": n, "path": str(dest)}, f"exported {n} rows to {dest}")
        elif a.cmd == "import":
            n = m.import_jsonl(Path(a.path))
            out({"imported": n}, f"imported {n} rows")
        elif a.cmd == "seed":
            s = m.seed(Path(a.path) if a.path else None)
            out(s, s.get("skipped") or f"imported {s['imported']} starter rows")
        elif a.cmd == "reembed":
            n = m.reembed()
            out({"embedded": n}, f"embedded {n} cards")
        elif a.cmd == "snapshot":
            snap = m.snapshot(snap_dir, int(cfg.get("keep_snapshots", 14)))
            out({"snapshot": str(snap)}, str(snap))
        elif a.cmd == "nightly" and a.if_stale and (age := export_age(export_dir)) is not None \
                and age < EXPORT_STALE_SECONDS:
            out({"skipped": "export under 24 h old"}, "nightly skipped: the last export is under 24 h old")
        elif a.cmd == "nightly":
            export_dir.mkdir(parents=True, exist_ok=True)
            refuse_empty_export(m, export_dir / "cards.jsonl", path, snap_dir)  # before the snapshot rotation too
            snap = m.snapshot(snap_dir, int(cfg.get("keep_snapshots", 14)))
            n = m.export(export_dir / "cards.jsonl")
            git = publish_export(export_dir, cfg.get("export_remote"))
            out({"snapshot": str(snap), "exported": n, "git": git}, f"snapshot {snap}; exported {n} rows; git {git}")
    except MemError as exc:
        if a.json:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        else:
            print(f"mem: {exc}", file=sys.stderr)
        return 3 if isinstance(exc, Claimed) else 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
