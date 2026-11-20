from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "Drawer/git-branch-policy.py"
# git writes the all-zero object id for a deleted ref (githooks(5), pre-push stdin).
ZERO_OID = "0" * 40
# The policy CLI has one refusal channel: print(f"Git policy: {exc}", stderr).
REFUSAL_PREFIX = "Git policy: "
# The host's own prepared interpreter every printed command names: a new seat has no py launcher.
PREPARED = ".venv/bin/python -B"
OWN_SESSION = "0a1b2c3d-0000-4000-8000-000000000000"  # the fixture session id
NAME = "0a1b2c3d-route"  # a task worktree name with that session's uuid8
sys.path.insert(0, str(ROOT / "Drawer"))
import harness_config  # noqa: E402
import pushretry  # noqa: E402
import steer_check  # noqa: E402,F401  (other test files import it from here)
import testgit  # noqa: E402

testgit.arm()  # no git template in any fixture repo
CFG = harness_config.load(ROOT)


def var(suffix: str) -> str:
    """This hub's session variable for suffix (HUB -> <env_prefix>_HUB)."""
    return harness_config.env_name(suffix, CFG)


# The seat variables a child git and its hooks need, and nothing else: a session's own
# GIT_* or hub session state must never reach this fixture.
INHERITED_ENV = (
    "PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", *testgit.ENV,
)
# The fixture's own stand-in knobs, armed on os.environ so the hooks a policy call
# starts in this process read them too.
FIXTURE_ENV = ("CONFORM_EXIT", "REFRESH_LOG", "HOOK_LOG", "PROBE_EXIT")
# Stands in for gitleaks in the copied pre-commit: the secret scan has its own tests (test_precommit).
FAKE_GITLEAKS = "#!/bin/sh\nexit 0\n"
SPEC = importlib.util.spec_from_file_location("git_branch_policy", MODULE_PATH)
assert SPEC and SPEC.loader
policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(policy)


# The base fixture every case starts from: built once per test process into a template
# folder, then copied into each case's own temporary folder (the per-case
# git init, commits and pushes cost about a third of this file's run).
# key -> (template folder, the folder its hub Git config names: where it was built)
_TEMPLATES: dict[str, tuple[tempfile.TemporaryDirectory, str]] = {}
_INSTALLED: dict[str, dict[str, object]] = {}  # an install key -> the result that install gave


def _build_template(base: Path) -> None:
    """A bare remote.git and a hub clone with the policy fixture commits, pushed to main."""
    empty_gitconfig = base / "empty.gitconfig"
    empty_gitconfig.write_text("", encoding="utf-8")
    fake_bin = base / "bin"
    fake_bin.mkdir()
    (fake_bin / "gitleaks").write_text(FAKE_GITLEAKS, encoding="utf-8")
    (fake_bin / "gitleaks").chmod(0o755)
    env = {name: os.environ[name] for name in INHERITED_ENV if name in os.environ}
    env.update({"GIT_CONFIG_GLOBAL": str(empty_gitconfig), "GIT_CONFIG_SYSTEM": str(empty_gitconfig),
                "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"})
    remote, repo = base / "remote.git", base / "hub"

    def git(*arguments: str) -> None:
        subprocess.run(["git", "-C", str(repo), *arguments], check=True, capture_output=True, env=env)

    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True, env=env)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True, env=env)
    git("config", "user.name", "Policy Test")
    git("config", "user.email", "policy@example.invalid")
    (repo / "fixture.txt").write_text("base\n", encoding="utf-8")
    git("add", "fixture.txt")
    git("commit", "-m", "fixture")
    git("branch", "-M", "main")
    git("remote", "add", "origin", str(remote))
    git("push", "origin", "main")
    drawer = repo / "Drawer"
    (drawer / "git-hooks").mkdir(parents=True)
    shutil.copy2(MODULE_PATH, drawer / "git-branch-policy.py")
    for hook in sorted((ROOT / "Drawer/git-hooks").iterdir()):
        if hook.is_file():
            shutil.copy2(hook, drawer / "git-hooks" / hook.name)
    # The hooks and the policy read this hub's config. The copy binds its gates, so a
    # refused check-commit or check-push blocks as it will after the base proof.
    shutil.copy2(ROOT / "Drawer/harness_config.py", drawer / "harness_config.py")
    shutil.copy2(ROOT / "Drawer/oscompat.py", drawer / "oscompat.py")
    shutil.copy2(ROOT / "Drawer/gatelib.py", drawer / "gatelib.py")
    (repo / "harness.toml").write_text(
        re.sub(r"(?m)^bind = false", "bind = true", (ROOT / "harness.toml").read_text(encoding="utf-8")),
        encoding="utf-8")
    (repo / CFG["project"]["marker_file"]).write_text("", encoding="utf-8")
    shutil.copy2(ROOT / ".gitleaks.toml", repo / ".gitleaks.toml")
    # The name check has its own tests (test_name_check); here it passes.
    (drawer / "name-check.py").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    # The index rules check has its own tests (test_rules_check); here it passes.
    (drawer / "rules_check.py").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    # pre-commit runs harness-conform.py --commit before check-commit; this fixture is about
    # Git policy, so the stand-in passes unless a test arms it to hold. HOOK_LOG records each
    # run: pre-commit is the hook trace.
    (drawer / "harness-conform.py").write_text("import os, sys\nlog = os.environ.get('HOOK_LOG')\n"
                                              "log and open(log, 'a').write('pre-commit\\n')\n"
                                              "sys.exit(int(os.environ.get('CONFORM_EXIT', '0')))\n",
                                              encoding="utf-8")
    # The MCP render has its own tests (test_settings_sync).
    (drawer / "settings-sync.py").write_text(
        "import os, sys\nlog = os.environ.get('REFRESH_LOG')\n"
        "log and open(log, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n", encoding="utf-8")
    # The probe has its own tests (Drawer/probes); here it passes unless PROBE_EXIT arms it red.
    (drawer / "probes").mkdir()
    (drawer / "probes" / "probe.py").write_text(
        "import os, sys\ncode = int(os.environ.get('PROBE_EXIT', '0'))\n"
        "print('FAIL replay_x: fixture red' if code else 'PASS smoke')\nsys.exit(code)\n", encoding="utf-8")
    git("add", "Drawer", "harness.toml", CFG["project"]["marker_file"], ".gitleaks.toml")
    git("commit", "-m", "policy fixture")
    git("push", "origin", "main")
    _compact(base)


def _compact(base: Path) -> None:
    """Pack the template's refs and objects and drop what a case need not copy: a few files, not hundreds."""
    for git_dir in (base / "hub" / ".git", base / "remote.git"):
        if git_dir.is_dir():
            testgit.slim(git_dir)


def _snapshot(key: str, base: Path) -> None:
    """Keep base, as one case's setUp left it, as the template under key."""
    template = tempfile.TemporaryDirectory()
    shutil.copytree(base, template.name, symlinks=True, dirs_exist_ok=True)
    _compact(Path(template.name))
    _TEMPLATES[key] = (template, str(base))


def _copy_template(key: str, base: Path) -> bool:
    """Copy the template under key into base, with its hub Git config moved to base's paths.
    False when no template is kept under key yet."""
    if key not in _TEMPLATES:
        return False
    template, built_in = _TEMPLATES[key]
    for stale in ("hub", "remote.git"):
        shutil.rmtree(base / stale, ignore_errors=True)
    shutil.copytree(template.name, base, symlinks=True, dirs_exist_ok=True)
    config = base / "hub" / ".git" / "config"
    text = config.read_text(encoding="utf-8")
    # install writes resolved paths (/private/var/... on macOS), git remote add the plain ones.
    for old, new in ((os.path.realpath(built_in), os.path.realpath(base)), (built_in, str(base))):
        text = text.replace(old, new)
    config.write_text(text, encoding="utf-8")
    return True


class GitBranchPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.empty_gitconfig = base / "empty.gitconfig"
        self.empty_gitconfig.write_text("", encoding="utf-8")
        # Every git child, not only the ones that take child_env(): a seat core.autocrlf
        # (Git for Windows sets it in the system config) would normalise a fixture blob
        # in one repo and not the next, and git status would grow a line no case wrote.
        pinned = mock.patch.dict(os.environ,
                                 {"GIT_CONFIG_GLOBAL": str(self.empty_gitconfig),
                                  "GIT_CONFIG_SYSTEM": str(self.empty_gitconfig)})
        pinned.start()
        self.addCleanup(pinned.stop)
        self.remote = base / "remote.git"
        self.repo = base / "hub"
        if "policy" not in _TEMPLATES:
            seed = tempfile.TemporaryDirectory()
            _build_template(Path(seed.name))
            _TEMPLATES["policy"] = (seed, seed.name)
        _copy_template("policy", base)
        self.fake_bin = base / "bin"
        searched = mock.patch.dict(os.environ, {"PATH": f"{self.fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"})
        searched.start()
        self.addCleanup(searched.stop)
        self.conform = self.repo / "Drawer" / "harness-conform.py"
        self.env: dict[str, str] = {}  # session variables a case arms for the policy and its hooks

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def child_env(self) -> dict[str, str]:
        """Only the seat variables git and its hooks need, plus this case's armed variables."""
        base = {name: os.environ[name] for name in (*INHERITED_ENV, *FIXTURE_ENV)
                if name in os.environ}
        # No seat gitconfig reaches the fixture: a global hooksPath, signing key or
        # alias would steer the policy under test.
        base["GIT_CONFIG_GLOBAL"] = str(self.empty_gitconfig)
        base["GIT_CONFIG_SYSTEM"] = str(self.empty_gitconfig)
        return {**base, **getattr(self, "env", {})}

    def git(self, *arguments: str, check: bool = True,
            input_text: str | None = None) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", "-C", str(self.repo), *arguments], text=True, input=input_text,
            capture_output=True, env=self.child_env(), check=False,
        )
        if check and result.returncode:
            self.fail(f"git {' '.join(arguments)} failed: {result.stderr}")
        return result

    def refusals(self, stderr: str) -> list[str]:
        """Every policy refusal line a hook printed, whole."""
        return [line for line in stderr.splitlines() if line.startswith(REFUSAL_PREFIX)]

    def assert_message(self, message: str, head: str, tail: str) -> None:
        """A contract message around one detail only the run knows: both halves, whole."""
        self.assertEqual(head, message[:len(head)])
        self.assertEqual(tail, message[len(message) - len(tail):])

    def advance_main(self, text: str = "upstream\n", *, path: str = "upstream.txt",
                     also: dict[str, str] | None = None) -> str:
        clone = Path(self.temporary.name) / f"upstream-{len(list(Path(self.temporary.name).glob('upstream-*')))}"
        subprocess.run(
            ["git", "clone", "--shared", "-b", "main", str(self.remote), str(clone)],  # alternates: no object copy
            check=True, capture_output=True, text=True,
        )
        subprocess.run(["git", "-C", str(clone), "config", "user.name", "Upstream Test"],
                       check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(clone), "config", "user.email", "upstream@example.invalid"],
                       check=True, capture_output=True, text=True)
        for name, content in {path: text, **(also or {})}.items():
            target = clone / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            subprocess.run(["git", "-C", str(clone), "add", name],
                           check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(clone), "commit", "-m", "advance accepted main"],
                       check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(clone), "push", "origin", "main"],
                       check=True, capture_output=True, text=True)
        return subprocess.run(
            ["git", "-C", str(clone), "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True,
        ).stdout.strip()

    def accepted(self, description: str, call, *arguments, **keywords):
        """An accepted path: its refusal is this test's FAIL, never an ERROR."""
        try:
            return call(*arguments, **keywords)
        except policy.PolicyError as refusal:
            self.fail(f"{description} refused the attested seat: {refusal}")

    def install(self, *, refusal_ok: bool = False) -> dict[str, object]:
        # An accepted install from a state seen before is copied from its snapshot.
        # Only a case's first install: a later one runs on a state no other case has.
        first = not getattr(self, "installed", False)
        self.installed = True
        key = self.install_key() if first and not refusal_ok else None
        base = Path(self.temporary.name)
        if key and _copy_template(key, base):
            return dict(_INSTALLED[key])
        with mock.patch.dict(os.environ, self.env, clear=False):
            if refusal_ok:
                return policy.install(self.repo)
            result = self.accepted("install", policy.install, self.repo)
        if key:
            _snapshot(key, base)
            _INSTALLED[key] = dict(result)
        return result

    def install_key(self, *arguments: str) -> str | None:
        """The install inputs and every repo state install reads, with this case's folder cut out;
        None while a case mocks the policy or subprocess, so its install always runs."""
        if any(isinstance(value, mock.NonCallableMock) for value in vars(policy).values()) or \
                isinstance(subprocess.run, mock.NonCallableMock) or isinstance(subprocess.Popen, mock.NonCallableMock):
            return None
        reads = [self.git(*command, check=False).stdout for command in (
            ("status", "--porcelain", "-uall", "--ignored"), ("ls-files", "-v"),
            ("for-each-ref", "--format=%(refname) %(objectname) %(upstream)"), ("symbolic-ref", "-q", "HEAD"),
            ("config", "--local", "--list"), ("worktree", "list", "--porcelain"),
            ("--git-dir", str(self.remote), "for-each-ref"))]
        exclude = self.repo / ".git" / "info" / "exclude"
        reads.append(exclude.read_text(encoding="utf-8") if exclude.is_file() else "")
        environment = sorted((name, value) for name, value in os.environ.items()
                             if name.startswith(var("")) or name in FIXTURE_ENV)
        state = repr((arguments, sorted(self.env.items()), environment, reads))
        for folder in (os.path.realpath(self.temporary.name), self.temporary.name):
            state = state.replace(folder, "<base>")
        return "install " + state

    def freshness(self, source: str) -> dict[str, object]:
        with mock.patch.dict(os.environ, self.env, clear=False):
            return self.accepted("freshness", policy.freshness, self.repo, source)

    def test_install_binds_main_and_sets_same_ref_destination(self) -> None:
        result = self.install()
        self.assertEqual(result["branch"], "main")
        self.assertEqual(self.git("config", "--get", "core.hooksPath").stdout.strip(), "Drawer/git-hooks")
        self.assertEqual(self.git("config", "--get", "hub.bound").stdout.strip(), "true")
        self.assertEqual(
            self.git("config", "--get", "branch.main.merge").stdout.strip(),
            "refs/heads/main",
        )

    def test_hooks_allow_main_commit_and_push_but_refuse_other_branch_tag_and_multiple_refs(self) -> None:
        self.install()
        (self.repo / "fixture.txt").write_text("owned\n", encoding="utf-8")
        self.git("add", "fixture.txt")
        self.git("commit", "-m", "owned")
        self.git("push")

        for arguments, refusal in (
            (("push", "origin", "HEAD:refs/heads/other"),
             "Hub pushes are allowed only on main; current target is other"),
            (("push", "origin", "HEAD:refs/heads/main/second"),
             "Hub pushes are allowed only on main; current target is main/second"),
        ):
            denied = self.git(*arguments, check=False)
            self.assertNotEqual(0, denied.returncode)
            self.assertEqual([REFUSAL_PREFIX + refusal], self.refusals(denied.stderr))

        self.git("tag", "release-test")
        denied_tag = self.git("push", "origin", "refs/tags/release-test", check=False)
        self.assertNotEqual(0, denied_tag.returncode)
        self.assertEqual(
            [REFUSAL_PREFIX
             + "Hub pushes may update branches only; tags and other refs are refused"],
            self.refusals(denied_tag.stderr),
        )

        # A foreign ref smuggled in beside main: every update is checked, not just one,
        # and the refusal leaves main on the remote where it stood.
        landed = self.git("ls-remote", "origin", "refs/heads/main").stdout.split()[0]
        (self.repo / "fixture.txt").write_text("second owned\n", encoding="utf-8")
        self.git("add", "fixture.txt")
        self.git("commit", "-m", "second owned")
        denied_pair = self.git(
            "push", "origin", "HEAD:refs/heads/main", "HEAD:refs/heads/other", check=False,
        )
        self.assertNotEqual(0, denied_pair.returncode)
        self.assertEqual(
            [REFUSAL_PREFIX + "Hub pushes are allowed only on main; current target is other"],
            self.refusals(denied_pair.stderr),
        )
        self.assertEqual(
            landed, self.git("ls-remote", "origin", "refs/heads/main").stdout.split()[0],
        )

    def test_pre_commit_holds_when_harness_conform_fails(self) -> None:
        self.install()
        (self.repo / "fixture.txt").write_text("held\n", encoding="utf-8")
        self.git("add", "fixture.txt")
        with self.hook_logged():
            denied = self.git("commit", "-m", "held", check=False)
        self.assertNotEqual(0, denied.returncode)
        # The positive control for assert_no_hook_ran: a commit leaves the pre-commit trace.
        self.assertEqual("pre-commit\n", self.hook_log.read_text(encoding="utf-8"))
        # The hold changes nothing: the fixture commit is still HEAD and the staged
        # bytes are still in the index, ready for the same commit.
        self.assertEqual("policy fixture", self.git("log", "-1", "--format=%s").stdout.strip())
        self.assertEqual(
            ["fixture.txt"], self.git("diff", "--cached", "--name-only").stdout.splitlines(),
        )
        # The stand-in passes again: the held commit lands unchanged.
        self.git("commit", "-m", "released")
        self.assertEqual("released", self.git("log", "-1", "--format=%s").stdout.strip())

    def test_pre_merge_commit_holds_when_harness_conform_fails(self) -> None:
        """A merge commit runs pre-merge-commit, never pre-commit: the merged tree takes the gates."""
        self.install()
        start = self.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        self.git("switch", "-c", "side")
        (self.repo / "side.txt").write_text("side\n", encoding="utf-8")
        self.git("add", "side.txt")
        # The side commit is scaffolding; the branch policy owns which branch may commit.
        self.git("commit", "--no-verify", "-m", "side work")
        self.git("switch", start)
        with mock.patch.dict(os.environ, {"CONFORM_EXIT": "1"}):
            denied = self.git("merge", "--no-ff", "--no-edit", "side", check=False)
        self.assertNotEqual(denied.returncode, 0)
        self.assertEqual("", self.git("rev-list", "-1", "--merges", "HEAD").stdout.strip())
        # The refusal leaves the merge open; concluding it by hand runs pre-commit, now green.
        self.git("commit", "--no-edit")
        self.assertNotEqual("", self.git("rev-list", "-1", "--merges", "HEAD").stdout.strip())

    def test_main_can_commit_and_push_a_law_path(self) -> None:
        self.install()
        (self.repo / "PRODUCT-CONVENTIONS.md").write_text("law change\n", encoding="utf-8")
        self.git("add", "PRODUCT-CONVENTIONS.md")
        self.git("commit", "-m", "law change")
        self.git("push")

    def task_worktree(self, branch: str) -> Path:
        """A bound seat plus one linked worktree on a new local branch."""
        self.install()
        linked = Path(self.temporary.name) / "task-tree"
        self.git("worktree", "add", "-b", branch, str(linked), "main")
        (linked / "task.txt").write_text("task work\n", encoding="utf-8")
        self.git("-C", str(linked), "add", "task.txt")
        return linked

    def test_task_branch_commit_in_linked_worktree_passes(self) -> None:
        linked = self.task_worktree("task/combat-proof")
        with mock.patch.dict(os.environ, self.env, clear=False):
            result = self.accepted("check-commit", policy.check_commit, linked)
        self.assertEqual("task/combat-proof", result["branch"])
        self.assertEqual("PASS", result["result"])
        self.git("-C", str(linked), "commit", "-m", "task work")
        self.assertEqual("", self.git("-C", str(linked), "status", "--porcelain").stdout)

    def test_task_branch_commit_refused_in_main_checkout_or_when_tracked_or_on_another_branch(self) -> None:
        linked = self.task_worktree("task/combat-proof")
        with mock.patch.dict(os.environ, self.env, clear=False):
            # Tracked: an upstream entry means the branch was or will be pushed.
            self.git("-C", str(linked), "config", "branch.task/combat-proof.remote", "origin")
            with self.assertRaisesRegex(policy.PolicyError, re.escape(
                    "a task branch is never tracked; run git branch --unset-upstream task/combat-proof")):
                policy.check_commit(linked)
            self.git("-C", str(linked), "config", "--unset", "branch.task/combat-proof.remote")
            # Published: a remote-tracking ref of the same name exists.
            self.git("update-ref", "refs/remotes/origin/task/combat-proof", "main")
            with self.assertRaisesRegex(policy.PolicyError, "are refused: a task branch is never pushed$"):
                policy.check_commit(linked)
            self.git("update-ref", "-d", "refs/remotes/origin/task/combat-proof")
            # A non-task name in a linked worktree keeps the main-only rule.
            self.git("-C", str(linked), "switch", "-c", "scratch")
            with self.assertRaisesRegex(policy.PolicyError, "allowed only on"):
                policy.check_commit(linked)
            # The main checkout itself is never a task-branch seat.
            self.git("switch", "-c", "task/in-main")
            (self.repo / "task.txt").write_text("main checkout task\n", encoding="utf-8")
            self.git("add", "task.txt")
            with self.assertRaisesRegex(policy.PolicyError, "only in a linked worktree"):
                policy.check_commit(self.repo)
        denied = self.git("commit", "-m", "task in main checkout", check=False)
        self.assertNotEqual(0, denied.returncode)
        self.assertEqual(
            [REFUSAL_PREFIX + "Hub commits on task branch task/in-main are allowed only in a linked worktree"],
            self.refusals(denied.stderr))

    def test_agent_can_resolve_and_continue_rebase_conflict_under_hooks(self) -> None:
        self.install()
        (self.repo / "fixture.txt").write_text("local version\n", encoding="utf-8")
        self.git("add", "fixture.txt")
        self.git("commit", "-m", "local conflict")
        accepted = self.advance_main("accepted upstream version\n", path="fixture.txt")
        self.git("fetch", "origin", "+refs/heads/main:refs/remotes/origin/main")
        conflicted = self.git("rebase", "origin/main", check=False)
        self.assertNotEqual(0, conflicted.returncode)
        (self.repo / "fixture.txt").write_text(
            "semantically resolved version\n", encoding="utf-8",
        )
        self.git("add", "fixture.txt")
        self.git("-c", "core.editor=true", "rebase", "--continue")
        self.assertEqual("main", self.git("branch", "--show-current").stdout.strip())
        self.assertEqual(0, self.git("merge-base", "--is-ancestor", accepted, "HEAD").returncode)
        self.git("push")

    def test_main_push_passes_check_push(self) -> None:
        receipt = self.install()
        self.assertEqual(receipt["branch"], "main")
        head = self.git("rev-parse", "HEAD").stdout.strip()
        update = f"refs/heads/main {head} refs/heads/main {head}"
        with mock.patch.dict(os.environ, self.env, clear=False):
            checked = self.accepted(
                "the main push", policy.check_push,
                self.repo, "origin", str(self.remote), update,
            )
        self.assertEqual(checked["refs"], ["refs/heads/main"])

    def push_to_an_origin_with_no_main(self, branch: str) -> str:
        """Bind, then forget origin/main as a clone of an empty origin has it; the update line git
        gives pre-push for a push that creates `branch` there."""
        self.install()
        self.git("update-ref", "-d", "refs/remotes/origin/main")
        head = self.git("rev-parse", "HEAD").stdout.strip()
        return f"refs/heads/main {head} refs/heads/{branch} {ZERO_OID}"

    def test_the_first_push_of_main_to_an_origin_with_no_main_passes_check_push(self) -> None:
        update = self.push_to_an_origin_with_no_main("main")
        with mock.patch.dict(os.environ, self.env, clear=False):
            checked = self.accepted(
                "the first main push", policy.check_push, self.repo, "origin", str(self.remote), update)
        self.assertEqual(["refs/heads/main"], checked["refs"])

    def test_a_push_to_main_that_lacks_the_accepted_main_is_still_refused(self) -> None:
        self.install()
        self.advance_main()
        self.git("fetch", "origin", "+refs/heads/main:refs/remotes/origin/main")
        head = self.git("rev-parse", "HEAD").stdout.strip()
        remote = self.git("rev-parse", "refs/remotes/origin/main").stdout.strip()
        update = f"refs/heads/main {head} refs/heads/main {remote}"
        with mock.patch.dict(os.environ, self.env, clear=False):
            with self.assertRaisesRegex(policy.PolicyError, "tip does not contain accepted main"):
                policy.check_push(self.repo, "origin", str(self.remote), update)

    def test_the_first_push_of_another_branch_gets_the_same_answer(self) -> None:
        update = self.push_to_an_origin_with_no_main("other")
        with mock.patch.dict(os.environ, self.env, clear=False):
            with self.assertRaisesRegex(policy.PolicyError, "are allowed only on main; current target is other"):
                policy.check_push(self.repo, "origin", str(self.remote), update)

    def test_can_delete_integrated_non_main_branch(self) -> None:
        self.install()
        tip = self.git("rev-parse", "HEAD").stdout.strip()
        update = f"(delete) {ZERO_OID} refs/heads/cleanup {tip}"
        with mock.patch.dict(os.environ, self.env, clear=False):
            checked = self.accepted(
                "the integrated branch deletion", policy.check_push,
                self.repo, "origin", str(self.remote), update,
            )
        self.assertEqual(checked["refs"], ["refs/heads/cleanup"])

    def test_can_delete_patch_equivalent_non_main_branch(self) -> None:
        self.install()
        self.git("switch", "-c", "cleanup")
        (self.repo / "equivalent.txt").write_text("equivalent branch patch\n", encoding="utf-8")
        self.git("add", "equivalent.txt")
        self.git("commit", "--no-verify", "-m", "branch patch")
        tip = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("switch", "main")
        self.git("cherry-pick", "--no-commit", tip)
        self.git("commit", "-m", "accepted equivalent patch")
        self.git("push")
        self.assertNotEqual(
            0, self.git("merge-base", "--is-ancestor", tip, "origin/main", check=False).returncode,
        )
        update = f"(delete) {ZERO_OID} refs/heads/cleanup {tip}"
        with mock.patch.dict(os.environ, self.env, clear=False):
            checked = policy.check_push(self.repo, "origin", str(self.remote), update)
        self.assertEqual(checked["refs"], ["refs/heads/cleanup"])

    def test_cannot_delete_unintegrated_branch(self) -> None:
        self.install()
        self.git("switch", "-c", "cleanup")
        (self.repo / "unmerged.txt").write_text("unmerged branch patch\n", encoding="utf-8")
        self.git("add", "unmerged.txt")
        self.git("commit", "--no-verify", "-m", "unmerged branch patch")
        tip = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("switch", "main")
        update = f"(delete) {ZERO_OID} refs/heads/cleanup {tip}"
        with mock.patch.dict(os.environ, self.env, clear=False):
            with self.assertRaisesRegex(policy.PolicyError, "not integrated into accepted main"):
                policy.check_push(self.repo, "origin", str(self.remote), update)

    def test_main_branch_deletion_is_refused(self) -> None:
        self.install()
        tip = self.git("rev-parse", "HEAD").stdout.strip()
        main_update = f"(delete) {ZERO_OID} refs/heads/main {tip}"
        with mock.patch.dict(os.environ, self.env, clear=False):
            with self.assertRaisesRegex(policy.PolicyError, "main branch deletion is refused"):
                policy.check_push(self.repo, "origin", str(self.remote), main_update)

    def test_verified_origin_url_cannot_be_redirected_at_pre_push(self) -> None:
        self.install()
        update = "refs/heads/main " + "1" * 40 + " refs/heads/main " + "2" * 40
        with mock.patch.dict(os.environ, self.env, clear=False):
            with self.assertRaisesRegex(policy.PolicyError, "origin URL"):
                policy.check_push(self.repo, "origin", "https://elsewhere.invalid/repo.git", update)

    def test_worktree_install_keeps_one_upstream_entry(self) -> None:
        self.git("config", "extensions.worktreeConfig", "true")
        # The clone's own upstream entry, as git clone or a plain push -u leaves it.
        self.git("config", "--local", "branch.main.remote", "origin")
        self.git("config", "--local", "branch.main.merge", "refs/heads/main")
        branch = self.install()["branch"]
        self.assertEqual("main", branch)
        for key in (f"branch.{branch}.merge", f"branch.{branch}.remote"):
            origins = self.git("config", "--show-origin", "--get-all", key).stdout.split()
            self.assertEqual(["file:.git/config.worktree", f"refs/heads/{branch}" if key.endswith("merge") else "origin"], origins, key)
        with mock.patch.dict(os.environ, self.env, clear=False):
            self.assertEqual(
                "PASS", self.accepted("status", policy.status, self.repo)["result"],
            )
            self.git("config", "--local", f"branch.{branch}.merge", f"refs/heads/{branch}")
            with self.assertRaisesRegex(policy.PolicyError, "two upstream entries"):
                policy.status(self.repo)

    def test_status_accepts_this_checkouts_absolute_hooks_path_and_refuses_any_other(self) -> None:
        # Claude Code's worktree isolation rewrites core.hooksPath as the absolute path.
        self.install()
        other = Path(self.temporary.name) / "other" / "Drawer" / "git-hooks"
        with mock.patch.dict(os.environ, self.env, clear=False):
            self.git("config", "core.hooksPath", str(self.repo / "Drawer" / "git-hooks"))
            try:
                result = policy.status(self.repo)["result"]
            except policy.PolicyError as refusal:  # a refusal is this test's FAIL, never an ERROR
                result = str(refusal)
            self.assertEqual("PASS", result)
            for foreign in (str(other), "Drawer/other-hooks"):
                self.git("config", "core.hooksPath", foreign)
                with self.assertRaisesRegex(policy.PolicyError, "tracked hub Git hooks"):
                    policy.status(self.repo)

    def test_an_unbound_checkout_fails_closed(self) -> None:
        with mock.patch.dict(os.environ, self.env, clear=False):
            with self.assertRaisesRegex(policy.PolicyError, "no verified branch-policy binding"):
                policy.check_commit(self.repo)
            self.install()
            self.git("config", "hub.bound", "false")
            with self.assertRaisesRegex(policy.PolicyError, "no verified branch-policy binding"):
                policy.status(self.repo)

    def test_install_refuses_another_branch(self) -> None:
        self.git("switch", "-c", "scratch")
        with self.assertRaisesRegex(policy.PolicyError, "checkout is on scratch; preserve its work"):
            self.install(refusal_ok=True)
        self.assertEqual("", self.git("config", "--get", "hub.bound", check=False).stdout)

    def test_install_refuses_shared_worktree_config(self) -> None:
        sibling = Path(self.temporary.name) / "sibling"
        self.git("worktree", "add", "-b", "scratch", str(sibling))
        with mock.patch.dict(os.environ, self.env, clear=False):
            with self.assertRaisesRegex(policy.PolicyError, "worktreeConfig"):
                policy.install(self.repo)

    def test_freshness_unconfigured_never_claims_current(self) -> None:
        result = self.freshness("startup")
        self.assertEqual("unconfigured", result["state"])
        self.assertEqual(
            "GIT FRESHNESS SETUP REQUIRED — this Hub checkout has no verified branch-policy "
            "binding. Run the setup skill's branch binding before code work; do not commit or "
            "push.",
            result.get("message", ""),
        )

    def test_freshness_current_accepts_ahead_only_and_preserves_dirty_work(self) -> None:
        self.install()
        (self.repo / "local.txt").write_text("local\n", encoding="utf-8")
        result = self.freshness("startup")
        self.assertEqual("current", result["state"])
        self.assertIs(True, result["dirty"])
        self.assertEqual(result["head_before"], result["head_after"])
        self.assertEqual("local\n", (self.repo / "local.txt").read_text(encoding="utf-8"))

    def test_startup_rebases_clean_main_with_local_commits_onto_accepted_main(self) -> None:
        self.install()
        (self.repo / "local.txt").write_text("local\n", encoding="utf-8")
        self.git("add", "local.txt")
        self.git("commit", "-m", "local work")
        accepted = self.advance_main()
        before = self.git("rev-parse", "HEAD").stdout.strip()
        result = self.freshness("startup")
        self.assertEqual("rebased", result["state"])
        self.assertEqual(before, result["head_before"])
        self.assertNotEqual(before, result["head_after"])
        self.assertEqual(accepted, result["accepted_main"])
        self.assertEqual("main", self.git("branch", "--show-current").stdout.strip())
        self.assertEqual(0, self.git("merge-base", "--is-ancestor", accepted, "HEAD").returncode)
        self.assertEqual("local\n", (self.repo / "local.txt").read_text(encoding="utf-8"))

    def hook_logged(self):
        """Arm the pre-commit trace and hold the gate, so a commit through a hook fails and is seen."""
        self.hook_log = Path(self.temporary.name) / "hook.log"
        return mock.patch.dict(os.environ, {"HOOK_LOG": str(self.hook_log), "CONFORM_EXIT": "1"})

    def assert_no_hook_ran(self) -> None:
        """Pre-commit, the one hook live under the freshness lock, left no trace: nothing was committed."""
        self.assertFalse(self.hook_log.exists())

    def seed_notes(self) -> None:
        """notes.txt on accepted main, so upstream and the checkout change separate lines of one file."""
        (self.repo / "notes.txt").write_text(NOTES, encoding="utf-8")
        self.git("add", "notes.txt")
        self.git("commit", "-m", "notes")
        self.git("push", "origin", "HEAD:refs/heads/main")

    def local_commit(self) -> None:
        (self.repo / "local.txt").write_text("local commit\n", encoding="utf-8")
        self.git("add", "local.txt")
        self.git("commit", "-m", "local work")

    def test_dirty_stale_start_fast_forwards_keeping_index_and_edits(self) -> None:
        self.install()
        accepted = self.advance_main()
        (self.repo / "peer-staged.txt").write_text("peer staged bytes\n", encoding="utf-8")
        self.git("add", "peer-staged.txt")
        (self.repo / "fixture.txt").write_text("interrupted bytes\n", encoding="utf-8")
        (self.repo / "new-note.txt").write_text("untracked bytes\n", encoding="utf-8")
        before_status = self.git("status", "--porcelain").stdout
        before_cached = self.git("diff", "--cached").stdout
        with self.hook_logged():
            result = self.freshness("resume")
        self.assert_no_hook_ran()
        self.assertEqual("rebased", result["state"])
        self.assertIs(True, result["fast_forward"])
        self.assertEqual(accepted, self.git("rev-parse", "HEAD").stdout.strip())
        self.assertEqual(before_status, self.git("status", "--porcelain").stdout)
        self.assertEqual(before_cached, self.git("diff", "--cached").stdout)
        self.assertEqual("interrupted bytes\n", (self.repo / "fixture.txt").read_text(encoding="utf-8"))

    def test_freshness_under_a_private_index_moves_nothing(self) -> None:
        # A harness-review lane inherits GIT_INDEX_FILE; its session-start fast-forward
        # moved HEAD and the worktree while the checkout's index kept the old tree.
        self.install()
        before_head = self.git("rev-parse", "HEAD").stdout.strip()
        self.advance_main()
        (self.repo / "fixture.txt").write_text("interrupted bytes\n", encoding="utf-8")
        private = Path(self.temporary.name) / "private.index"
        shutil.copyfile(self.repo / ".git" / "index", private)
        before_status = self.git("status", "--porcelain").stdout
        with mock.patch.dict(os.environ, {"GIT_INDEX_FILE": str(private)}):
            result = self.freshness("startup")
        self.assertEqual(("blocked", "private_index"), (result["state"], result.get("reason")))
        self.assertEqual(before_head, self.git("rev-parse", "HEAD").stdout.strip())
        self.assertEqual(before_status, self.git("status", "--porcelain").stdout)

    def test_dirty_start_with_local_commits_rebases_in_place_without_a_hook(self) -> None:
        self.install()
        self.local_commit()
        accepted = self.advance_main()
        (self.repo / "fixture.txt").write_text("interrupted bytes\n", encoding="utf-8")
        (self.repo / "new-note.txt").write_text("untracked bytes\n", encoding="utf-8")
        with self.hook_logged():
            result = self.freshness("resume")
        self.assert_no_hook_ran()
        self.assertEqual(("rebased", 1, 1, 0),
                         (result["state"], result.get("replayed"), result.get("ahead"),
                          result.get("behind")))
        self.assertEqual(
            f"GIT FRESHNESS REBASED — main replayed 1 local commits onto accepted main "
            f"{accepted[:12]} in place; uncommitted work stayed in place, nothing was committed.",
            result.get("message", ""),
        )
        self.assertEqual(0, self.git("merge-base", "--is-ancestor", accepted, "HEAD").returncode)
        self.assertEqual("local work", self.git("log", "-1", "--format=%s").stdout.strip())
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), result.get("head_after"))
        self.assertEqual(" M fixture.txt\n?? new-note.txt\n", self.git("status", "--porcelain").stdout)
        self.assertEqual("interrupted bytes\n", (self.repo / "fixture.txt").read_text(encoding="utf-8"))
        self.assertEqual("untracked bytes\n", (self.repo / "new-note.txt").read_text(encoding="utf-8"))

    def stage_and_edit_notes(self) -> tuple[str, str]:
        """Stage line three of notes.txt and leave line six unstaged on top: (index, worktree) bytes."""
        staged = NOTES.replace("three", "THREE")
        (self.repo / "notes.txt").write_text(staged, encoding="utf-8")
        self.git("add", "notes.txt")
        (self.repo / "notes.txt").write_text(staged.replace("six", "SIX"), encoding="utf-8")
        return staged, staged.replace("six", "SIX")

    def test_colliding_fast_forward_carries_staged_and_unstaged_work_without_a_hook(self) -> None:
        self.seed_notes()
        self.install()
        accepted = self.advance_main(NOTES.replace("one", "ONE"), path="notes.txt")
        self.stage_and_edit_notes()
        with self.hook_logged():
            result = self.freshness("startup")
        self.assert_no_hook_ran()
        # Git refuses the fast-forward over the dirty notes.txt; the carry moves it anyway.
        self.assertIn("notes.txt", result.get("fast_forward_refused", ""))
        self.assertEqual(("rebased", accepted, 1, 0, 0),
                         (result["state"], result.get("head_after"), result.get("carried"),
                          result.get("ahead"), result.get("behind")))
        self.assertEqual(
            f"GIT FRESHNESS REBASED — main moved onto accepted main {accepted[:12]} (HEAD "
            f"{accepted[:12]}, ahead 0); uncommitted work was carried, staged and unstaged, "
            "nothing was committed.",
            result.get("message", ""),
        )
        self.assertEqual(accepted, self.git("rev-parse", "HEAD").stdout.strip())
        upstream = NOTES.replace("one", "ONE")
        self.assertEqual(upstream.replace("three", "THREE"), self.git("show", ":notes.txt").stdout)
        self.assertEqual(upstream.replace("three", "THREE").replace("six", "SIX"),
                         (self.repo / "notes.txt").read_text(encoding="utf-8"))
        self.assertEqual("MM notes.txt\n", self.git("status", "--porcelain").stdout)
        self.assertFalse((self.repo / ".git/hub-land.index").exists())

    def test_colliding_replay_carries_staged_work_onto_the_replayed_commits(self) -> None:
        self.seed_notes()
        self.install()
        self.local_commit()
        accepted = self.advance_main(NOTES.replace("one", "ONE"), path="notes.txt")
        staged = NOTES.replace("three", "THREE")
        (self.repo / "notes.txt").write_text(staged, encoding="utf-8")
        self.git("add", "notes.txt")
        with self.hook_logged():
            result = self.freshness("startup")
        self.assert_no_hook_ran()
        self.assertIn("notes.txt", result.get("replay_refused", ""))
        head = self.git("rev-parse", "HEAD").stdout.strip()
        self.assertEqual(("rebased", head, 1, 1, 0),
                         (result["state"], result.get("head_after"), result.get("carried"),
                          result.get("ahead"), result.get("behind")))
        self.assertEqual(
            f"GIT FRESHNESS REBASED — main moved onto accepted main {accepted[:12]} (HEAD "
            f"{head[:12]}, ahead 1); uncommitted work was carried, staged and unstaged, "
            "nothing was committed.",
            result.get("message", ""),
        )
        # The local commit was replayed onto accepted main, not committed again with the work.
        self.assertEqual(accepted, self.git("rev-parse", "HEAD^").stdout.strip())
        self.assertEqual("local work", self.git("log", "-1", "--format=%s").stdout.strip())
        self.assertEqual(NOTES.replace("one", "ONE"), self.git("show", "HEAD:notes.txt").stdout)
        self.assertEqual(NOTES.replace("one", "ONE").replace("three", "THREE"),
                         self.git("show", ":notes.txt").stdout)
        self.assertEqual("M  notes.txt\n", self.git("status", "--porcelain").stdout)

    def test_carry_refused_by_an_ignored_file_keeps_staging_and_runs_no_hook(self) -> None:
        self.seed_notes()
        self.install()
        accepted = self.advance_main(NOTES.replace("one", "ONE"), path="notes.txt",
                                     also={"build.log": "accepted log\n"})
        staged, edited = self.stage_and_edit_notes()
        before_head = self.git("rev-parse", "HEAD").stdout.strip()
        before_status = self.git("status", "--porcelain").stdout
        git = policy._git

        # Git overwrites a file the checkout ignores, so an ignored file alone never refuses
        # the move. A peer's untracked build.log, written after the carry snapshotted the
        # worktree, is outside the private index: Git refuses to overwrite it.
        def peer_writes(repo, *arguments, **keywords):
            result = git(repo, *arguments, **keywords)
            if arguments == ("update-index", "-q", "--refresh") and keywords.get("env"):
                (self.repo / "build.log").write_text("peer log\n", encoding="utf-8")
            return result

        with self.hook_logged(), mock.patch.object(policy, "_git", peer_writes):
            result = self.freshness("startup")
        self.assert_no_hook_ran()
        before_status += "?? build.log\n"
        self.assertEqual(("blocked", "carry_refused", before_head),
                         (result["state"], result.get("reason"), result.get("head_after")))
        self.assert_message(
            result.get("message", ""),
            f"GIT FRESHNESS BLOCKED — Git refused to move the worktree to accepted main {accepted[:12]}: ",
            ". Nothing moved.",
        )
        self.assertIn("build.log", result.get("message", ""))
        self.assertEqual(before_head, self.git("rev-parse", "HEAD").stdout.strip())
        self.assertEqual(before_status, self.git("status", "--porcelain").stdout)
        self.assertEqual(staged, self.git("show", ":notes.txt").stdout)
        self.assertEqual(edited, (self.repo / "notes.txt").read_text(encoding="utf-8"))
        self.assertEqual("peer log\n", (self.repo / "build.log").read_text(encoding="utf-8"))
        self.assertFalse((self.repo / ".git/hub-land.index").exists())

    def failed_carry(self, fail) -> tuple[dict[str, object], str]:
        """A dirty start whose fast-forward collides on notes.txt; fail(arguments) arms one
        carry step. Return (result, the HEAD it started on)."""
        self.seed_notes()
        self.install()
        self.advance_main(NOTES.replace("one", "ONE"), path="notes.txt")
        staged, edited = self.stage_and_edit_notes()
        before_head = self.git("rev-parse", "HEAD").stdout.strip()
        git = policy._git

        def failing(repo, *arguments, **keywords):
            # Only the carry runs merge-tree here: the fast-forward has none.
            if arguments[0] == "merge-tree":
                fail(arguments)
            return git(repo, *arguments, **keywords)

        with mock.patch.object(policy, "_git", failing):
            result = self.freshness("startup")
        self.assertEqual(before_head, self.git("rev-parse", "HEAD").stdout.strip())
        self.assertEqual(staged, self.git("show", ":notes.txt").stdout)
        self.assertEqual(edited, (self.repo / "notes.txt").read_text(encoding="utf-8"))
        self.assertFalse((self.repo / ".git/hub-land.index").exists())
        return result, before_head

    def test_carry_failure_moves_nothing_and_leaves_no_private_index(self) -> None:
        def broken(_arguments):
            raise policy.PolicyError("simulated merge-tree failure")

        result, before_head = self.failed_carry(broken)
        self.assertEqual(("blocked", "carry_failed", before_head),
                         (result["state"], result.get("reason"), result.get("head_after")))
        accepted = self.git("rev-parse", "origin/main").stdout.strip()
        self.assertEqual(
            f"GIT FRESHNESS BLOCKED — carrying uncommitted work onto accepted main {accepted[:12]} "
            "failed: simulated merge-tree failure. Nothing moved; run freshness again.",
            result.get("message", ""),
        )

    def test_timed_out_carry_blocks_with_nothing_moved(self) -> None:
        def slow(_arguments):
            raise policy.GitTimeout("Git merge-tree timed out", quiesced=True)

        result, before_head = self.failed_carry(slow)
        self.assertEqual(("blocked", "carry_timeout", before_head),
                         (result["state"], result.get("reason"), result.get("head_after")))
        accepted = self.git("rev-parse", "origin/main").stdout.strip()
        self.assertEqual(
            f"GIT FRESHNESS BLOCKED — carrying uncommitted work onto accepted main {accepted[:12]} "
            "exceeded its bounded deadline. Nothing moved; run freshness again.",
            result.get("message", ""),
        )

    def colliding_start(self, fail) -> tuple[dict[str, object], str, str, str, str]:
        """A dirty start whose fast-forward collides on notes.txt; fail(arguments) may raise on one
        policy Git call. Return (result, accepted, the HEAD it started on, staged, edited bytes)."""
        self.seed_notes()
        self.install()
        accepted = self.advance_main(NOTES.replace("one", "ONE"), path="notes.txt")
        staged, edited = self.stage_and_edit_notes()
        before_head = self.git("rev-parse", "HEAD").stdout.strip()
        git = policy._git

        def failing(repo, *arguments, **keywords):
            fail(arguments)
            return git(repo, *arguments, **keywords)

        with mock.patch.object(policy, "_git", failing):
            result = self.freshness("startup")
        return result, accepted, before_head, staged, edited

    def test_a_carry_failure_after_the_head_swap_moves_head_and_the_worktree_back(self) -> None:
        # Defect: a failure after update-ref swapped HEAD but before the index moved leaves HEAD
        # on accepted main, or the worktree on the carried tree, over an index still holding I.
        def broken_index_move(arguments):
            if arguments[0] == "read-tree" and len(arguments) == 2:
                raise policy.PolicyError("simulated index move failure")

        result, accepted, before_head, staged, edited = self.colliding_start(broken_index_move)
        self.assertEqual(("blocked", "carry_failed", before_head),
                         (result["state"], result.get("reason"), result.get("head_after")))
        self.assertEqual(
            f"GIT FRESHNESS BLOCKED — carrying uncommitted work onto accepted main {accepted[:12]} "
            "failed: simulated index move failure. HEAD and the worktree were moved back; run "
            "freshness again.",
            result.get("message", ""),
        )
        self.assertEqual(before_head, self.git("rev-parse", "HEAD").stdout.strip())
        self.assertEqual(staged, self.git("show", ":notes.txt").stdout)
        self.assertEqual(edited, (self.repo / "notes.txt").read_text(encoding="utf-8"))
        self.assertEqual("MM notes.txt\n", self.git("status", "--porcelain").stdout)
        self.assertFalse((self.repo / ".git/hub-land.index").exists())

    def test_the_carry_keeps_an_intent_to_add_entry(self) -> None:
        # Defect: the carry rebuilds the index from write-tree, which drops an intent-to-add
        # entry, so the new file falls back to untracked.
        self.seed_notes()
        self.install()
        accepted = self.advance_main(NOTES.replace("one", "ONE"), path="notes.txt")
        self.stage_and_edit_notes()
        (self.repo / "draft.txt").write_text("draft bytes\n", encoding="utf-8")
        self.git("add", "-N", "draft.txt")
        with self.hook_logged():
            result = self.freshness("startup")
        self.assert_no_hook_ran()
        self.assertEqual(("rebased", accepted), (result["state"], result.get("head_after")))
        self.assertEqual(accepted, self.git("rev-parse", "HEAD").stdout.strip())
        self.assertEqual(" A draft.txt\nMM notes.txt\n", self.git("status", "--porcelain").stdout)
        self.assertEqual("draft bytes\n", (self.repo / "draft.txt").read_text(encoding="utf-8"))

    def test_in_place_rebase_keeps_a_peers_index_edits_and_the_commit_author(self) -> None:
        self.install()
        (self.repo / "local.txt").write_text("local commit\n", encoding="utf-8")
        self.git("add", "local.txt")
        self.git("commit", "--author", "Peer Author <peer@example.invalid>",
                 "--date", "1700000000 +0130", "-m", "local work — first line\n\nbody line\n")
        original = self.git("log", "-1", "--date=raw", "--format=%an|%ae|%ad|%B").stdout
        (self.repo / "second.txt").write_text("second commit\n", encoding="utf-8")
        self.git("add", "second.txt")
        self.git("commit", "-m", "second local work")
        accepted = self.advance_main()
        (self.repo / "peer-staged.txt").write_text("peer staged bytes\n", encoding="utf-8")
        self.git("add", "peer-staged.txt")
        (self.repo / "fixture.txt").write_text("peer staged edit\n", encoding="utf-8")
        self.git("add", "fixture.txt")
        (self.repo / "fixture.txt").write_text("peer unstaged edit\n", encoding="utf-8")
        (self.repo / "new-note.txt").write_text("untracked bytes\n", encoding="utf-8")
        before_status = self.git("status", "--porcelain").stdout
        before_cached = self.git("diff", "--cached").stdout
        with mock.patch.dict(os.environ, {"CONFORM_EXIT": "1"}):
            result = self.freshness("startup")
        self.assertEqual(("rebased", 2), (result["state"], result.get("replayed")))
        self.assertEqual(accepted, self.git("rev-parse", "HEAD~2").stdout.strip())
        self.assertEqual(original, self.git("log", "-1", "--date=raw", "--format=%an|%ae|%ad|%B",
                                            "HEAD~1").stdout)
        self.assertEqual("second local work", self.git("log", "-1", "--format=%s").stdout.strip())
        self.assertEqual("second commit\n", self.git("show", "HEAD:second.txt").stdout)
        self.assertEqual(before_status, self.git("status", "--porcelain").stdout)
        self.assertEqual(before_cached, self.git("diff", "--cached").stdout)
        self.assertEqual("peer unstaged edit\n", (self.repo / "fixture.txt").read_text(encoding="utf-8"))

    def test_local_commit_conflict_with_a_dirty_tree_blocks_with_nothing_moved(self) -> None:
        self.install()
        (self.repo / "fixture.txt").write_text("local version\n", encoding="utf-8")
        self.git("add", "fixture.txt")
        self.git("commit", "-m", "local conflict")
        before_head = self.git("rev-parse", "HEAD").stdout.strip()
        accepted = self.advance_main("upstream version\n", path="fixture.txt")
        (self.repo / "new-note.txt").write_text("untracked bytes\n", encoding="utf-8")
        before_status = self.git("status", "--porcelain").stdout
        with self.hook_logged():
            result = self.freshness("startup")
        self.assert_no_hook_ran()
        self.assertEqual(("blocked", "replay_conflict", before_head),
                         (result["state"], result.get("reason"), result.get("head_after")))
        self.assertEqual(["fixture.txt"], result.get("conflicts"))
        self.assertEqual(
            f"GIT FRESHNESS BLOCKED — local commit {before_head[:12]} conflicts with accepted main "
            f"{accepted[:12]} in fixture.txt and the worktree holds uncommitted work; nothing moved. "
            "Once that work is committed or reverted, run git rebase origin/main, resolve each "
            f"conflict, finish the rebase, and run {PREPARED} Drawer/git-branch-policy.py publish. "
            "Never stash, reset or check out another session's work; never ask the user.",
            result.get("message", ""),
        )
        self.assertEqual(before_head, self.git("rev-parse", "HEAD").stdout.strip())
        self.assertEqual(before_status, self.git("status", "--porcelain").stdout)
        self.assertEqual("untracked bytes\n", (self.repo / "new-note.txt").read_text(encoding="utf-8"))

    def test_local_commits_landed_upstream_as_one_squash_are_dropped(self) -> None:
        self.install()
        self.local_commit()
        (self.repo / "second.txt").write_text("second commit\n", encoding="utf-8")
        self.git("add", "second.txt")
        self.git("commit", "-m", "second local work")
        accepted = self.advance_main("local commit\n", path="local.txt",
                                     also={"second.txt": "second commit\n"})
        (self.repo / "fixture.txt").write_text("interrupted bytes\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"CONFORM_EXIT": "1"}):
            result = self.freshness("startup")
        self.assertEqual(("rebased", 0), (result["state"], result.get("replayed")))
        self.assertEqual((0, 0), (result["ahead"], result["behind"]))
        self.assertEqual(accepted, self.git("rev-parse", "HEAD").stdout.strip())
        self.assertEqual(" M fixture.txt\n", self.git("status", "--porcelain").stdout)

    def test_local_merge_history_with_a_dirty_tree_blocks_with_nothing_moved(self) -> None:
        self.install()
        self.git("checkout", "-b", "side")
        (self.repo / "side.txt").write_text("side version\n", encoding="utf-8")
        self.git("add", "side.txt")
        self.git("commit", "--no-verify", "-m", "side change")  # the policy allows only main
        self.git("checkout", "main")
        self.local_commit()
        # The merge commit carries its own change; a linear replay of both sides would drop it.
        self.git("merge", "--no-commit", "side")
        (self.repo / "fixture.txt").write_text("resolved\n", encoding="utf-8")
        self.git("commit", "-am", "merge side, resolved")
        self.advance_main()
        (self.repo / "new-note.txt").write_text("untracked bytes\n", encoding="utf-8")
        before_head = self.git("rev-parse", "HEAD").stdout.strip()
        with self.hook_logged():
            result = self.freshness("startup")
        self.assert_no_hook_ran()
        self.assertEqual(("blocked", "merge_history_dirty", before_head),
                         (result["state"], result.get("reason"), result.get("head_after")))
        self.assertEqual(
            "GIT FRESHNESS BLOCKED — main holds a local merge commit and uncommitted work; nothing "
            "moved. Run freshness again once that work is committed or reverted. Never stash, reset "
            "or check out another session's work.",
            result.get("message", ""),
        )
        self.assertEqual(before_head, self.git("rev-parse", "HEAD").stdout.strip())
        self.assertEqual("?? new-note.txt\n", self.git("status", "--porcelain").stdout)
        self.assertEqual("untracked bytes\n", (self.repo / "new-note.txt").read_text(encoding="utf-8"))

    def peer_commit_race(self, *, after_move: bool) -> None:
        """A peer commit lands before or after the worktree move; the swap refuses either way."""
        self.install()
        self.local_commit()
        self.advance_main()
        (self.repo / "peer-staged.txt").write_text("peer staged bytes\n", encoding="utf-8")
        self.git("add", "peer-staged.txt")
        git = policy._git

        def peer_commits(repo, *arguments, **keywords):
            before = arguments[0] == "read-tree" and not after_move
            if before:
                self.git("commit", "-m", "peer commit")
            result = git(repo, *arguments, **keywords)
            if arguments[0] == "read-tree" and after_move:
                self.git("commit", "-m", "peer commit")
            return result

        with mock.patch.object(policy, "_git", peer_commits):
            result = self.freshness("startup")
        peer = self.git("rev-parse", "HEAD").stdout.strip()
        self.assertEqual(("blocked", "peer_commit_race"), (result["state"], result.get("reason")))
        self.assertEqual("peer commit", self.git("log", "-1", "--format=%s").stdout.strip())
        self.assertEqual(peer, result["head_after"])
        self.assertIn("Run git read-tree -m -u ", result["message"])
        self.assertIn(f" {peer} to return the worktree to it", result["message"])

    def test_peer_commit_before_the_move_blocks_and_keeps_head_on_it(self) -> None:
        self.peer_commit_race(after_move=False)

    def test_peer_commit_after_the_move_blocks_and_keeps_head_on_it(self) -> None:
        self.peer_commit_race(after_move=True)

    def timed_out_move(self, *, quiesced: bool) -> dict[str, object]:
        self.install()
        self.local_commit()
        self.advance_main()
        (self.repo / "fixture.txt").write_text("interrupted bytes\n", encoding="utf-8")
        before_head = self.git("rev-parse", "HEAD").stdout.strip()
        git = policy._git

        def slow_read_tree(repo, *arguments, **keywords):
            if arguments[0] == "read-tree":
                raise policy.GitTimeout("Git read-tree timed out", quiesced=quiesced)
            return git(repo, *arguments, **keywords)

        with mock.patch.object(policy, "_git", slow_read_tree):
            result = self.freshness("startup")
        self.assertEqual(before_head, self.git("rev-parse", "HEAD").stdout.strip())
        self.assertEqual("interrupted bytes\n", (self.repo / "fixture.txt").read_text(encoding="utf-8"))
        return result

    def test_timed_out_move_blocks_without_moving_head(self) -> None:
        result = self.timed_out_move(quiesced=True)
        self.assertEqual(("blocked", "replay_timeout"), (result["state"], result.get("reason")))

    def test_unverified_move_timeout_names_the_unverified_process_tree(self) -> None:
        result = self.timed_out_move(quiesced=False)
        self.assertEqual(("blocked", "replay_process_tree_unverified"),
                         (result["state"], result.get("reason")))

    def test_carried_edit_already_upstream_leaves_a_clean_tree(self) -> None:
        self.install()
        self.local_commit()
        accepted = self.advance_main("upstream version\n", path="fixture.txt")
        (self.repo / "fixture.txt").write_text("upstream version\n", encoding="utf-8")
        with self.hook_logged():
            result = self.freshness("startup")
        self.assert_no_hook_ran()
        # The dirty path is one the rebase changes, so the in-place move was refused first.
        self.assertIn("fixture.txt", result.get("replay_refused", ""))
        # The carried edit equals accepted main's: nothing is left uncommitted and nothing committed.
        self.assertEqual(("rebased", 0), (result["state"], result.get("carried")))
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), result.get("head_after"))
        self.assertEqual("local work", self.git("log", "-1", "--format=%s").stdout.strip())
        self.assertEqual(accepted, self.git("rev-parse", "HEAD^").stdout.strip())
        self.assertEqual("upstream version\n", (self.repo / "fixture.txt").read_text(encoding="utf-8"))
        self.assertEqual("", self.git("status", "--porcelain").stdout)

    def test_resume_rebase_conflict_aborts_and_names_conflict_files(self) -> None:
        self.install()
        (self.repo / "fixture.txt").write_text("resume local version\n", encoding="utf-8")
        self.git("add", "fixture.txt")
        self.git("commit", "-m", "resume local conflict")
        before = self.git("rev-parse", "HEAD").stdout.strip()
        self.advance_main("resume upstream version\n", path="fixture.txt")
        result = self.freshness("resume")
        self.assertEqual("blocked", result["state"])
        self.assertEqual("rebase_conflict", result["reason"])
        self.assertEqual(["fixture.txt"], result["conflicts"])
        self.assertEqual(
            "GIT FRESHNESS BLOCKED — rebasing main onto accepted main conflicted in "
            f"fixture.txt. The rebase was aborted and HEAD {before[:12]} restored. Run "
            "git rebase origin/main, resolve each conflict, finish the rebase, and run "
            f"{PREPARED} Drawer/git-branch-policy.py publish. Never ask the user.",
            result.get("message", ""),
        )
        self.assertEqual(before, self.git("rev-parse", "HEAD").stdout.strip())
        self.assertEqual("", self.git("status", "--porcelain").stdout)
        self.assertFalse((self.repo / self.git("rev-parse", "--git-path", "rebase-merge").stdout.strip()).exists())
        self.assertFalse((self.repo / self.git("rev-parse", "--git-path", "rebase-apply").stdout.strip()).exists())

    def test_replay_refusal_that_conflicts_keeps_the_index_and_runs_no_hook(self) -> None:
        self.install()
        self.local_commit()
        # Upstream changes the dirty path, so the in-place rebase refuses and the carry runs;
        # the one-line file changed on both sides is a true conflict.
        accepted = self.advance_main("upstream version\n", path="fixture.txt")
        (self.repo / "peer-staged.txt").write_text("peer staged bytes\n", encoding="utf-8")
        self.git("add", "peer-staged.txt")
        (self.repo / "fixture.txt").write_text("refused bytes\n", encoding="utf-8")
        (self.repo / "new-note.txt").write_text("untracked\n", encoding="utf-8")
        before_head = self.git("rev-parse", "HEAD").stdout.strip()
        before_status = self.git("status", "--porcelain").stdout
        before_cached = self.git("diff", "--cached").stdout
        with self.hook_logged():
            result = self.freshness("startup")
        self.assert_no_hook_ran()
        self.assertEqual(("blocked", "carry_conflict", before_head),
                         (result["state"], result.get("reason"), result.get("head_after")))
        self.assertEqual(
            f"GIT FRESHNESS BLOCKED — uncommitted edits in fixture.txt conflict with accepted main "
            f"{accepted[:12]}; nothing moved, main stays at {before_head[:12]}. Run freshness again "
            "once that work is committed or reverted. Never stash, reset or check out another "
            "session's work.",
            result.get("message", ""),
        )
        self.assertIn("fixture.txt", result.get("replay_refused", ""))
        self.assertEqual(before_head, self.git("rev-parse", "HEAD").stdout.strip())
        self.assertEqual(before_status, self.git("status", "--porcelain").stdout)
        self.assertEqual(before_cached, self.git("diff", "--cached").stdout)
        self.assertEqual("refused bytes\n", (self.repo / "fixture.txt").read_text(encoding="utf-8"))
        self.assertFalse((self.repo / ".git/hub-land.index").exists())

    def test_dirty_conflicting_start_restores_the_tree_and_index_it_found(self) -> None:
        self.install()
        accepted = self.advance_main("upstream version\n", path="fixture.txt")
        (self.repo / "peer-staged.txt").write_text("peer staged bytes\n", encoding="utf-8")
        self.git("add", "peer-staged.txt")
        (self.repo / "fixture.txt").write_text("local version\n", encoding="utf-8")
        before_head = self.git("rev-parse", "HEAD").stdout.strip()
        before_status = self.git("status", "--porcelain").stdout
        before_cached = self.git("diff", "--cached").stdout
        with self.hook_logged():
            result = self.freshness("startup")
        self.assert_no_hook_ran()
        self.assertEqual(("blocked", "carry_conflict", before_head),
                         (result["state"], result.get("reason"), result.get("head_after")))
        self.assertIn("fixture.txt", result.get("fast_forward_refused", ""))
        self.assertEqual(
            f"GIT FRESHNESS BLOCKED — uncommitted edits in fixture.txt conflict with accepted main "
            f"{accepted[:12]}; nothing moved, main stays at {before_head[:12]}. Run freshness again "
            "once that work is committed or reverted. Never stash, reset or check out another "
            "session's work.",
            result.get("message", ""),
        )
        self.assertEqual(before_head, self.git("rev-parse", "HEAD").stdout.strip())
        self.assertEqual(before_status, self.git("status", "--porcelain").stdout)
        self.assertEqual(before_cached, self.git("diff", "--cached").stdout)
        self.assertEqual("local version\n", (self.repo / "fixture.txt").read_text(encoding="utf-8"))
        self.assertFalse((self.repo / ".git/hub-land.index").exists())

    def test_pre_push_refuses_a_rewrite_of_main(self) -> None:
        self.install()
        self.advance_main()
        self.git("fetch", "origin", "+refs/heads/main:refs/remotes/origin/main")
        self.git("reset", "--hard", "refs/remotes/origin/main~1")
        (self.repo / "rewrite.txt").write_text("rewrite\n", encoding="utf-8")
        self.git("add", "rewrite.txt")
        self.git("commit", "-m", "rewrite")
        denied = self.git("push", "--force", "origin", "HEAD:refs/heads/main", check=False)
        self.assertNotEqual(0, denied.returncode)
        self.assertEqual(
            [REFUSAL_PREFIX + "Hub push to main is refused because the tip does not "
             f"contain accepted main; run {PREPARED} Drawer/git-branch-policy.py publish instead "
             "of git push"],
            self.refusals(denied.stderr),
        )

    def test_publish_rebases_and_pushes_main(self) -> None:
        self.install()
        (self.repo / "local.txt").write_text("local\n", encoding="utf-8")
        self.git("add", "local.txt")
        self.git("commit", "-m", "local work")
        self.git("push")
        self.advance_main()
        with mock.patch.dict(os.environ, self.env, clear=False):
            result = policy.publish(self.repo)
        self.assertEqual("published", result["state"])
        self.assertEqual("rebased", result["freshness"])
        self.assertEqual("refs/heads/main", result["pushed_ref"])
        remote = self.git("ls-remote", "origin", "refs/heads/main").stdout.split()[0]
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), remote)

    def test_publish_retries_a_remote_side_push_fault_through_pushretry(self) -> None:
        # publish pushes through Drawer/pushretry.py: a remote-side fault retries, so a
        # GitHub hiccup after a green gate is no blocked publish.
        self.install()
        (self.repo / "local.txt").write_text("local\n", encoding="utf-8")
        self.git("add", "local.txt")
        self.git("commit", "-m", "local work")
        git, pushes = policy._git, []

        def faulting(repo, *arguments, **keywords):
            if arguments[:1] == ("push",):
                pushes.append(arguments)
                if len(pushes) == 1:
                    return subprocess.CompletedProcess(arguments, 1, stdout="",
                                                       stderr="error: RPC failed; HTTP 500 curl 22\n")
            return git(repo, *arguments, **keywords)

        with mock.patch.dict(os.environ, self.env, clear=False), mock.patch.object(policy, "_git", faulting), \
                mock.patch.object(pushretry, "BACKOFF_S", (0, 0, 0)):
            result = policy.publish(self.repo)
        self.assertEqual("published", result["state"])
        self.assertEqual(2, len(pushes))
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(),
                         self.git("ls-remote", "origin", "refs/heads/main").stdout.split()[0])

    def test_timed_out_rebase_aborts_and_restores_original_clean_branch(self) -> None:
        self.install()
        (self.repo / "fixture.txt").write_text("local timeout version\n", encoding="utf-8")
        self.git("add", "fixture.txt")
        self.git("commit", "-m", "local timeout conflict")
        before = self.git("rev-parse", "HEAD").stdout.strip()
        self.advance_main("upstream timeout version\n", path="fixture.txt")
        real_git = policy._git
        # The fake cannot assert: freshness catches every exception on this path and
        # turns it into its own blocked reason. It records; the test asserts after.
        rebase_codes: list[int] = []

        def timeout_after_real_conflict(repo, *arguments, **kwargs):
            if arguments == ("rebase", "refs/remotes/origin/main"):
                attempted = real_git(repo, *arguments, check=False)
                rebase_codes.append(attempted.returncode)
                raise policy.GitTimeout("simulated bounded rebase timeout", quiesced=True)
            return real_git(repo, *arguments, **kwargs)

        with mock.patch.object(policy, "_git", side_effect=timeout_after_real_conflict), \
             mock.patch.dict(os.environ, self.env, clear=False):
            result = policy.freshness(
                self.repo, "startup", rebase_timeout=0.1, recovery_timeout=5,
            )
        # git rebase exits 1 when it stops on a conflict: the timeout is over real work.
        self.assertEqual([1], rebase_codes)
        self.assertEqual("blocked", result["state"])
        self.assertEqual("rebase_timeout", result["reason"])
        self.assertEqual(["fixture.txt"], result["conflicts"])
        self.assertEqual(
            "GIT FRESHNESS BLOCKED — rebasing main onto accepted main timed out while "
            f"resolving fixture.txt. The rebase was aborted and HEAD {before[:12]} restored. "
            "Run git rebase origin/main, resolve each conflict, finish the rebase, and run "
            f"{PREPARED} Drawer/git-branch-policy.py publish. Never ask the user.",
            result.get("message", ""),
        )
        self.assertEqual(before, self.git("rev-parse", "HEAD").stdout.strip())
        self.assertEqual("", self.git("status", "--porcelain").stdout)
        self.assertEqual(
            "local timeout version\n", (self.repo / "fixture.txt").read_text(encoding="utf-8")
        )
        for marker in ("rebase-merge", "rebase-apply"):
            path = Path(self.git("rev-parse", "--git-path", marker).stdout.strip())
            self.assertFalse(path.exists())

    def test_timed_out_abort_reports_recovery_required_without_claiming_restoration(self) -> None:
        self.install()
        (self.repo / "fixture.txt").write_text("local abort-timeout version\n", encoding="utf-8")
        self.git("add", "fixture.txt")
        self.git("commit", "-m", "local abort-timeout conflict")
        before = self.git("rev-parse", "HEAD").stdout.strip()
        self.advance_main("upstream abort-timeout version\n", path="fixture.txt")
        real_git = policy._git
        # The fake cannot assert: freshness catches every exception on this path and
        # turns it into its own blocked reason. It records; the test asserts after.
        rebase_codes: list[int] = []

        def time_out_rebase_and_abort(repo, *arguments, **kwargs):
            if arguments == ("rebase", "refs/remotes/origin/main"):
                attempted = real_git(repo, *arguments, check=False)
                rebase_codes.append(attempted.returncode)
                raise policy.GitTimeout("simulated bounded rebase timeout", quiesced=True)
            if arguments == ("rebase", "--abort"):
                raise policy.GitTimeout("simulated bounded abort timeout", quiesced=True)
            return real_git(repo, *arguments, **kwargs)

        try:
            with mock.patch.object(policy, "_git", side_effect=time_out_rebase_and_abort), \
                 mock.patch.dict(os.environ, self.env, clear=False):
                result = policy.freshness(
                    self.repo, "startup", rebase_timeout=0.1, recovery_timeout=0.1,
                )
            # git rebase exits 1 when it stops on a conflict: the timeout is over real work.
            self.assertEqual([1], rebase_codes)
            self.assertEqual("blocked", result["state"])
            self.assertEqual("abort_failed", result["reason"])
            self.assertEqual(
                "GIT FRESHNESS BLOCKED — rebase failed and automatic abort did not restore "
                "the original HEAD. Stop work and repair this worktree before continuing.",
                result.get("message", ""),
            )
            self.assertNotEqual(before, self.git("rev-parse", "HEAD").stdout.strip())
            self.assertNotEqual("", self.git("status", "--porcelain").stdout)
        finally:
            real_git(self.repo, "rebase", "--abort", check=False)
        self.assertEqual(before, self.git("rev-parse", "HEAD").stdout.strip())
        self.assertEqual("", self.git("status", "--porcelain").stdout)

    def test_timed_git_command_quiesces_descendants_before_recovery(self) -> None:
        self.install()
        self.advance_main()
        self.git(
            "fetch", "--no-tags", "origin",
            "+refs/heads/main:refs/remotes/origin/main",
        )
        self.assertNotEqual(
            self.git("rev-parse", "HEAD").stdout.strip(),
            self.git("rev-parse", "origin/main").stdout.strip(),
        )
        # The hook child reports itself on a held socket, then blocks on a byte that never
        # comes. Its end of the connection closes only when the process itself is gone, so
        # the verdict is that event and never a wall-clock guess.
        marker = Path(self.temporary.name) / "late-child-write.txt"
        reached = Path(self.temporary.name) / "late-child-reached.txt"
        listener = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(listener.close)
        listener.settimeout(20)
        child = Path(self.temporary.name) / "late-child.py"
        child.write_text(
            "import pathlib, socket\n"
            f"link = socket.create_connection(('127.0.0.1', {listener.getsockname()[1]}))\n"
            "link.sendall(b'started')\n"
            f"pathlib.Path({json.dumps(str(reached))}).write_text('reached', encoding='utf-8')\n"
            "link.recv(1)\n"
            f"pathlib.Path({json.dumps(str(marker))}).write_text('late', encoding='utf-8')\n",
            encoding="utf-8",
        )
        hook = self.repo / "Drawer/git-hooks/pre-rebase"
        hook.write_text(
            f'#!/bin/sh\n"{Path(sys.executable).as_posix()}" "{child.as_posix()}"\n',
            encoding="utf-8",
        )
        hook.chmod(0o755)
        with self.assertRaises(policy.GitTimeout) as timed:
            policy._git(
                self.repo, "rebase", "refs/remotes/origin/main",
                check=False, timeout=2,
            )
        self.assertIs(True, timed.exception.quiesced)
        try:
            link, _peer = listener.accept()
        except TimeoutError:
            self.fail("the pre-rebase hook child never reached the held socket")
        self.addCleanup(link.close)
        link.settimeout(10)
        alive = False
        while True:
            try:
                chunk = link.recv(64)
            except ConnectionResetError:
                break            # a reset socket is a killed descendant: it is gone
            except TimeoutError:
                alive = True     # the descendant still holds its end open
                break
            if not chunk:
                break
        # The child got past sendall, and never past the byte that never came. A reset
        # discards what the parent had buffered, so the child's own file is the evidence.
        self.assertEqual("reached", reached.read_text(encoding="utf-8"))
        self.assertIs(False, alive)
        self.assertFalse(marker.exists())
        self.assertIsNone(policy._operation_in_progress(self.repo))

    def test_a_lost_binding_precedes_fetch_and_leaves_tracking_ref_unchanged(self) -> None:
        self.install()
        tracked = self.git("rev-parse", "origin/main").stdout.strip()
        self.advance_main()
        self.git("config", "--unset", "hub.bound")
        self.assertEqual("unconfigured", self.freshness("startup")["state"])
        self.assertEqual(tracked, self.git("rev-parse", "origin/main").stdout.strip())

    def test_origin_mismatch_precedes_fetch_and_leaves_tracking_ref_unchanged(self) -> None:
        self.install()
        tracked = self.git("rev-parse", "origin/main").stdout.strip()
        self.advance_main()
        self.git("remote", "set-url", "origin", str(Path(self.temporary.name) / "redirected.git"))
        with mock.patch.dict(os.environ, self.env, clear=False):
            with self.assertRaisesRegex(policy.PolicyError, "origin URL"):
                policy.freshness(self.repo, "startup")
        self.assertEqual(tracked, self.git("rev-parse", "origin/main").stdout.strip())

    def test_fetch_failure_is_blocking_and_never_claims_current(self) -> None:
        self.install()
        # The positive control: the reachable remote reports this branch current.
        self.assertEqual("current", self.freshness("startup")["state"])
        missing = str(Path(self.temporary.name) / "missing-remote.git")
        self.git("remote", "set-url", "origin", missing)
        self.git("config", "hub.originUrl", missing)
        for source in ("startup", "resume"):
            result = self.freshness(source)
            self.assertEqual("blocked", result["state"])
            self.assertEqual("fetch_failed", result["reason"])
            self.assertEqual(source, result["source"])
            # The whole message around git's own failure detail: it never says CURRENT.
            self.assert_message(
                result.get("message", ""),
                "GIT FRESHNESS BLOCKED — accepted main could not be fetched: ",
                ". Authentication or network repair is required; do not claim this "
                "checkout is current.",
            )

    def test_existing_git_operation_blocks_before_fetch(self) -> None:
        self.install()
        tracked = self.git("rev-parse", "origin/main").stdout.strip()
        self.advance_main()
        raw = self.git("rev-parse", "--git-path", "rebase-merge").stdout.strip()
        marker = Path(raw)
        if not marker.is_absolute():
            marker = self.repo / marker
        marker.mkdir(parents=True)
        result = self.freshness("startup")
        self.assertEqual("blocked", result["state"])
        self.assertEqual("operation_in_progress", result["reason"])
        self.assertEqual(tracked, self.git("rev-parse", "origin/main").stdout.strip())

    def test_overlapping_freshness_checks_are_serialized_per_worktree(self) -> None:
        self.install()
        with policy._FreshnessLock(self.repo, timeout=0.1):
            with mock.patch.dict(os.environ, self.env, clear=False):
                with self.assertRaisesRegex(policy.PolicyError, "another session"):
                    policy.freshness(self.repo, "startup", lock_timeout=0.05)

    def test_startup_rebases_clean_main_onto_accepted_main(self) -> None:
        self.install()
        before = self.git("rev-parse", "HEAD").stdout.strip()
        accepted = self.advance_main()
        with mock.patch.dict(os.environ, self.env, clear=False):
            result = policy.freshness(self.repo, "startup")
        self.assertEqual("rebased", result["state"])
        self.assertNotEqual(before, self.git("rev-parse", "HEAD").stdout.strip())
        self.assertEqual(accepted, self.git("rev-parse", "origin/main").stdout.strip())
        self.assertEqual(0, self.git("merge-base", "--is-ancestor", accepted, "HEAD").returncode)


