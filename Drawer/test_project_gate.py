"""Selftest for Drawer/project-gate.py, the one project-scope hook.

Run: .venv/bin/python -B -m unittest Drawer/test_project_gate.py. Judges only; writes nothing.
Routing is the contract: the right gate judges the call, no gate module loads for a tool no gate
claims, a manual polling loop or an unscoped search on Bash is refused, and unreadable hook JSON
denies. Every expected value below is that contract written out -- the GATES table, the manifest
entry and each refusal (its gate name and problem phrase) -- never read back from the module
under test.

Every child runs a PRIVATE COPY of the dispatcher and the gate it routes to, in a private hub with
its own empty home (setUpModule), so the gate's box-local log is the private hub's. The private hub
carries a copy of this hub's harness.toml (the gates read the memory server name from it) and its
own harness.local.toml, which names the private product project.
"""
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

# realpath: a checkout reached through a symlink (a probe worktree under /tmp on macOS) must give
# the gates the same hub root they resolve from their own files, or every path reads as
# outside the hub and the gate passes everything.
HERE = os.path.dirname(os.path.realpath(__file__))
HUB = os.path.dirname(HERE)
GATE = os.path.join(HERE, "project-gate.py")

SPEC = importlib.util.spec_from_file_location("hub_project_gate", GATE)
DISPATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DISPATCH)

sys.path.insert(0, HERE)
import harness_config  # noqa: E402
import testgit  # noqa: E402

testgit.arm()  # no git template in any fixture repo

CONFIG = harness_config.load(HUB)
# The memory server tool spelling, from harness.toml: mcp__<server>__.
SERVER = harness_config.server_name(CONFIG)
KG = "mcp__%s__" % SERVER.replace("_", "-")

# The files a child imports: the dispatcher, the gate it routes to and the shared gate body.
SOURCES = ("project-gate.py", "doc-gate.py", "fork-gate.py", "gatelib.py", "rules_check.py", "rules_audit.py", "git_guard.py", "oscompat.py")

BOX = None       # the private hub's temp dir; setUpModule fills these in
BOX_HUB = ""     # the private hub root the child judges against
BOX_DRAWER = ""  # its Drawer, holding the private copies the child runs
BASE_ENV = {}


def setUpModule():
    """One private hub for the run, with an empty home beside it."""
    global BOX, BOX_HUB, BOX_DRAWER, BASE_ENV
    BOX = tempfile.TemporaryDirectory(prefix="hub-project-gate-box-")
    root = os.path.realpath(BOX.name)
    BOX_HUB = os.path.join(root, "hub")
    BOX_DRAWER = os.path.join(BOX_HUB, "Drawer")
    home = os.path.join(root, "home")  # outside the hub: an empty seat, no saved profile
    os.makedirs(os.path.join(BOX_DRAWER, "hooks"))
    os.makedirs(home)
    for name in SOURCES:
        shutil.copy(os.path.join(HERE, name), os.path.join(BOX_DRAWER, name))
    shutil.copy(os.path.join(HUB, "harness.toml"), os.path.join(BOX_HUB, "harness.toml"))
    # The marker makes the private hub the root the child's config read finds, not this checkout.
    open(os.path.join(BOX_HUB, CONFIG["project"]["marker_file"]), "w").close()
    with open(os.path.join(BOX_HUB, "harness.local.toml"), "w", encoding="utf-8") as fh:
        fh.write("[product]\npath = %s\n" % json.dumps(os.path.join(root, "product")))
        fh.write('broad_roots = [".gen", ".import"]\n')
    # The hook's whole environment: what the gates read and nothing the seat happens to carry.
    BASE_ENV = {"CLAUDE_PROJECT_DIR": BOX_HUB, "DOC_GATE": "dry", "PYTHONDONTWRITEBYTECODE": "1",
                "HOME": home, "USERPROFILE": home, "PYTHONPATH": HERE}
    if os.name == "nt":  # a Windows interpreter needs its system root to start
        BASE_ENV["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")


def tearDownModule():
    BOX.cleanup()


# The prepared interpreter each printed remedy names on this host: a new seat has no py launcher.
PY = (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python") + " -B"
# -- the contract: the refusal the routed gate hands back, and the dispatcher's own -----------
UNREADABLE = (
    "project-gate: cannot read the hook JSON, so it denies (fail closed). "
    f"Fix: retry; if it repeats, run {PY} Drawer/project-gate.py < the-event.json.")
MANUAL_POLL = (
    "project-gate: manual polling is refused. Use Monitor for background jobs, "
    "or a foreground Workflow leg for bounded worker legs.")


class Refusal:
    """A gate refusal as the contract names it: the gate's name and the problem.

    It equals a reason that opens with "<gate>: " and carries the problem phrase, so a reworded
    remedy, ruling date or override line stays green while a wrong gate or wrong problem fails.
    `%` fills the phrase, as it filled the old full-text template.
    """
    __hash__ = None

    def __init__(self, gate, problem):
        self.gate, self.problem = gate, problem

    def __mod__(self, detail):
        return Refusal(self.gate, self.problem % detail)

    def __eq__(self, reason):
        return (isinstance(reason, str) and reason.startswith(self.gate + ": ")
                and self.problem in reason)

    def __repr__(self):
        return "Refusal(%r, %r)" % (self.gate, self.problem)


HIDING_REFUSED = Refusal("doc-gate", "%s is a hiding place")


def run(payload, env_extra=None):
    """The gate as the harness runs it: one process, hook JSON on stdin.

    Returns (decision, reason). A crash, an unreadable reply or a missing decision comes back
    as its own decision string, so the caller's assert fails on it instead of this helper
    raising: an ERROR proves nothing about the dispatcher.
    """
    env = dict(BASE_ENV)
    env.update(env_extra or {})
    return verdict(subprocess.run([sys.executable, os.path.join(BOX_DRAWER, "project-gate.py")],
                                  input=payload, capture_output=True, text=True, env=env,
                                  cwd=BOX_HUB, timeout=60))


def verdict(out):
    """(decision, reason) from one finished gate process with text stdout and stderr."""
    if out.returncode != 0:
        return "crashed", out.stderr.strip()[-400:]
    if not out.stdout.strip():
        return "pass", ""
    try:
        body = json.loads(out.stdout).get("hookSpecificOutput", {})
    except json.JSONDecodeError:
        return "unreadable reply", out.stdout.strip()[:400]
    return body.get("permissionDecision", "no decision"), body.get("permissionDecisionReason", "")


def judge(tool, **tool_input):
    return run(json.dumps({"tool_name": tool, "tool_input": tool_input}))


class Routing(unittest.TestCase):
    def test_write_tools_route_to_the_doc_gate(self):
        for tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            with self.subTest(tool=tool):
                self.assertEqual("doc-gate.py", DISPATCH.gate_for(tool))

    def test_unclaimed_tools_route_nowhere(self):
        # TodoWrite and BashOutput contain a routed name; the old matchers fired on
        # them too and the old gates did nothing. The dispatcher must do the same.
        # Bash reaches only the dispatcher's own refusals; no gate file judges it.
        for tool in ("Read", "Grep", "Glob", "WebFetch", "", "Task", "TodoWrite", "BashOutput", "Bash",
                     "Monitor", "AskUserQuestion", "set_card_status", KG + "add_card",
                     "mcp__other__query_files", KG + "get_card", KG + "Write", KG + "add", "add"):
            with self.subTest(tool=tool):
                self.assertIsNone(DISPATCH.gate_for(tool))

    def test_agent_and_workflow_route_to_the_fork_gate(self):
        for tool in ("Agent", "Workflow"):
            with self.subTest(tool=tool):
                self.assertEqual("fork-gate.py", DISPATCH.gate_for(tool))


class Verdicts(unittest.TestCase):
    """The routed gate's own verdict must reach the harness unchanged."""

    def test_doc_gate_verdicts_arrive_through_the_dispatcher(self):
        self.assertEqual(("deny", HIDING_REFUSED % "Operator/attic/notes.md"),
                         judge("Write", file_path="Operator/attic/notes.md"))
        self.assertEqual(("pass", ""), judge("Write", file_path="Operator/notes.md"))
        self.assertEqual(("pass", ""), judge("Bash", command="ls Producer/inbox"))

    def test_fork_gate_verdicts_arrive_through_the_dispatcher(self):
        # The fork gate is back (operator ruling): no leg runs on the session model.
        self.assertEqual("deny", judge("Agent", prompt="audit the branch")[0])
        self.assertEqual("deny", judge("Workflow", script="await agent('x', {})")[0])
        self.assertEqual(("pass", ""), judge("Workflow", script="await agent('x', { model: 'haiku' })"))

    def test_retired_gate_tools_pass(self):
        # The task and plain-terms gates are gone: their tools reach no gate.
        self.assertEqual(("pass", ""), judge(KG + "set_card_status", uuid="abcd", status="done"))
        self.assertEqual(("pass", ""), judge("AskUserQuestion", questions=[]))

    def test_unclaimed_tools_pass_without_loading_a_gate(self):
        self.assertEqual(("pass", ""), judge("Read", file_path="Operator/notes.md"))
        self.assertEqual(("pass", ""), judge("Grep", pattern="copy"))
        # The decision alone cannot see a load: doc-gate returns silently for a tool outside its
        # own list, so a dispatcher that imports a gate for every call still reads
        # ("pass", ""). Spy on the import itself -- the no-load contract is why this file exists.
        loaded, judged = [], []

        class Spy:
            def run(self, event):
                judged.append(event)

        def load_spy(filename):
            loaded.append(filename)
            return Spy()

        def dispatch(event):
            """The dispatcher in this process: (exit code, everything it printed)."""
            with patch.object(DISPATCH, "load", load_spy), \
                    patch.object(sys, "stdin", io.TextIOWrapper(
                        io.BytesIO(json.dumps(event).encode("utf-8")), encoding="utf-8")), \
                    redirect_stdout(io.StringIO()) as out:
                code = DISPATCH.main()
            return code, out.getvalue()

        read = {"tool_name": "Read", "tool_input": {"file_path": "Operator/notes.md"}}
        grep = {"tool_name": "Grep", "tool_input": {"pattern": "copy"}}
        self.assertEqual((0, ""), dispatch(read))
        self.assertEqual((0, ""), dispatch(grep))
        self.assertEqual([], loaded)
        self.assertEqual([], judged)
        # Positive control: a claimed tool loads its one gate and hands it the whole event, so
        # the empty list above is the contract and not a spy that never fires.
        write = {"tool_name": "Write", "tool_input": {"file_path": "Operator/notes.md"}}
        self.assertEqual((0, ""), dispatch(write))
        self.assertEqual(["doc-gate.py"], loaded)
        self.assertEqual([write], judged)


class ManualPolling(unittest.TestCase):
    """The project-gate contract: a live polling loop sleeps and checks within its own span.
    A one-off read of a task's output after its notice, and loop words in a document body,
    are not polls."""

    # Defect: the whole command is scanned, so a sleep beside a one-off read reads as a poll.
    # Injection: project-gate.py manual_poll_reason() `for span in loop_spans(command)` ->
    # `for span in [command]`.
    def test_a_one_off_task_output_read_passes(self):
        for command in ("cat ./tasks/worker.output", "sleep 5; cat ./tasks/worker.output"):
            with self.subTest(command=command):
                self.assertEqual(("pass", ""), judge("Bash", command=command))

    def test_polling_loops_are_denied(self):
        for command in ("until grep -q READY log.txt; do sleep 1; done",
                        "while true; do cat tasks/worker.output; sleep 5; done"):
            with self.subTest(command=command):
                self.assertEqual(("deny", MANUAL_POLL), judge("Bash", command=command))

    # Defect: the check scans past the loop, so a sleep after a finished loop reads as a poll.
    # Injection: project-gate.py manual_poll_reason() `for span in loop_spans(command)` ->
    # `for span in [command] if LOOP_WORD.search(command) else []`.
    def test_a_sleep_outside_the_loop_and_a_document_body_pass(self):
        for command in ('for f in *.log; do grep READY "$f"; done; sleep 5',
                        "cat > poll.sh <<'EOF'\nwhile true; do sleep 1; grep READY log.txt; done\nEOF"):
            with self.subTest(command=command):
                self.assertEqual(("pass", ""), judge("Bash", command=command))

    def test_unrelated_shell_commands_pass(self):
        self.assertEqual(("pass", ""), judge("Bash", command="sleep 1"))
        self.assertEqual(("pass", ""), judge("Bash", command="grep READY log.txt"))
        self.assertEqual(("pass", ""), judge("Bash", command="cat tasks/worker.log"))


class MonitorCommands(unittest.TestCase):
    """Since CLI 2.1.314 a Monitor command also matches a Bash hook matcher; the CLI runs the one
    gate entry once per Monitor call, as tool_name Monitor (probed on 2.1.316). A poll loop is right in a Monitor
    command and wrong in a Bash call; every other Bash refusal holds in both; the Flight events watch runs."""
    POLL = "while true; do gh pr view 1 --json comments | grep -c Flight; sleep 30; done"
    EVENTS = ".venv/bin/python -B Drawer/flight-watchdog.py events"

    def judge(self, tool, command):
        return run(json.dumps({"tool_name": tool, "tool_use_id": "toolu_x", "cwd": BOX_HUB,
                               "tool_input": {"command": command, "description": "watch"}}))

    # Defect: a Monitor poll loop is refused. Injection: manual_poll_reason() drop the
    # Monitor early return.
    def test_a_poll_loop_in_a_monitor_command_passes_and_in_a_bash_call_is_denied(self):
        self.assertEqual(("pass", ""), self.judge("Monitor", self.POLL))
        self.assertEqual(("deny", MANUAL_POLL), self.judge("Bash", self.POLL))

    # Defect: the Monitor event reads no command, so it never judges a hook bypass.
    # Injection: SHELL_TOOLS = ("Bash",).
    def test_no_verify_in_a_monitor_command_is_refused(self):
        self.assertEqual(("deny", Refusal("project-gate", "--no-verify")),
                         self.judge("Monitor", "git -C ~/product commit --no-verify -m wip"))

    def test_a_placeholder_post_in_a_monitor_command_is_refused(self):
        self.assertEqual(("deny", Refusal("project-gate", "placeholder card id")),
                         self.judge("Monitor", "gh pr comment 1 --body '**x**: lesson ab12...'"))

    def test_the_flight_events_watch_passes(self):
        self.assertEqual(("pass", ""), self.judge("Monitor", self.EVENTS))


class HistoryRewrite(unittest.TestCase):
    """Git filter-branch -f in the product clone cleared every ref under refs/original, another
    session's backup too. The fix widened the refusal to the class (Drawer/git_guard.py):
    filter-branch and filter-repo, a delete of refs/original, a force push to main, reset
    --hard on a shared or another session's branch, and branch -D of work not on origin/main. A session's
    own reset and the delete of its merged work pass. Each test cuts one rule; the pass test guards all."""
    REFUSED = Refusal("project-gate", "history rewrite refused")
    OWN = "aaaaaaaa-1111-4111-8111-111111111111"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="hub-git-guard-")
        root = os.path.realpath(cls.tmp.name)
        git = lambda *a, cwd=root: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)
        cls.origin, cls.clone = os.path.join(root, "origin.git"), os.path.join(root, "clone")
        git("init", "-q", "--bare", "-b", "main", cls.origin)
        git("clone", "-q", cls.origin, cls.clone)
        c = lambda *a: git("-c", "user.name=t", "-c", "user.email=t@t", *a, cwd=cls.clone)
        c("commit", "-q", "--allow-empty", "-m", "one")
        c("push", "-q", "origin", "HEAD:main")
        c("branch", "merged")
        c("branch", "task/aaaaaaaa-merged")
        c("branch", "unmerged")
        cls.own, cls.other = os.path.join(root, "own"), os.path.join(root, "other")
        c("worktree", "add", "-q", "-b", "session/aaaaaaaa", cls.own)
        c("worktree", "add", "-q", "-b", "session/bbbbbbbb", cls.other)
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "two", cwd=cls.own)
        c("update-ref", "refs/heads/unmerged", "session/aaaaaaaa")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def judge_in(self, cwd, command):
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd,
                              "session_id": self.OWN})
        return run(payload, {"PATH": os.environ.get("PATH", "")})

    def assert_refused(self, cwd, commands, phrase):
        for command in commands:
            with self.subTest(command=command):
                verdict, reason = self.judge_in(cwd, command)
                self.assertEqual(("deny", self.REFUSED), (verdict, reason))
                self.assertIn(phrase, reason)

    # Injection: git_guard.reason() drop the FILTER check.
    def test_filter_branch_and_filter_repo_are_refused_with_the_reword_remedy(self):
        self.assert_refused(self.own, (
            "git filter-branch -f --msg-filter 'sed s/a/b/' HEAD~2..HEAD",
            "cd ~/My_Product && git -C . filter-branch --env-filter x -- --all",
            "git -c core.pager=cat filter-branch HEAD", "git-filter-branch HEAD", "git filter-repo --force"),
            "rebase")

    # Injection: git_guard.reason() drop the ORIGINAL_DELETE check.
    def test_a_delete_of_refs_original_is_refused(self):
        self.assert_refused(self.own, (
            "git update-ref -d refs/original/refs/heads/session/9f8e7d6c",
            "git for-each-ref --format='%(refname)' refs/original | xargs -n1 git update-ref -d",
            "rm -rf .git/refs/original", "trash ~/My_Product/.git/refs/original"), "refs/original")

    # Injection: git_guard.reason() never call push_reason.
    def test_a_force_push_to_main_is_refused(self):
        self.assert_refused(self.own, (
            "git push --force origin HEAD:main", "git push -f origin main", "git push -uf origin HEAD:refs/heads/main",
            "git push --force-with-lease origin main", "git push --force-with-lease=main:abc origin HEAD:main",
            "git push origin +HEAD:main", "git push origin +refs/heads/main",
            "git push --force \\\n  origin main", "cat <<EOF && git push -f origin main\nbody\nEOF",
            "# note\ngit push -f origin main"), "force push to")
        self.assert_refused(self.clone, ("git push -f", "git push --force origin HEAD"), "force push to main")

    # Injection: git_guard.reason() never call reset_reason.
    def test_reset_hard_on_a_shared_or_another_sessions_branch_is_refused(self):
        self.assert_refused(self.clone, ("git reset --hard origin/main",), "reset --hard on main")
        self.assert_refused(self.own, (f"git -C {self.clone} reset --hard HEAD~1",
                                       f"cd {self.other} && git reset --hard"), "reset --hard on")

    # Injection: git_guard.reason() never call branch_reason.
    def test_branch_delete_of_work_not_on_origin_main_is_refused(self):
        self.assert_refused(self.clone, ("git branch -D unmerged", "git branch -d --force unmerged",
                                         "git branch -Df merged unmerged", "git branch -D gone"), "not on origin/main")

    def test_own_work_reads_and_a_reword_pass(self):
        for cwd, command in (
                (self.own, "git reset --hard origin/main"), (self.clone, "git branch -D task/aaaaaaaa-merged"),
                (self.clone, "git branch -d unmerged"), (self.own, "git push origin HEAD:main"),
                (self.own, "git push -f origin HEAD:session/aaaaaaaa"), (self.own, "git reset --soft HEAD~1"),
                (self.clone, "git for-each-ref refs/original"), (self.clone, "git log -1 refs/original/refs/heads/main"),
                (self.own, "GIT_SEQUENCE_EDITOR=\"sed -i '' 1s/pick/reword/\" git rebase -i HEAD~2"),
                (self.own, "git commit --amend -m 'Reword'"), (self.own, "grep -n filter-branch Drawer/project-gate.py"),
                # A lesson that quotes a refused command is no rewrite.
                (self.own, "mem add --kind lesson --body 'never run git filter-branch -f; rm -rf .git/refs/original "
                           "and git push -f origin main lose work'"),
                (self.own, "cat > note.sh <<'EOF'\ngit filter-branch HEAD\ngit push -f origin main\nEOF")):
            with self.subTest(command=command):
                self.assertEqual(("pass", ""), self.judge_in(cwd, command))


