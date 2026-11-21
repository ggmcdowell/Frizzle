"""Every PreToolUse deny reason is short: at most 200 characters, led by the gate name, with the fix.

Run: .venv/bin/python -B -m pytest -q Drawer/test_deny_reasons.py. Judges only; writes nothing in the checkout.

An agent reads the whole reason on every refusal, so a long one costs context on each call. The cap is written out
here (200), never read back from gatelib.REASON_CAP, so a raised constant cannot move the test with it.

DrivenRefusals runs a PRIVATE COPY of Drawer/project-gate.py in a private live hub (a Git main checkout with an
origin, two task worktrees and a product checkout whose gate runs), under a deep folder so every path a reason
names is long. One event per refusal class; each must deny, name its problem and fit the cap.
StaticTemplates fills each reason template and constant of the gate modules with its longest input.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import harness_config  # noqa: E402
import testgit  # noqa: E402
from test_project_gate import verdict  # noqa: E402  the (decision, reason) reader of one gate process

testgit.arm()

CAP = 200
GATES = ("project-gate: ", "doc-gate: ")
SOURCES = ("project-gate.py", "doc-gate.py", "gatelib.py", "git_guard.py", "rules_check.py", "rules_audit.py",
           "harness_config.py", "oscompat.py", "product_worktree.py")
OWN = "aaaaaaaa-1111-4111-8111-111111111111"
LONG = "a-long-folder-name-so-each-path-in-a-deny-reason-runs-past-its-clip"
if os.name == "nt":
    # Windows: LONG nests four times in one fixture path, and git cannot make a folder past MAX_PATH; a smoke run's
    # temp folder is already deep. Keep LONG as long as the temp folder leaves room, never under 24 characters
    # (so "unmerged-" + LONG still runs past the 32-character clip).
    LONG = LONG[:max(24, min(len(LONG), (190 - len(tempfile.gettempdir())) // 4))]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DrivenRefusals(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="hub-deny-reasons-")
        # Windows: git cannot make a .git/worktrees folder past MAX_PATH, so the root nests LONG once, not twice.
        root = os.path.join(os.path.realpath(cls.tmp.name), *([LONG] if os.name == "nt" else [LONG, LONG]))
        cls.hub, cls.product = os.path.join(root, "hub"), os.path.join(root, "product")
        home, origin = os.path.join(root, "home"), os.path.join(root, "origin.git")
        drawer = os.path.join(cls.hub, "Drawer")
        os.makedirs(drawer)
        os.makedirs(home)
        for name in SOURCES:
            shutil.copy(os.path.join(HERE, name), os.path.join(drawer, name))
        shutil.copy(os.path.join(os.path.dirname(HERE), "harness.toml"), os.path.join(cls.hub, "harness.toml"))
        marker = harness_config.load(os.path.dirname(HERE))["project"]["marker_file"]
        open(os.path.join(cls.hub, marker), "w").close()
        with open(os.path.join(cls.hub, "harness.local.toml"), "w", encoding="utf-8") as fh:
            fh.write("[product]\npath = %s\nbroad_roots = [\".gen\"]\n" % json.dumps(cls.product))
        with open(os.path.join(cls.hub, ".gitignore"), "w", encoding="utf-8") as fh:
            fh.write("/harness.local.toml\n*.log\n")
        empty = os.path.join(root, "empty.gitconfig")
        with open(empty, "w", encoding="utf-8") as fh:
            if os.name == "nt":  # the deep folders pass MAX_PATH (260): git needs core.longpaths to write there
                fh.write("[core]\n\tlongpaths = true\n")
        cls.env = {"CLAUDE_PROJECT_DIR": cls.hub, "DOC_GATE": "dry", "PYTHONDONTWRITEBYTECODE": "1",
                   "HOME": home, "USERPROFILE": home, "PYTHONPATH": HERE, "PATH": os.environ.get("PATH", ""),
                   "GIT_CONFIG_GLOBAL": empty, "GIT_CONFIG_SYSTEM": empty, **testgit.ENV}
        if os.name == "nt":
            cls.env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")

        def git(*args, cwd=cls.hub):
            subprocess.run(["git", "-C", cwd, "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                           check=True, capture_output=True, env=cls.env)

        git("init", "-q", "--bare", "-b", "main", origin, cwd=root)
        git("init", "-q", "-b", "main")
        git("add", "-A")
        git("commit", "-q", "-m", "seed")
        git("remote", "add", "origin", origin)
        git("push", "-q", "origin", "HEAD:main")
        trees = os.path.join(root, "hub_wt")
        cls.own, cls.peer = os.path.join(trees, "aaaaaaaa-own"), os.path.join(trees, "bbbbbbbb-" + LONG)
        git("worktree", "add", "-q", "-b", "task/aaaaaaaa-own", cls.own)
        git("worktree", "add", "-q", "-b", "task/bbbbbbbb-peer", cls.peer)
        git("commit", "-q", "--allow-empty", "-m", "ahead", cwd=cls.own)
        cls.unmerged = "unmerged-" + LONG
        cls.unowned = "unowned-" + LONG
        git("branch", cls.unmerged, "task/aaaaaaaa-own")
        git("branch", cls.unowned, "main")
        os.makedirs(os.path.join(cls.peer, "build", LONG))
        os.makedirs(os.path.join(cls.product, "tests"))
        git("init", "-q", "-b", "main", cwd=cls.product)
        with open(os.path.join(cls.product, "gate-verdict.json"), "w", encoding="utf-8") as fh:
            json.dump({"status": "running", "startedAtUtc": datetime.now(timezone.utc).isoformat()}, fh)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def judge(self, payload):
        out = subprocess.run([sys.executable, os.path.join(self.hub, "Drawer", "project-gate.py")], input=payload,
                             capture_output=True, text=True, env=self.env, cwd=self.hub, timeout=60)
        return verdict(out)

    def event(self, tool, cwd=None, **tool_input):
        return json.dumps({"tool_name": tool, "tool_input": tool_input, "cwd": cwd or self.own, "session_id": OWN})

    def cases(self):
        """(class, payload, the problem phrase its reason must carry)."""
        bash = lambda command, cwd=None: self.event("Bash", cwd, command=command)  # noqa: E731
        peer = self.peer.replace(os.sep, "/")  # a shell command line holds slashes; a backslash is an escape there
        write = lambda path: self.event("Write", None, file_path=path, content="x\n")  # noqa: E731
        return (
            ("force push", bash("git push -f origin main"), "force push to main"),
            ("reset --hard on main", bash("git reset --hard origin/main", self.hub), "reset --hard on main"),
            ("rm of a peer folder", bash("rm -rf %s/build/%s" % (peer, LONG)), "delete guard refused (SAF-4)"),
            ("worktree remove", bash("git worktree remove %s" % peer), "raw git worktree remove"),
            ("git clean outside own tree", bash("git -C %s clean -xdf" % peer), "git clean"),
            ("find -delete outside own tree", bash("find %s/build -delete" % peer), "find:"),
            ("branch -D unmerged", bash("git branch -D " + self.unmerged, self.hub), "not on origin/main"),
            ("branch -D not own", bash("git branch -D " + self.unowned, self.hub), "no id of this session"),
            ("pkill", bash("pkill -f node"), "kill by name"),
            ("computed kill", bash("kill $(pgrep node)"), "computed"),
            ("self kill", bash("kill $$"), "$PPID"),
            ("unparsed delete", bash("rm -rf 'unclosed"), "does not parse"),
            ("filtered feed", bash("echo 'rm -rf /x' | sed s/a/b/ | bash"), "changes the echoed text"),
            ("--no-verify", bash("git commit --no-verify -m x"), "hook bypass refused (SAF-7)"),
            ("hooksPath override", bash("git -c core.hooksPath=/x commit -m x"), "core.hooksPath override"),
            ("hooksPath config", bash("git config core.hooksPath /x"), "core.hooksPath"),
            ("filter-branch", bash("git filter-branch -f HEAD"), "filter-branch"),
            ("refs/original delete", bash("git update-ref -d refs/original/refs/heads/main"), "refs/original"),
            ("HEAD move", bash("git switch --detach"), "HEAD move refused"),
            ("law-file redirect", bash("echo x > %s/AGENTS.md" % self.own), "law-file shell write refused"),
            ("law-file script", bash("python3 -c \"open('%s/SKILL.md','w').write('x')\"" % self.own), "script names"),
            ("manual poll", bash("while true; do grep -q ok log; sleep 5; done"), "manual polling"),
            ("unscoped search", bash("grep -rn Foo %s/.gen" % self.product), "unscoped search refused"),
            ("placeholder post", bash("gh pr comment 1 --body 'Lesson 0f... closes it'"), "placeholder card id"),
            ("unreadable event", "not json", "cannot read the hook JSON"),
            ("live-hub edit", write(os.path.join(self.hub, "Drawer", LONG + ".py")), "live hub"),
            ("CLAUDE.md shadow", write(os.path.join(self.hub, LONG, "CLAUDE.md")), "Claude instruction file"),
            ("hiding place", write(os.path.join(self.hub, LONG, "attic", "x.md")), "hiding place"),
            ("backup name", write(os.path.join(self.hub, LONG + ".bak")), "backup-named"),
            ("archive", write(os.path.join(self.hub, LONG + ".zip")), "archive file"),
            ("test edit while the gate runs", write(os.path.join(self.product, "tests", LONG + ".py")),
             "product gate is running"),
        )

    def test_each_refusal_class_denies_short_and_names_its_gate(self):
        for name, payload, phrase in self.cases():
            with self.subTest(name):
                decision, reason = self.judge(payload)
                self.assertEqual("deny", decision, reason)
                self.assertIn(phrase, reason)
                self.assertTrue(reason.startswith(GATES), reason)
                self.assertLessEqual(len(reason), CAP, reason)


class StaticTemplates(unittest.TestCase):
    """Each reason constant, and each template filled with its longest input, fits the cap."""

    @classmethod
    def setUpClass(cls):
        cls.guard = load("deny_reasons_git_guard", os.path.join(HERE, "git_guard.py"))
        cls.gatelib = load("deny_reasons_gatelib", os.path.join(HERE, "gatelib.py"))
        cls.dispatch = load("deny_reasons_project_gate", os.path.join(HERE, "project-gate.py"))
        cls.doc = load("deny_reasons_doc_gate", os.path.join(HERE, "doc-gate.py"))

    def fits(self, reasons):
        for reason in reasons:
            with self.subTest(reason=reason[:60]):
                self.assertTrue(reason.startswith(GATES), reason)
                self.assertLessEqual(len(reason), CAP, reason)

    def test_the_whole_reason_constants_fit(self):
        g, d = self.guard, self.dispatch
        self.fits((g.FILTER_REFUSED, g.ORIGINAL_REFUSED, g.NO_VERIFY_REFUSED, g.OVERRIDE_REFUSED, g.CONFIG_REFUSED,
                   d.UNREADABLE, d.MANUAL_POLL_REASON, d.PLACEHOLDER_POST_REASON))

    def test_the_delete_guard_bodies_fit_with_their_prefix_and_remedy(self):
        g, name = self.guard, "x" * 200
        bare = (g.NEVER_KILL, g.COMPUTED_KILL, g.SELF_KILL, g.UNPARSED, g.WORKTREE_REFUSED)  # no remedy after them
        remedied = (g.INEXACT, g.FILTERED.format(g.clip(name, 24)), g.UNREAD.format(g.clip(name, 24)),
                    g.UNREAD.format("a substitution in command position"))
        self.fits([g.DELETE_REFUSED + body for body in bare] +
                  [g.DELETE_REFUSED + body + g.DELETE_REMEDY for body in remedied])

    def test_the_unscoped_search_template_fits(self):
        self.fits([self.gatelib.UNSCOPED_SEARCH_REASON % ("grep -r", self.gatelib.clip("/x" * 200))])

    def test_the_doc_gate_templates_fit_their_longest_input(self):
        d, path = self.doc, "/".join([LONG] * 4)
        with patch.object(d, "PRODUCT", path):
            self.fits((d.reason_hide(path, d.hiding_place(LONG + ".bak")), d.reason_hide(path, d.hiding_place("attic/x")),
                       d.reason_live_hub(path), d.reason_gate_running(path, path), d.reason_shadow(path),
                       d.reason_main_checkout(path, [path])))


if __name__ == "__main__":
    unittest.main(verbosity=1)
