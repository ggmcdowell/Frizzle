"""Tests for Drawer/mem/mem.py: the hub memory store, search, digest and export."""
import contextlib
import hashlib
import io
import json
import math
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "mem"))
import mem  # noqa: E402
import harness_config  # noqa: E402  (beside mem: mem puts Drawer on sys.path)

MEM_DB, MEM_EMBED = mem.harness_config.env_name("MEM_DB"), mem.harness_config.env_name("MEM_EMBED")  # the hub's prefix
import task_delivery  # noqa: E402  (beside mem: mem puts Drawer on sys.path)

LIVE_DB = mem.config()["db"]  # [mem] db, ~-rooted: a test roots it under a scratch HOME


class FakeEmbedder:
    """Hashed bag of words: texts that share words sit close; no network."""
    dim = 64

    def __init__(self, on=True):
        self.on = on

    def embed(self, texts):
        if not self.on:
            return None
        out = []
        for t in texts:
            v = [0.0] * self.dim
            for w in t.lower().split():
                v[int(hashlib.md5(w.strip(".,:").encode()).hexdigest(), 16) % self.dim] += 1
            n = math.sqrt(sum(x * x for x in v)) or 1
            out.append([x / n for x in v])
        return out


class MemTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.m = mem.Mem(Path(self.tmp.name) / "hub.db", FakeEmbedder())

    def tearDown(self):
        self.m.db.close()
        self.tmp.cleanup()

    def test_a_first_open_makes_its_schema_under_a_write_lock(self):
        # Concurrent first opens of a fresh DB raced the autocommit CREATEs; a CREATE that
        # read the schema, then wrote after another open committed, failed 'database is locked' at once.
        said, connect = [], mem.sqlite3.connect

        def traced(*a, **k):
            db = connect(*a, **k)
            db.set_trace_callback(said.append)
            return db
        with mock.patch.object(mem.sqlite3, "connect", traced):
            mem.Mem(Path(self.tmp.name) / "fresh.db", FakeEmbedder()).db.close()
        first_create = next(i for i, s in enumerate(said) if s.lstrip().upper().startswith("CREATE"))
        self.assertIn("BEGIN IMMEDIATE", [s.strip().rstrip(";").upper() for s in said[:first_create]])

    def test_a_first_open_retries_a_locked_switch_to_wal(self):
        # A first open that lost the race to switch a fresh DB to WAL failed at once.
        class Raced(mem.sqlite3.Connection):
            refused = 0

            def execute(self, sql, *a):
                if sql == "PRAGMA journal_mode=WAL" and not Raced.refused:
                    Raced.refused = 1
                    raise mem.sqlite3.OperationalError("database is locked")
                return super().execute(sql, *a)
        connect = mem.sqlite3.connect
        with mock.patch.object(mem.sqlite3, "connect", lambda *a, **k: connect(*a, factory=Raced, **k)), \
                mock.patch.object(mem.time, "sleep"):
            m = mem.Mem(Path(self.tmp.name) / "raced.db", FakeEmbedder())
        self.assertEqual(1, Raced.refused)
        self.assertEqual("wal", m.db.execute("PRAGMA journal_mode").fetchone()[0])
        m.db.close()

    def test_add_and_get_by_prefix(self):
        r = self.m.add("lesson", "The export needs a second run after a fresh install.", topics=["build proof"])
        got = self.m.get(r["id"][:8])
        self.assertEqual(got["body"], r["body"])
        self.assertEqual(got["topics"], ["build proof"])
        self.assertTrue(got["box"])

    def test_supersede_is_a_new_row_and_old_body_stays(self):
        old = self.m.add("decision", "Slash damage rounds down.")
        new = self.m.add("decision", "Slash damage rounds up.", supersedes=old["id"][:8])
        self.assertEqual(new["supersedes"], old["id"])
        self.assertEqual(self.m.get(old["id"])["superseded_by"], new["id"])
        self.assertEqual(self.m.get(old["id"])["body"], "Slash damage rounds down.")
        self.assertEqual([r["id"] for r in self.m.search("slash damage")], [new["id"]])
        self.assertIn(old["id"], [r["id"] for r in self.m.search("slash damage", all_rows=True)])
        with self.assertRaisesRegex(mem.MemError, "already superseded"):
            self.m.add("decision", "Slash damage rounds to even.", supersedes=old["id"])

    def test_near_duplicate_refused_unless_new(self):
        self.m.add("fact", "The hub map has six stations.")
        with self.assertRaisesRegex(mem.MemError, "near-duplicate"):
            self.m.add("fact", "The hub map has six stations.")
        self.m.add("fact", "The hub map has six stations.", new=True)
        self.assertEqual(self.m.counts()["fact"], 2)

    def test_design_needs_feedback_source(self):
        with self.assertRaisesRegex(mem.MemError, "feedback"):
            self.m.add("design", "Enemies hunt in packs of three.")
        r = self.m.add("design", "Enemies hunt in packs of three.", source="feedback:the human wants packs")
        self.assertEqual(r["source"], "feedback:the human wants packs")

    def test_task_defaults_status_and_closed_task_leaves_search(self):
        t = self.m.add("task", "Telegraph name: draw the family name above the effect line. Why: x. Done when: y.")
        self.assertEqual((t["status"], t["priority"], t["title"]), ("todo", "p2", "Telegraph name"))
        self.assertTrue(self.m.search("telegraph family name"))
        self.m.set_status(t["id"], "done")
        self.assertFalse(self.m.search("telegraph family name"))
        with self.assertRaises(mem.MemError):
            self.m.add("lesson", "x", priority="p1")

    HOME = "/".join(("", "Users", "someone"))  # a user-home path, built so the name check reads no literal

    def test_the_text_lint_refuses_each_rule_on_add(self):
        # A task body takes the form; a body has a cap; no ISO date, user-home or volume path or
        # File:123 cite. A ~ path passes.
        form = "Lantern oil: track the oil. Why: the lamp never dims. Done when: oil drains at night."
        refused = {"free-body task": ("task", "Lantern oil: track the oil."),
                   "1801-char task": ("task", form + "x" * (1801 - len(form))),
                   "601-char fact": ("fact", "x" * 601),
                   "ISO date": ("fact", "The bell rang on 2026-11-16."),
                   "user-home path": ("fact", f"The cache sits at {self.HOME}/cache."),
                   "volume path": ("fact", "The dump sits on /Volumes/Backup/dump."),
                   "File:123 cite": ("lesson", "The guard sits at mem.py:303.")}
        for name, (kind, body) in refused.items():
            with self.subTest(name), self.assertRaises(mem.MemError):
                self.m.add(kind, body, new=True)
        self.assertEqual(0, self.m.db.execute("SELECT COUNT(*) FROM cards").fetchone()[0])
        self.m.add("task", form)
        self.m.add("task", form.replace("oil.", "oil in ~/Hub.", 1), new=True)
        self.m.add("fact", "x" * 600)
        self.m.add("task", "Lantern wick: " + "w" * (1800 - 60) + ". Why: dim. Done when: lit.", new=True)

    def test_the_path_lint_sees_the_windows_and_git_bash_forms(self):
        users = "U" + "sers"  # built so the name check reads no literal home path
        for body in (f"The cache sits at C:/{users}/a/cache.", f"The cache sits at C:\\{users}\\a\\cache.",
                     f"The cache sits at /c/{users}/a/cache."):
            with self.subTest(body), self.assertRaises(mem.MemError):
                self.m.add("fact", body, new=True)
        self.m.add("fact", "The notes live at https://example.test/x and in ~/Hub.", new=True)
        for body in ("Answer y:/n at the prompt.", "The ratio is w:/o here.", "The tree /a/" + "home/x is fine."):
            self.m.add("fact", body, new=True)  # a letter, a colon and a slash is no drive path

    def test_a_windows_test_run_command_is_recognised(self):
        for command in ('"C:\\hub\\.venv\\Scripts\\python.exe" -B -m pytest -q',
                        'C:\\hub\\.venv\\Scripts\\python.exe -m unittest',
                        '"C:\\Program Files\\Py\\python.exe" -B Drawer\\test_mem.py',
                        '"C:\\hub\\.venv\\Scripts\\pytest.exe" -q'):
            with self.subTest(command):
                self.assertTrue(mem.TEST_RUN_RE.match(command))
        self.assertFalse(mem.TEST_RUN_RE.match('"C:\\hub\\.venv\\Scripts\\python.exe" Drawer\\mem\\mem.py digest'))

    def test_the_process_table_comes_from_powershell_on_windows(self):
        done = mock.Mock(stdout="10 1 python -m pytest\n")
        with mock.patch.object(mem.oscompat, "WINDOWS", True), \
                mock.patch.object(mem.subprocess, "run", return_value=done) as run:
            self.assertEqual("10 1 python -m pytest\n", mem.process_table())
        self.assertEqual("powershell", run.call_args.args[0][0])
        self.assertEqual(mem.WIN_PS, run.call_args.args[0][-1])
        with mock.patch.object(mem.subprocess, "run", side_effect=OSError):
            self.assertEqual("", mem.process_table())

    def test_the_restore_hint_prints_a_windows_path_as_quoted_posix(self):
        with mock.patch.object(mem.oscompat, "WINDOWS", True):
            self.assertEqual("'C:/work/a b/hub.db'", mem.sh_arg(Path("C:/work/a b/hub.db")))
            self.assertEqual("C:/hub/hub.db", mem.sh_arg(Path("C:/hub/hub.db")))

    def test_an_env_file_with_a_bom_still_yields_the_key(self):
        env = Path(self.tmp.name) / ".env"
        env.write_bytes(b"\xef\xbb\xbfOPENAI_API_KEY=sk-test\r\n")
        with mock.patch.object(mem.harness_config, "path", return_value=str(env)):
            self.assertEqual("sk-test", mem.Embedder._key_from("x"))

    def test_an_incident_tagged_task_skips_the_lint(self):
        # A fault record is never lost: no form, a path and a date pass on an incident record.
        self.m.add("task", f"the sweep took {self.HOME}/tree on 2026-11-16 at sweep.py:12", tags=["incident"])
        with self.assertRaises(mem.MemError):
            self.m.add("task", f"the sweep took {self.HOME}/tree on 2026-11-16 at sweep.py:12", tags=["harness"],
                       new=True)

    def test_superseding_an_old_free_body_row_needs_a_form_body(self):
        # Old rows are not checked; a row that supersedes one is new, so it takes the lint.
        with mock.patch.object(mem, "lint_body"):
            old = self.m.add("task", "Old task written before the lint")
        with self.assertRaises(mem.MemError):
            self.m.add("task", "Old task written before the lint, now longer", supersedes=old["id"])
        new = self.m.add("task", "Old task: keep it. Why: it still counts. Done when: it ships.", supersedes=old["id"])
        self.assertEqual(old["id"], new["supersedes"])

    def test_incident_py_writes_the_form_with_no_date(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("incident", Path(__file__).with_name("incident.py"))
        incident = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(incident)
        run = mock.Mock(returncode=0, stdout="1a2b3c4d task\n", stderr="")
        with mock.patch.object(incident.subprocess, "run", return_value=run) as called:
            card, what = incident.open_record("5a033aed-0000", "INCIDENT the bell hung; impact: no wake; cause: a lock")
        self.assertEqual(("1a2b3c4d", "the bell hung"), (card, what))
        argv = called.call_args.args[0]
        body = argv[argv.index("--body") + 1]
        mem.lint_body("task", body)  # the form, no date: it passes even without the incident tag
        self.assertTrue(body.startswith("Incident: the bell hung, reported by session 5a033aed."), body)

    def test_a_lesson_closes_an_open_incident_record_and_a_tag_lists_it(self):
        # Docstring, Incident: a lesson added with --closes takes the tag closes:<id> and moves the incident to
        # done in one transaction; only a lesson closes, only an open incident record (tag incident) is closed.
        incident = self.m.add("task", "Incident: the watchdog dropped a DONE line", tags=["incident"])
        work = self.m.add("task", "Delete audit: audit every delete path. Why: x. Done when: y.", tags=["harness"])
        self.assertEqual([incident["id"]], [r["id"] for r in self.m.list(kind="task", tag="incident")])
        for kind, body, target, why in (("fact", "a fact", incident, "only a lesson"),
                                        ("lesson", "a lesson on audits", work, "no incident record")):
            with self.assertRaisesRegex(mem.MemError, why):
                self.m.add(kind, body, closes=target["id"][:8])
        lesson = self.m.add("lesson", "A delete must prove its target dead.", closes=incident["id"][:8])
        self.assertEqual([f"closes:{incident['id']}"], lesson["tags"])
        self.assertEqual("done", self.m.get(incident["id"])["status"])
        with self.assertRaisesRegex(mem.MemError, "already done"):
            self.m.add("lesson", "A second lesson on the sweep.", closes=incident["id"])

    def test_a_git_history_or_delete_incident_closes_only_with_a_lost_work_check_line(self):
        # Docstring, Incident: an incident that names git history or a delete closes only with a
        # 'LOST-WORK CHECK: <command or audit> -> <result>' line in the record or the lesson.
        rewrite = self.m.add("task", "Incident: a forced git filter-branch deleted refs/original/refs/heads/x",
                             tags=["incident"])
        tagged = self.m.add("task", "Incident: a sweep lost a peer branch", tags=["incident", "git-history"])
        for record in (rewrite, tagged):
            with self.assertRaisesRegex(mem.MemError, "LOST-WORK CHECK"):
                self.m.add("lesson", "Rewrite one ref only.", closes=record["id"])
            self.assertEqual("todo", self.m.get(record["id"])["status"])
        with self.assertRaisesRegex(mem.MemError, "LOST-WORK CHECK"):
            self.m.add("lesson", "Rewrite one ref only. LOST-WORK CHECK: none", closes=rewrite["id"])
        self.m.add("lesson", "Rewrite one ref only. LOST-WORK CHECK: git fsck --lost-found -> 0 lost commits",
                   closes=rewrite["id"])
        self.assertEqual("done", self.m.get(rewrite["id"])["status"])
        checked = self.m.add("task", "Incident: rm took a tree. LOST-WORK CHECK: audit of the tree -> all pushed",
                             tags=["incident"])
        self.m.add("lesson", "Prove a tree dead before rm.", closes=checked["id"])
        self.assertEqual("done", self.m.get(checked["id"])["status"])

    def test_keyword_fallback_without_embeddings(self):
        self.m.add("lesson", "Each worktree needs its own build cache.")
        self.m.embedder = FakeEmbedder(on=False)
        self.assertEqual(len(self.m.search("worktree cache")), 1)
        self.assertEqual(self.m.search("!!!"), [])

    def test_kind_filter(self):
        self.m.add("lesson", "Swipe feel needs tuning.")
        self.m.add("taste", "Keep: swipe feel snappy.")
        self.assertEqual([r["kind"] for r in self.m.search("swipe feel", kind="taste")], ["taste"])

    def test_digest_is_bounded_and_names_open_tasks(self):
        for i in range(40):
            self.m.add("task", f"Task {i}: do thing number {i} " + "word " * 30 + " Why: x. Done when: y.", new=True)
        for i in range(30):
            self.m.add("decision", f"Decision {i}: " + "long " * 80, new=True)
        d = self.m.digest()
        self.assertLessEqual(len(d), mem.DIGEST_CHARS)
        self.assertIn("OPEN TASKS (40)", d)

    def test_the_export_is_written_with_lf_and_replaced_through_oscompat(self):
        self.m.add("lesson", "A lesson for the LF export.")
        out = Path(self.tmp.name) / "lf.jsonl"
        with mock.patch.object(mem.oscompat, "replace", wraps=mem.oscompat.replace) as replace:
            self.m.export(out)
        replace.assert_called_once()
        self.assertNotIn(b"\r", out.read_bytes())

    def test_export_import_round_trip(self):
        old = self.m.add("lesson", "Old lesson text.")
        self.m.add("lesson", "New lesson text.", supersedes=old["id"])
        self.m.add("task", "Ship: send the build. Why: x. Done when: y.", priority="p1", topics=["release"])
        out = Path(self.tmp.name) / "cards.jsonl"
        self.assertEqual(self.m.export(out), 3)
        other = mem.Mem(Path(self.tmp.name) / "copy.db", FakeEmbedder())
        self.assertEqual(other.import_jsonl(out), 3)
        self.assertEqual(other.import_jsonl(out), 0)
        self.assertEqual(other.counts(), self.m.counts())
        self.assertEqual([json.loads(line) for line in out.read_text().splitlines()],
                         [other.get(r["id"]) for r in map(json.loads, out.read_text().splitlines())])
        other.db.close()

    def test_snapshot_keeps_newest(self):
        folder = Path(self.tmp.name) / "snaps"
        folder.mkdir()
        for day in ("2026-01-01", "2026-01-02"):
            (folder / f"hub-{day}.db").write_bytes(b"x")
        snap = self.m.snapshot(folder, keep=2)
        self.assertTrue(snap.is_file())
        self.assertEqual(len(list(folder.glob("hub-*.db"))), 2)

    def test_a_failed_snapshot_keeps_the_earlier_snapshot_of_the_day(self):
        folder = Path(self.tmp.name) / "snaps"
        first = self.m.snapshot(folder, keep=2)
        before = first.read_bytes()
        real = self.m.db

        class Refuses:
            def execute(self, sql, *args):
                if sql.startswith("VACUUM"):
                    raise mem.sqlite3.OperationalError("database or disk is full")
                return real.execute(sql, *args)

        self.m.db = Refuses()
        try:
            with self.assertRaises(mem.sqlite3.OperationalError):
                self.m.snapshot(folder, keep=2)
        finally:
            self.m.db = real
        self.assertEqual(before, first.read_bytes())
        self.assertEqual([first], list(folder.glob("hub-*")))

    def test_a_corrupt_snapshot_copy_keeps_the_earlier_snapshot_of_the_day(self):
        folder = Path(self.tmp.name) / "snaps"
        first = self.m.snapshot(folder, keep=2)
        before = first.read_bytes()
        real = self.m.db

        class Corrupts:
            def execute(self, sql, *args):
                done = real.execute(sql, *args)
                if sql.startswith("VACUUM"):
                    part = Path(args[0][0])
                    data = bytearray(part.read_bytes())
                    data[100:] = b"\xff" * (len(data) - 100)  # keep the header, scribble every page
                    part.write_bytes(bytes(data))
                return done

        self.m.db = Corrupts()
        try:
            with self.assertRaises(mem.sqlite3.DatabaseError):
                self.m.snapshot(folder, keep=2)
        finally:
            self.m.db = real
        self.assertEqual(before, first.read_bytes())
        self.assertEqual([first], list(folder.glob("hub-*")))

    def test_publish_export_commits_once_and_pushes_to_the_bare_remote(self):
        import subprocess
        folder, bare = Path(self.tmp.name) / "export", Path(self.tmp.name) / "backup.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        folder.mkdir()
        self.m.add("lesson", "Export me.")
        self.m.export(folder / "cards.jsonl")
        first = mem.publish_export(folder, str(bare))
        self.assertRegex(first, r"^[0-9a-f]{12} pushed$")
        self.assertEqual("unchanged", mem.publish_export(folder, None))
        log = subprocess.run(["git", "-C", str(bare), "log", "--oneline", "main"], capture_output=True, text=True)
        self.assertEqual(1, len(log.stdout.splitlines()))


RACE_CHILD = """
import json, os, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import mem
m = mem.Mem(sys.argv[2])
go = Path(sys.argv[4])
while not go.exists():
    time.sleep(0.001)
try:
    print(json.dumps({"claim_id": m.claim(sys.argv[3], sys.argv[5])["claim_id"]}))
except mem.Claimed as exc:
    print(json.dumps({"lost": str(exc)}))
"""


class RestateLintTest(unittest.TestCase):
    """A fact, taste or design that restates a rule file line is refused, naming the file."""

    RULE = ("Delete with trash, not rm. A cleanup that deletes must prove its target dead: no live session "
            "owns it, and a missing lock is no proof.")

    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory(ignore_cleanup_errors=True)))
        (self.tmp / "AGENTS.md").write_text(f"# Rules\n\n- {self.RULE}\n")
        self.m = mem.Mem(self.tmp / "hub.db", FakeEmbedder(on=False))
        self.m.rules_root = self.tmp
        self.addCleanup(self.m.db.close)

    def test_copied_fact_refused_naming_file(self):
        with self.assertRaisesRegex(mem.MemError, r"AGENTS\.md"):
            self.m.add("fact", self.RULE)

    def test_skill_file_counts(self):
        skill = self.tmp / ".claude" / "skills" / "qa"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("A qa session proves; it never fixes, and it names each failing screen by its title.\n")
        with self.assertRaisesRegex(mem.MemError, r"\.claude/skills/qa/SKILL\.md"):
            self.m.add("taste", "A qa session proves; it never fixes, and it names each failing screen by its title.")

    def test_same_body_as_lesson_or_decision_passes(self):
        self.assertEqual(self.m.add("lesson", self.RULE)["kind"], "lesson")
        self.assertEqual(self.m.add("decision", self.RULE, new=True)["kind"], "decision")

    def test_fresh_fact_passes(self):
        body = "The desktop export of the demo build takes about four minutes on a warm import cache."
        self.assertEqual(self.m.add("fact", body)["kind"], "fact")

    def test_short_quote_passes(self):
        self.assertEqual(self.m.add("fact", "Delete with trash, not rm.")["kind"], "fact")


