"""Selftest for Drawer/doc-gate.py (run: .venv/bin/python -B -m pytest -q Drawer/test_doc_gate.py).
Judges only; writes nothing in the checkout.

Every child runs a PRIVATE COPY of the gate in a private hub (setUpModule), so the gate's
box-local log beside its own file is the private hub's, never this seat's.

Every expected value is the contract written out -- the gate's docstring and its refusal texts.
Nothing is read back from Drawer/doc-gate.py, so a constant changed by mistake cannot move the
test with it.
"""
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

# realpath: a checkout reached through a symlink (a probe worktree under /tmp on macOS) must give
# the gate the same hub root the gate computes from its own file, or every path reads as outside
# the hub and the gate passes everything.
HERE = os.path.dirname(os.path.realpath(__file__))
# The files the child imports: the gate, the dispatcher and the shared body they load.
SOURCES = ("doc-gate.py", "product_worktree.py", "gatelib.py", "harness_config.py", "oscompat.py", "project-gate.py", "rules_check.py",
           "rules_audit.py")

BOX = None   # the private hub's temp dir; setUpModule fills these in
HUB = ""     # the private hub root the child judges against
GATE = ""    # the private copy of doc-gate.py the child runs
BASE_ENV = {}


def setUpModule():
    """One private hub for the run: a Drawer holding only the files the gate imports."""
    global BOX, HUB, GATE, BASE_ENV
    BOX = tempfile.TemporaryDirectory(prefix="hub-doc-gate-box-")
    HUB = os.path.join(os.path.realpath(BOX.name), "hub")
    drawer = os.path.join(HUB, "Drawer")
    os.makedirs(drawer)
    for name in SOURCES:
        shutil.copy(os.path.join(HERE, name), os.path.join(drawer, name))
    shutil.copy(os.path.join(os.path.dirname(HERE), "harness.toml"), os.path.join(HUB, "harness.toml"))
    GATE = os.path.join(drawer, "doc-gate.py")
    # The hook's whole environment: what doc-gate.py reads and nothing else. A copied parent
    # environment would let a seat's own variables decide the verdict.
    BASE_ENV = {"DOC_GATE": "dry", "CLAUDE_PROJECT_DIR": HUB, "PYTHONDONTWRITEBYTECODE": "1"}
    if os.name == "nt":  # a Windows interpreter needs its system root to start
        BASE_ENV["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")


def tearDownModule():
    BOX.cleanup()


# A corner folder no seat carries, so a fixture path can never collide with a real corner.
ABSENT_CORNER = "CrawlFixture"

# -- the contract: the refusal bodies the gate gives an agent ---------------------------------
REFUSAL_HIDE = "doc-gate: %s is %s. Fix: delete it, or put what lasts in memory (mem add)."
WHY_HIDING = "a hiding place (attic, archive, backup, rejected, old, copy)"
WHY_BACKUP_NAME = "a backup-named file (.bak, .orig, -old, -copy, -rejected, sync-conflict)"
WHY_ARCHIVE = "an archive file inside the hub"
MAIN_CHECKOUT = "doc-gate: %s is in the main product checkout; you hold a product worktree (WRK-10). Fix: edit %s."


def tail(text, size):
    """A long path as a short reason names it: its last size characters, led by '...'."""
    return text if len(text) <= size else "..." + text[-(size - 3):]


def verdict(tool, tool_input, env_extra=None, cwd=None, entry=None):
    """The gate as the harness runs it: one process, hook JSON on stdin; cwd is the event's own.
    `entry` runs another hook file of the private hub (the project-gate.py dispatcher).

    Returns (decision, reason). A crash, an unreadable reply or a missing decision comes back
    as its own decision string, so the caller's assert fails on it instead of this helper
    raising: an ERROR proves nothing about the gate.
    """
    env = dict(BASE_ENV)
    env.update(env_extra or {})
    event = {"tool_name": tool, "tool_input": tool_input}
    if cwd:
        event["cwd"] = cwd
    out = subprocess.run([sys.executable, entry or GATE], input=json.dumps(event),
                         capture_output=True, text=True, env=env, cwd=HUB)
    if out.returncode != 0:
        return "crashed", out.stderr.strip()[-400:]
    if not out.stdout.strip():
        return "pass", ""
    try:
        body = json.loads(out.stdout).get("hookSpecificOutput", {})
    except json.JSONDecodeError:
        return "unreadable reply", out.stdout.strip()[:400]
    return body.get("permissionDecision", "no decision"), body.get("permissionDecisionReason", "")