# A seven-line file the land tests change on separate lines, so a carry has room to merge.
NOTES = "one\ntwo\nthree\nfour\nfive\nsix\nseven\n"


class FailureDetailTests(unittest.TestCase):
    def test_a_failed_push_names_its_cause_not_the_closing_line(self):
        # A past fault: publish printed only "error: failed to push some refs to ..." twice.
        stderr = ("remote: error: GH013: Repository rule violations found for refs/heads/main.\n"
                  "To https://github.com/x/hub.git\n"
                  " ! [remote rejected] HEAD -> main (push declined due to repository rule violations)\n"
                  "error: failed to push some refs to 'https://github.com/x/hub.git'\n"
                  "hint: Updates were rejected\n")
        detail = policy._failure_detail(subprocess.CompletedProcess([], 1, stdout="", stderr=stderr))
        self.assertIn("remote rejected", detail)
        self.assertIn("GH013", detail)
        self.assertNotIn("failed to push some refs", detail)

    def test_a_bare_failure_still_says_something(self):
        detail = policy._failure_detail(subprocess.CompletedProcess(
            [], 1, stdout="", stderr="error: failed to push some refs to 'origin'\n"))
        self.assertIn("failed to push some refs", detail)


class PrintedCommandTests(unittest.TestCase):
    def test_the_help_names_the_prepared_interpreter(self) -> None:
        shown = subprocess.run([sys.executable, "-B", str(MODULE_PATH), "--help"],
                               capture_output=True, text=True, check=True).stdout
        self.assertIn(f"{PREPARED} Drawer/git-branch-policy.py worktree-add", " ".join(shown.split()))
        self.assertNotIn("<python>", shown)