class ClaimTest(unittest.TestCase):
    """Claim columns, task_log and an atomic claim."""

    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory(ignore_cleanup_errors=True)))
        self.path = self.tmp / "hub.db"
        self.m = mem.Mem(self.path, FakeEmbedder())
        self.addCleanup(self.m.db.close)
        self.task = self.m.add("task", "Lantern glow: wire the glow to dusk. Why: x. Done when: y.", priority="p1")

    def test_two_racing_claims_give_one_claim_id(self):
        # Two processes wait on one go-file, then claim the same task at once: exactly one gets a claim_id.
        env = {**os.environ, MEM_EMBED: "off"}
        for n in range(5):
            task = self.m.add("task", f"Race task {n}: claim me. Why: x. Done when: y.", new=True)
            go = self.tmp / f"go{n}"
            kids = [subprocess.Popen([sys.executable, "-B", "-c", RACE_CHILD, str(Path(mem.__file__).parent),
                                      str(self.path), task["id"], str(go), f"session-{k}"],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
                    for k in range(2)]
            time.sleep(1.5)  # both children open the DB and wait on the go-file
            go.touch()
            outs = [json.loads(kid.communicate(timeout=60)[0]) for kid in kids]
            won = [o["claim_id"] for o in outs if "claim_id" in o]
            self.assertEqual(1, len(won), outs)
            self.assertEqual(1, sum("lost" in o for o in outs), outs)
            self.assertIn("already claimed", next(o["lost"] for o in outs if "lost" in o))
            card = self.m.get(task["id"])
            self.assertEqual((won[0], "doing"), (card["claim_id"], card["status"]))
            self.assertEqual(["claim", "claim_refused"], [e["event"] for e in self.m.task_log(task["id"])])

    def test_a_refused_claim_writes_a_claim_refused_row(self):
        c = self.m.claim(self.task["id"], "session-a")
        with self.assertRaisesRegex(mem.Claimed, "already claimed by session session-a"):
            self.m.claim(self.task["id"], "session-b")
        log = self.m.task_log(self.task["id"])
        self.assertEqual(["claim", "claim_refused"], [e["event"] for e in log])
        self.assertEqual("session-b", log[-1]["session"])
        self.assertNotIn("claim_id", log[-1])
        self.assertIn("session-a", log[-1]["detail"])
        self.assertIn(c["claim_id"], log[-1]["detail"])
        card = self.m.get(self.task["id"])
        self.assertEqual((c["claim_id"], "session-a", "doing"),
                         (card["claim_id"], card["claim_session"], card["status"]))

    def test_a_today_schema_db_migrates_once(self):
        old = self.tmp / "today.db"
        db = mem.sqlite3.connect(old)
        db.executescript(mem.SCHEMA.split("CREATE TABLE IF NOT EXISTS task_log")[0])
        db.execute("INSERT INTO cards(id,kind,body,status,priority,created_at,sha256) "
                   "VALUES('11111111-0000-4000-8000-000000000000','task','Old task: keep me.','todo','p2','2026-01-01','x')")
        db.commit()
        db.close()
        said, connect = [], mem.sqlite3.connect

        def traced(*a, **k):
            conn = connect(*a, **k)
            conn.set_trace_callback(said.append)
            return conn
        with mock.patch.object(mem.sqlite3, "connect", traced):
            first = mem.Mem(old, FakeEmbedder())
            first.db.close()
            alters = sum(s.lstrip().upper().startswith("ALTER") for s in said)
            said.clear()
            second = mem.Mem(old, FakeEmbedder())
        self.addCleanup(second.db.close)
        self.assertEqual(len(mem.CLAIM_COLUMNS), alters)
        self.assertFalse([s for s in said if s.lstrip().upper().startswith("ALTER")])
        cols = {r[1] for r in second.db.execute("PRAGMA table_info(cards)")}
        self.assertTrue({c for c, _ in mem.CLAIM_COLUMNS} <= cols)
        self.assertEqual("Old task: keep me.", second.get("11111111")["body"])
        claimed = second.claim("11111111", "session-a")
        self.assertTrue(claimed["claim_id"])
        with self.assertRaises(mem.sqlite3.IntegrityError):  # the unique index on claim_id where set
            second.db.execute("INSERT INTO cards(id,kind,body,created_at,sha256,claim_id) "
                              "VALUES('x','task','b','t','s',?)", (claimed["claim_id"],))

    def test_release_needs_the_claim_id(self):
        c = self.m.claim(self.task["id"][:8], "session-a", model="opus", effort="high", worktree="/wt/a")
        self.assertEqual(("session-a", "opus", "high", "/wt/a"),
                         (c["claim_session"], c["run_model"], c["run_effort"], c["worktree"]))
        with self.assertRaisesRegex(mem.MemError, "not the live claim"):
            self.m.release(self.task["id"], "wrong-claim")
        self.assertEqual(c["claim_id"], self.m.get(self.task["id"])["claim_id"])
        r = self.m.release(self.task["id"], c["claim_id"], reason="handing it back")
        self.assertNotIn("claim_id", r)
        self.assertEqual(("todo", "/wt/a"), (r["status"], r["worktree"]))
        with self.assertRaisesRegex(mem.MemError, "holds no claim"):
            self.m.release(self.task["id"], c["claim_id"])
        self.assertEqual([("claim", c["claim_id"]), ("release", c["claim_id"])],
                         [(e["event"], e["claim_id"]) for e in self.m.task_log(self.task["id"])])
        self.assertNotEqual(c["claim_id"], self.m.claim(self.task["id"], "session-b")["claim_id"])

    def test_a_second_claim_is_refused(self):
        self.m.claim(self.task["id"], "session-a")
        with self.assertRaisesRegex(mem.Claimed, "already claimed by session session-a"):
            self.m.claim(self.task["id"], "session-b")

    def test_a_claim_refuses_a_non_task_a_closed_task_and_an_open_depends_on(self):
        fact = self.m.add("fact", "The lantern burns oil.")
        with self.assertRaisesRegex(mem.MemError, "only a task takes a claim"):
            self.m.claim(fact["id"], "session-a")
        with self.assertRaisesRegex(mem.MemError, "needs a session"):
            self.m.claim(self.task["id"], " ")
        dep = self.m.add("task", "Oil model: track lantern oil. Why: x. Done when: y.", new=True)
        blocked = self.m.add("task", "Dusk glow: needs oil. Why: x. Done when: y.", depends_on=[dep["id"]], new=True)
        with self.assertRaisesRegex(mem.MemError, f"depends on open task {dep['id']} \\(todo\\)"):
            self.m.claim(blocked["id"], "session-a")
        self.assertNotIn("claim_id", self.m.get(blocked["id"]))
        self.m.set_status(dep["id"], "done")
        self.assertTrue(self.m.claim(blocked["id"], "session-a")["claim_id"])
        self.m.set_status(self.task["id"], "dropped")
        with self.assertRaisesRegex(mem.MemError, "is dropped: only an open task"):
            self.m.claim(self.task["id"], "session-a")

    def test_a_superseded_refusal_names_the_live_id(self):
        mid = self.m.add("task", "Lantern glow v2: wire the glow to dusk and dawn. Why: x. Done when: y.", supersedes=self.task["id"])
        tip = self.m.add("task", "Lantern glow v3: wire the glow to the sky colour. Why: x. Done when: y.", supersedes=mid["id"])
        with self.assertRaisesRegex(mem.MemError, f"live card is {tip['id']}"):
            self.m.claim(self.task["id"], "session-a")

    def test_status_guards(self):
        c = self.m.claim(self.task["id"], "session-a")
        with self.assertRaisesRegex(mem.MemError, "is claimed by session session-a"):
            self.m.set_status(self.task["id"], "done")
        self.assertEqual("doing", self.m.get(self.task["id"])["status"])
        with self.assertRaisesRegex(mem.MemError, "is doing, not todo"):
            self.m.set_status(self.task["id"], "parked", expect="todo")
        self.m.release(self.task["id"], c["claim_id"])
        done = self.m.set_status(self.task["id"], "done", expect="todo")
        self.assertTrue(done["closed_at"])
        with self.assertRaisesRegex(mem.MemError, "reopening it to todo needs --reason"):
            self.m.set_status(self.task["id"], "todo")
        with self.assertRaisesRegex(mem.MemError, "needs --reason"):
            self.m.set_status(self.task["id"], "todo", reason="  ")
        back = self.m.set_status(self.task["id"], "todo", reason="the glow flickers again")
        self.assertNotIn("closed_at", back)
        self.assertEqual("done -> todo: the glow flickers again", self.m.task_log(self.task["id"])[-1]["detail"])
        incident = self.m.add("task", "Incident: the glow script crashed.", tags=["incident"], new=True)
        self.m.claim(incident["id"], "session-a")
        with self.assertRaisesRegex(mem.MemError, "is claimed"):
            self.m.add("lesson", "Guard the glow script.", closes=incident["id"])
        self.assertEqual("doing", self.m.get(incident["id"])["status"])

    def test_a_claim_that_lands_before_the_incident_close_lock_refuses_the_close(self):
        # Review finding: add(closes=) read the claim before its write lock, so a claim in between was set done.
        incident = self.m.add("task", "Incident: the bell script hung.", tags=["incident"], new=True)
        peer = mem.Mem(self.path, FakeEmbedder())
        self.addCleanup(peer.db.close)
        real = self.m.near_dup

        def claim_meanwhile(*a):
            peer.claim(incident["id"], "session-peer")
            return real(*a)
        with mock.patch.object(self.m, "near_dup", claim_meanwhile):
            with self.assertRaisesRegex(mem.MemError, "claimed or closed meanwhile"):
                self.m.add("lesson", "Time out the bell script.", closes=incident["id"])
        self.assertEqual(("doing", "session-peer"), tuple(self.m.get(incident["id"])[k] for k in ("status", "claim_session")))
        self.assertFalse(self.m.search("Time out the bell script", kind="lesson"))
        other = self.m.add("task", "Incident: the oil gauge froze.", tags=["incident"], new=True)
        self.m.add("lesson", "Thaw the oil gauge first.", closes=other["id"])
        closed = self.m.get(other["id"])
        self.assertTrue(closed["closed_at"])
        self.assertIn("-> done: closed by lesson", self.m.task_log(other["id"])[-1]["detail"])

    def test_mcp_set_status_shares_the_guard(self):
        tools = {}

        class FakeMCP:
            def __init__(self, name):
                pass

            def tool(self):
                return lambda fn: tools.setdefault(fn.__name__, fn)

            def run(self):
                pass
        fake = mock.MagicMock(FastMCP=FakeMCP)
        with mock.patch.dict(sys.modules, {"mcp": mock.MagicMock(), "mcp.server": mock.MagicMock(),
                                           "mcp.server.fastmcp": fake}):
            mem.serve_mcp(lambda: self.m)
        self.m.claim(self.task["id"], "session-a")
        with self.assertRaises(mem.MemError) as direct:
            self.m.set_status(self.task["id"], "done")
        self.assertEqual({"error": str(direct.exception)}, tools["set_status"](self.task["id"], "done"))
        self.assertEqual({"error": f"task {self.task['id']} is doing, not todo"},
                         tools["set_status"](self.task["id"], "parked", expect="todo"))
        self.assertEqual("parked", tools["set_status"](self.task["id"], "parked", expect="doing")["status"])

    def test_export_round_trips_the_claim_columns_and_task_log(self):
        c = self.m.claim(self.task["id"], "session-a", model="opus", effort="high", worktree="/wt/a")
        self.m.db.execute("UPDATE cards SET evidence=?, commits=?, delivered=1, closed_at=? WHERE id=?",
                          ("pushed abc1234 to main", json.dumps(["abc1234"]), "2026-11-16T00:00:00+00:00",
                           self.task["id"]))
        self.m.add("lesson", "Glow follows the sky.")
        out, again = self.tmp / "cards.jsonl", self.tmp / "again.jsonl"
        self.m.export(out)
        line = next(json.loads(x) for x in out.read_text().splitlines() if self.task["id"] in x)
        self.assertEqual((c["claim_id"], "session-a", ["abc1234"], 1, "/wt/a"),
                         (line["claim_id"], line["claim_session"], line["commits"], line["delivered"], line["worktree"]))
        self.assertEqual(["claim"], [e["event"] for e in line["log"]])
        other = mem.Mem(self.tmp / "copy.db", FakeEmbedder())
        self.addCleanup(other.db.close)
        self.assertEqual(2, other.import_jsonl(out))
        other.export(again)
        self.assertEqual(out.read_text(), again.read_text())
        self.assertEqual(self.m.task_log(self.task["id"]), other.task_log(self.task["id"]))
        with self.assertRaisesRegex(mem.MemError, "not the live claim"):
            other.release(self.task["id"], "wrong")
        self.assertEqual("todo", other.release(self.task["id"], c["claim_id"])["status"])


class ReadyRouteTest(unittest.TestCase):
    """get --follow, list --ready with chain, and checked route fields on add."""

    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory(ignore_cleanup_errors=True)))
        self.m = mem.Mem(self.tmp / "hub.db", FakeEmbedder(on=False))
        self.addCleanup(self.m.db.close)

    def task(self, name, *deps, **kw):
        return self.m.add("task", f"{name}: do {name}. Why: x. Done when: y.", depends_on=list(deps), new=True, **kw)

    def test_a_chain_gives_the_root_its_depth_and_leaves_blocked_tasks_out(self):
        a = self.task("Alpha", priority="p3")
        b = self.task("Beta", a["id"])
        c = self.task("Gamma", b["id"])
        solo = self.task("Solo", priority="p0", tags=["harness"])
        ready = self.m.ready()
        self.assertEqual([a["id"], solo["id"]], [r["id"] for r in ready])  # chain first, then priority
        self.assertEqual({a["id"]: 2, solo["id"]: 0}, {r["id"]: r["chain"] for r in ready})
        self.assertNotIn(b["id"], [r["id"] for r in ready])
        self.assertNotIn(c["id"], [r["id"] for r in ready])
        self.assertEqual([solo["id"]], [r["id"] for r in self.m.ready("harness")])
        self.m.set_status(a["id"], "done")
        self.assertEqual({b["id"]: 1, solo["id"]: 0}, {r["id"]: r["chain"] for r in self.m.ready()})

    def test_a_claimed_task_is_left_out(self):
        a = self.task("Alpha")
        self.m.claim(a["id"], "session-a")
        self.assertEqual([], self.m.ready())

    def test_a_dependent_on_a_superseded_card_follows_the_live_one(self):
        a = self.task("Alpha")
        b = self.task("Beta", a["id"])
        a2 = self.m.add("task", "Alpha: do it again. Why: x. Done when: y.", supersedes=a["id"])
        self.assertEqual({a2["id"]: 1}, {r["id"]: r["chain"] for r in self.m.ready()})
        self.m.set_status(a2["id"], "done")
        self.assertEqual([b["id"]], [r["id"] for r in self.m.ready()])

    def test_get_follow_returns_the_live_card(self):
        a = self.task("Alpha")
        a2 = self.m.add("task", "Alpha: second form. Why: x. Done when: y.", supersedes=a["id"])
        a3 = self.m.add("task", "Alpha: third form. Why: x. Done when: y.", supersedes=a2["id"])
        self.assertEqual(a["id"], self.m.get(a["id"])["id"])
        self.assertEqual(a3["id"], self.m.get(a["id"][:8], follow=True)["id"])

    def test_add_refuses_a_route_field_harness_toml_does_not_name(self):
        for kw, word in (({"model": "gpt"}, "model 'gpt'"), ({"effort": "low"}, "effort 'low'"),
                         ({"area": "foo"}, "area 'foo'")):
            with self.assertRaisesRegex(mem.MemError, word):
                self.task("Refused", **kw)
        ok = self.task("Routed", model="sonnet", effort="high", area="harness")
        self.assertEqual(("sonnet", "high", "harness"), (ok["model"], ok["effort"], ok["area"]))

    def test_the_cli_takes_ready_follow_and_route_flags(self):
        env = {**os.environ, MEM_DB: str(self.tmp / "cli.db"), MEM_EMBED: "off"}

        def run(*argv):
            return subprocess.run([sys.executable, "-B", str(Path(mem.__file__)), *argv, "--json"],
                                  capture_output=True, text=True, env=env)
        a = json.loads(run("add", "--kind", "task", "--body", "Alpha: x. Why: x. Done when: y.",
                           "--model", "opus", "--effort", "medium", "--area", "harness").stdout)
        b = json.loads(run("add", "--kind", "task", "--new", "--depends-on", a["id"],
                           "--body", "Beta: x. Why: x. Done when: y.").stdout)
        self.assertEqual([[a["id"], 1]], [[r["id"], r["chain"]] for r in json.loads(run("list", "--ready").stdout)])
        a2 = json.loads(run("add", "--kind", "task", "--supersedes", a["id"],
                            "--body", "Alpha: y. Why: x. Done when: y.").stdout)
        self.assertEqual(a2["id"], json.loads(run("get", a["id"], "--follow").stdout)["id"])
        bad = run("add", "--kind", "task", "--model", "gpt", "--body", "Gamma: x. Why: x. Done when: y.")
        self.assertEqual(2, bad.returncode)
        self.assertIn("allowed_models", json.loads(bad.stdout)["error"])
        self.assertTrue(b["id"])