LIVE_HUB = ("doc-gate: %s is a Git file in the live hub; every session runs from it. Fix: edit in a task worktree "
            "(git-flow skill), then land it.")
NEW_FILE = LIVE_HUB.replace("a Git file", "a new file")


class LiveHubCheckout(unittest.TestCase):
    """The live hub is a Git main checkout: a write to any file Git does not ignore is refused
    there, a pad passes, and the same file in a task worktree passes."""

    @classmethod
    def setUpClass(cls):
        cls.box = tempfile.TemporaryDirectory(prefix="hub-doc-gate-live-")
        cls.hub = os.path.join(os.path.realpath(cls.box.name), "hub")
        drawer = os.path.join(cls.hub, "Drawer")
        os.makedirs(drawer)
        for name in SOURCES:
            shutil.copy(os.path.join(HERE, name), os.path.join(drawer, name))
        shutil.copy(os.path.join(os.path.dirname(HERE), "harness.toml"), os.path.join(cls.hub, "harness.toml"))
        with open(os.path.join(cls.hub, ".gitignore"), "w") as f:
            f.write("/SCRATCHPAD.md\n/Flight/SCRATCHPAD.md\n")
        git = lambda *a: subprocess.run(["git", "-C", cls.hub, *a], check=True, capture_output=True)  # noqa: E731
        git("init", "-q", "-b", "main")
        git("add", "-A")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "seed")
        cls.worktree = os.path.join(os.path.realpath(cls.box.name), "wt")
        git("worktree", "add", "-q", "-b", "task/x", cls.worktree)

    @classmethod
    def tearDownClass(cls):
        cls.box.cleanup()

    def judge(self, path):
        return verdict("Edit", {"file_path": path}, entry=os.path.join(self.hub, "Drawer", "doc-gate.py"))

    def test_a_tracked_file_in_the_live_hub_is_refused(self):
        self.assertEqual(("deny", LIVE_HUB % "Drawer/gatelib.py"),
                         self.judge(os.path.join(self.hub, "Drawer", "gatelib.py")))

    def test_a_new_unignored_file_in_the_live_hub_is_refused(self):
        self.assertEqual(("deny", NEW_FILE % "Drawer/new.py"), self.judge(os.path.join(self.hub, "Drawer", "new.py")))

    def test_a_git_ignored_pad_in_the_live_hub_passes(self):
        self.assertEqual(("pass", ""), self.judge(os.path.join(self.hub, "SCRATCHPAD.md")))
        self.assertEqual(("pass", ""), self.judge(os.path.join(self.hub, "Flight", "SCRATCHPAD.md")))

    def test_a_pad_in_another_folder_of_the_live_hub_is_refused(self):
        self.assertEqual(("deny", NEW_FILE % "Operator/SCRATCHPAD.md"),
                         self.judge(os.path.join(self.hub, "Operator", "SCRATCHPAD.md")))

    def test_the_same_file_in_a_task_worktree_passes(self):
        self.assertEqual(("pass", ""), self.judge(os.path.join(self.worktree, "Drawer", "gatelib.py")))