class RefusalJsonTests(unittest.TestCase):
    """A JSON verb refuses with one {"error"} stdout line; a hook verb prints nothing ."""

    def run_cli(self, repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-B", str(MODULE_PATH), arguments[0], "--repo", str(repo),
                               *arguments[1:]], capture_output=True, text=True, stdin=subprocess.DEVNULL)

    def test_a_refused_worktree_add_prints_its_error_as_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            subprocess.run(["git", "init", "-q", temporary], check=True)
            ran = self.run_cli(Path(temporary).resolve(), "worktree-add", "--name", "Bad Name")
        self.assertEqual(1, ran.returncode)
        lines = ran.stdout.splitlines()
        self.assertEqual(1, len(lines), ran.stdout)
        self.assertIn("a task name is lower-case letters", json.loads(lines[0])["error"])
        self.assertTrue(ran.stderr.startswith(REFUSAL_PREFIX), ran.stderr)

    def test_a_refused_hook_verb_prints_nothing_on_stdout(self) -> None:
        for verb in (["check-commit"], ["check-push", "--remote", "origin", "--url", "file:///nowhere.git"]):
            with self.subTest(verb=verb[0]), tempfile.TemporaryDirectory() as temporary:
                subprocess.run(["git", "init", "-q", temporary], check=True)
                ran = self.run_cli(Path(temporary).resolve(), *verb)
                self.assertEqual(1, ran.returncode)
                self.assertEqual("", ran.stdout)
                self.assertTrue(ran.stderr.startswith(REFUSAL_PREFIX), ran.stderr)


class GitChildTests(unittest.TestCase):
    def test_git_child_never_inherits_caller_stdin(self) -> None:
        """A stdio MCP server's pipe on fd 0 never reaches a Git child: it reads an empty stdin."""
        read_end, write_end = os.pipe()
        os.write(write_end, b"the caller's pipe\n")
        os.close(write_end)
        saved = os.dup(0)
        os.dup2(read_end, 0)
        os.close(read_end)
        try:
            with tempfile.TemporaryDirectory() as temporary:
                result = policy._git(Path(temporary), "hash-object", "--stdin")
        finally:
            os.dup2(saved, 0)
            os.close(saved)
        # git's object id of the empty blob
        self.assertEqual("e69de29bb2d1d6434b8b29ae775ad8c2e48c5391", result.stdout.strip())


if __name__ == "__main__":
    unittest.main()