class CompleteTest(unittest.TestCase):
    """Mem.complete is the checked close; each refusal leaves the task as it was."""

    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory(ignore_cleanup_errors=True)))
        self.m = mem.Mem(self.tmp / "hub.db", FakeEmbedder(on=False))
        self.addCleanup(self.m.db.close)
        self.task = self.m.add("task", "Lantern glow: wire the glow to dusk. Why: x. Done when: y.")["id"]
        self.claim = self.m.claim(self.task, "session-a")["claim_id"]
        self.delivered = {"delivered": True, "commits": [{"repo": "/r", "commit": "a1" * 20}]}

    def complete(self, **kw):
        args = dict(claim_id=self.claim, evidence="pushed a1a1a1a1a1 with the glow test", commits=[f"/r={'a1' * 20}"])
        args.update(kw)
        return self.m.complete(self.task, **args)

    def test_a_claim_that_moves_during_the_delivery_check_refuses_the_close(self):
        def moved(*a):
            other = mem.Mem(self.tmp / "hub.db", FakeEmbedder(on=False))
            other.release(self.task, self.claim)
            other.db.close()
            return self.delivered
        with mock.patch.object(task_delivery, "verify", moved), self.assertRaises(mem.MemError):
            self.complete()
        self.assertEqual("todo", self.m.get(self.task)["status"])
        self.assertNotIn("delivered", self.m.get(self.task))

    def test_a_refused_delivery_leaves_the_task_doing_and_writes_no_lesson(self):
        def refuse(*a):
            raise task_delivery.DeliveryError("commit a1a1a1a1 is not on origin/main")
        with mock.patch.object(task_delivery, "verify", refuse), self.assertRaises(mem.MemError) as caught:
            self.complete(lesson="The glow needs the sky clock.")
        self.assertIn("stays doing", str(caught.exception))
        self.assertEqual(("doing", self.claim), (self.m.get(self.task)["status"], self.m.get(self.task)["claim_id"]))
        self.assertEqual([], self.m.list(kind="lesson"))

    def test_a_close_writes_the_lesson_and_one_complete_log_row_in_one_step(self):
        with mock.patch.object(task_delivery, "verify", lambda *a: self.delivered):
            out = self.complete(lesson="The glow needs the sky clock.")
        self.assertEqual(("done", 1, [{"repo": "/r", "commit": "a1" * 20}]),
                         (out["status"], out["delivered"], out["commits"]))
        self.assertEqual([f"task:{self.task[:8]}"], self.m.get(out["lesson"])["topics"])
        self.assertEqual(["claim", "complete"], [e["event"] for e in self.m.task_log(self.task)])

    def cited_card(self):
        fixed = mem.uuidlib.UUID("5c1e0d2a-7b3f-4e91-a6c8-0d9e2f4b1a3c")
        with mock.patch.object(mem.uuidlib, "uuid4", lambda: fixed):
            return self.m.add("decision", "Dusk follows the sky clock. Why: one clock.")["id"]

    def test_a_lesson_may_name_a_full_card_uuid(self):
        cited = self.cited_card()
        with mock.patch.object(task_delivery, "verify", lambda *a: self.delivered):
            out = self.complete(lesson=f"The glow fix follows card {cited.upper()}; wire dusk to the sky clock.")
        self.assertEqual("done", out["status"])
        self.assertEqual([f"task:{self.task[:8]}"], self.m.get(out["lesson"])["topics"])

    def test_a_lesson_naming_a_uuid_of_no_card_is_refused(self):
        self.cited_card()
        with mock.patch.object(task_delivery, "verify", lambda *a: self.delivered), \
                self.assertRaises(mem.MemError) as caught:
            self.complete(lesson="The glow fix follows 5c1e0d2a-7b3f-4e91-a6c8-abcdef123456.")
        self.assertIn("names no commit id ('abcdef123456')", str(caught.exception))
        self.assertEqual("doing", self.m.get(self.task)["status"])

    def refuses_a_lesson_citing_the_prefix_of(self, sha):
        # A cited sha whose 9-char prefix is all digits (or all a-f) escaped the letter-and-digit scan.
        self.assertEqual(40, len(sha))
        delivered = {"delivered": True, "commits": [{"repo": "/r", "commit": sha}]}
        with mock.patch.object(task_delivery, "verify", lambda *a: delivered), \
                self.assertRaises(mem.MemError) as caught:
            self.complete(evidence=f"pushed {sha[:10]} with the glow test", commits=[f"/r={sha}"],
                          lesson=f"The glow fix is in {sha[:9]}; wire dusk to the sky clock.")
        self.assertIn(f"names no commit id ({sha[:9]!r})", str(caught.exception))
        self.assertEqual("doing", self.m.get(self.task)["status"])

    def test_a_lesson_naming_an_all_digit_prefix_of_a_cited_sha_is_refused(self):
        self.refuses_a_lesson_citing_the_prefix_of("123456789" + "a1" * 15 + "b")

    def test_a_lesson_naming_an_all_letter_prefix_of_a_cited_sha_is_refused(self):
        self.refuses_a_lesson_citing_the_prefix_of("abcdefabc" + "1" * 31)

    def test_mem_add_takes_a_lesson_that_names_a_full_card_uuid(self):
        cited = self.cited_card()
        row = self.m.add("lesson", f"The glow fix follows card {cited}; wire dusk to the sky clock.")
        self.assertEqual("lesson", row["kind"])

    def test_a_hex_word_with_no_digit_is_no_commit_id(self):
        self.assertIsNone(mem.COMMIT_ID.search("the effaced decade deadbeef"))
        self.assertTrue(mem.COMMIT_ID.search("see 1b57b98"))