class HidingPlaces(unittest.TestCase):
    def test_attic_write_is_refused(self):
        self.assertEqual(("deny", REFUSAL_HIDE % ("Drawer/attic/x.md", WHY_HIDING)),
                         verdict("Write", {"file_path": os.path.join(HUB, "Drawer", "attic", "x.md")}))

    def test_old_folder_is_refused(self):
        self.assertEqual(("deny", REFUSAL_HIDE % ("TechArt/old/x.txt", WHY_HIDING)),
                         verdict("Write", {"file_path": "TechArt/old/x.txt"}))

    def test_backup_named_file_is_refused(self):
        self.assertEqual(("deny", REFUSAL_HIDE % ("Drawer/mem/foo.bak", WHY_BACKUP_NAME)),
                         verdict("Write", {"file_path": "Drawer/mem/foo.bak"}))

    def test_hub_zip_is_refused(self):
        self.assertEqual(("deny", REFUSAL_HIDE % ("Operator/stash.zip", WHY_ARCHIVE)),
                         verdict("Write", {"file_path": "Operator/stash.zip"}))

    def test_an_inbox_archive_is_a_hiding_place_like_any_other(self):
        # The hub has no inbox exemption: an _archive folder anywhere is a hiding place.
        self.assertEqual(("deny", REFUSAL_HIDE % ("Producer/inbox/_archive/2026-09-02-x.md", WHY_HIDING)),
                         verdict("Write", {"file_path": "Producer/inbox/_archive/2026-09-02-x.md"}))

    # Defect: the hub's own copy exclude list reads as a backup-named file, so no edit can land.
    # Injection: doc-gate.py ALLOW_HIDE -> [].
    def test_the_copy_exclude_list_passes_and_a_lookalike_is_refused(self):
        self.assertEqual(("pass", ""), verdict("Write", {"file_path": "Drawer/never-copy.txt"}))
        self.assertEqual(("deny", REFUSAL_HIDE % ("Drawer/notes-copy.txt", WHY_BACKUP_NAME)),
                         verdict("Write", {"file_path": "Drawer/notes-copy.txt"}))

    def test_multiedit_and_notebookedit_are_judged_on_their_own_path_input(self):
        # Crawl B01: project-gate.py routes four write tools here, but only Write and Edit were
        # ever sent. Dropping MultiEdit/NotebookEdit from the tool tuple, or dropping the
        # notebook_path fallback the NotebookEdit tool actually uses, passed both suites green:
        # a notebook written into an attic went unjudged.
        self.assertEqual(("deny", REFUSAL_HIDE % ("Drawer/attic/x.ipynb", WHY_HIDING)),
                         verdict("NotebookEdit", {"notebook_path": "Drawer/attic/x.ipynb"}))
        self.assertEqual(("deny", REFUSAL_HIDE % ("Operator/notes.bak", WHY_BACKUP_NAME)),
                         verdict("MultiEdit", {"file_path": "Operator/notes.bak"}))

    def test_the_dispatcher_routes_a_write_here(self):
        # The live hook entry is Drawer/project-gate.py, which imports this gate for a write tool.
        dispatch = os.path.join(HUB, "Drawer", "project-gate.py")
        self.assertEqual(("deny", REFUSAL_HIDE % ("Drawer/attic/a.py", WHY_HIDING)),
                         verdict("Write", {"file_path": "Drawer/attic/a.py"}, entry=dispatch))

    def test_the_off_switch_passes_every_write(self):
        self.assertEqual(("pass", ""), verdict("Write", {"file_path": "Drawer/attic/a.py"},
                                               {"DOC_GATE": "off"}))