class UnboundHooks(unittest.TestCase):
    """A hub repo (or the product checkout) with core.hooksPath unset commits and pushes with no secret scan and no name
    check (Drawer/git_guard.py hooks_reason). A bound repo and a repo that is neither hub nor product pass."""
    OWN = "aaaaaaaa-1111-4111-8111-111111111111"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hub-hooks-guard-")
        self.addCleanup(self.tmp.cleanup)
        root = os.path.realpath(self.tmp.name)
        self.git = lambda cwd, *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd,
                                                  check=True, capture_output=True, text=True)
        self.hub = os.path.join(root, "hubrepo")
        self.plain = os.path.join(root, "plain")
        self.product = os.path.join(root, "prodrepo")
        for folder in (self.hub, self.plain, self.product):
            os.makedirs(folder)
            self.git(folder, "init", "-q", "-b", "main")
        shutil.copy(os.path.join(HUB, "harness.toml"), os.path.join(self.hub, "harness.toml"))
        open(os.path.join(self.hub, CONFIG["project"]["marker_file"]), "w").close()
        self.git(self.hub, "add", "-A")
        self.git(self.hub, "commit", "-q", "-m", "seed")
        self.wt = os.path.join(root, "hubwt")
        self.git(self.hub, "worktree", "add", "-q", "-b", "task/x", self.wt)

    def judge_in(self, cwd, command, env=None):
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd,
                              "session_id": self.OWN})
        return run(payload, {"PATH": os.environ.get("PATH", ""), **(env or {})})

    def test_commit_and_push_in_an_unbound_hub_or_hub_worktree_are_refused_within_the_cap(self):
        for cwd in (self.hub, self.wt):
            for command in ("git commit -m x", "git push origin HEAD:main", "git add a && git commit -am x",
                            f"git -C {cwd} commit -m x"):
                with self.subTest(cwd=cwd, command=command):
                    verdict_, reason = self.judge_in(cwd, command)
                    self.assertEqual("deny", verdict_)
                    self.assertIn("core.hooksPath is unbound", reason)
                    self.assertIn("git-branch-policy.py install --repo", reason)
                    self.assertLessEqual(len(reason), 200)

    def test_a_bound_hub_passes_and_other_git_calls_pass_while_unbound(self):
        self.assertEqual(("pass", ""), self.judge_in(self.hub, "git status"))
        self.assertEqual(("pass", ""), self.judge_in(self.hub, "git log -1"))
        self.assertEqual(("pass", ""), self.judge_in(self.hub, "git config --get core.hooksPath"))
        for value in ("Drawer/git-hooks", os.path.join(self.hub, "Drawer", "git-hooks")):
            self.git(self.hub, "config", "core.hooksPath", value)
            for cwd in (self.hub, self.wt):
                with self.subTest(value=value, cwd=cwd):
                    self.assertEqual(("pass", ""), self.judge_in(cwd, "git commit -m x"))
                    self.assertEqual(("pass", ""), self.judge_in(cwd, "git push origin HEAD:session/aaaaaaaa"))
        self.git(self.hub, "config", "core.hooksPath", "/some/other/hooks")
        self.assertEqual("deny", self.judge_in(self.hub, "git commit -m x")[0])  # bound to the wrong folder

    def test_a_repo_that_is_neither_hub_nor_product_passes_unbound(self):
        self.assertEqual(("pass", ""), self.judge_in(self.plain, "git commit -m x"))
        self.assertEqual(("pass", ""), self.judge_in(self.plain, "git push origin main"))

    def test_the_product_checkout_and_its_worktree_are_judged_like_the_hub(self):
        self.git(self.product, "commit", "-q", "--allow-empty", "-m", "seed")
        product_wt = os.path.join(os.path.dirname(self.product), "prodwt")
        self.git(self.product, "worktree", "add", "-q", "-b", "task/p", product_wt)
        env = {harness_config.env_name("PRODUCT_PATH"): self.product}
        for cwd in (self.product, product_wt):
            with self.subTest(cwd=cwd):
                self.assertEqual("deny", self.judge_in(cwd, "git commit -m x", env)[0])
        self.git(self.product, "config", "core.hooksPath", os.path.join(BOX_DRAWER, "product-hooks"))
        os.makedirs(os.path.join(BOX_DRAWER, "product-hooks"), exist_ok=True)
        self.assertEqual(("pass", ""), self.judge_in(self.product, "git commit -m x", env))


class HeadStaysOnBranch(unittest.TestCase):
    """A risky-review reviewer leg ran git checkout -q --detach <head> in a hub task worktree, at
    the commit it was already on. HEAD left the task branch, so hub-land.py refused with 'has no worktree'. Bash
    refuses a checkout or switch that moves HEAD off its branch in a linked worktree or on main
    (Drawer/git_guard.py head_move_reason). A path checkout, a no-op and the repair of a detached tree pass.
    A git checkout <name> of a name that is only a remote branch makes a local branch and moves HEAD
    (remote DWIM), so a word that names no commit passes only when it is a path or no remote branch has that name."""
    REFUSED = Refusal("project-gate", "HEAD move refused")
    OWN = "aaaaaaaa-1111-4111-8111-111111111111"
    BRANCH = "task/aaaaaaaa-fix"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hub-head-guard-")
        self.addCleanup(self.tmp.cleanup)
        root = os.path.realpath(self.tmp.name)
        git = lambda *a, cwd=root: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a],
                                                  cwd=cwd, check=True, capture_output=True)
        origin, self.clone = os.path.join(root, "origin.git"), os.path.join(root, "clone")
        git("init", "-q", "--bare", "-b", "main", origin)
        git("clone", "-q", origin, self.clone)
        git("commit", "-q", "--allow-empty", "-m", "one", cwd=self.clone)
        git("push", "-q", "origin", "HEAD:main", cwd=self.clone)
        git("push", "-q", "origin", "HEAD:refs/heads/remote-only", cwd=self.clone)
        git("push", "-q", "origin", "HEAD:refs/heads/a.txt", cwd=self.clone)
        git("fetch", "-q", "origin", cwd=self.clone)
        git("branch", "task/other", cwd=self.clone)
        self.wt, self.det = os.path.join(root, "aaaaaaaa-fix"), os.path.join(root, "det")
        git("worktree", "add", "-q", "-b", self.BRANCH, self.wt, cwd=self.clone)
        git("worktree", "add", "-q", "--detach", self.det, cwd=self.clone)
        open(os.path.join(self.wt, "a.txt"), "w").close()
        git("add", "a.txt", cwd=self.wt)
        git("commit", "-q", "-m", "two", cwd=self.wt)
        self.sha = subprocess.run(["git", "-C", self.wt, "rev-parse", "HEAD"], check=True, capture_output=True,
                                  text=True).stdout.strip()

    def judge_in(self, cwd, command):
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd,
                              "session_id": self.OWN})
        return run(payload, {"PATH": os.environ.get("PATH", "")})

    # Injection: git_guard.reason() never call head_move_reason.
    def test_the_review_leg_line_leaves_the_task_worktree_on_its_branch(self):
        command = (f"cd '{self.wt}' && git status --short | head -3; git checkout -q --detach '{self.sha}' 2>&1 "
                   f"| head -1")
        verdict, reason = self.judge_in(self.clone, command)
        if verdict == "pass":  # Claude Code runs a command the gate passes
            subprocess.run(["bash", "-c", command], capture_output=True)
        head = subprocess.run(["git", "-C", self.wt, "symbolic-ref", "--short", "-q", "HEAD"], capture_output=True,
                              text=True).stdout.strip()
        self.assertEqual(self.BRANCH, head)
        self.assertEqual(("deny", self.REFUSED), (verdict, reason))
        self.assertIn("has no worktree", reason)

    def test_a_checkout_or_switch_off_the_branch_is_refused(self):
        sha, wt = self.sha, self.wt
        for cwd, command in [(wt, c) for c in (
                f"git checkout {sha}", "git checkout --detach", "git checkout -b other", "git checkout -B other",
                "git checkout --orphan x", "git checkout -", "git checkout @{-1}", "git checkout main",
                "git checkout -qb other", "git switch --detach main", "git switch -d main", "git switch -c other",
                "git switch -", "git switch main", f"bash -c 'git checkout {sha}'")] + [
                (self.clone, f"git -C {wt} checkout -q --detach {sha}"), (self.clone, "git switch -c feature"),
                (self.clone, "git checkout -b feature"), (self.clone, "git checkout task/other")]:
            with self.subTest(cwd=cwd, command=command):
                self.assertEqual(("deny", self.REFUSED), self.judge_in(cwd, command))

    def test_a_path_checkout_a_no_op_a_read_and_the_repair_pass(self):
        sha, wt = self.sha, self.wt
        for cwd, command in [(wt, c) for c in (
                "git checkout -- a.txt", "git checkout HEAD -- a.txt", f"git checkout {sha} -- a.txt",
                f"git checkout {sha} a.txt", "git checkout a.txt", f"git checkout {self.BRANCH}", "git checkout",
                "git checkout HEAD", "git checkout -p -- a.txt", f"git switch {self.BRANCH}", f"git show {sha}:a.txt",
                "git diff main...HEAD", "git log -1")] + [
                (self.det, "git checkout task/other"), (self.det, "git switch -c task/new"),
                (self.clone, "git checkout main")]:
            with self.subTest(cwd=cwd, command=command):
                self.assertEqual(("pass", ""), self.judge_in(cwd, command))

    def head(self):
        return subprocess.run(["git", "-C", self.wt, "symbolic-ref", "--short", "-q", "HEAD"], capture_output=True,
                              text=True).stdout.strip()

    # Injection: git_guard.head_move_reason() pass a word that names no commit without the remote check.
    def test_a_name_only_on_the_remote_is_refused_and_head_stays(self):
        command = "git checkout remote-only"
        verdict, reason = self.judge_in(self.wt, command)
        if verdict == "pass":  # Claude Code runs a command the gate passes
            subprocess.run(command.split(), cwd=self.wt, capture_output=True)
        self.assertEqual(self.BRANCH, self.head())
        self.assertEqual(("deny", self.REFUSED), (verdict, reason))
        for cwd, command in [(self.wt, "git switch remote-only"), (self.clone, "git checkout remote-only"),
                             (self.clone, f"git -C {self.wt} checkout remote-only"),
                             (self.wt, "git checkout -q remote-only")]:
            with self.subTest(cwd=cwd, command=command):
                self.assertEqual(("deny", self.REFUSED), self.judge_in(cwd, command))
        # a.txt is a file and a remote branch: git stops with 'could be both a local file and a tracking branch'.
        for cwd, command in [(self.wt, "git checkout a.txt"), (self.det, "git checkout remote-only")]:
            with self.subTest(cwd=cwd, command=command):
                self.assertEqual(("pass", ""), self.judge_in(cwd, command))

    # Injection: git_guard.head_move_reason() return at once on any -- word.
    def test_a_bare_trailing_dash_dash_does_not_hide_a_move(self):
        for command in ("git checkout task/other --", "git checkout --detach --", "git checkout remote-only --"):
            with self.subTest(command=command):
                self.assertEqual(("deny", self.REFUSED), self.judge_in(self.wt, command))
        self.assertEqual(("pass", ""), self.judge_in(self.wt, f"git checkout {self.BRANCH} --"))

    # Injection: git_guard.long_opt() read --force as a prefix of --force-create.
    def test_switch_force_to_the_current_branch_passes(self):
        self.assertEqual(("pass", ""), self.judge_in(self.wt, f"git switch --force {self.BRANCH}"))
        for command in ("git switch --force main", "git switch --force-create x"):
            with self.subTest(command=command):
                self.assertEqual(("deny", self.REFUSED), self.judge_in(self.wt, command))