class EvidenceLabelTest(unittest.TestCase):
    """WRK-4: a critical claim carries its evidence label; mem complete refuses one with none."""

    def test_each_critical_term_class_is_flagged_without_a_label(self):
        for text, term in (("Fixed the boot crash on load.", "crash"), ("The product crashed at dusk.", "crashed"),
                           ("No save loss after the patch.", "save loss"), ("A data-loss risk is gone.", "data-loss"),
                           ("It lost data on quit.", "lost data"), ("The blocker is fixed.", "blocker"),
                           ("It blocks play on the title.", "blocks play"), ("The gate fault is fixed.", "gate fault"),
                           ("A hook failure held the commit.", "hook failure"), ("The broken tool is mended.", "broken tool"),
                           ("The fix is in. The boot-crash is gone.", "crash")):
            with self.subTest(text=text):
                self.assertEqual([term], mem.unlabeled(text))

    def test_each_label_passes_the_same_claim(self):
        for label in ("[device]", "[sim]", "[test]", "[unverified]", "[TEST]"):
            with self.subTest(label=label):
                self.assertEqual([], mem.unlabeled(f"Pushed a1a1a1a1a1. {label} Fixed the boot crash on load."))

    def test_a_label_reaches_only_its_own_sentence(self):
        self.assertEqual(["crash"], mem.unlabeled("[test] The glow test is green. Fixed the boot crash."))
        self.assertEqual(["blocker"], mem.unlabeled("- [test] Smoke PASS. | The blocker is gone."))
        self.assertEqual(["crash"], mem.unlabeled("[sim] The glow works.\nNo crash on boot."))

    def test_text_with_no_critical_term_and_an_identifier_pass(self):
        self.assertEqual([], mem.unlabeled("Pushed a1a1a1a1a1 with the glow test; the gate and hook run green."))
        self.assertEqual([], mem.unlabeled("test_a_crash_case and blocked_rows pass at a1a1a1a1a1."))
        self.assertEqual([], mem.unlabeled(""))

    def test_complete_refuses_an_unlabeled_claim_and_leaves_the_task_doing(self):
        m = mem.Mem(Path(self.enterContext(tempfile.TemporaryDirectory(ignore_cleanup_errors=True))) / "hub.db", FakeEmbedder(on=False))
        self.addCleanup(m.db.close)
        task = m.add("task", "Boot fix: stop the boot crash. Why: x. Done when: y.")["id"]
        claim = m.claim(task, "session-a")["claim_id"]
        delivered = {"delivered": True, "commits": [{"repo": "/r", "commit": "a1" * 20}]}
        args = dict(claim_id=claim, commits=[f"/r={'a1' * 20}"])
        with mock.patch.object(task_delivery, "verify", lambda *a: delivered):
            with self.assertRaises(mem.MemError) as caught:
                m.complete(task, evidence="Pushed a1a1a1a1a1. Fixed the boot crash on load.", **args)
            self.assertIn("[device], [sim], [test] or [unverified]", str(caught.exception))
            self.assertEqual(("doing", claim), (m.get(task)["status"], m.get(task)["claim_id"]))
            out = m.complete(task, evidence="Pushed a1a1a1a1a1. [test] Fixed the boot crash on load.", **args)
        self.assertEqual("done", out["status"])