class HubWrites(unittest.TestCase):
    """Outside the three rules every hub write passes: a new .md or .txt, a role file, a shell."""

    def test_new_documents_and_role_files_pass(self):
        cases = {path: verdict("Write", {"file_path": path, "content": "x\n"})[0] for path in (
            "Operator/notes.md", "UI/notes.txt", ABSENT_CORNER + "/AGENTS.md", "Drawer/mem/SCRATCHPAD.md")}
        self.assertEqual({k: "pass" for k in cases}, cases)

    def test_a_claude_instruction_file_in_the_hub_or_a_parent_is_refused(self):
        shadow = ("doc-gate: %s is a Claude instruction file; Claude Code then skips AGENTS.md. Fix: put the rule in "
                  "AGENTS.md or a skill.")
        for path, named in (("CLAUDE.md", "CLAUDE.md"), (".claude/CLAUDE.md", ".claude/CLAUDE.md"),
                            ("Drawer/CLAUDE.local.md", "Drawer/CLAUDE.local.md"),
                            (os.path.join(os.path.dirname(HUB), "CLAUDE.md"), os.path.join(os.path.dirname(HUB), "CLAUDE.md"))):
            with self.subTest(path=path):
                self.assertEqual(("deny", shadow % tail(named, 48)), verdict("Write", {"file_path": path, "content": "x\n"}))
        elsewhere = os.path.join(tempfile.gettempdir(), "outside-the-hub", "CLAUDE.md")
        self.assertEqual(("pass", ""), verdict("Write", {"file_path": elsewhere}))

    def test_shell_tools_are_not_judged(self):
        # The gate judges the write tools only: a shell line, even one that moves into an attic,
        # is not its call.
        self.assertEqual(("pass", ""), verdict("Bash", {"command": "mv Flight/state Drawer/attic/x/"}))
        self.assertEqual(("pass", ""), verdict("Monitor", {"command": "echo hi > Drawer/attic/x"}))

    def test_outside_hub_passes(self):
        # An absolute path from the platform's own temp root. A hardcoded "C:/..." is a
        # relative path on a POSIX box, which lands back inside the hub and denies.
        outside = os.path.join(tempfile.gettempdir(), "outside-the-hub", "old", "plan.bak")
        self.assertEqual(("pass", ""), verdict("Write", {"file_path": outside}))

    def test_a_hub_reached_through_a_link_is_still_the_hub(self):
        # Crawl B01: a hook that binds CLAUDE_PROJECT_DIR to the event cwd; a cwd that names the
        # hub through a link read every path as outside the hub, and a hiding place passed.
        with tempfile.TemporaryDirectory() as tmp:
            link = os.path.join(tmp, "hub-link")
            if os.name == "nt":  # a junction needs no symlink privilege
                import _winapi
                _winapi.CreateJunction(HUB, link)
            else:
                os.symlink(HUB, link)
            via_link = {"CLAUDE_PROJECT_DIR": link}
            self.assertEqual(("deny", REFUSAL_HIDE % ("Drawer/attic/y.md", WHY_HIDING)),
                             verdict("Write", {"file_path": "Drawer/attic/y.md"}, via_link))
            self.assertEqual(("deny", REFUSAL_HIDE % ("Drawer/attic/x.md", WHY_HIDING)),
                             verdict("Write", {"file_path": os.path.join(link, "Drawer", "attic", "x.md")}))

    def test_tool_variables_expand_or_pass(self):
        # Audit 2026-09-20: a path behind a variable the hook cannot expand resolves at run time,
        # never inside the hub. The variable travels in the hook's own environment.
        self.assertEqual(("pass", ""), verdict("Write", {"file_path": "$KG_UNSET_DIR/attic/x.md"}))
        self.assertEqual(("deny", REFUSAL_HIDE % ("Drawer/attic/x.md", WHY_HIDING)),
                         verdict("Write", {"file_path": "$KG_TEST_DIR/Drawer/attic/x.md"}, {"KG_TEST_DIR": HUB}))

    # Defect: a relative path resolves from CLAUDE_PROJECT_DIR, not the session's own folder.
    # Injection: doc-gate.py run() `base = cwd if ... else None` -> `base = None`.
    def test_relative_paths_resolve_from_the_event_cwd(self):
        corner = os.path.join(HUB, "UI")
        os.makedirs(corner, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="hub-doc-gate-outside-") as outside:
            self.assertEqual(("pass", ""), verdict("Write", {"file_path": "attic/a.py"}, cwd=outside))
            self.assertEqual(("deny", REFUSAL_HIDE % ("UI/attic/a.py", WHY_HIDING)),
                             verdict("Write", {"file_path": "attic/a.py"}, cwd=corner))


# A refusal in a linked worktree names it, hub-parent-relative, before the checkout path.
WT_LABEL = "hub_wt/route: "