class DeleteKillGuard(unittest.TestCase):
    """'Delete with trash, prove the target dead' was prose only. The Bash gate refuses a folder
    delete (rm -r, trash) outside the session's own task worktrees and the temp folders, a raw git worktree
    remove, git clean -x, -X or -d outside own trees, git branch -D of a branch with no own id, pkill, killall,
    fuser -k and a kill by a computed pid, and a line that does not parse and holds a delete or kill word
    (Drawer/git_guard.py delete_reason). Own work, temp folders, a file delete and a literal pid pass."""
    REFUSED = Refusal("project-gate", "delete guard refused")
    OWN = "aaaaaaaa-1111-4111-8111-111111111111"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="hub-delete-guard-")
        root = os.path.realpath(cls.tmp.name)
        git = lambda *a, cwd=root: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a],
                                                  cwd=cwd, check=True, capture_output=True)
        cls.origin, cls.hub = os.path.join(root, "origin.git"), os.path.join(root, "hub")
        git("init", "-q", "--bare", "-b", "main", cls.origin)
        git("clone", "-q", cls.origin, cls.hub)
        git("commit", "-q", "--allow-empty", "-m", "one", cwd=cls.hub)
        git("push", "-q", "origin", "HEAD:main", cwd=cls.hub)
        git("branch", "merged", cwd=cls.hub)
        git("branch", "task/aaaaaaaa-done", cwd=cls.hub)
        trees = os.path.join(root, "hub_wt")
        cls.own, cls.peer = os.path.join(trees, "aaaaaaaa-own"), os.path.join(trees, "bbbbbbbb-peer")
        git("worktree", "add", "-q", "-b", "task/aaaaaaaa-own", cls.own, cwd=cls.hub)
        git("worktree", "add", "-q", "-b", "task/bbbbbbbb-peer", cls.peer, cwd=cls.hub)
        cls.tmpdir = os.path.join(root, "tmpdir")
        for folder in (cls.own, cls.peer, cls.hub, os.path.join(cls.tmpdir, "scratch")):
            os.makedirs(os.path.join(folder, "build"))
            open(os.path.join(folder, "note.txt"), "w").close()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def judge_in(self, cwd, command, env=None):
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd,
                              "session_id": self.OWN})
        return run(payload, {"PATH": os.environ.get("PATH", ""), **(env or {})})

    def assert_refused(self, cwd, commands, phrase):
        for command in commands:
            with self.subTest(command=command):
                verdict, reason = self.judge_in(cwd, command)
                self.assertEqual(("deny", self.REFUSED), (verdict, reason))
                self.assertIn(phrase, reason)

    def assert_pass(self, cwd, commands, env=None):
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(("pass", ""), self.judge_in(cwd, command, env))

    # Oneshot run 2026-11-17: Flight's flight.log held 2 delete guard refusals of lines that delete nothing. A [ test
    # read as a glob command word, so as a shell, and a pipe into tail or a python filter counted as text fed to any
    # later shell on the line, past the ; that ends the pipeline.
    def test_a_pipe_that_ends_before_a_shell_passes(self):
        self.assert_pass(self.hub, (
            "T=$(.venv/bin/python -B Drawer/mem/mem.py get 4f638e9e --json 2>/dev/null | .venv/bin/python -c "
            "'import json,sys;print(json.load(sys.stdin).get(\"id\"))' 2>/dev/null); echo \"T=$T\"; "
            "[ -z \"$T\" -o \"$T\" = None ] && .venv/bin/python -B Drawer/mem/mem.py get 4f638e9e --help 2>&1 | head",
            "git fetch -q origin 2>&1|tail -2; git branch -r --contains bdad0069 2>&1|head; T=$(mktemp -d); "
            "git worktree add -q --detach $T/w origin/main 2>&1|tail -1; (cd $T/w && env -u K ../../../$PWD/.venv/bin/python "
            "-B Drawer/hub-hello.py 2>&1; echo \"rc=$?\"); echo T=$T",
            "git worktree list | tail -1; (bash -c ls)",
            "[ -z \"$x\" ] && echo hi | head",
            "[[ -n $x ]] && git branch -r | head"))

    def test_a_filter_whose_output_reaches_a_shell_is_refused(self):
        b = f"{self.peer}/build"
        self.assert_refused(self.own, (
            f"while read l; do echo 'rX -rf {b}' | tr X m; done < f | bash",
            f"(echo 'rX -rf {b}' | tr X m) | bash",
            f"{{ echo 'rX -rf {b}' | tr X m; }} | bash",
            f"if true; then echo 'rX -rf {b}' | tr X m; fi | sh",
            f"for i in 1; do echo 'rX -rf {b}' | tr X m; done | bash",
            f"echo 'rX -rf {b}' | tr X m | (cd /; bash)",
            f"echo 'rX -rf {b}' | tr X m |& bash",
            f"bash <(echo 'rX -rf {b}' | tr X m)",
            f"echo 'rX -rf {b}' | tr X m > s.sh; bash s.sh",
            f"echo 'rX -rf {b}' | tr X m | tee s.sh; bash s.sh"), "changes the echoed text")

    # Injection: git_guard.delete_reason() never judge rm or trash.
    def test_a_folder_delete_outside_own_trees_and_temp_is_refused(self):
        hub, own, peer = self.hub, self.own, self.peer
        self.assert_refused(own, (
            f"rm -rf {peer}/build", f"rm -R {hub}", "rm -rf ../bbbbbbbb-peer/build", f"rm -rf {own}",
            f"sudo rm -rf {hub}/build", f"cd {hub} && rm -rf build", f"rm -rf {hub}/bui*", f"trash {peer}/build",
            f"trash {hub}/build", f"trash {own}", "rm -rf /", f"rm --recursive -f {hub}/build",
            f"sh -c 'rm -rf {hub}/build'", f"bash <<'EOF'\nrm -rf {hub}/build\nEOF"), "is not inside")
        self.assert_refused(hub, ("rm -r build", "trash build", "command rm -fr build"), "is not inside")
        self.assert_refused(own, ("rm -rf $DIR", "rm -rf $(cat list)", "trash `pwd`/build", f"rm -rf {hub}/{{a,b}}",
                                  "cd $X && rm -rf build"), "cannot place")
        self.assert_refused(own, ("git ls-files | xargs rm -rf", f"find {hub} -type d | xargs trash"),
                            "from a pipe or find")

    # Injection: git_guard.delete_reason() never judge git worktree remove.
    def test_a_raw_worktree_remove_is_refused(self):
        self.assert_refused(self.hub, (f"git worktree remove {self.own}", f"git -C {self.hub} worktree remove --force "
                                       f"{self.peer}", f"git worktree remove {self.peer}"), "git-branch-policy.py worktree-remove")

    # Injection: git_guard.delete_reason() never call clean_reason.
    def test_git_clean_of_ignored_files_or_folders_outside_own_trees_is_refused(self):
        self.assert_refused(self.own, (f"git -C {self.hub} clean -fdx", f"git -C {self.peer} clean -fd",
                                       f"cd {self.hub} && git clean -fX", f"git clean -fdx {self.peer}/build"), "git clean")

    # Injection: git_guard.branch_reason() drop the own id check.
    def test_branch_delete_of_a_branch_with_no_own_id_is_refused(self):
        self.assert_refused(self.hub, ("git branch -D merged", "git branch -d --force merged",
                                       "git branch -D task/aaaaaaaa-done merged"), "carries no")

    # Injection: git_guard.delete_reason() never call kill_reason.
    def test_a_kill_by_name_or_by_a_computed_pid_is_refused(self):
        self.assert_refused(self.own, ("pkill -f myapp", "killall node", "fuser -k 8080/tcp", "sudo pkill claude",
                                       "kill -9 -1", "kill 0", "kill -- -4242"), "by name, file or group")
        self.assert_refused(self.own, ("kill $(pgrep myapp)", "pgrep myapp | xargs kill", "kill `cat pidfile`",
                                       "PID=$(pgrep -f myapp); kill $PID", "kill -9", "lsof -ti :8080 | xargs kill -9"),
                            "computed")

    # Injection: git_guard.reason() drop the DELETE_TEXT fallback.
    def test_a_line_that_does_not_parse_and_holds_a_delete_or_kill_word_is_refused(self):
        self.assert_refused(self.own, (f"rm -rf {self.peer}/build 'unclosed", "kill 'x", "pkill \"myapp",
                                       "git worktree remove 'x"), "does not parse")

    # Each form below passed the gate before the fix and its review.
    def test_continuations_heredoc_openers_wrappers_and_quoted_substitutions_are_judged(self):
        hub, own = self.hub, self.own
        self.assert_refused(own, (
            f"rm -rf \\\n  {hub}/build", f"cat <<EOF | bash\nrm -rf {hub}/build\nEOF",
            f"cat <<EOF && rm -rf {hub}/build\nbody\nEOF", f"sudo bash -c 'rm -rf {hub}/build'",
            f"nohup sh -c 'rm -rf {hub}/build' &", f"sudo -n rm -rf {hub}/build", f"env -u FOO rm -rf {hub}/build",
            f"env -C /tmp rm -rf {hub}/build", f"dash -c 'rm -rf {hub}/build'", f"echo \"$(rm -rf {hub}/build)\""),
            "is not inside")
        self.assert_refused(own, ("cat <<EOF; pkill claude\nbody\nEOF", "timeout 5 sh -c 'pkill x'"), "by name")
        self.assert_refused(own, ('kill "$(cat pid)"', 'kill -9 "$(cat /tmp/p)"'), "computed")
        self.assert_refused(own, (f"git --work-tree {hub} clean -fdx", f"GIT_WORK_TREE={hub} git clean -fdx"),
                            "git clean")
        self.assert_refused(own, ("rm -rf a\x00b",), "delete guard")

    # A comment line hid the command after it. A single-quoted or escaped substitution is text.
    def test_a_comment_hides_no_command_and_quoted_text_is_no_substitution(self):
        hub, own = self.hub, self.own
        self.assert_refused(own, (f"# cleanup\nrm -rf {hub}/build", f"echo hi # note\nrm -rf {hub}/build",
                                  f"echo \"$(rm -rf {hub}/build)\""), "is not inside")
        self.assert_refused(own, (f"bash -c 'echo 1\n# c\nrm -rf {hub}/build'", f"sh -c \"x=1 # c\nrm -rf {hub}/build\"",
                                  f"eval 'echo 1\n# c\nrm -rf {hub}/build'"), "is not inside")
        self.assert_refused(own, ("# stop it\npkill myapp",), "by name")
        self.assert_pass(own, ("git commit -m 'drop `rm -rf` use trash'", "echo '$(rm -rf /)'",
                               "git commit -m \"fix: guard \\`kill \\$(pgrep x)\\` words\"", "echo \"a # b\" && ls",
                               "echo $# ${#x}", "cat > /tmp/t.md <<'EOF'\nrm -rf /\nEOF\nbash -n /tmp/t.sh"))

    def test_redirects_symlinks_and_tmpdir_pass(self):
        own, hub = self.own, self.hub
        link = os.path.join(own, "hub-build-link")
        if not os.path.islink(link):
            os.symlink(os.path.join(hub, "build"), link)
        self.assert_pass(own, ("ps -p 123 >/dev/null && kill 123 2>/dev/null",
                               "kill -9 123 >/dev/null 2>&1; ps aux | grep x", "kill 123 > /dev/null",
                               "rm -rf hub-build-link", "trash hub-build-link", "rm -rf build 2>/dev/null"))
        self.assert_refused(own, ("rm -rf hub-build-link/",), "is not inside")
        self.assert_pass(own, ('rm -rf "$TMPDIR/scratch"', "trash ${TMPDIR}/scratch"), {"TMPDIR": self.tmpdir})

    # A Flight review of the delete guard: each form below passed the landed gate.
    def test_find_delete_cd_control_flow_case_and_self_kill_are_refused(self):
        hub, own, peer = self.hub, self.own, self.peer
        self.assert_refused(own, (f"find {hub}/build -delete", f"find {peer} -name build -delete",
                                  f"find -L {hub} -name build -exec rm -rf {{}} +", f"find {hub} -type d -exec trash {{}} +"),
                            "is not inside")
        self.assert_refused(hub, ("(cd /tmp); rm -rf build", "false && cd /tmp; rm -rf build", "cd -- /tmp && rm -rf build",
                                  "pushd /tmp; popd; rm -rf build", "(cd /tmp && true); trash build",
                                  f"bash -c 'cd {own} && rm -rf ../../hub/build'"), "is not inside")
        self.assert_refused(own, (f"RM -rf {hub}/build", f"Trash {hub}/build", f"SUDO rm -rf {hub}/build"), "is not inside")
        self.assert_refused(own, ("PKILL myapp", "KillAll node"), "by name")
        self.assert_refused(own, ("GIT worktree remove x",), "git-branch-policy.py worktree-remove")
        self.assert_refused(own, ("kill -9 $PPID", "kill -9 $$", "kill ${PPID}"), "this shell")
        self.assert_refused(own, ("P=$(cat pidfile); kill $P", "P=`cat pidfile` && kill -9 ${P}"), "computed")
        self.assert_refused(own, ('D=$(mktemp -d /opt/guard/x.XXXX) && rm -rf "$D"', 'T=$(mktemp -d); T=/x; rm -rf "$T"'),
                            "cannot place")
        self.assert_refused(own, ('T=$(mktemp -d) && rm -rf "$T/.."',), "is not inside")

    # A review of the guard: a quoted or escaped find group, more self kills, a reused mktemp name, find -exec sh -c.
    def test_find_groups_self_kills_and_reused_mktemp_names_are_refused(self):
        hub = self.hub
        self.assert_refused(self.own, (f"find {hub} \\( -name a -o -name build \\) -delete",
                                       f"find {hub} '(' -name build ')' -delete"), "is not inside")
        self.assert_refused(self.own, ("kill -9 -$$", "kill $BASHPID", "kill ${PPID:-1}"), "this shell")
        self.assert_refused(self.own, ('T=$(mktemp -d); read -r T; rm -rf "$T"', 'T=$(mktemp -d); T+=/x; rm -rf "$T"'),
                            "cannot place")
        self.assert_refused(self.own, (f"find {hub} -type d -exec sh -c 'rm -rf \"$1\"' _ {{}} \\;",), "runs a shell")
        self.assert_pass(self.own, ("kill -0 $$", "kill -s 0 $PPID", "git commit -m 'guard (find) groups; fix'",
                                    "echo \\( a \\)"))

    def test_mktemp_folders_find_under_temp_and_own_finds_pass(self):
        own, tmpdir = self.own, self.tmpdir
        self.assert_pass(own, ('TMPD=$(mktemp -d) && rm -rf "$TMPD"', "d=$(mktemp -d -t guard) ; trash $d",
                               'T="$(mktemp -d)"; rm -rf "${T}/sub"', f"find {own}/build -delete",
                               "find . -name '*.pyc' -delete", f"find {own} -name build -exec rm -rf {{}} +",
                               "find /tmp/no-such-guard-folder -delete", f"find {self.hub} -name '*.pyc' -exec rm {{}} +",
                               "kill 4242", "kill $!"))
        self.assert_pass(own, (f"find {tmpdir}/scratch -name build -exec rm -rf {{}} +", f"find {tmpdir}/scratch -delete",
                               f"cd {tmpdir} && rm -rf scratch"),
                         {"TMPDIR": tmpdir})

    # A command piped into a shell passed the gate (an earlier decision kept it out of scope).
    def test_a_command_piped_into_a_shell_is_judged(self):
        hub, peer, own, tmpdir = self.hub, self.peer, self.own, self.tmpdir
        self.assert_refused(own, (
            f"echo 'rm -rf {peer}/build' | bash", f"echo rm -rf {peer}/build | sh", f"echo \"trash {peer}\" | zsh",
            f"sh -c 'trash {peer}/build'", f"printf '%s\\n' 'rm -rf {hub}/build' | bash",
            f"printf 'rm -rf %s\\n' {peer}/build | sh -s", f"echo 'echo hi; rm -rf {peer}/build' | sudo bash",
            f"echo -e 'ls\\nrm -rf {peer}/build' | bash", f"echo 'rm -rf {peer}/build' | cat | tee /dev/null | bash -",
            f"cd {own} && echo 'rm -rf ../bbbbbbbb-peer/build' | bash", f"echo 'rm -rf {peer}/build' | (bash)",
            f"printf 'rm -rf %s\\n' ./build {peer}/build | bash", f"printf 'rm -rf %s;' a {peer}/build | bash",
            f"echo 'rm -rf {peer}/build' | bash -s -- a", f"echo 'rm -rf {peer}/build' | bash -o pipefail",
            f"(echo 'rm -rf {peer}/build') | bash", f"{{ echo 'rm -rf {peer}/build'; }} | bash",
            f"echo 'rm -rf {peer}/build' | (cd /; bash)", f"echo -e '\\x72m -rf {peer}/build' | bash",
            f"printf '\\162m -rf {peer}/build' | bash", f"echo 'rm -rf {peer}/build' | cat -n | sed s/a/a/ | bash",
            f"echo 'rm -rf {peer}/build' | xargs -I{{}} bash -c '{{}}'", f"echo 'rm -rf {peer}/build' | bash /dev/stdin",
            f"echo 'rm -rf {peer}/build' | source /dev/stdin",
            f"echo -e 'rm\\t-rf\\t{peer}/build' | bash", f"printf '%s ' rm -rf {peer}/build | bash",
            f"echo 'rm -rf {peer}/build' |\n bash", f"echo 'rm -rf {peer}/build' | # c\n bash",
            f"printf '\\x72m -rf {peer}/build\\x00' | bash", f"printf '\\u0072m -rf {peer}/build' | zsh",
            f"echo 'rm -rf {peer}/build' | bash -s -- -rc"), "is not inside")
        self.assert_refused(own, ("echo 'pkill myapp' | bash",), "by name")
        self.assert_refused(own, ("echo 'kill $(pgrep myapp)' | sh",), "computed")
        self.assert_refused(own, ("echo \"rm -rf $DIR\" | bash",), "cannot place")
        self.assert_pass(own, (f"echo 'rm -rf {tmpdir}/scratch' | bash", f"sh -c 'trash {tmpdir}/scratch'",
                               f"printf 'rm -rf %s\\n' {tmpdir}/scratch | sh", f"echo 'rm -rf {own}/build' | bash",
                               "echo 'ls -la' | bash", f"echo 'rm -rf {peer}/build' | grep rm",
                               f"echo 'rm -rf {peer}/build' || bash", "echo hi; bash -n x.sh"), {"TMPDIR": tmpdir})

    # The cold review found shell feeds the piped-shell guard did not read.
    def feed_forms(self, target, start):
        """Each named shell-feed form that deletes the folder target (start/build), as one command list."""
        t = f"{target}/build"
        return (
            f"{{ echo 'cd {target}'; echo 'rm -rf build'; }} | bash", f"(printf 'cd {target}\\n'; printf 'rm -rf build') | sh",
            f"echo 'rm -rf {t}' | bash -c \"$(cat)\"", f"echo 'rm -rf {t}' | sh -c bash", f"echo 'rm -rf {t}' | sh -c 'sh'",
            f"echo 'rm -rf {t}' | bash -c 'source /dev/stdin'", f"echo 'rm -rf {t}' | eval \"$(cat)\"",
            f"eval \"$(echo 'rm -rf {t}')\"", f"eval \"$(printf 'rm -rf %s' {t})\"", f"eval `echo rm -rf {t}`",
            f"bash -c \"$(echo rm -rf {t})\"", f"$(echo rm) -rf {t}",
            f"echo 'rm -rf {t}' | while read l; do eval \"$l\"; done", f"echo 'rm -rf {t}' | while read -r l; do $l; done",
            f"echo 'rm -rf {t}' | $SHELL", f"echo 'rm -rf {t}' | \"$SHELL\"", f"echo 'rm -rf {t}' | ${{SHELL}}",
            f"$SHELL -c 'rm -rf {t}'", f"echo 'rm -rf {t}' | csh", f"echo 'rm -rf {t}' | tcsh",
            f"echo $'rm -rf {t}' | bash", f"echo $'\\x72m -rf {t}' | bash", f"bash -c $'rm -rf {t}'", f"rm -rf $'{t}'",
            f"bash < <(echo 'rm -rf {t}')", f"bash <(echo 'rm -rf {t}')", f"source <(echo 'rm -rf {t}')",
            f". <(printf 'rm -rf {t}')", f"echo 'rm -rf {t}' > >(bash)", f"bash <<< 'rm -rf {t}'",
            f"$'\\x72m' -rf {t}", f"$(printf '\\x72m') -rf {t}", f"bash <(printf '\\162m -rf {t}')",
            f"`printf '\\x72m'` -rf {t}", f"echo $'\\x72m -rf {t}' > >(tcsh)")

    def review_forms(self, target):
        """The forms the cold review found working against the first cut of the shell-feed guard."""
        t = f"{target}/build"
        return (
            f"bash<<<'rm -rf {t}'", f"bash <<<'rm -rf {t}'", f"bash 0<<< 'rm -rf {t}'", f"bash /dev/fd/3 3<<< 'rm -rf {t}'",
            f"cat <<< 'rm -rf {t}' | bash", f"tee <<< 'rm -rf {t}' | bash",
            f"{{ echo -n 'r'; echo 'm -rf {t}'; }} | bash", f"{{ printf 'rm -rf {t[:-3]}'; echo ild; }} | bash",
            f"eval \"$(echo -n 'r'; echo 'm -rf {t}')\"", f"bash -c \"$(printf 'r'; printf 'm -rf {t}')\"",
            f"bash <(echo -n 'r'; echo 'm -rf {t}')", f"$(echo -n r; echo m) -rf {t}",
            f"print 'rm -rf {t}' | bash", f"print -r -- 'rm -rf {t}' | bash", f"bash < <(print 'rm -rf {t}')",
            f"eval \"$(print 'rm -rf {t}')\"", f"yes 'rm -rf {t}' | bash", f"bash =(echo 'rm -rf {t}')",
            f"eval \"$(cat <<< 'rm -rf {t}')\"", f"bash <(cat <<< 'rm -rf {t}')", f"eval \"$(echo 'rm -rf {t}' | cat)\"",
            f"x='rm -rf {t}'; eval \"$x\"", f"x='rm -rf {t}'; bash -c \"$x\"", f"x='rm -rf {t}'; echo \"$x\" | bash",
            f"x='rm -rf {t}'; bash <<< \"$x\"", f"read x <<< 'rm -rf {t}'; eval \"$x\"", f"read -r x <<< 'rm -rf {t}'; $x",
            f"cmd=$(echo 'rm -rf {t}'); eval \"$cmd\"",
            f"echo 'rm -rf {t}' | bash5", f"echo 'rm -rf {t}' | /usr/local/bin/bash-5.2", f"echo 'rm -rf {t}' | /bin/$0",
            f"echo 'rm -rf {t}' | stdbuf -o0 bash", f"echo 'rm -rf {t}' | parallel", f"echo 'rm -rf {t}' | sudo -s",
            f"echo 'rm -rf {t}' | su -c bash")

    def test_the_review_forms_are_judged(self):
        own, tmpdir = self.own, self.tmpdir
        self.assert_refused(own, self.review_forms(self.peer), "is not inside")
        self.assert_refused(own, (
            "sed 's/X/m/' <<< 'rX -rf x' | bash", "eval \"$(echo 'rX -rf x' | sed 's/X/m/')\"",
            "bash -c \"$(echo rX | sed 's/X/m/')\"", "bash < <(echo 'rX -rf x' | sed 's/X/m/')",
            "source <(echo 'rX' | tr X m)", "echo 'rX -rf x' > >(sed 's/X/m/' | bash)",
            "echo 'rX' | tee >(sed s/X/m/ | bash) >/dev/null", "s=sed; echo 'rX -rf x' | $s 's/X/m/' | bash",
            "{ echo 'rm -rf build'; echo 'cd /x'; } | sort | bash", "echo 'rm -rf x/y' | grep -o 'rm -rf x/' | bash",
            "echo 'rm -rf x' | head -c 5 | bash"), "cannot read")
        self.assert_pass(own, self.review_forms(f"{tmpdir}/scratch") + (
            "eval \"$(direnv hook zsh)\"", "eval \"$(ssh-agent -s)\"", "source <(kubectl completion zsh)",
            "diff <(echo a) <(echo b)", "grep foo <<< \"$x\"", "echo $SHELL | cat", "ls | $SHELL -c 'wc -l'",
            "find . | xargs -I{} sh -c 'wc -l {}'", "git log --oneline | head -5", "ls | sort | uniq -c"),
            {"TMPDIR": tmpdir})

    def second_review_forms(self, target, start):
        """Readable forms the second cold review found working."""
        t = f"{target}/build"
        return (
            f"{{ bash; }} <<< 'rm -rf {t}'", f"(bash) <<< 'rm -rf {t}'", f"{{ read x; eval \"$x\"; }} <<< 'rm -rf {t}'",
            f"{{ cat | sh; }} <<< 'rm -rf {t}'", f"exec <<< 'rm -rf {t}'; bash", f"exec 3<<< 'rm -rf {t}'; bash <&3",
            f"cat <<EOF | xargs -I{{}} sh -c {{}}\nrm -rf {t}\nEOF", f"eval \"$(cat <<EOF\nrm -rf {t}\nEOF\n)\"",
            f"while read l; do eval \"$l\"; done <<< 'rm -rf {t}'", f"bash <<< $(echo 'rm -rf {t}')",
            f"eval -- \"$(echo 'rm -rf {t}')\"", f"builtin eval \"$(echo 'rm -rf {t}')\"",
            f"builtin source <(echo 'rm -rf {t}')", f"builtin . <(echo 'rm -rf {t}')",
            f"echo 'rm -rf {t}' | bash --rcfile /dev/null", f"echo 'rm -rf {t}' | pwsh", f"echo 'rm -rf {t}' | oksh",
            f"echo 'rm -rf {t}' | ${{SHELL##*/}}", f"echo 'rm -rf {t}' | arch -arm64 bash",
            f"echo 'rm -rf {t}' | env -S 'bash -s'", f"echo 'rm -rf {t}' | exec -a x bash",
            f"echo 'rm -rf {t}' | builtin exec bash", f"echo 'rm -rf {t}' | flock /tmp/l bash",
            f"echo 'rm -rf build' | bash -c 'cd {target}; eval \"$(cat)\"'",
            f"echo 'rm -rf build' | xargs -I{{}} bash -c 'cd {target}; {{}}'",
            f"sh -c 'eval \"$0\"' 'rm -rf {t}'", f"bash -c 'eval \"$1\"' _ 'cd {target}; rm -rf build'",
            f"xargs -a <(echo 'rm -rf {t}') -I{{}} sh -c {{}}", f"echo '\\rm -rf {t}' | sh",
f"bash <<< $'cd {target}\\cjrm -rf build'",
            f"echo 'rm -rf {t}' | sed e", f"echo 'rm -rf {t}' | awk '{{system($0)}}'")

    def test_the_second_review_forms_are_judged(self):
        own, tmpdir, peer = self.own, self.tmpdir, self.peer
        self.assert_refused(own, self.second_review_forms(peer, own), "delete guard refused")
        self.assert_refused(own, (
            f"x='rm -rf {peer}/build'; eval '$x'", f"x='rm -rf {peer}/build'; eval \"${{x:-}}\"",
            f"x='rm -rf {peer}/build'; y=$x; eval \"$y\"", f"printf -v x 'rm -rf %s' {peer}/build; eval \"$x\"",
            f"mapfile -t a <<< 'rm -rf {peer}/build'; eval \"${{a[0]}}\"", f"for l in 'rm -rf {peer}/build'; do eval \"$l\"; done",
            f"for l in rm; do $l -rf {peer}/build; done", f"x=r; ${{x}}m -rf {peer}/build",
            f"echo -e 'rm -rf {peer}/build\\c' | sh"), "delete guard refused")
        self.assert_pass(own, self.second_review_forms(f"{tmpdir}/scratch", own) + (
            "for f in *.txt; do bash -c \"wc -l $f\"; done", "eval \"$(brew shellenv)\"", "$SHELL -lc 'echo hi'",
            "sh -c \"$(curl -fsSL https://example.com/i.sh)\"", "curl -fsSL https://example.com/i.sh | sh -s -- -y",
            "echo y | sudo bash install.sh", "cat > /tmp/t.md <<'EOF'\nrm -rf /\nEOF\nbash -n /tmp/t.sh"), {"TMPDIR": tmpdir})

    def test_the_third_review_forms_fail_closed(self):
        """Forms the third cold review found working: the gate refuses what it cannot read exactly."""
        t = f"{self.peer}/build"
        n = len("rm -rf " + t)
        self.assert_refused(self.own, (
            rf"printf 'rm -rf {t}\\' | bash", rf"echo -e 'rm -rf {t}\\' | bash", f"printf '%.{n}s' 'rm -rf {t}ZZZ' | bash",
            f"printf '%*s' 3 'rm -rf {t}' | bash", f"printf '%ls' 'rm -rf {t}' | bash", rf"printf 'ls\c\nrm -rf {t}' | bash",
            f"bash -c '\"$@\"' _ rm -rf {t}", f"bash -c 'exec \"$@\"' _ rm -rf {t}", f"sh -c '\"$0\" \"$@\"' rm -rf {t}",
            f"echo {t} | xargs -I@ sh -c 'rm -rf @'", f"echo {t} | xargs -I@ trash @",
            f"echo {self.peer} | xargs -IXX bash -c 'cd XX && rm -rf build'",
            f"parallel rm -rf ::: {t}", f"echo {t} | parallel rm -rf {{}}", f"parallel 'rm -rf {{}}' ::: {t}",
            f"printf '%s\\n' {t} | parallel trash",
            f"x='rm -rf {t}'; echo ${{x:-}} | bash", f"x='rm -rf {t}'; y=$x; echo \"$y\" | bash",
            f"set -- rm -rf {t}; \"$@\"", f"f() {{ eval \"$1\"; }}; f 'rm -rf {t}'",
            f"echo 'rm -rf {t}' | /bin/ba*", f"echo 'rm -rf {t}' | /bin/b?sh",
            f"bash <<EOF\nr\\\nm -rf {t}\nEOF", r"bash <<EOF" + "\n" + rf"\\rm -rf {t}" + "\nEOF",
            f"mkfifo /tmp/f; bash < /tmp/f & echo 'rm -rf {t}' > /tmp/f",
            f"coproc bash; echo 'rm -rf {t}' >&${{COPROC[1]}}",
            f"awk 'BEGIN{{system(\"rm -rf {t}\")}}'", f"awk 'BEGIN{{print \"rm -rf {t}\" | \"bash\"}}'",
            f"echo x | sed 's/x/rm -rf {t.replace('/', '\\/')}/e'", f"sed '1e rm -rf {t}' f",
            f"echo 'set x = rm; $x -rf {t}' | csh"), "delete guard refused")

    def test_the_fourth_review_forms_fail_closed(self):
        """Forms the fourth cold review found working."""
        t = f"{self.peer}/build"
        self.assert_refused(self.own, (
            f"x=rm; x+=' -rf {t}'; $x", f"x=rm; x+=' -rf {t}'; eval \"$x\"", f"x=rm; x+=' -rf {t}'; echo $x | bash",
            f"x=rm; x=\"$x -rf {t}\"; $x", f"export x=rm; export x=\"$x -rf {t}\"; $x", f"x=rm; (x=\"$x -rf {t}\"; $x)",
            f"x=rm; false && x=ls; $x -rf {t}", f"x=rm; echo hi || x=ls; eval \"$x -rf {t}\"",
            f"x=ls; for x in rm; do $x -rf {t}; done", f"x=rm; for x in 'rm -rf {t}'; do $x; done",
            f"x=rm; printf -v x '%s -rf {t}' rm; $x", f"a=r; b=m; bash -c \"$a$b -rf {t}\"",
            f"echo${{IFS}}'rm -rf {t}' | bash", f"echo$IFS'rm -rf {t}' | sh", rf"printf${{IFS}}'rm -rf {t}\n' | bash",
            f"{{echo,'rm -rf {t}'}} | bash", f"echo ${{x:-rm}} -rf {t} | bash", f"echo 'rm -rf {t}' | {{sh,}}",
            f"rm${{IFS}}-rf${{IFS}}{t}", f"echo 'rm${{IFS}}-rf${{IFS}}{t}' | bash", f"bash -c 'rm${{IFS}}-rf${{IFS}}{t}'",
            f"/bin/r[m] -rf {t}", f"echo '/bin/r[m] -rf {t}' | bash", f"bash -c '/bin/r? -rf {t}'", f"{{rm,-rf,{t}}}",
            f"echo '{{rm,-rf,{t}}}' | bash", r"printf '%s%b\n' ': \x23 ;' 'rm -rf \x2e\x2e/bbbbbbbb-peer/build' | bash",
            f"cat <<EOF\n$(rm -rf {t})\nEOF", f"trap 'rm -rf {t}' EXIT",
            f"echo 'rm -rf {t}' > /tmp/s.sh; bash /tmp/s.sh", f"echo 'rm -rf {t}' > /tmp/s.sh; bash < /tmp/s.sh",
            f"echo 'rm -rf {t}' > /tmp/s.sh; . /tmp/s.sh", f"echo 'rm -rf {t}' | ssh localhost",
            f"echo 'rm -rf {t}' | script -q /dev/null"), "delete guard refused")

    def test_the_fifth_review_forms_fail_closed(self):
        """Forms the fifth cold review found working."""
        t, peer = f"{self.peer}/build", self.peer
        self.assert_refused(self.own, (
            f"echo 'r${{u}}m -rf {t}' | bash", f"bash <<< 'r${{u}}m -rf {t}'", f"bash -c 'r${{u}}m -rf {t}'",
            f"trap 'r${{u}}m -rf {t}' EXIT", f"find {peer} -name build -exec r${{u}}m -rf {{}} +",
            f"echo 'rm$u -rf {t}' | sh", f"echo 'r$@m -rf {t}' | sh", f"echo 'r$(:)m -rf {t}' | sh",
            f"echo \"t\\$u''rash {t}\" | sh", f"r${{u}}m -rf {t}",
            f"a=r; a+=m; echo $a -rf {t} | bash", f"a=r; a+=m; bash <<< \"$a -rf {t}\"",
            f"a=r; a+=m; source <(echo $a -rf {t})", f"a=r; a+=m; cat <<< \"$a -rf {t}\" | sh",
            f"a=r; a+=m; trap \"$a -rf {t}\" EXIT", f"a=r; a+=m; find {peer} -exec $a -rf {{}} +",
            f"a=r; b=m; a=$a$b; echo $a -rf {t} | sh", f"a=(r m); echo ${{a[0]}}${{a[1]}} -rf {t} | sh",
            f"a[0]=r; a[1]=m; echo ${{a[0]}}${{a[1]}} -rf {t} | sh", f"tr=tra; tr+=sh; echo $tr {t} | sh",
            f"echo '<<EOF'\nrm -rf {t}\nEOF", f"echo \"<<EOF\"\nrm -rf {t}\nEOF", f"echo \\<<EOF\nrm -rf {t}\nEOF",
            f"# <<EOF\nrm -rf {t}\nEOF", f"true '<<EOF'\ntrash {t}\nEOF"), "delete guard refused")

    def test_the_sixth_review_forms_fail_closed(self):
        """Forms the sixth cold review found working."""
        t, peer = f"{self.peer}/build", self.peer
        self.assert_refused(self.own, (
            f"echo $((1<<EOF))\nrm -rf {t}\nEOF", f"echo ${{x:-<<EOF}}\nrm -rf {t}\nEOF",
            f"ls {peer} | sed 's|^|rm -rf {peer}/|' | sh", f"ls {peer} | awk '{{print \"rm -rf \" $0}}' | sh",
            f"function f {{ rm -rf $1; }}; f {t}", f"f() {{ sh -c \"$1\"; }}; f \"rm -rf {t}\"",
            f": ${{x:=rm}}; $x -rf {t}", f": ${{x=rm}}; eval \"$x -rf {t}\"", f"getopts a: x -a rm; $OPTARG -rf {t}",
            f"echo build | xargs -J % rm -rf {peer}/%", f"echo build | xargs -tI X rm -rf {peer}/X",
            f"cat <<'EOF' >/tmp/x.sh\nrm -rf {t}\nEOF\nsh /tmp/x.sh", f"echo \"rm -rf {t}\" >/tmp/x.sh; cd /tmp && sh x.sh",
            f"parallel sh -c ::: \"rm -rf {t}\"", f"parallel <<<'rm -rf {t}'", f"parallel {{}} ::: \"rm -rf {t}\"",
            f"parallel --colsep , rm -rf {{1}} ::: {t}", f"sudo -s <<< 'rm -rf {t}'", f"sudo -s 'rm -rf {t}'",
            f"date '+rm -rf {t}' | sh", f"git log -1 --format='rm -rf {t}' | sh", f"eval \"$(date '+rm -rf {t}')\"",
            f"$(which rm) -rf {t}", f"trap -- 'rm -rf {t}' EXIT", f"env -S 'rm -rf {t}'", f"env -S 'bash -c \"rm -rf {t}\"'",
            f"echo 'rm -rf build' | env -C {peer} sh", f"sh -c '$0 -rf $1' rm {t}", f"coproc echo 'rm -rf {t}'; sh <&${{COPROC[0]}}",
            f"y=xrm; echo ${{y#x}} -rf {t} | sh", f"y=rmx; echo ${{y%x}} -rf {t} | sh", f"bash -Cc 'rm -rf {t}'",
            f"echo 'rm -rf {t}' | yash", f"PROMPT_COMMAND='rm -rf {t}' bash -i", f"let 'a[$(rm -rf {t})]=1'",
            f"awk 'BEGIN{{cmd=\"rm -rf {t}\"; cmd | getline}}'", f"awk 'BEGIN{{system(\"r\" \"m -rf {t}\")}}'"),
            "delete guard refused")

    def spliced_forms(self, target):
        """Forms the seventh cold review found: fed code splices into the line's own commands."""
        t = f"{target}/build"
        return (
            f"echo 'cd {target}' | bash -c 'eval \"$(cat)\"; rm -rf build'", f"echo 'cd {target}' | bash -c 'source /dev/stdin; rm -rf build'",
            f"echo 'cd {target}' | bash -c '. /dev/stdin; rm -rf build'", f"echo 'cd {target}' | $SHELL -c 'source /dev/stdin; rm -rf build'",
            f"echo 'cd {target}' | csh -c 'source /dev/stdin; rm -rf build'",
            f"echo 'cd {target}' | {{ while read l; do eval \"$l\"; done; rm -rf build; }}",
            f"echo rm | bash -c '$(cat) -rf {t}'", f"echo -rf | bash -c 'rm $(cat) {t}'", f"echo -rf | bash -c 'read x; rm $x {t}'",
            f"echo rm | eval \"$(cat) -rf {t}\"", f"echo rm | $SHELL -c '$(cat) -rf {t}'",
            f"source <(echo 'cd {target}'); rm -rf build", f". <(echo cd {target}) && rm -rf build",
            f"source =(echo 'cd {target}'); rm -rf build", f"source <(echo 'cd {target}'); trash build",
            f"echo 'cd {target}' | source /dev/stdin; rm -rf build", f"echo 'cd {target}' | . /dev/stdin; rm -rf build",
            f"echo 'cd {target}' | {{ eval \"$(cat)\"; }}; rm -rf build", f"eval 'cd {target}'; rm -rf build",
            f"eval \"$(echo cd {target})\"; rm -rf build")

    def test_fed_code_is_judged_where_it_runs(self):
        self.assert_refused(self.own, self.spliced_forms(self.peer) + (
            rf"echo $'rm -rf {self.peer}/build\0junk' | bash",), "delete guard refused")
        self.assert_pass(self.own, self.spliced_forms(f"{self.tmpdir}/scratch"), {"TMPDIR": self.tmpdir})

    def test_the_eighth_review_forms_fail_closed(self):
        """Forms the eighth cold review found working."""
        p, t = self.peer, f"{self.peer}/build"
        self.assert_refused(self.own, (
            f"source /dev/stdin <<< 'cd {p}'; rm -rf build", f". /dev/stdin <<EOF\ncd {p}\nEOF\nrm -rf build",
            f"source /dev/stdin < <(echo 'cd {p}'); rm -rf build", f"exec <<< 'cd {p}'; source /dev/stdin; rm -rf build",
            f"exec 3<<< 'cd {p}'; source /dev/fd/3; rm -rf build", f"source -- /dev/stdin <<< 'cd {p}'; rm -rf build",
            f"source /dev/stdi? <<< 'cd {p}'; rm -rf build", f"source /dev/std{{in,}} <<< 'cd {p}'; rm -rf build",
            f"source /dev/fd/$((0)) <<< 'cd {p}'; rm -rf build",
            f"while read l; do $l; done <<< 'cd {p}'; rm -rf build",
            f"while read -r a b; do \"$a\" \"$b\"; done <<< 'cd {p}'; rm -rf build",
            f"eval \"$(cat <<EOF\ncd {p}\nEOF\n)\"; rm -rf build",
            f"echo 'cd {p}' | bash -c 'builtin source /dev/stdin; rm -rf build'",
            rf"echo $'\562m -rf {t}' | bash", rf"printf '\562m -rf {t}' | bash", rf"bash -c $'\562m -rf {t}'",
            rf"$'\562m' -rf {t}", rf"bash -c $'\\0; rm -rf {t}'", rf"echo $'\\0\nrm -rf {t}' | bash",
            "echo 'cd ../*peer; rm -rf build' | bash", "echo 'cd ../bbbbbbbb-pee?; rm -rf build' | bash",
            f"echo 'builtin cd {p}; rm -rf build' | bash", f"echo '{{ cd {p}; }}; rm -rf build' | bash",
            "echo 'rm -rf ~+/../bbbbbbbb-peer/build' | bash", f"printf 'r\"\"m -rf {t}\\necho \"' | bash",
            f"echo 'r``m -rf {t}' | bash"), "delete guard refused")

    def test_the_ninth_review_forms_fail_closed(self):
        """Forms the ninth cold review found working."""
        p, t = self.peer, f"{self.peer}/build"
        self.assert_refused(self.own, (
            f"echo 'repeat 1 rm -rf {t}' | tcsh", f"echo 'repeat 1 rm -rf {t}' | zsh", f"echo 'noglob rm -rf {t}' | $SHELL",
            f"echo 'nocorrect rm -rf {t}' | zsh", f"echo '- rm -rf {t}' | zsh", f"echo '=rm -rf {t}' | zsh", f"=rm -rf {t}",
            f"printf '%c%c -rf {t}' rxx mxx | bash", f"printf 'r%s' m | bash -c '$(cat -u) -rf {t}'",
            f"echo rm | bash -c '$(cat /dev/fd/0) -rf {t}'", f"echo rm | $SHELL -c '$(cat <&0) -rf {t}'",
            f"printf 'r%s' m | eval \"$(tee) -rf {t}\"", f"printf 'r%s' m | eval \"`cat` -rf {t}\"",
            f"printf 'r%s' m | eval \"$(head -n1) -rf {t}\"", f"printf 'r%s' m | eval $(cat) -rf {t}",
            f"source <(cat <<EOF\ncd {p}\nEOF\n); rm -rf build", f". <(cat <<EOF\ncd {p}\nEOF\n); rm -rf build",
            f"echo 'rm -rf build' | bash -c 'cd {p}; bash; echo $(cat)'",
            f"printf 'r%s\\n' m | xargs -I{{}} sh -c '{{}} -rf {t}'", f"printf 'r%s\\n' m | xargs sh -c '$0 -rf {t}'"),
            "delete guard refused")

    def test_the_tenth_review_forms_fail_closed(self):
        """Forms the tenth cold review found working."""
        p, t = self.peer, f"{self.peer}/build"
        trees = os.path.dirname(p)
        self.assert_refused(self.own, (
            rf"printf 'rm -rf {t}\0401' | bash", rf"printf 'rm -rf {t}\0401' | sh -c \"$(cat)\"", rf"bash <(printf 'rm -rf {t}\0401')",
            rf"echo $'rm -rf {trees}\057bbbbbbbb-peer/build' | bash", rf"rm -rf {trees}$'\057'bbbbbbbb-peer/build",
            f"print -u1 'rm -rf {t}' | bash", f"print -C1 'rm -rf {t}' | zsh", f"print -P 'rm -rf %(0?:{t}:)' | bash",
            f"print -P 'cd %(0?:{p}:)' | bash"), "delete guard refused")

    def test_the_eleventh_review_forms_fail_closed(self):
        """Forms the eleventh cold review found working."""
        p, t = self.peer, f"{self.peer}/build"
        self.assert_refused(self.own, (
            f"echo '>/dev/null; rm -rf {t}' | bash", f"echo '<x; rm -rf {t}' | bash", f"echo '2>/dev/null; rm -rf {t}' | bash",
            f"echo 'cd {p}' '>/dev/null; rm -rf build' | bash", f"source <(echo '>/dev/null; rm -rf {t}')",
            f"echo 'echo rm -rf {t}' | bash | bash", f"echo 'echo cd {p}; echo rm -rf build' | bash | bash",
            f"bash -c 'echo rm -rf {t}' | bash", f"bash <<< 'echo rm -rf {t}' | bash", f"eval 'echo rm -rf {t}' | bash",
            f"echo 'r\\\nm -rf {t}' | bash", f"bash <<< 'r\\\nm -rf {t}'", f"eval 'r\\\nm -rf {t}'", f"bash -c 'r\\\nm -rf {t}'",
            f"bash <<'EOF'\nr\\\nm -rf {t}\nEOF", f"r\\\nm -rf {t}",
            f"echo 'for f do rm -rf $f; done' | sh -s -- {t}", f"bash -c 'for f do rm -rf \"$f\"; done' _ {t}",
            f"set -- {t}; for f do rm -rf $f; done",
            f"printf '%s\\n' {p} 'rm -rf build' | {{ read a; read b; cd \"$a\"; $b; }}",
            f"printf '%s\\n' {p} 'rm -rf build' | while read a; do read b; cd \"$a\"; $b; done",
            f"bash -c 'cd \"$1\"; shift; \"$@\"' _ {p} rm -rf build", f"echo '/usr/bin/time rm -rf {t}' | bash",
            f"env -P /bin rm -rf {t}"), "delete guard refused")

    def test_the_twelfth_review_forms_fail_closed(self):
        """Forms the twelfth cold review found working."""
        p, t, tmp = self.peer, f"{self.peer}/build", f"{self.tmpdir}/scratch/build"
        self.assert_refused(self.own, (
            rf"echo '\cd {p}; rm -rf build' | bash", rf"echo '\cat /dev/null; rm -rf {t}' | sh",
            f"eval \"$(echo '\\rm -rf {t}')\"", f"bash -c \"$(echo 'x\\c; rm -rf {t}')\"",
            f"bash <<< \"$(echo 'x\\c; rm -rf {t}')\"",
            f"{{ echo 'cd {p}'; cat <<EOF\nrm -rf build\nEOF\n}} | bash", f"( echo 'cd {p}'; cat <<EOF\nrm -rf build\nEOF\n) | bash",
            "echo 'cd ~+/../bbbbbbbb-peer; rm -rf build' | bash", "cd ~+/../bbbbbbbb-peer; rm -rf build",
            f"echo 'cd {p}\n{{rm,}} -rf build' | bash", f"cd {p}\n{{rm,}} -rf build",
            f"echo 'cd {p}' | bash 2>&1 -c 'eval \"$(cat)\"; rm -rf build'", f"{{ echo 2>&1 'cd {p}'; echo 'rm -rf build'; }} | bash",
            f"bash < <(echo 2>&1 'rm -rf {t}')", f"rm 2>&1 -rf {t}", f"bash 2>&1 -c \"rm -rf {t}\"", f"env 2>&1 rm -rf {t}",
            f"${{u}}rm -rf {t}", f"$u$u rm -rf {t}", f"\"$u\"rm -rf {t}", f"$! rm -rf {t}"), "delete guard refused")
        self.assert_pass(self.own, (f"echo 'rm -rf {tmp}' | bash -c 'read x; eval \"$x\"'",
                                    f"echo 'rm -rf {tmp}' | bash -c 'while read l; do eval \"$l\"; done'"), {"TMPDIR": self.tmpdir})

    def test_the_thirteenth_review_forms_fail_closed(self):
        """Escapes zsh and bash decode differently, found by the thirteenth cold review."""
        p, t = self.peer, f"{self.peer}/build"
        self.assert_refused(self.own, (
            rf"echo 'r\xm -rf {t}' | bash", rf"print 'r\um -rf {t}' | bash", rf"source <(echo 'r\xm -rf {t}')",
            rf"echo $'r\0m -rf {t}' | bash", rf"echo $'cd {p}\0\nrm -rf build' | bash", rf"bash -c $'cd {p}\0\nrm -rf build'",
            rf"echo $'cd {p}\C-Jrm -rf build' | bash", rf"rm $'\-rf' {t}", rf"$'r\m' -rf {t}", rf"rm -rf $'{t}\.'",
            rf"echo $'cd {p}\;rm -rf build' | bash", rf"bash -c $'echo \'cd {p}\x{{a}}rm -rf build\' | bash'"),
            "delete guard refused")

    def test_an_eval_cd_moves_a_later_fed_delete(self):
        """The fourteenth cold review found an eval cd the folder tracker missed."""
        p = self.peer
        self.assert_refused(self.own, (
            f"eval 'cd {p}'; echo 'rm -rf build' | bash", f"eval \"$(echo cd {p})\"; echo 'rm -rf build' | bash",
            f"eval \"cd $(echo {p})\"; echo 'rm -rf build' | bash", f"eval cd {p} && printf 'rm -rf build\\n' | sh",
            f"eval \"$(printf 'cd %s' {p})\"\necho 'rm -rf build' | $SHELL", f"eval 'cd {p}'; bash < <(echo 'rm -rf build')",
            f"eval 'cd {p}'; echo $'rm -rf build' | bash", f"eval 'cd {p}'; (echo 'rm -rf build' | bash)"), "delete guard refused")
        self.assert_pass(self.own, (f"eval 'cd {self.tmpdir}/scratch'; echo 'rm -rf build' | bash",), {"TMPDIR": self.tmpdir})

    def test_the_fifteenth_review_forms_fail_closed(self):
        """Forms the fifteenth cold review found working."""
        p, t = self.peer, f"{self.peer}/build"
        self.assert_refused(self.own, (
            f"eval \"$(echo $'cd {p}')\"; rm -rf build", f"eval \"$(printf $'cd {p}')\" && rm -rf build",
            f"eval \"$(echo $'cd {p}')\"; echo 'rm -rf build' | bash",
            f"eval \"$(echo '${{x:-rm -rf {t}}}')\"", f"bash -c \"$(echo '${{x:-rm -rf {t}}}')\"",
            f"bash <<EOF\n${{x:-rm -rf {t}}}\nEOF", f"eval \"${{x:-rm -rf {t}}}\"", f"bash -c '${{x:-rm -rf {t}}}'",
            f"eval \"${{x:-cd {p}}}\"; rm -rf build", f"${{x:-rm -rf {t}}}", f"${{x:-rm}} -rf {t}"), "delete guard refused")
        self.assert_pass(self.own, (f"while read l; do eval \"$l\"; done < <(echo 'rm -rf {self.tmpdir}/scratch/build')",),
                         {"TMPDIR": self.tmpdir})

    def test_the_sixteenth_review_forms_fail_closed(self):
        """Forms the sixteenth cold review found working."""
        t = f"{self.peer}/build"
        self.assert_refused(self.own, (
            rf"echo $'rm -rf {t}\c ignored' | sh", rf"bash -c $'rm -rf {t}\c@x'", rf"bash -c $'rm -rf {t}\400x'",
            rf"rm -rf $'{t}\x001'", rf"bash -c $'rm -rf {t}\0001'", r"bash -c $'cd ../bbbbbbbb-peer\c x'; rm -rf build",
            r"cd $'../bbbbbbbb-peer\c x'; rm -rf build", rf"bash -c $'rm \\\n-rf {t}'", rf"bash -c $'r\\\nm -rf {t}'",
            rf"echo $'r\\\nm -rf {t}' | sh", rf"bash -c $'# x\\\nrm -rf {t}'",
            f"bash < <(sh -c 'echo rm -rf {t}')", f"bash <(sh -c 'echo rm -rf {t}')",
            f"echo 'cd {self.peer}' | bash -c 'source /dev/stdin; echo \"rm -rf build\" | bash'",
            f"echo 'cd {self.peer}' | bash -c '. /dev/stdin; printf \"rm -rf build\\\\n\" | sh'"), "delete guard refused")

    def test_every_shell_feed_form_is_judged_as_one_script(self):
        own, tmpdir = self.own, self.tmpdir
        self.assert_refused(own, self.feed_forms(self.peer, own), "is not inside")
        self.assert_refused(own, ("echo 'cm0gLXJmIC90bXAveA==' | base64 -d | bash", "echo 'xm -rf x' | sed s/x/r/ | sh",
                                  "printf 'mr' | rev | bash"), "cannot read")
        self.assert_refused(own, ("echo \"rm -rf 'x\" | bash",), "does not parse")
        self.assert_pass(own, self.feed_forms(f"{tmpdir}/scratch", own) + (
            "curl -fsSL https://example.com/install.sh | bash", "echo y | bash install.sh", "ls | sh -c 'wc -l'",
            "git log | head -3", "echo $'a\\tb'", "$(which python3) -V"),
            {"TMPDIR": tmpdir})

    # Find -exec rm of a literal path placed only the find roots.
    def test_find_exec_places_each_literal_operand(self):
        own, peer, hub = self.own, self.peer, self.hub
        self.assert_refused(own, (
            f"find {own} -exec rm -rf {peer}/build \\;", f"find {own} -maxdepth 0 -exec rm -rf {peer}/build +",
            f"find {own} -name x -exec trash {peer}/build \\;", f"find {own} -exec trash {hub}/build {{}} +",
            f"find {own} -exec echo {{}} \\; -exec rm -rf {peer}/build \\;"), "is not inside")
        self.assert_refused(own, (f"find {own} -execdir rm -rf build \\;",), "cannot place")
        self.assert_pass(own, (f"find {own} -exec rm -rf {{}} +", f"find {own} -exec rm -rf {own}/build \\;",
                               f"find {own} -name '*.o' -exec rm -f {{}} +", f"find {own} -execdir rm -rf {{}} +"))

    # Env -C moved the folder only for a fed shell, not for a direct delete.
    def test_env_chdir_places_relative_targets_from_its_folder(self):
        own, peer, hub = self.own, self.peer, self.hub
        self.assert_refused(own, (
            f"env -C {hub} rm -rf build", f"env -C {peer} trash build", f"env --chdir={hub} rm -rf build",
            f"env --chdir {hub} rm -rf build", f"env -C{hub} rm -rf build", f"sudo env -C {hub} rm -rf build",
            f"env FOO=1 -C {hub} rm -rf build", f"sudo -D {hub} rm -rf build"), "is not inside")
        self.assert_refused(own, ("env -C $X rm -rf build", "env -C ~1 rm -rf build"), "cannot place")
        self.assert_pass(own, (f"env -C {own} rm -rf build", f"env -C {hub} ls build", f"env -C {hub} rm -f note.txt"))

    # Review round 1: env, sudo or a -C after the command word is an operand and moves nothing; a
    # bundled option (env -iC, sudo -nD) or sudo --chdir still moves the folder.
    def test_only_a_command_word_env_or_sudo_moves_the_folder(self):
        own, hub = self.own, self.hub
        self.assert_refused(own, (
            f"cd {hub} && rm -rf build env -C {own}/x", f"cd {hub} && rm -rf -- build env --chdir={own}/x",
            f"cd {hub} && trash build sudo -D {own}/x", f"cd {hub} && rm -rf build sudo --chdir {own}/x",
            f"cd {hub} && rm -rf build -C {own}/x", f"env -iC {hub} rm -rf build", f"env -i -C {hub} rm -rf build",
            f"sudo -nD {hub} rm -rf build", f"sudo --chdir {hub} rm -rf build", f"sudo -D{hub} rm -rf build",
            f"sudo -u root -D {hub} rm -rf build", f"env -C {own} env -C {hub} rm -rf build"), "is not inside")
        self.assert_pass(own, (f"sudo -nD {own} rm -rf build", f"env -iC {own} rm -rf build",
                               f"cd {hub} && ls build env -C {own}/x", f"env -C {hub} env -C {own} rm -rf build"))

    # ~1, ~+1 (the folder stack) and ~x (zsh hash -d) are no home path; expanduser leaves them as is.
    def test_a_stack_or_named_tilde_target_is_refused(self):
        hub = self.hub
        self.assert_refused(self.own, (
            "rm -rf ~1/build", "rm -rf ~+1/build", "trash ~2", f"hash -d x={hub}; rm -rf ~x/build",
            f"pushd {hub}; pushd /tmp; rm -rf ~1", "echo 'rm -rf ~1' | bash", "bash -c 'rm -rf ~1'",
            "find ~1 -delete"), "names a folder stack entry")
        self.assert_refused(self.own, ("cd ~1 && rm -rf build", "cd ~x && trash build", "cd ~+1 && rm -rf build"),
                            "cannot place")
        self.assert_pass(self.own, ("rm -rf ~/build", "rm -rf ~+/build", "rm -f ~1/x.txt", "cd ~1 && ls"))

    # A for list word (script) and command -v $t read as code runners, so the man pipe was refused.
    def test_a_read_only_man_lookup_line_passes(self):
        hub = self.hub
        self.assert_pass(self.own, (
            r"for t in jq taskpolicy sandbox-exec script osascript flock tmux genv perl python3; do printf '%s: ' $t; "
            r"command -v $t || echo none; done; man env 2>/dev/null | col -b | grep -nE '^\s+-C|chdir' | head; "
            r"echo SHELL=$SHELL",
            "for t in jq script; do printf '%s: ' $t; done; man env | col -b | grep chdir",
            "for t in jq sh; do command -v $t; done", "command -V bash", "command -pv rm"))
        self.assert_refused(self.own, (f"command rm -rf {hub}/build", f"command -p rm -rf {hub}/build"), "is not inside")
        self.assert_refused(self.own, (
            f"for x in {hub}/build; do rm -rf $x; done", "for t in a; do $t -rf x; done",
            f"for s in sh; do echo 'rm -rf {hub}/build' | $s; done"), "delete guard refused")

    def test_own_trees_temp_folders_a_file_delete_and_a_literal_pid_pass(self):
        own, hub, peer, tmpdir = self.own, self.hub, self.peer, self.tmpdir
        self.assert_pass(own, (
            f"rm -rf {own}/build", "rm -rf build", "trash build", f"cd {own} && rm -rf ./build/", "rm -rf build/*",
            f"git -C {own} clean -fdx", "git clean -fd build", "git clean -ndx", f"git -C {hub} clean -n -dx",
            f"git -C {hub} clean -f", f"trash {hub}/note.txt", f"rm {peer}/note.txt", f"rm -rf {hub}/note.txt",
            f"rm -f {hub}/note.txt", f"trash {hub}/gone-folder", "kill 12345", "kill -9 12345", "kill -TERM 123 456",
            "kill %1", "kill $!", "kill -l", "git worktree list", "git worktree add ../x",
            "git branch -D task/aaaaaaaa-done", "git branch -d merged",
            ".venv/bin/python -B Drawer/git-branch-policy.py worktree-remove --name aaaaaaaa-own",
            "grep -rn 'rm -rf' Drawer", "echo kill", "docker run --rm img",
            "mem add --kind lesson --body 'never rm -rf a peer tree, git worktree remove or pkill myapp'",
            "cat > note.sh <<'EOF'\nrm -rf /\npkill myapp\nEOF"))
        self.assert_pass(hub, ("git branch -D task/aaaaaaaa-done", f"rm -rf {own}/build"))
        self.assert_pass(own, (f"rm -rf {tmpdir}/scratch", f"trash {tmpdir}/scratch/build", f"cd {tmpdir} && rm -rf scratch"),
                         {"TMPDIR": tmpdir})
        if os.name != "nt":
            scratch = tempfile.mkdtemp(prefix="hub-delete-guard-", dir="/tmp")
            try:
                self.assert_pass(own, (f"rm -rf {scratch}", f"trash {scratch}", f"rm -rf /private{scratch}"))
            finally:
                shutil.rmtree(scratch)