class LivenessTest(unittest.TestCase):
    """Interrupt and reap act only on a claim whose session has no live sessions row; notes."""

    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory(ignore_cleanup_errors=True)))
        self.home = self.tmp / "home"
        sessions = self.home / ".claude" / "sessions"
        sessions.mkdir(parents=True)
        (sessions / f"{os.getpid()}.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": "session-live"}))
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()  # a pid that no longer runs
        (sessions / f"{gone.pid}.json").write_text(json.dumps({"pid": gone.pid, "sessionId": "session-gone"}))
        self.env = {**os.environ, **harness_config.home_vars(self.home), MEM_DB: str(self.tmp / "hub.db"), MEM_EMBED: "off"}
        self.enterContext(mock.patch.dict(os.environ, harness_config.home_vars(self.home)))
        self.m = mem.Mem(self.tmp / "hub.db", FakeEmbedder())
        self.addCleanup(self.m.db.close)
        self.live = self.m.add("task", "Live claim: keep it. Why: x. Done when: y.")
        self.dead = self.m.add("task", "Dead claim: park it. Why: x. Done when: y.", new=True)
        self.gone = self.m.add("task", "Gone claim: park it too. Why: x. Done when: y.", new=True)
        self.c_live = self.m.claim(self.live["id"], "session-live", worktree="~/wt/live")
        self.c_dead = self.m.claim(self.dead["id"], "session-none", worktree="~/wt/dead")
        self.c_gone = self.m.claim(self.gone["id"], "session-gone")

    def cli(self, *argv):
        run = subprocess.run([sys.executable, "-B", str(Path(mem.__file__)), *argv, "--json"],
                             capture_output=True, text=True, env=self.env)
        return run.returncode, json.loads(run.stdout.splitlines()[0]) if run.stdout.strip() else run.stderr

    def test_interrupt_refuses_a_live_session_and_parks_a_dead_one(self):
        with self.assertRaisesRegex(mem.MemError, "live session session-live"):
            self.m.interrupt(self.live["id"])
        self.assertEqual(("doing", self.c_live["claim_id"]),
                         (self.m.get(self.live["id"])["status"], self.m.get(self.live["id"])["claim_id"]))
        r = self.m.interrupt(self.dead["id"][:8])
        self.assertEqual(("parked", "~/wt/dead"), (r["status"], r["worktree"]))
        self.assertNotIn("claim_id", r)
        self.assertNotIn("claim_session", r)
        log = [e for e in self.m.task_log(self.dead["id"]) if e["event"] == "interrupt"]
        self.assertEqual(1, len(log))
        self.assertEqual((self.c_dead["claim_id"], "session-none"), (log[0]["claim_id"], log[0]["session"]))
        self.assertEqual("parked", self.m.interrupt(self.gone["id"])["status"])  # a row whose pid is gone is dead
        with self.assertRaisesRegex(mem.MemError, "holds no claim"):
            self.m.interrupt(self.dead["id"])
        code, out = self.cli("interrupt", self.live["id"][:8])
        self.assertEqual(2, code)
        self.assertIn("live session", out["error"])

    def test_claim_state_reads_the_claude_config_dir(self):
        moved = self.tmp / "cfg"
        (moved / "sessions").mkdir(parents=True)
        (moved / "sessions" / "7.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": "session-none"}))
        row = self.m.get(self.dead["id"])
        self.assertEqual("dead", self.m.claim_state(row))
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(moved)}):
            self.assertEqual("live", self.m.claim_state(row))

    def test_reap_dry_run_changes_nothing_and_reap_parks_each_dead_claim(self):
        before = (self.m.list("task", all_rows=True), {t["id"]: self.m.task_log(t["id"])
                                                     for t in (self.live, self.dead, self.gone)})
        code, rows = self.cli("reap", "--dry-run")
        self.assertEqual(0, code, rows)
        self.assertEqual({self.dead["id"], self.gone["id"]}, {d["id"] for d in rows})
        self.assertEqual("~/wt/dead", next(d for d in rows if d["id"] == self.dead["id"])["worktree"])
        self.assertEqual(before, (self.m.list("task", all_rows=True), {t["id"]: self.m.task_log(t["id"])
                                                                     for t in (self.live, self.dead, self.gone)}))
        code, rows = self.cli("reap")
        self.assertEqual(0, code, rows)
        self.assertEqual({self.dead["id"], self.gone["id"]}, {d["id"] for d in rows})
        self.assertEqual(["doing", "parked", "parked"],
                         [self.m.get(t["id"])["status"] for t in (self.live, self.dead, self.gone)])
        self.assertEqual(self.c_live["claim_id"], self.m.get(self.live["id"])["claim_id"])
        self.assertEqual([], self.cli("reap", "--dry-run")[1])

    def assert_unknown_kept(self, task):
        """Liveness unknown fails closed: interrupt refuses, reap lists the claim as unknown and parks nothing."""
        claim_id = self.m.get(task["id"])["claim_id"]
        with self.assertRaisesRegex(mem.MemError, "liveness unknown"):
            self.m.interrupt(task["id"])
        for dry in (True, False):
            rows = {d["id"]: d for d in self.m.reap(dry_run=dry)}
            self.assertEqual("unknown", rows[task["id"]]["state"])
        self.assertEqual(("doing", claim_id), (self.m.get(task["id"])["status"], self.m.get(task["id"])["claim_id"]))

    def test_a_corrupt_session_row_fails_closed(self):
        (self.home / ".claude" / "sessions" / "99999.json").write_text("{not json")
        self.assert_unknown_kept(self.dead)
        self.assert_unknown_kept(self.gone)
        self.assertEqual("doing", self.m.get(self.live["id"])["status"])

    def test_a_missing_sessions_dir_fails_closed(self):
        import shutil
        shutil.rmtree(self.home / ".claude" / "sessions")
        self.assert_unknown_kept(self.dead)
        self.assert_unknown_kept(self.live)

    def test_a_claim_with_no_session_string_fails_closed(self):
        self.m.db.execute("UPDATE cards SET claim_session='' WHERE id=?", (self.dead["id"],))
        self.assertEqual("dead", {d["id"]: d["state"] for d in self.m.reap(dry_run=True)}[self.gone["id"]])
        self.assert_unknown_kept(self.dead)
        self.assertEqual("parked", self.m.get(self.gone["id"])["status"])  # the reap in the helper parks only dead

    def test_notes_since_returns_newer_rows_and_flags_a_stop(self):
        code, first = self.cli("note", self.live["id"][:8], "check the lantern test first", "--session", "flight-1")
        self.assertEqual(0, code, first)
        self.assertEqual(("check the lantern test first", False, "flight-1"),
                         (first["text"], first["stop"], first["session"]))
        stop = self.m.note(self.live["id"], "halt: the design changed", stop=True)
        self.assertTrue(stop["stop"])
        code, rows = self.cli("notes", self.live["id"][:8])
        self.assertEqual([(first["n"], False), (stop["n"], True)], [(n["n"], n["stop"]) for n in rows])
        code, rows = self.cli("notes", self.live["id"][:8], "--since", str(first["n"]))
        self.assertEqual([("halt: the design changed", True)], [(n["text"], n["stop"]) for n in rows])
        self.assertEqual([], self.m.notes(self.live["id"], stop["n"]))
        with self.assertRaisesRegex(mem.MemError, "needs text"):
            self.m.note(self.live["id"], "  ")
        code, log = self.cli("log", self.live["id"][:8])
        self.assertEqual(["claim", "note", "stop"], [e["event"] for e in log])
        self.env["CLAUDE_CODE_SESSION_ID"] = "session-env"
        self.assertEqual("session-env", self.cli("note", self.live["id"][:8], "from the env")[1]["session"])

    def test_a_note_that_starts_with_a_dash_reads_back(self):
        # A list-line note needs no '--' before it.
        for argv in (("- check the lantern test", "--session", "flight-1"),
                     ("--session", "flight-1", "-- two dashes, still text"), ("-x", "--stop")):
            code, n = self.cli("note", self.live["id"][:8], *argv)
            self.assertEqual(0, code, n)
            text = next(a for a in argv if a.startswith("-") and a not in ("--session", "--stop"))
            self.assertEqual(text, n["text"])
        rows = self.cli("notes", self.live["id"][:8])[1]
        self.assertEqual([("- check the lantern test", False), ("-- two dashes, still text", False), ("-x", True)],
                         [(r["text"], r["stop"]) for r in rows])
        self.assertEqual("flight-1", rows[0]["session"])