class LinkedWorktrees(unittest.TestCase):
    """A linked task worktree is a checkout of the hub: its edits meet the hub's rules, and a
    refusal names the worktree (doc-gate rel() and where()).

    Real Git builds a hub and one linked worktree at <box>/hub_wt/route, so the gate's own
    `git worktree list` is what finds it. The seat's Git config never reaches the fixture.
    """
    TASK_SID = "11111111-2222-4333-8444-555555555555"     # holds product_wt/1234abcd by a task lock
    SESSION_SID = "66666666-7777-4888-9999-aaaaaaaaaaaa"  # holds product_wt/abcd1234 by a spawn session lock
    FREE_SID = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"     # holds no product worktree

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("doc_gate_linked_worktrees", GATE)
        cls.gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.gate)
        cls.tmp = tempfile.TemporaryDirectory(prefix="hub-doc-gate-wt-")
        box = os.path.realpath(cls.tmp.name)
        cls.hub = os.path.join(box, "hub")
        cls.tree = os.path.join(box, "hub_wt", "route")
        cls.outside = os.path.join(box, "elsewhere")
        os.makedirs(cls.hub)
        os.makedirs(cls.outside)
        empty = os.path.join(box, "empty.gitconfig")
        open(empty, "w").close()
        cls.env = {"DOC_GATE": "dry", "CLAUDE_PROJECT_DIR": cls.hub,
                   "GIT_CONFIG_GLOBAL": empty, "GIT_CONFIG_SYSTEM": empty,
                   **{k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "HOME", "USERPROFILE")
                      if k in os.environ}}

        def git(*args, cwd=cls.hub):
            subprocess.run(["git", "-C", cwd, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                            *args], check=True, capture_output=True, env=cls.env)

        git("init", "-b", "main")
        with open(os.path.join(cls.hub, "AGENTS.md"), "w", encoding="utf-8", newline="") as f:
            f.write("a\n")
        git("add", "AGENTS.md")
        git("commit", "-m", "hub")
        git("worktree", "add", "-b", "task/route", cls.tree)
        # The product repo ([product] path's checkout) and one spawned session's linked worktree of it.
        cls.product = os.path.join(box, "product")
        cls.product_tree = os.path.join(box, "product_wt", "abcd1234")
        os.makedirs(os.path.join(cls.product, "project"))
        for path in (os.path.join(cls.product, "README.md"), os.path.join(cls.product, "project", "main.gd")):
            with open(path, "w", encoding="utf-8") as f:
                f.write("x\n")
        git("init", "-b", "main", cwd=cls.product)
        git("add", "-A", cwd=cls.product)
        git("commit", "-m", "product", cwd=cls.product)
        git("worktree", "add", "-b", "session/abcd1234", cls.product_tree, cwd=cls.product)
        # The two lock forms Drawer/product_worktree.py writes: a task tree and a spawn session tree.
        cls.task_tree = os.path.join(box, "product_wt", "1234abcd")
        git("worktree", "add", "-b", "task/1234abcd", cls.task_tree, cwd=cls.product)
        git("worktree", "lock", "--reason", "task 1234abcd claim 9f9f9f9f session %s 1700000000" % cls.TASK_SID,
            cls.task_tree, cwd=cls.product)
        git("worktree", "lock", "--reason", "session %s 1700000000" % cls.SESSION_SID, cls.product_tree, cwd=cls.product)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        for stub in (patch.dict(os.environ, self.env, clear=True),
                     patch.object(self.gate, "HUB", self.hub),
                     patch.object(self.gate, "PRODUCT", self.product)):
            stub.start()
            self.addCleanup(stub.stop)

    def run_gate(self, tool, event=None, **tool_input):
        out = io.StringIO()
        with redirect_stdout(out):
            self.gate.run({"tool_name": tool, "tool_input": tool_input, **(event or {})})
        if not out.getvalue().strip():
            return "pass", ""
        body = json.loads(out.getvalue()).get("hookSpecificOutput", {})
        return body.get("permissionDecision"), body.get("permissionDecisionReason")

    def test_hiding_place_in_a_linked_worktree_is_refused(self):
        self.assertEqual(("deny", REFUSAL_HIDE % (WT_LABEL + "Drawer/attic/a.py", WHY_HIDING)),
                         self.run_gate("Write", file_path=os.path.join(self.tree, "Drawer", "attic", "a.py"),
                                       content="x\n"))

    def test_a_worktree_role_file_or_new_md_passes(self):
        cases = {
            "role file": self.run_gate("Edit", file_path=os.path.join(self.tree, "AGENTS.md"),
                                       old_string="a", new_string="a" * 20000),
            "new md": self.run_gate("Write", file_path=os.path.join(self.tree, "UI", "plan.md"), content="x\n"),
        }
        self.assertEqual({"role file": ("pass", ""), "new md": ("pass", "")}, cases)

    def test_a_folder_outside_every_checkout_still_passes(self):
        self.assertEqual(("pass", ""), self.run_gate(
            "Write", file_path=os.path.join(self.outside, "plan.md"), content="x\n"))

    def test_a_test_file_edit_waits_for_the_running_gate_of_its_own_checkout(self):
        # Docstring rule 3: the verdict file of the checkout that holds the test decides; a done,
        # stale or absent mark passes, and a non-test file passes while the gate runs.
        verdict = os.path.join(self.product_tree, "gate-verdict.json")
        os.makedirs(os.path.dirname(verdict), exist_ok=True)
        self.addCleanup(os.remove, verdict)
        test = os.path.join(self.product_tree, "project", "tests", "test_gate.gd")

        def mark(status, hours_ago):
            started = self.gate.datetime.now(self.gate.timezone.utc) - self.gate.timedelta(hours=hours_ago)
            with open(verdict, "w", encoding="utf-8") as f:
                json.dump({"status": status, "startedAtUtc": started.strftime("%Y-%m-%dT%H:%M:%S.000Z")}, f)
            return self.run_gate("Edit", file_path=test, old_string="a", new_string="b")[0]

        cases = {"running": mark("running", 0), "done": mark("done", 0), "stale": mark("running", 3)}
        mark("running", 0)
        cases["code"] = self.run_gate("Edit", file_path=os.path.join(self.product_tree, "project", "main.gd"),
                                      old_string="x", new_string="y")[0]
        cases["main checkout"] = self.run_gate("Edit", file_path=os.path.join(self.product, "project", "tests", "test_a.gd"),
                                               old_string="a", new_string="b")[0]
        self.assertEqual({"running": "deny", "done": "pass", "stale": "pass", "code": "pass",
                          "main checkout": "pass"}, cases)

    def test_product_repo_code_passes_without_the_hub_rules(self):
        # Docstring rule 1 judges only the hub and its task worktrees. In the hub an "old" folder
        # is a hiding place; in the product repo it passes.
        cases = {
            "code": self.run_gate("Write", file_path=os.path.join(self.product, "project", "old", "a.gd"),
                                  content="x\n"),
            "txt": self.run_gate("Write", file_path=os.path.join(self.product_tree, "project", "notes.txt"),
                                 content="x\n"),
        }
        self.assertEqual({"code": ("pass", ""), "txt": ("pass", "")}, cases)

    def test_a_main_checkout_write_from_a_session_that_holds_a_product_worktree_is_refused(self):
        # Docstring rule 5 (WRK-10): a write tool path in the main product checkout is refused when a
        # product worktree lock names the event's session; the refusal points at the held tree.
        main = os.path.join(self.product, "project", "main.gd")
        task = MAIN_CHECKOUT % ("product: project/main.gd", tail(os.path.join(self.task_tree, "project/main.gd"), 64))
        held = {"session_id": self.TASK_SID}
        cases = {
            "write": self.run_gate("Write", held, file_path=main, content="x\n"),
            "edit": self.run_gate("Edit", held, file_path=main, old_string="x", new_string="y"),
            "multiedit": self.run_gate("MultiEdit", held, file_path=main, edits=[]),
            "notebook": self.run_gate("NotebookEdit", held, notebook_path=os.path.join(self.product, "n.ipynb")),
            "session lock": self.run_gate("Write", {"session_id": self.SESSION_SID.upper()}, file_path=main,
                                          content="x\n"),
            "no tree": self.run_gate("Write", {"session_id": self.FREE_SID}, file_path=main, content="x\n"),
            "no session": self.run_gate("Write", file_path=main, content="x\n"),
            "own tree": self.run_gate("Write", held, file_path=os.path.join(self.task_tree, "project", "main.gd"),
                                      content="x\n"),
            "md": self.run_gate("Write", held, file_path=os.path.join(self.product, "README.md"), content="x\n"),
        }
        self.assertEqual({
            "write": ("deny", task), "edit": ("deny", task), "multiedit": ("deny", task),
            "notebook": ("deny", MAIN_CHECKOUT % ("product: n.ipynb", tail(os.path.join(self.task_tree, "n.ipynb"), 64))),
            "session lock": ("deny", MAIN_CHECKOUT % ("product: project/main.gd",
                                                      tail(os.path.join(self.product_tree, "project/main.gd"), 64))),
            "no tree": ("pass", ""), "no session": ("pass", ""), "own tree": ("pass", ""),
            "md": ("deny", MAIN_CHECKOUT % ("product: README.md", tail(os.path.join(self.task_tree, "README.md"), 64)))},
            cases)

    def test_a_git_fault_on_the_main_checkout_rule_passes(self):
        # Rule 5 fails open: a Git that cannot list the product worktrees never blocks a write.
        main = os.path.join(self.product, "project", "main.gd")
        with patch.object(self.gate.product_worktree, "git",
                          side_effect=subprocess.TimeoutExpired(["git"], 120)):
            self.assertEqual(("pass", ""), self.run_gate("Write", {"session_id": self.TASK_SID},
                                                         file_path=main, content="x\n"))
        not_git = os.path.join(self.outside, "product")
        os.makedirs(os.path.join(not_git, "project"), exist_ok=True)
        with patch.object(self.gate, "PRODUCT", not_git):
            self.assertEqual(("pass", ""), self.run_gate("Write", {"session_id": self.TASK_SID},
                                                         file_path=os.path.join(not_git, "project", "main.gd"),
                                                         content="x\n"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