class HookBypass(unittest.TestCase):
    """A worker ran git commit --no-verify in the product repo, and an earlier worker committed under
    a non-default hooks path. Bash refuses each hook bypass (Drawer/git_guard.py hook_bypass_reason); a quoted
    message, a grep pattern, a hooks path read and the tracked install pass."""
    NO_VERIFY = Refusal("project-gate", "--no-verify")
    OVERRIDE = Refusal("project-gate", "core.hooksPath override")
    CONFIG = Refusal("project-gate", "config write or unset of core.hooksPath")

    def judge(self, command):
        return run(json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": BOX_HUB}))

    def assert_refused(self, refusal, commands):
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(("deny", refusal), self.judge(command))

    # Injection: hook_bypass_reason() drop the no_verify() check.
    def test_no_verify_is_refused(self):
        self.assert_refused(self.NO_VERIFY, (
            "git commit --no-verify -m 'x'", "git commit -nm 'x'", "git commit -m 'x' -n",
            "git -C ~/My_Product_wt/0a1b2c3d commit -am x --no-verify",
            "git push --no-verify origin HEAD:main", "git commit --no-verif -m x", "git merge --no-verify topic",
            "cd /tmp && git add -A && git commit --no-verify -m 'wip'", "bash -c 'git commit --no-verify -m x'",
            "env GIT_TRACE=1 git commit -n -m x", "sudo git commit --no-verify -m x", "command git commit -n -m x",
            "time git push --no-verify", "bash -lc 'git commit --no-verify -m x'", "eval \"git commit --no-verify\"",
            "if true; then git commit --no-verify -m x; fi", "{ git commit --no-verify -m x; }",
            "for i in 1; do git commit --no-verify -m x; done", "timeout 60 git commit --no-verify -m x",
            "gtimeout 5m git push --no-verify", "nice -n 10 git commit -n -m x", "caffeinate -i git commit --no-verify"))

    # Injection: hook_bypass_reason() drop the CONFORM_OFF check.
    def test_an_inline_harness_conform_off_is_refused_like_no_verify(self):
        self.assert_refused(Refusal("project-gate", "HARNESS_CONFORM=off"), (
            "HARNESS_CONFORM=off git commit -m x", "env HARNESS_CONFORM=off git commit -m x",
            "HARNESS_CONFORM=off git push origin HEAD", "export HARNESS_CONFORM=off; git commit -m x",
            "cd /tmp && HARNESS_CONFORM=off git commit -m x", "bash -c 'HARNESS_CONFORM=off git commit -m x'",
            "HARNESS_CONFORM=OFF git commit -m x", "HARNESS_CONFORM='off' git commit -m x"))
        for command in ("HARNESS_CONFORM=dry git commit -m x", "git commit -m 'HARNESS_CONFORM=off is refused'"):
            with self.subTest(command=command):
                self.assertEqual("pass", self.judge(command)[0])

    def test_the_no_verify_refusal_names_the_way_forward(self):
        """The worker made a wip commit to feed the product gate, which gates the working tree."""
        self.assertIn("run the product gate with no commit", self.judge("git commit -qm wip --no-verify")[1])

    def test_the_override_refusal_names_the_way_forward(self):
        """A worker took the product clone's .githooks for its hooks path."""
        self.assertIn("already runs the clone's own .githooks",
                      self.judge("git -c core.hooksPath=.githooks commit -qm x")[1])

    # Injection: hook_bypass_reason() drop the global option scan and the GIT_CONFIG_* check.
    def test_a_hooks_path_override_is_refused(self):
        self.assert_refused(self.OVERRIDE, (
            "git -c core.hooksPath=/dev/null commit -m x", "git -c core.hookspath=/tmp/none push origin HEAD:main",
            "git --config-env=core.hooksPath=HP commit -m x",
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=/dev/null git commit -m x",
            "GIT_CONFIG_PARAMETERS=\"'core.hooksPath'='/dev/null'\" git commit -m x",
            "export GIT_CONFIG_KEY_0=core.hooksPath; git commit -m x"))

    # Injection: hook_bypass_reason() never call config_reason.
    def test_a_hooks_path_config_change_is_refused(self):
        self.assert_refused(self.CONFIG, (
            "git config core.hooksPath /dev/null", "git config --local core.hooksPath .git/hooks",
            "git -C ~/My_Product config core.hooksPath /tmp/hooks", "git config --unset core.hooksPath",
            "git config unset core.hooksPath", "git config set core.hooksPath /tmp/x",
            "git config --global core.hooksPath ~/hooks", "git config -f .git/config core.hooksPath x",
            "git config core.hooksPath /tmp/evil/Drawer/git-hooks", "git config core.hooksPath ../evil/Drawer/git-hooks"))

    def test_quoted_mentions_reads_and_the_tracked_install_pass(self):
        for command in (
                "git commit -m 'Refuse --no-verify and core.hooksPath overrides'",
                "git commit -m \"--no-verify is banned\"",
                "grep -rn -- '--no-verify' Drawer/git_guard.py", "grep -n 'core.hooksPath' Build/MANUAL.md",
                "git log --grep='no-verify' --oneline", "git push origin HEAD:main", "git push -n origin HEAD:main",
                "git commit -m x", "git config core.hooksPath", "git config --get core.hooksPath",
                "git config get core.hooksPath", "git config --show-origin --get core.hooksPath",
                "git -C ~/My_Product config core.hooksPath %s/Drawer/product-hooks" % os.path.dirname(BOX_DRAWER),
                "git commit -m x -m '-n'", "git commit --message '--no-verify' -m x", "git commit -m x -- -n",
                "git config core.hooksPath Drawer/git-hooks",
                "mem add --kind lesson --body 'never run git commit --no-verify or git -c core.hooksPath=x'",
                "cat > note.sh <<'EOF'\ngit commit --no-verify\nEOF"):
            with self.subTest(command=command):
                self.assertEqual(("pass", ""), self.judge(command))


class LawShellFence(unittest.TestCase):
    """A shell write to a law file (AGENTS.md, SKILL.md, PRODUCT-CONVENTIONS.md, CLAUDE.md,
    CLAUDE.local.md) skips doc-gate rule 2 (the live hub) and rule 4 (the CLAUDE files), so Bash refuses it
    (Drawer/git_guard.py law_write_reason). Reads, a quoted mention and a write under ~/.claude pass."""
    LAW = Refusal("project-gate", "law-file shell write refused")

    @classmethod
    def setUpClass(cls):
        """A loose temp folder under /tmp (the child sees no TMPDIR): no .git and no hub marker above it."""
        global LOOSE
        LOOSE = os.path.realpath(tempfile.mkdtemp(prefix="hub-law-fence-loose-", dir="/tmp"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(LOOSE, ignore_errors=True)

    def judge(self, command):
        return run(json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": BOX_HUB}))

    def assert_refused(self, commands):
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(("deny", self.LAW), self.judge(command))

    # Injection: law_write_reason() drop the redirect scan.
    def test_a_redirect_onto_a_law_file_is_refused(self):
        self.assert_refused((
            "echo x > AGENTS.md", "echo x >> .claude/skills/qa/SKILL.md", "echo x>CLAUDE.md",
            "cat a &> PRODUCT-CONVENTIONS.md", "printf x >| CLAUDE.local.md", "echo x 2> agents.md",
            "echo x > \"AGENTS.md\"", "cat <<'EOF' > AGENTS.md\nnew rules\nEOF",
            "echo x > %s/AGENTS.md" % BOX_HUB, "bash -c 'echo x > AGENTS.md'", "echo \"$(echo x > SKILL.md)\"",
            "cat <<'EOF' | bash\necho x > CLAUDE.md\nEOF", "cd /tmp && echo x > AGENTS.md",
            "cd %s && echo x > AGENTS.md" % BOX_HUB))

    # Injection: law_write_reason() drop the tee and sed or perl in-place rules.
    def test_tee_and_in_place_edits_of_a_law_file_are_refused(self):
        self.assert_refused((
            "echo x | tee AGENTS.md", "echo x | tee -a .claude/skills/qa/SKILL.md", "echo x | sudo tee CLAUDE.md",
            "sed -i '' 's/a/b/' AGENTS.md", "sed -i.bak s/a/b/ SKILL.md", "sed --in-place s/a/b/ AGENTS.md",
            "sed -Ei 's/a/b/' PRODUCT-CONVENTIONS.md", "cd .claude/skills/qa && sed -i '' s/a/b/ SKILL.md",
            "perl -pi -e 's/a/b/' AGENTS.md", "perl -i -pe s/a/b/ CLAUDE.md", "perl -0777 -i.bak -pe s/a/b/ SKILL.md"))

    # Injection: law_write_reason() drop the cp, mv and ln rules.
    def test_cp_mv_and_ln_onto_a_law_file_are_refused(self):
        self.assert_refused((
            "cp /tmp/x AGENTS.md", "cp -f /tmp/x .claude/skills/qa/SKILL.md", "cp /tmp/AGENTS.md .claude/skills/qa/",
            "cp -t . /tmp/CLAUDE.md", "mv AGENTS.md /tmp/x.txt", "cp /tmp/AGENTS.md %s/sub/" % BOX_HUB, "mv /tmp/new.md SKILL.md",
            "ln -s AGENTS.md CLAUDE.md", "ln -sf /tmp/x PRODUCT-CONVENTIONS.md", "install -m 644 /tmp/x AGENTS.md"))

    # Injection: law_write_reason() drop the interpreter script rule.
    def test_an_inline_script_that_names_a_law_file_and_writes_is_refused(self):
        self.assert_refused((
            "python3 -c \"open('AGENTS.md', 'w').write('x')\"",
            ".venv/bin/python -B - <<'EOF'\nfrom pathlib import Path\nPath('.claude/skills/qa/SKILL.md').write_text('x')\nEOF",
            "cat <<'EOF' | python3\nimport shutil\nshutil.copy('/tmp/x', 'CLAUDE.md')\nEOF",
            "node -e \"require('fs').writeFileSync('PRODUCT-CONVENTIONS.md', 'x')\"",
            "python3 -c \"import fileinput\nfor l in fileinput.input('AGENTS.md', inplace=True): print(l)\"",
            "perl -e 'open(my $f, \">\", \"AGENTS.md\"); print $f 1'",
            "python3 -c \"import pathlib; pathlib.Path('/tmp/z').replace('AGENTS.md')\""))

    def test_a_cd_from_a_loose_temp_folder_keeps_a_relative_write_fenced(self):
        """A re-review of the law fence: the temp pass read a relative path from the event cwd, so a cd into the hub
        opened it. A relative law-file write is fenced whatever the cwd."""
        for command in ("cd %s && sed -i '' s/a/b/ AGENTS.md" % BOX_HUB, "cd %s && echo x > CLAUDE.md" % BOX_HUB,
                        "echo x > AGENTS.md"):
            with self.subTest(command=command):
                self.assertEqual(("deny", self.LAW), run(json.dumps(
                    {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": LOOSE})))

    def test_reads_quoted_mentions_and_home_claude_writes_pass(self):
        for command in (
                "cat AGENTS.md", "grep -n Sentinel AGENTS.md", "grep -rn x .claude/skills --include=SKILL.md",
                "git show HEAD:AGENTS.md", "git show HEAD:AGENTS.md > /tmp/agents.txt", "git diff -- AGENTS.md",
                "wc -l AGENTS.md .claude/skills/*/SKILL.md", "sed -n 1,5p AGENTS.md", "head CLAUDE.md 2>/dev/null",
                "diff AGENTS.md /tmp/x", "cp AGENTS.md /tmp/agents.txt", "git add AGENTS.md",
                "git commit -m 'trim AGENTS.md > SKILL.md'", "echo \"a > CLAUDE.md\"", "echo x > /tmp/notes.txt",
                "python3 -c \"print(open('AGENTS.md').read())\"", "python3 -c \"open('/tmp/x', 'w').write('AGENTS')\"",
                "perl -ne 'print if /x/' AGENTS.md", "sed -e s/a/b/ SKILL.md > /tmp/x",
                "python3 -c \"import pathlib;print(pathlib.Path('AGENTS.md').read_text().replace('a','b'))\"",
                "cp .claude/skills/qa/SKILL.md %s/" % LOOSE, "cp AGENTS.md %s/AGENTS.md" % LOOSE,
                "echo x > %s/CLAUDE.md" % LOOSE,
                "echo x > ~/.claude/CLAUDE.md", "echo x | tee -a ~/.claude/CLAUDE.md",
                "cat > note.sh <<'EOF'\necho x > AGENTS.md\nEOF",
                "ssh -o BatchMode=yes u@192.0.2.1 'python3 -' <<'EOF'\nopen('/x/.claude/CLAUDE.md','w')\nEOF",
                "mem add --kind lesson --body 'sed -i on AGENTS.md is refused'"):
            with self.subTest(command=command):
                self.assertEqual(("pass", ""), self.judge(command))


class PlaceholderPost(unittest.TestCase):
    """Flight posted 'Lesson 0f...' on hub PR 1 a third time; the post was composed in the
    shell call that wrote the card, before its id existed. Bash refuses such a post; a full id passes."""
    REFUSED = Refusal("project-gate", "placeholder card id is refused")

    # Injection: placeholder_post_reason() drop `PLACEHOLDER_ID.search(command) or`.
    def test_a_post_with_a_placeholder_id_is_refused(self):
        for command in (
                "gh pr comment 1 --body '**Report**: Lesson 0f... closes it'",
                "gh pr comment 1 -b \"task 86d9\u2026 is open\"",
                "gh api repos/{owner}/{repo}/issues/1/comments -f body='**Fix**: lesson ab... landed'",
                "gh api -X PATCH repos/{owner}/{repo}/issues/comments/6516115183 -f body='see 3c7... now'",
                "gh pr comment 1 --body-file - <<'EOF'\n**Close**: the id below names it\nEOF",
                "gh pr comment 1 --body '**Close**: card in the next line'",
                "gh pr comment 1 --body '**Close**: TBD id'"):
            with self.subTest(command=command):
                verdict, reason = judge("Bash", command=command)
                self.assertEqual(("deny", self.REFUSED), (verdict, reason))
                self.assertIn("printed id", reason)

    # Injection: placeholder_post_reason() drop `or MEM_ADD.search(command)`.
    def test_a_post_in_the_shell_call_of_a_mem_add_is_refused(self):
        for command in (
                ".venv/bin/python -B Drawer/mem/mem.py add --kind lesson --body x && gh pr comment 1 --body 'Lesson dbfa553b'",
                "mem add --kind decision --body y; gh api repos/o/r/issues/1/comments -f body='decision d18797f0'"):
            with self.subTest(command=command):
                self.assertEqual(("deny", self.REFUSED), judge("Bash", command=command))

    def test_a_post_with_full_ids_and_other_reads_pass(self):
        for command in (
                "gh pr comment 1 --body '**Report**: lesson dbfa553b closes incident ae126d50. Next...'",
                "gh pr comment 1 --body-file - <<'EOF'\n**Plan**: add a... wait, task 86d9a1b2 first\nEOF",
                "gh api repos/{owner}/{repo}/issues/1/comments --jq '.[].body'",
                "echo 'Lesson 0f...'", ".venv/bin/python -B Drawer/mem/mem.py add --kind lesson --body 'see 0f...'",
                # A lesson or an incident line that quotes the commands is no post.
                ".venv/bin/python -B Drawer/mem/mem.py add --kind lesson --body 'Flight ran a mem add and a gh pr "
                "comment in one call; Lesson 0f... went out'",
                ".venv/bin/python -B Drawer/incident.py 'the gate refuses a mem add that names gh pr comment; impact: x'"):
            with self.subTest(command=command):
                self.assertEqual(("pass", ""), judge("Bash", command=command))


class EvidenceLabels(unittest.TestCase):
    """The human's ruling: a label is needed only on a claim about a critical product or harness issue, so
    project-gate no longer refuses a Flight post to hub PR 1 with an unlabeled claim bullet (the old refusal is cut).
    Unlabeled and labeled bullets both pass."""

    # Injection: restore the old unlabeled_claim_reason refusal in main().
    def test_an_unlabeled_claim_bullet_passes(self):
        for command in (
                "gh pr comment 1 --body '**Hotfix**:\n- a tap on the user icon opens the ability station [test]\n"
                "- the door can never be opened'",
                "gh pr comment 1 --body-file - <<'EOF'\n**State**:\n1. the park found the editor in edit mode\nEOF",
                "gh api repos/{owner}/{repo}/issues/1/comments -f body='**Fix**:\n* build 7685d36 boots'"):
            with self.subTest(command=command):
                self.assertEqual(("pass", ""), judge("Bash", command=command))

    def test_an_unlabeled_bullet_in_a_body_file_passes(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "post.md").write_text("**Report**:\n- the chest opens\n", encoding="utf-8")
            verdict, _ = run(json.dumps({"tool_name": "Bash", "cwd": folder,
                                         "tool_input": {"command": "gh pr comment 1 --body-file post.md"}}))
        self.assertEqual("pass", verdict)

    def test_labeled_bullets_and_prose_pass(self):
        for command in (
                "gh pr comment 1 --body '**Hotfix**:\n- the camp boots [device]\n"
                "- a tap on the user icon opens the station [UNVERIFIED]\n- 12 tests green [test]\n1. the fight runs [sim]'",
                "gh pr comment 1 --body '**Plan**: the next build fixes the chest. No bullets here.'",
                "gh api repos/{owner}/{repo}/issues/1/comments --jq '.[].body'"):
            with self.subTest(command=command):
                self.assertEqual(("pass", ""), judge("Bash", command=command))


UNSCOPED = Refusal("project-gate", "unscoped search refused")


class UnscopedSearch(unittest.TestCase):
    """gatelib.unscoped_search_reason: a shell grep -r, find or ls -t over a whole generated tree
    ([product] broad_roots), or over [product] path, is refused; a narrower path such
    as scripts/<folder> passes, and a document body is data."""

    def project(self):
        """The private hub's [product] path, as setUpModule wrote it."""
        return os.path.join(os.path.dirname(BOX_HUB), "product")

    def test_a_recursive_grep_over_a_whole_generated_tree_is_refused(self):
        for command in ("grep -rn Foo .gen", "cd x && grep -R -e Foo ../product/.gen/",
                        "find .import -name '*.md5'", "ls -lt .gen/*"):
            with self.subTest(command=command):
                self.assertEqual(("deny", UNSCOPED), judge("Bash", command=command))

    def test_a_grep_under_a_project_folder_passes(self):
        for command in ("grep -rn Foo scripts/ui",
                        "grep -rn Foo %s/scripts/ui" % self.project()):
            with self.subTest(command=command):
                self.assertEqual(("pass", ""), judge("Bash", command=command))

    def test_a_search_over_the_project_root_from_config_is_refused(self):
        for command in ("grep -rn Foo " + self.project(), "find %s/ -name '*.py'" % self.project()):
            with self.subTest(command=command):
                self.assertEqual(("deny", UNSCOPED), judge("Bash", command=command))

    def test_a_heredoc_body_naming_a_generated_tree_passes(self):
        command = "cat <<'EOF'\ngrep -rn Foo .gen\nEOF"
        self.assertEqual(("pass", ""), judge("Bash", command=command))



class WindowsSearch(unittest.TestCase):
    """gatelib on Windows: a Git Bash drive path, a backslash path and an .exe name still name the search."""

    def test_windows_spellings_of_a_broad_root_and_the_project_root_are_refused(self):
        import gatelib
        with patch.object(gatelib.oscompat, "WINDOWS", True), \
                patch.object(gatelib.os.path, "normcase", lambda p: p.lower().replace("\\", "/")), \
                patch.object(gatelib.os.path, "abspath", lambda p: p), \
                patch.object(gatelib.os.path, "expanduser", lambda p: p), \
                patch.object(gatelib, "_product", lambda: ("c:/work/product", ("gen",))):
            for command in ("grep.exe -rn Foo gen", "find C:\\Work\\Product\\gen -name x",
                            "C:\\Git\\usr\\bin\\find.exe /c/work/product -name x"):
                with self.subTest(command=command):
                    self.assertIsNotNone(gatelib.unscoped_search_reason(command))
            self.assertIsNone(gatelib.unscoped_search_reason("grep.exe -rn Foo scripts/ui"))
            self.assertIsNotNone(gatelib.unscoped_search_reason("grep -rn Foo GEN"))  # NTFS ignores case


class FailClosed(unittest.TestCase):
    def test_unreadable_hook_json_denies(self):
        self.assertEqual(("deny", UNREADABLE), run("not json at all"))
        self.assertEqual(("deny", UNREADABLE), run("[]"))

    def test_utf8_hook_json_is_readable(self):
        # The harness sends UTF-8 bytes; Ł (C5 81) has no cp1252 reading. Readable JSON never
        # takes the unreadable deny, or every tool call that carries such text is blocked.
        payload = json.dumps({"tool_name": "Read", "tool_input": {"file_path": "Łódź — plan.txt"}},
                             ensure_ascii=False).encode("utf-8")
        out = subprocess.run([sys.executable, os.path.join(BOX_DRAWER, "project-gate.py")],
                             input=payload, capture_output=True, cwd=BOX_HUB,
                             env=dict(BASE_ENV, PYTHONIOENCODING="cp1252"))  # any seat
        self.assertEqual((0, b""), (out.returncode, out.stdout.strip()),
                         out.stderr.decode("utf-8", "replace")[-400:])

    def test_an_event_without_a_tool_name_passes(self):
        self.assertEqual(("pass", ""), run(json.dumps({"tool_input": {"command": "ls"}})))


class ManifestEntry(unittest.TestCase):
    """One dispatcher entry, and its matcher must reach every routed tool."""

    def setUp(self):
        with open(os.path.join(HERE, "settings-manifest.json"), encoding="utf-8") as fh:
            self.entries = json.load(fh)["hooks"]["PreToolUse"]

    def test_the_dispatcher_is_the_only_gate_entry(self):
        # The whole PreToolUse registration: the dispatcher and nothing else.
        self.assertEqual([
            {"matcher": "{GATES}",
             "command": "sh \"{HUB}/Drawer/hooks/run-hook.sh\" gate \"{HUB}/Drawer/project-gate.py\"",
             "timeout": 180},
        ], self.entries)

    def test_the_matcher_renders_from_the_table_and_fires_for_every_routed_tool(self):
        server = "".join("[-_]" if c in "-_" else re.escape(c) for c in SERVER)
        matcher = "^Write$|^Edit$|^MultiEdit$|^NotebookEdit$|^Agent$|^Workflow$|^Bash$|^Monitor$|^(mcp__%s__)?add$" % server
        self.assertEqual("{GATES}", self.entries[0]["matcher"])
        self.assertEqual(matcher, DISPATCH.claude_matcher())
        for tool in ("Write", "Edit", "MultiEdit", "NotebookEdit", "Agent", "Workflow", "Bash", "Monitor", KG + "add",
                     "mcp__%s__add" % SERVER.replace("-", "_")):
            with self.subTest(tool=tool):
                self.assertIsNotNone(re.search(matcher, tool), tool)
        for tool in ("TodoWrite", "BashOutput", "AgentOutput", "add_card", "mcp__editor-server__scene_play"):
            with self.subTest(tool=tool):
                self.assertIsNone(re.search(matcher, tool), tool)


class Table(unittest.TestCase):
    """The GATES table is the one registration."""

    def test_every_row_names_a_gate_file_that_exists(self):
        # claude tool, policy file (None: the dispatcher's own Bash refusals only), kg spellings.
        self.assertEqual((
            ("Write", "doc-gate.py", False),
            ("Edit", "doc-gate.py", False),
            ("MultiEdit", "doc-gate.py", False),
            ("NotebookEdit", "doc-gate.py", False),
            ("Agent", "fork-gate.py", False),
            ("Workflow", "fork-gate.py", False),
            ("Bash", None, False),
            ("Monitor", None, False),
            ("add", None, True),
        ), DISPATCH.GATES)
        self.assertTrue(os.path.isfile(os.path.join(HERE, "doc-gate.py")))
        self.assertTrue(os.path.isfile(os.path.join(HERE, "fork-gate.py")))

    def test_accepted_tools_give_each_file_its_names(self):
        self.assertEqual({"Write", "Edit", "MultiEdit", "NotebookEdit"}, DISPATCH.accepted_tools("doc-gate.py"))
        self.assertEqual({"Agent", "Workflow"}, DISPATCH.accepted_tools("fork-gate.py"))
        self.assertEqual(set(), DISPATCH.accepted_tools(None))

    def test_only_the_memory_add_row_is_door_prefixed(self):
        self.assertEqual(("add",), DISPATCH.KG_TOOLS)


class GateSwitches(unittest.TestCase):
    def test_the_doc_gate_keeps_its_off_switch(self):
        deny = json.dumps({"tool_name": "Write",
                           "tool_input": {"file_path": "Operator/attic/notes.md"}})
        self.assertEqual(("deny", HIDING_REFUSED % "Operator/attic/notes.md"), run(deny))
        self.assertEqual(("pass", ""), run(deny, {"DOC_GATE": "off"}))


if __name__ == "__main__":
    unittest.main(verbosity=1)