class JsonContractTest(unittest.TestCase):
    """Every verb takes --json and prints one compact JSON line; a MemError is {"error"}, exit 2."""

    def setUp(self):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory(ignore_cleanup_errors=True)))
        self.env = {**os.environ, MEM_DB: str(tmp / "hub.db"), MEM_EMBED: "off"}

    def run_mem(self, *argv):
        return subprocess.run([sys.executable, "-B", str(Path(mem.__file__)), *argv],
                              capture_output=True, text=True, env=self.env)

    def one_line(self, *argv):
        run = self.run_mem(*argv, "--json")
        self.assertEqual(0, run.returncode, run.stderr)
        lines = run.stdout.splitlines()
        self.assertEqual(1, len(lines), run.stdout)
        return json.loads(lines[0])

    def test_each_verb_prints_one_json_line(self):
        added = self.one_line("add", "--kind", "task", "--priority", "p2", "--body", "Lantern glow: wire it to dusk. Why: x. Done when: y.")
        self.assertEqual("task", added["kind"])
        self.assertEqual("Lantern glow: wire it to dusk. Why: x. Done when: y.", added["body"])
        self.assertEqual("doing", self.one_line("status", added["id"][:8], "doing")["status"])
        self.assertEqual(added["id"], self.one_line("get", added["id"][:8])["id"])
        self.assertEqual([added["id"]], [r["id"] for r in self.one_line("list", "--kind", "task")])
        self.assertIn(added["id"], [r["id"] for r in self.one_line("search", "lantern glow")])
        self.assertEqual({"task": 1}, self.one_line("counts"))
        self.assertIn("digest", self.one_line("digest"))

    def test_a_refusal_prints_its_error_on_stdout_and_exits_2(self):
        run = self.run_mem("get", "deadbeef", "--json")
        self.assertEqual(2, run.returncode)
        self.assertNotIn("no card matches", run.stderr)
        lines = run.stdout.splitlines()
        self.assertEqual(1, len(lines), run.stdout)
        self.assertEqual({"error": "no card matches 'deadbeef'"}, json.loads(lines[0]))

    def test_a_lost_claim_exits_3_and_release_needs_the_claim_id(self):
        added = self.one_line("add", "--kind", "task", "--body", "Dusk bell: ring it at sunset. Why: x. Done when: y.")
        won = self.one_line("claim", added["id"][:8], "--session", "session-a")
        self.assertTrue(won["claim_id"])
        lost = self.run_mem("claim", added["id"][:8], "--json", "--session", "session-b")
        self.assertEqual(3, lost.returncode, lost.stdout + lost.stderr)
        self.assertIn("already claimed by session session-a", json.loads(lost.stdout)["error"])
        refused = self.one_line("log", added["id"][:8])[-1]
        self.assertEqual(("claim_refused", "session-b"), (refused["event"], refused["session"]))
        done = self.run_mem("status", added["id"][:8], "done", "--json")
        self.assertEqual(2, done.returncode)
        self.assertIn("is claimed", json.loads(done.stdout)["error"])
        self.assertEqual(2, self.run_mem("release", added["id"][:8], "--claim-id", "nope", "--json").returncode)
        self.assertEqual("todo", self.one_line("release", added["id"][:8], "--claim-id", won["claim_id"])["status"])
        self.env["CLAUDE_CODE_SESSION_ID"] = "session-env"
        self.assertEqual("session-env", self.one_line("claim", added["id"][:8])["claim_session"])

    def test_text_mode_add_keeps_the_line_incident_py_reads(self):
        run = self.run_mem("add", "--kind", "task", "--tag", "incident", "--body", "A scratch fault for the regex.")
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertRegex(run.stdout, r"^[0-9a-f]{8} task\b")
        miss = self.run_mem("get", "deadbeef")
        self.assertEqual((2, ""), (miss.returncode, miss.stdout))
        self.assertIn("mem: no card matches", miss.stderr)


class EnvPrefixTest(unittest.TestCase):
    """MEM_DB and MEM_EMBED take the hub's env prefix; the bare names stop mem (they would open the live DB)."""

    def cli(self, env, *argv):
        base = {k: v for k, v in os.environ.items() if k not in (MEM_DB, MEM_EMBED, "MEM_DB", "MEM_EMBED")}
        return subprocess.run([sys.executable, "-B", str(Path(mem.__file__)), *argv], capture_output=True, text=True,
                              env={**base, **env})

    def test_the_prefixed_name_selects_the_db(self):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory(ignore_cleanup_errors=True)))
        added = self.cli({MEM_DB: str(tmp / "chosen.db"), MEM_EMBED: "off"}, "add", "--kind", "fact", "--new", "--body",
                         "A fact for the chosen store.")
        self.assertEqual(0, added.returncode, added.stderr)
        store = mem.Mem(tmp / "chosen.db", FakeEmbedder(on=False))
        self.addCleanup(store.db.close)
        self.assertEqual("A fact for the chosen store.", store.get(added.stdout.split()[0])["body"])

    def test_a_bare_name_with_no_prefixed_one_is_refused_and_opens_no_db(self):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory(ignore_cleanup_errors=True)))
        for bare in ("MEM_DB", "MEM_EMBED"):
            with self.subTest(bare):
                env = {"MEM_EMBED": "off", MEM_DB: str(tmp / "set.db")} if bare == "MEM_EMBED" else {
                    "MEM_DB": str(tmp / "bare.db"), MEM_EMBED: "off"}
                run = self.cli(env, "add", "--kind", "fact", "--new", "--body", "A fact the bare name must not store.")
                self.assertNotEqual(0, run.returncode)
                self.assertIn(f"{bare} is no longer read; set {mem.harness_config.env_name(bare)}", run.stderr)
                self.assertFalse((tmp / "bare.db").exists() or (tmp / "set.db").exists())


class LiveGuardTest(unittest.TestCase):
    def test_a_test_run_writes_a_scratch_store_by_default_and_never_opens_the_live_one(self):
        # A watchdog test once ran mem with no MEM_DB and opened a record in the live
        # memory. Under a test run, mem with no MEM_DB writes a scratch DB per run, never the live one, and
        # opening the live DB fails. The live DB here is a fake one under a scratch HOME.
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory(ignore_cleanup_errors=True)))
        env = {k: v for k, v in os.environ.items() if k != MEM_DB}
        env.update({**harness_config.home_vars(tmp), MEM_EMBED: "off"})
        live = Path(LIVE_DB.replace("~", str(tmp), 1))
        run = mem.test_run()
        self.assertIsNotNone(run)
        cli = [sys.executable, "-B", str(Path(mem.__file__)), "add", "--kind", "fact", "--new", "--body"]
        added = subprocess.run(cli + ["A test fact for the scratch store."], capture_output=True, text=True, env=env)
        self.assertEqual(0, added.returncode, added.stderr)
        self.assertFalse(live.exists(), "a test run wrote the live mem database")
        scratch = mem.Mem(Path(tempfile.gettempdir()) / "hub-test-mem" / f"{run}.db", FakeEmbedder(on=False))
        self.assertEqual("A test fact for the scratch store.", scratch.get(added.stdout.split()[0])["body"])
        scratch.db.close()
        named = subprocess.run(cli + ["A test fact for the live store."], capture_output=True, text=True,
                               env={**env, MEM_DB: str(live)})
        self.assertNotEqual(0, named.returncode)
        self.assertIn("LiveUnderTest", named.stderr)
        self.assertFalse(live.exists(), "a test run wrote the live mem database")
        with mock.patch.dict(os.environ, harness_config.home_vars(tmp)), self.assertRaises(mem.LiveUnderTest):
            mem.Mem(live, FakeEmbedder())



class LiveCheckoutGuardTest(unittest.TestCase):
    """mem.py from a task worktree once opened the live DB with new code and migrated its schema
    before land, with no snapshot. The live DB and the live hub here sit under a scratch HOME."""

    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory(ignore_cleanup_errors=True)))
        self.enterContext(mock.patch.dict(os.environ, harness_config.home_vars(self.home)))
        os.environ.pop("MEM_LIVE_WRITE", None)
        self.enterContext(mock.patch.object(mem, "test_run", return_value=None))
        self.live = Path(LIVE_DB.replace("~", str(self.home), 1))
        self.live.parent.mkdir(parents=True)
        old = mem.sqlite3.connect(self.live)  # the schema before the claim columns
        old.executescript(mem.SCHEMA)
        old.execute("INSERT INTO cards(id,kind,body,created_at,sha256) VALUES('c1','fact','an old row','now','x')")
        old.commit()
        old.close()
        self.hub = self.home / "Hub"  # the fixture's live hub: the main clone this code's checkout names
        self.enterContext(mock.patch.object(mem, "live_hub", return_value=self.hub))
        self.cli = self.hub / "Drawer" / "mem" / "mem.py"  # the live hub's mem.py
        self.cli.parent.mkdir(parents=True)
        self.cli.write_text("")

    def columns(self, db):
        con = mem.sqlite3.connect(db)
        try:
            return {r[1] for r in con.execute("PRAGMA table_info(cards)")}
        finally:
            con.close()

    def snapshots(self):
        return sorted((self.live.parent / "mem-snapshots").glob("premigrate-hub-*.db"))

    def test_code_outside_the_live_hub_neither_migrates_nor_writes_the_live_db(self):
        m = mem.Mem(self.live, FakeEmbedder())
        self.addCleanup(m.db.close)
        self.assertNotIn("claim_id", self.columns(self.live), "a worktree mem.py migrated the live DB")
        self.assertEqual("an old row", m.db.execute("SELECT body FROM cards WHERE id='c1'").fetchone()[0])
        with self.assertRaises(mem.LiveReadOnly):
            m.add("fact", "A fact a worktree must not write to the live DB.", new=True)
        self.assertEqual(1, m.db.execute("SELECT count(*) FROM cards").fetchone()[0])
        self.assertEqual([], self.snapshots())

    def test_the_named_override_migrates_the_live_db_after_a_snapshot(self):
        with mock.patch.dict(os.environ, {"MEM_LIVE_WRITE": "1"}):
            mem.Mem(self.live, FakeEmbedder()).db.close()
        self.assertIn("claim_id", self.columns(self.live))
        [snap] = self.snapshots()
        self.assertNotIn("claim_id", self.columns(snap), "the snapshot holds the schema before the migration")

    def test_the_live_hub_snapshots_before_a_migration_and_only_then(self):
        with mock.patch.object(mem, "this_hub", return_value=self.hub):
            m = mem.Mem(self.live, FakeEmbedder())
            self.assertIn("claim_id", self.columns(self.live))
            [snap] = self.snapshots()
            self.assertEqual(snap, m.premigrate_snapshot)
            self.assertNotIn("claim_id", self.columns(snap))
            con = mem.sqlite3.connect(snap)
            self.assertEqual("an old row", con.execute("SELECT body FROM cards WHERE id='c1'").fetchone()[0])
            con.close()
            m.add("fact", "A fact the live hub writes after its migration.", new=True)
            m.db.close()
            again = mem.Mem(self.live, FakeEmbedder())  # no schema change: no second snapshot
            self.assertIsNone(again.premigrate_snapshot)
            again.db.close()
        self.assertEqual(1, len(self.snapshots()))

    def test_with_no_live_hub_there_is_nothing_to_guard(self):
        self.cli.unlink()  # a fixture HOME: no live hub, so no live code the open could run ahead of
        mem.Mem(self.live, FakeEmbedder()).db.close()
        self.assertIn("claim_id", self.columns(self.live))

    def test_the_cli_outside_the_live_hub_runs_the_live_hub_mem(self):
        env = {k: v for k, v in os.environ.items() if k != MEM_DB}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(mem.subprocess, "run",
                                  return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertEqual(0, mem.main(["list", "--json"]))
        run.assert_called_once_with([sys.executable, "-B", str(self.cli), "list", "--json"])
        self.assertNotIn("claim_id", self.columns(self.live))


class LiveHubTest(unittest.TestCase):
    """The live hub is the main worktree of the checkout that holds mem.py when it holds the marker file, not a
    folder name under HOME: a task worktree names its main clone, a clone in any folder names itself."""

    def test_a_task_worktree_names_its_main_clone(self):
        marker = mem.harness_config.load(mem.harness_config.hub_root(mem.HERE))["project"]["marker_file"]
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory(ignore_cleanup_errors=True))).resolve()
        main, wt, plain = tmp / "AnyName", tmp / "AnyName_wt" / "t1", tmp / "plain"
        env = mem.harness_config.git_env()
        git = lambda *a: subprocess.run(["git", *a], check=True, capture_output=True, env=env)  # noqa: E731
        git("init", "-q", "-b", "main", str(main))
        (main / marker).write_text("")
        git("-C", str(main), "add", marker)
        git("-C", str(main), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "m")
        git("-C", str(main), "worktree", "add", "-q", "-b", "t1", str(wt))
        plain.mkdir()
        self.assertEqual(main, mem.live_hub(main).resolve())
        self.assertEqual(main, mem.live_hub(wt).resolve())
        self.assertEqual(plain, mem.live_hub(plain).resolve())  # no Git checkout: itself
        (main / marker).unlink()
        self.assertEqual(wt, mem.live_hub(wt).resolve())  # a main worktree with no marker is no hub


class SeedAndReembedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.emb = FakeEmbedder(on=False)
        self.m = mem.Mem(Path(self.tmp.name) / "hub.db", self.emb)
        self.addCleanup(self.m.db.close)

    def test_cards_added_with_no_key_get_vectors_from_reembed(self):
        a = self.m.add("fact", "The lantern ferret hums teal at dusk.")
        b = self.m.add("lesson", "Cold glass rings louder than warm glass.")
        self.assertEqual(2, len(self.m.unembedded()))
        with self.assertRaises(mem.MemError):  # no embedding: refused, nothing written
            self.m.reembed()
        self.emb.on = True
        self.assertEqual(2, self.m.reembed())
        self.assertEqual([], self.m.unembedded())
        self.assertEqual(0, self.m.reembed())
        vec = self.emb.embed(["teal ferret lantern"])[0]
        near = [r[0] for r in self.m.db.execute(
            "SELECT c.id FROM (SELECT rowid, distance FROM vec_cards WHERE embedding MATCH ? AND k = 2) v "
            "JOIN cards c ON c.rowid = v.rowid ORDER BY v.distance", (json.dumps(vec),))]
        self.assertEqual([a["id"], b["id"]], near)

    def test_the_starter_seed_loads_only_into_an_empty_db(self):
        out = self.m.seed()
        self.assertEqual(5, out["imported"])
        self.assertEqual({"design": 1, "taste": 3, "task": 1}, self.m.counts())
        [task] = self.m.ready()
        self.assertEqual("harness", task["area"])
        self.assertIn("starter", task["tags"])  # a hub-only task: area harness, no product repo
        again = self.m.seed()
        self.assertEqual(0, again["imported"])
        self.assertIn("skipped", again)

    def test_a_db_with_cards_takes_no_seed(self):
        self.m.add("fact", "A card the human wrote first.")
        self.assertEqual(0, self.m.seed()["imported"])
        self.assertEqual({"fact": 1}, self.m.counts())

    def test_each_seed_card_passes_the_add_lint(self):
        root = mem.harness_config.hub_root(mem.HERE)
        for line in mem.SEED.read_text(encoding="utf-8").splitlines():
            c = json.loads(line)
            with self.subTest(c["id"]):
                mem.lint_body(c["kind"], c["body"], c.get("tags", []))
                mem.lint_restate(c["kind"], c["body"], root)
                mem.check_route(c.get("model"), c.get("effort"), c.get("area"))
                self.assertEqual({"id", "kind", "body", "created_at"} - set(c), set())

    def test_doctor_refuses_a_db_another_hub_owns(self):
        # Two hubs with one slug on one box share the default roots: the second hub's doctor must not take the
        # first hub's DB as its own.
        base = Path(self.tmp.name)
        db, first, second = base / "shared" / "hub.db", base / "first", base / "second"
        for hub in (first, second):
            hub.mkdir()
            (hub / "harness.toml").write_text("[project]\n")
        with mock.patch.dict(os.environ, {MEM_EMBED: "off"}):
            ok, line = mem.doctor(db, hub=first)
            self.assertTrue(ok, line)
            ok, line = mem.doctor(db, hub=second)
            self.assertFalse(ok, line)
            self.assertTrue(line.startswith("FAIL"), line)
            self.assertIn(str(first.resolve()), line)
            self.assertIn("harness.local.toml", line)
            self.assertTrue(mem.doctor(db, hub=first)[0])
            (first / "harness.toml").unlink()  # the first hub is gone: its DB is free to take
            ok, line = mem.doctor(db, hub=second)
            self.assertTrue(ok, line)
            self.assertFalse(mem.doctor(db, hub=first)[0])

    def test_doctor_names_keyword_only_with_no_key(self):
        with mock.patch.dict(os.environ, {MEM_EMBED: "off"}):
            ok, line = mem.doctor(Path(self.tmp.name) / "doctor.db")
        self.assertTrue(ok, line)
        self.assertTrue(line.startswith("OK"), line)
        self.assertIn("keyword-only", line)
        self.assertTrue((Path(self.tmp.name) / "doctor.db").is_file())

    def doctor_line(self, name, export_dir=None, cards=1):
        db = Path(self.tmp.name) / name
        if cards:
            mem.Mem(db, FakeEmbedder()).add("fact", "A card for the doctor.")
        with mock.patch.dict(os.environ, {MEM_EMBED: "off"}):
            return mem.doctor(db, export_dir=export_dir)

    def test_doctor_warns_when_the_nightly_export_is_stale_or_missing(self):
        export = Path(self.tmp.name) / "export"
        export.mkdir()
        ok, line = self.doctor_line("missing.db", export)
        self.assertTrue(ok, line)
        self.assertTrue(line.startswith("WARN mem doctor:"), line)
        self.assertIn("nightly export is missing", line)
        (export / "cards.jsonl").write_text("")
        ok, line = self.doctor_line("fresh.db", export)
        self.assertTrue(line.startswith("OK mem doctor:"), line)
        old = time.time() - 30 * 3600
        os.utime(export / "cards.jsonl", (old, old))
        ok, line = self.doctor_line("stale.db", export)
        self.assertTrue(ok and line.startswith("WARN mem doctor:"), line)
        self.assertIn("nightly export is 30 h old", line)
        ok, line = self.doctor_line("empty.db", export / "none", cards=0)  # a fresh DB has nothing to back up yet
        self.assertTrue(line.startswith("OK mem doctor:"), line)

    def test_doctor_warns_when_a_set_openai_key_gets_a_401(self):
        class Refused(Exception):
            status_code = 401

        class Client:
            def __init__(self, **keywords):
                self.embeddings = self

            def create(self, **keywords):
                raise Refused("Incorrect API key provided")
        db = Path(self.tmp.name) / "key.db"
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-bogus-0000"}), \
             mock.patch.dict(os.environ, {MEM_EMBED: ""}), mock.patch("openai.OpenAI", Client):
            ok, line = mem.doctor(db)
        self.assertTrue(ok, line)
        self.assertTrue(line.startswith("WARN mem doctor:"), line)
        self.assertIn("API refused it (401)", line)

    def test_nightly_if_stale_skips_a_fresh_export_and_runs_a_stale_one(self):
        base = Path(self.tmp.name)
        export, db = base / "export", base / "night.db"
        cfg = {**mem.config(), "export_dir": str(export), "keep_snapshots": 2}
        with mock.patch.dict(os.environ, {MEM_DB: str(db), MEM_EMBED: "off"}), \
             mock.patch.object(mem, "config", return_value=cfg):
            self.assertEqual(0, mem.main(["add", "--kind", "fact", "--body", "A card to export.", "--json"]))
            self.assertEqual(0, mem.main(["nightly", "--if-stale", "--json"]))  # no export yet: runs
            exported = export / "cards.jsonl"
            self.assertTrue(exported.is_file())
            old = time.time() - 30 * 3600
            os.utime(exported, (old, old))
            self.assertEqual(0, mem.main(["nightly", "--if-stale", "--json"]))  # stale: runs again
            self.assertGreater(exported.stat().st_mtime, old + 3600)
            before = exported.stat().st_mtime
            time.sleep(0.05)
            self.assertEqual(0, mem.main(["nightly", "--if-stale", "--json"]))  # fresh: skipped
            self.assertEqual(before, exported.stat().st_mtime)


class LostDbTest(unittest.TestCase):
    """A lost or corrupt memory DB is named at boot with the restore command, and an empty DB never overwrites the export."""
    IMPORT = ".venv/bin/python -B Drawer/mem/mem.py import "

    @staticmethod
    def word(path):
        """A path as the restore line writes it: C:/x/y on Windows, quoted when the shell reads a character in it (a
        runner's 8.3 temp folder RUNNER~1 holds a tilde); a POSIX path as it is."""
        return shlex.quote(Path(path).as_posix()) if os.name == "nt" else str(path)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        base = Path(self.tmp.name)
        self.db, self.export, self.snaps = base / "hub.db", base / "export", base / "mem-snapshots"
        self.cfg = {**mem.config(), "export_dir": str(self.export)}

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {MEM_DB: str(self.db), MEM_EMBED: "off"}), \
             mock.patch.object(mem, "config", return_value=self.cfg), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = mem.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def backup(self):
        """Fill a DB with one card, export it (the nightly's backup) and drop the DB."""
        self.assertEqual(0, self.cli("add", "--kind", "fact", "--body", "A card worth restoring.")[0])
        self.assertEqual(0, self.cli("nightly")[0])
        for part in self.db.parent.glob("hub.db*"):
            part.unlink()

    def test_a_missing_db_with_a_backup_prints_one_restore_line(self):
        self.backup()
        code, out, _ = self.cli("digest")
        self.assertEqual(0, code)
        lines = [ln for ln in out.splitlines() if "Restore:" in ln]
        self.assertEqual(1, len(lines), out)
        self.assertIn(self.IMPORT + self.word(self.export / "cards.jsonl"), lines[0])

    def test_a_missing_db_with_only_a_snapshot_names_the_copy(self):
        self.backup()
        (self.export / "cards.jsonl").unlink()
        self.assertEqual(0, self.cli("add", "--kind", "fact", "--body", "A card in a snapshot.")[0])
        snap = Path(json.loads(self.cli("snapshot", "--json")[1])["snapshot"])
        self.db.unlink()
        _, out, _ = self.cli("digest")
        lines = [ln for ln in out.splitlines() if "Restore:" in ln]
        self.assertEqual(1, len(lines), out)
        self.assertIn(f"cp {self.word(snap)} {self.word(self.db)}", lines[0])

    def test_a_fresh_clone_with_no_backup_prints_no_restore_line(self):
        _, out, _ = self.cli("digest")
        self.assertNotIn("Restore:", out)

    def test_a_db_with_cards_prints_no_restore_line(self):
        self.backup()
        self.assertEqual(0, self.cli("add", "--kind", "fact", "--body", "A new card.")[0])
        _, out, _ = self.cli("digest")
        self.assertNotIn("Restore:", out)

    def test_a_corrupt_db_names_the_restore_in_one_stderr_line(self):
        self.backup()
        self.db.write_bytes(b"garbage, not a database " * 40)
        code, out, err = self.cli("digest")
        self.assertEqual(2, code)
        err = [ln for ln in err.splitlines() if ln.startswith("mem:")]  # a ResourceWarning may share stderr
        self.assertEqual(1, len(err), err)
        err = err[0]
        self.assertIn("file is not a database", err)
        self.assertIn(f"mv {self.word(self.db)} {self.word(f'{self.db}.bad')}; {self.IMPORT}{self.word(self.export / 'cards.jsonl')}", err)

    def test_a_locked_db_is_not_called_corrupt(self):
        with mock.patch.object(mem, "Mem", side_effect=mem.sqlite3.OperationalError("database is locked")):
            with self.assertRaises(mem.sqlite3.OperationalError):
                self.cli("digest")

    def test_nightly_with_an_empty_db_keeps_the_export_and_names_the_restore(self):
        self.backup()
        exported = self.export / "cards.jsonl"
        before = exported.read_text()
        code, _, err = self.cli("nightly")
        self.assertEqual(2, code)
        self.assertEqual(before, exported.read_text())
        self.assertIn(self.IMPORT + self.word(exported), err)

    def test_export_with_an_empty_db_keeps_a_non_empty_export(self):
        self.backup()
        exported = self.export / "cards.jsonl"
        before = exported.read_text()
        self.assertEqual(2, self.cli("export")[0])
        self.assertEqual(before, exported.read_text())

    def starter_only_with_a_bigger_backup(self):
        """A lost DB that setup seeded at once: the starter cards only, while the export holds one more card."""
        self.assertEqual(0, self.cli("seed")[0])
        self.assertEqual(0, self.cli("add", "--kind", "fact", "--body", "A card worth restoring.")[0])
        self.assertEqual(0, self.cli("nightly")[0])
        for part in self.db.parent.glob("hub.db*"):
            part.unlink()
        self.assertEqual(0, self.cli("seed")[0])
        return self.export / "cards.jsonl"

    def test_a_seeded_db_with_a_bigger_backup_opens_the_digest_with_the_restore(self):
        exported = self.starter_only_with_a_bigger_backup()
        code, out, _ = self.cli("digest")
        self.assertEqual(0, code)
        self.assertTrue(out.startswith("MEMORY DB HOLDS ONLY THE STARTER CARDS, but a backup holds more. Restore: "), out)
        self.assertEqual(1, len([ln for ln in out.splitlines() if "Restore:" in ln]), out)
        self.assertIn(self.IMPORT + self.word(exported), out)

    def test_nightly_and_export_of_a_seeded_db_keep_a_bigger_export(self):
        exported = self.starter_only_with_a_bigger_backup()
        before = exported.read_text()
        for verb in ("nightly", "export"):
            code, _, err = self.cli(verb)
            self.assertEqual(2, code, verb)
            self.assertIn(self.IMPORT + self.word(exported), err)
            self.assertIn("only the starter cards", err)
            self.assertEqual(before, exported.read_text(), verb)

    def test_a_seeded_db_with_a_starter_only_export_or_its_own_card_prints_no_restore_line(self):
        self.assertEqual(0, self.cli("seed")[0])
        self.assertEqual(0, self.cli("nightly")[0])  # the export holds the starter cards and no more
        self.assertNotIn("Restore:", self.cli("digest")[1])
        self.assertEqual(0, self.cli("nightly")[0])  # and an export of a seeded DB over it is allowed
        self.assertEqual(0, self.cli("add", "--kind", "fact", "--body", "My own card.")[0])
        self.assertEqual(0, self.cli("nightly")[0])
        self.assertNotIn("Restore:", self.cli("digest")[1])

    def test_an_empty_db_may_write_the_first_export(self):
        self.assertEqual(0, self.cli("nightly")[0])
        self.assertEqual("", (self.export / "cards.jsonl").read_text())


class IncidentLogGuardTest(unittest.TestCase):
    """A test that reaches the incidents.jsonl writer keeps out of the live file."""
    INCIDENT_CASES = ("test_incident_py_writes_the_form_with_no_date",)

    def test_the_incident_cases_leave_the_live_incidents_jsonl_unchanged(self):
        import harness_config
        var = harness_config.env_name("INCIDENTS")
        real = harness_config.path(harness_config.load(Path(__file__).resolve().parent.parent)["project"]["state_root"])
        real_file = real / "incidents.jsonl"
        before = real_file.read_bytes() if real_file.exists() else None
        real_load = harness_config.load
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as live:
            def load(*a, **k):  # the run's config names a scratch live state_root: a write there is a live write
                cfg = real_load(*a, **k)
                return {**cfg, "project": {**cfg["project"], "state_root": live}}
            env = {k: v for k, v in os.environ.items() if k != var}
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(harness_config, "load", load):
                result = unittest.TestResult()
                unittest.TestSuite(MemTest(name) for name in self.INCIDENT_CASES).run(result)
            self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
            self.assertFalse((Path(live) / "incidents.jsonl").exists(), "an incident case wrote the live incidents.jsonl")
        self.assertEqual(before, real_file.read_bytes() if real_file.exists() else None)


if __name__ == "__main__":
    unittest.main()
