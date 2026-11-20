"""The seat command: configure, check, repair, cleanup, bind-git, one lock."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
from unittest.mock import patch

DRAWER = Path(__file__).resolve().parents[1]
if str(DRAWER) not in sys.path:
    sys.path.insert(0, str(DRAWER))
import harness_config  # noqa: E402
from runtime import filelock, profile, seat  # noqa: E402


def _unavailable(*_args, **_keywords):
    raise AssertionError("this call must not be reached in this suite")


def record(name, status, reason="", detail=None):
    return {"name": name, "status": status, "reason": reason, "detail": detail}


class SeatFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="runtime seat ")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.hub = self.home / "a hub" / "checkout"
        (self.hub / "Drawer").mkdir(parents=True)
        (self.hub / "uv.lock").write_bytes(b"version = 1\n")
        self.state = self.home / "state"
        (self.hub / "harness.toml").write_text(f'[project]\nstate_root = "{self.state}"\n', encoding="utf-8")
        self.profile = dict(schema_version=1, hub_root=str(self.hub), member_name="member",
                            agent="claude", box_name="test-box")
        self.home_patch = patch.object(Path, "home", return_value=self.home)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.hub_patch = patch.object(seat, "HUB", self.hub)
        self.hub_patch.start()
        self.addCleanup(self.hub_patch.stop)
        self.path = profile.default_profile_path(self.hub)
        profile.save_profile(self.path, self.profile)

    def stamp_current_pins(self):
        """Stamp the seat at today's pins, as an earlier configure leaves it."""
        stamp = self.path.parent / "seat-pins.json"
        stamp.write_text(json.dumps(seat.pins(self.hub)), encoding="utf-8")
        return stamp

    def run_seat(self, argv, **keywords):
        """Run the CLI, returning its exit status and everything it printed."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = seat.main(argv, **keywords)
        return status, out.getvalue(), err.getvalue()

    def sterile_env(self, **extra):
        prefix = profile.env_name("")
        values = {key: value for key, value in os.environ.items()
                  if not key.startswith(prefix) and key != "CLAUDE_CONFIG_DIR"}
        values.update(extra)
        return mock.patch.dict(os.environ, values, clear=True)


class LockTests(SeatFixture):
    def setUp(self):
        super().setUp()
        self.patcher = patch.object(seat, "SEAT_LOCK_SECONDS", 0.2)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    @contextlib.contextmanager
    def held(self):
        """Hold the seat lock from another thread, as a second seat command would."""
        taken, release = threading.Event(), threading.Event()

        def hold():
            with filelock.FileLock(self.path.parent / "seat.lock", 5):
                taken.set()
                release.wait(10)
        worker = threading.Thread(target=hold)
        worker.start()
        try:
            self.assertTrue(taken.wait(10), "the holding thread never took the lock")
            yield
        finally:
            release.set()
            worker.join(10)

    def test_every_subcommand_refuses_while_another_holds_the_lock(self):
        with self.held():
            for argv in (["check"], ["configure"], ["cleanup"]):
                with self.subTest(argv=argv):
                    status, out, err = self.run_seat(argv, run_command=_unavailable)
                    self.assertEqual(3, status)
                    self.assertEqual("", out)
                    self.assertEqual(f"seat: another seat command holds {self.path.parent / 'seat.lock'}\n",
                                     err)


class ConfigureTests(SeatFixture):
    def setUp(self):
        super().setUp()
        # The pin tests move one of today's pins.
        self.stamp = self.stamp_current_pins()
        self.sync_command = [str(seat.interpreter(self.hub)), "-B",
                             str(self.hub / "Drawer" / "settings-sync.py"), "--apply"]

    def configured(self, **environment):
        calls = []

        def run_command(args, *, cwd, env=None, capture=False, timeout=None):
            calls.append([str(item) for item in args])
            return subprocess.CompletedProcess(args, 0, "settings-sync: every file is current\n", "")
        return calls, run_command, self.sterile_env(**environment)

    def test_a_current_seat_refreshes_its_registrations_and_installs_nothing(self):
        calls, run_command, environment = self.configured()
        with environment:
            status, out, err = self.run_seat(["configure"], run_command=run_command)
        self.assertEqual((0, ""), (status, err))
        # One child: the settings and .mcp.json refresh. Every pin is already stamped.
        self.assertEqual([self.sync_command], calls)
        self.assertEqual(["settings-sync: every file is current",
                          f"configure: trusted {len(seat.trust_dirs(self.hub))} folders in "
                          f"{self.home / '.claude.json'}"], out.splitlines())
        self.assertEqual([], seat.stale_pins(self.hub))
        # A second run finds every folder trusted and writes nothing.
        with environment:
            status, out, err = self.run_seat(["configure"], run_command=run_command)
        self.assertEqual((0, "", ["settings-sync: every file is current"]), (status, err, out.splitlines()))

    # A pack applied after setup binds on the next configure, with no bind-git run. Injection: drop the
    # bind_product_hooks call in configure().
    def test_configure_binds_the_product_clone_to_the_hub_product_hooks(self):
        product = self.home / "product"
        product.mkdir()
        text = (self.hub / "harness.toml").read_text(encoding="utf-8")
        (self.hub / "harness.toml").write_text(text + f'\n[product]\npath = {json.dumps(str(product))}\n', encoding="utf-8")
        calls, run_command, environment = self.configured()
        with environment:
            status, out, err = self.run_seat(["configure"], run_command=run_command)
        self.assertEqual((0, ""), (status, err))
        hooks = self.hub / "Drawer" / "product-hooks"
        self.assertEqual([self.sync_command, ["git", "-C", str(product.resolve()), "config", "core.hooksPath", str(hooks)]],
                         calls)
        self.assertIn(f"bind-git: product hooks {hooks}", out)

    # configure trusts the hub root and the task-worktree parents in the client config, so a session never
    # stops at the trust dialog. Injection: drop the trust_folders call in configure().
    def test_configure_trusts_the_hub_and_worktree_parents_and_keeps_every_other_key(self):
        config = self.home / ".claude.json"
        config.write_text(json.dumps({"userID": "u", "projects": {"/tmp": {"allowedTools": ["x"]}}}),
                          encoding="utf-8")
        config.chmod(0o600)
        calls, run_command, environment = self.configured()
        with environment:
            status, out, err = self.run_seat(["configure"], run_command=run_command)
        self.assertEqual((0, ""), (status, err))
        value = json.loads(config.read_text(encoding="utf-8"))
        want = {str(self.hub), f"{self.hub}_wt"}
        self.assertEqual(want, set(seat.trust_dirs(self.hub)))
        self.assertEqual(want, {k for k, v in value["projects"].items() if v.get("hasTrustDialogAccepted") is True})
        self.assertEqual("u", value["userID"])
        self.assertEqual({"allowedTools": ["x"]}, value["projects"]["/tmp"])
        self.assertEqual(0o600, config.stat().st_mode & 0o777)

    def test_trust_honors_the_client_config_dir_and_covers_the_product_worktrees(self):
        product = self.home / "product"
        subprocess.run(["git", "init", "-q", str(product)], check=True)
        with (self.hub / "harness.toml").open("a", encoding="utf-8") as handle:
            handle.write(f'[product]\npath = "{product}"\n')
        self.assertIn(f"{product}_wt", seat.trust_dirs(self.hub))
        folder = self.home / "client"
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(folder)}):
            self.assertEqual(folder / ".claude.json", seat.client_config_path())
            added = seat.trust_folders(seat.trust_dirs(self.hub))
        self.assertEqual(added, [k for k, v in json.loads((folder / ".claude.json").read_text())["projects"].items()
                                 if v["hasTrustDialogAccepted"]])
        self.assertFalse((self.home / ".claude.json").exists())

    def test_a_moved_lock_syncs_from_its_folder_and_stamps_once(self):
        self.stamp.write_text(json.dumps({"hub-uv-lock": "0" * 64}), encoding="utf-8")
        self.assertEqual(["hub-uv-lock"], seat.stale_pins(self.hub))
        sync = [["uv", "sync", "--locked", "--link-mode", "copy", "--project", str(self.hub)]]
        for run, expected_sync, expected_where in ((1, sync, [(self.hub, 900)]), (2, [], [])):
            calls, run_command, environment = self.configured()
            seen = []

            def recording(args, *, cwd, env=None, capture=False, timeout=None):
                if args[0] == "uv":
                    seen.append((Path(cwd), timeout))
                return run_command(args, cwd=cwd, env=env, capture=capture, timeout=timeout)
            with environment:
                status, out, err = self.run_seat(["configure"], run_command=recording)
            self.assertEqual((0, ""), (status, err), f"run {run}")
            self.assertEqual(expected_sync, [call for call in calls if call[0] == "uv"], f"run {run}")
            # The sync runs from its lock's folder, as the smoke repairs do, on the 900-second budget.
            self.assertEqual(expected_where, seen, f"run {run}")
            self.assertEqual([], seat.stale_pins(self.hub), f"run {run}")
        self.assertEqual({"hub-uv-lock": "dbab12665d98aef021ba64953c61b0ed8a908cfb56a1c01e2fcb4b052b71a2a1"},
                         json.loads(self.stamp.read_text(encoding="utf-8")))

    def test_a_failed_sync_leaves_its_pin_unstamped(self):
        self.stamp.write_text(json.dumps({"hub-uv-lock": "0" * 64}), encoding="utf-8")
        calls, run_command, environment = self.configured()

        def failing_sync(args, **keywords):
            if "--locked" in args:
                return subprocess.CompletedProcess(args, 2, "", "error: lock does not match\n")
            return run_command(args, **keywords)
        with environment:
            status, out, err = self.run_seat(["configure"], run_command=failing_sync)
        self.assertEqual((1, "configure: hub-uv-lock sync failed (exit 2): error: lock does not match\n"),
                         (status, err))
        self.assertEqual(["hub-uv-lock"], seat.stale_pins(self.hub))

    def test_a_failed_registration_refresh_stops_the_command(self):
        def run_command(args, *, cwd, env=None, capture=False, timeout=None):
            return subprocess.CompletedProcess(args, 2, "", "settings-sync: manifest is not JSON\n")
        with self.sterile_env():
            status, out, err = self.run_seat(["configure"], run_command=run_command)
        self.assertEqual((1, ""), (status, out))
        self.assertEqual("configure: settings-sync.py failed (exit 2): settings-sync: manifest is not JSON\n", err)

    def test_a_profile_without_an_agent_configures_as_before(self):
        # A profile saved before the agent field was required still loads without one.
        self.path.write_text(json.dumps({key: value for key, value in self.profile.items() if key != "agent"}),
                             encoding="utf-8")
        calls, run_command, environment = self.configured()
        with environment:
            status, out, err = self.run_seat(["configure"], run_command=run_command)
        self.assertEqual((0, ""), (status, err))
        self.assertEqual([self.sync_command], calls)

    def test_a_seat_without_a_readable_profile_runs_nothing(self):
        self.path.write_text("not json", encoding="utf-8")
        with self.sterile_env():
            status, out, err = self.run_seat(["configure"], run_command=_unavailable)
        self.assertEqual((1, ""), (status, out))
        self.assertEqual("seat: configure failed (ProfileError: profile is unavailable or invalid "
                         "(JSONDecodeError))\n", err)


class BootChild:
    """A seat configure child: writes its output to the log, then answers wait()."""

    def __init__(self, code, text="", slow=False):
        self.code, self.text, self.slow = code, text, slow

    def __call__(self, argv, **keywords):
        self.argv, self.keywords = argv, keywords
        keywords["stdout"].write(self.text)
        return self

    def wait(self, timeout=None):
        if self.slow:
            raise subprocess.TimeoutExpired("seat", timeout)
        return self.code


class BootStepsTests(SeatFixture):
    """session-start runs seat configure when uv.lock moved."""

    def boot(self, child, **keywords):
        with patch.object(seat.subprocess, "Popen", side_effect=child) as popen:
            return seat.boot_steps(self.hub, **keywords), popen.call_count

    def test_a_moved_lock_runs_configure_once_with_its_log_beside_the_stamp(self):
        child = BootChild(0, "configure: hub-uv-lock synced\n")
        self.assertEqual(("SEAT STEPS — configure (hub-uv-lock) ran ok.", 1), self.boot(child))
        self.assertEqual([sys.executable, "-B", str(Path(seat.__file__).resolve()), "configure"], child.argv)
        self.assertEqual(str(self.hub), child.keywords["cwd"])
        self.assertIs(subprocess.DEVNULL, child.keywords["stdin"])
        log = self.path.parent / "boot-configure.log"
        first = log.read_text(encoding="utf-8").splitlines()
        self.assertTrue(first[0].startswith("== boot "))
        self.assertEqual("configure: hub-uv-lock synced", first[1])
        self.boot(BootChild(0, "second\n"))  # a later boot appends: an earlier child may still write
        self.assertEqual(first, log.read_text(encoding="utf-8").splitlines()[:2])

    def test_a_current_seat_runs_nothing(self):
        self.stamp_current_pins()
        self.assertEqual(("", 0), self.boot(BootChild(0)))

    def test_a_task_worktree_runs_nothing(self):
        (self.hub / ".git").write_text("gitdir: /x/.git/worktrees/checkout\n", encoding="utf-8")
        self.assertEqual(("", 0), self.boot(BootChild(0)))

    def test_a_checkout_with_no_profile_runs_nothing(self):
        self.path.unlink()
        self.assertEqual(("", 0), self.boot(BootChild(0)))

    def test_a_failed_configure_names_its_last_line_and_log(self):
        log = self.path.parent / "boot-configure.log"
        line, _ = self.boot(BootChild(1, "configure: hub-uv-lock sync failed (exit 2): no network\n"))
        self.assertEqual("SEAT STEPS — configure (hub-uv-lock) failed (exit 1): configure: hub-uv-lock sync "
                         f"failed (exit 2): no network (log {log}); the next boot retries, tell the human "
                         "if it repeats.", line)

    def test_a_held_seat_lock_skips(self):
        self.assertEqual(("SEAT STEPS — configure (hub-uv-lock) skipped: another seat command holds the seat "
                          "lock; the next boot runs it.", 1), self.boot(BootChild(3)))

    def test_a_configure_past_the_budget_is_handed_off_not_killed(self):
        child = BootChild(0, slow=True)
        line, _ = self.boot(child, budget=5)
        self.assertIn("handed off: still running past the 5 s boot budget; it stamps the seat when it ends", line)
        self.assertNotIn("timeout", child.keywords)

    def test_no_budget_left_hands_off_without_a_spawn(self):
        self.assertEqual(("SEAT STEPS — configure (hub-uv-lock) handed off: no boot budget left; the next boot "
                          "runs it.", 0), self.boot(BootChild(0), budget=0))


class InitTests(SeatFixture):
    def setUp(self):
        super().setUp()
        self.path.unlink()
        cfg = {"project": {"human": "Alex"}}
        self.enterContext(patch.object(seat.harness_config, "load", lambda hub: cfg))
        self.cfg = cfg

    def test_init_writes_a_profile_that_configure_and_bind_git_can_read(self):
        with patch.object(seat.getpass, "getuser", return_value="desk"):
            status, out, _ = self.run_seat(["init"])
        self.assertEqual(0, status)
        value = profile.load_runtime_profile(self.hub)
        self.assertEqual(("alex", "desk", "operator", str(self.hub)),
                         (value["member_name"], value["box_name"], value["role"], value["hub_root"]))
        self.assertIn("init: wrote profile box=desk member=alex role=operator", out)

    def test_init_never_overwrites_a_profile(self):
        with patch.object(seat.getpass, "getuser", return_value="desk"):
            self.assertEqual(0, self.run_seat(["init"])[0])
        status, out, _ = self.run_seat(["init", "--member", "other"])
        self.assertEqual(0, status)
        self.assertEqual("alex", profile.load_profile(self.path)["member_name"])
        self.assertIn("init: profile exists box=desk member=alex role=operator", out)

    def test_init_moves_a_corrupt_profile_aside_and_writes_a_fresh_one(self):
        bad = self.path.with_name(self.path.name + ".bad")
        bad.write_text("older", encoding="utf-8")
        self.path.write_text("{not json", encoding="utf-8")
        with patch.object(seat.getpass, "getuser", return_value="desk"):
            status, out, _ = self.run_seat(["init"])
        self.assertEqual(0, status)
        self.assertEqual("{not json", bad.read_text(encoding="utf-8"))  # the older .bad is replaced
        self.assertEqual("alex", profile.load_profile(self.path)["member_name"])
        self.assertIn(f"init: saved profile was invalid; moved it to {bad.name} and wrote a fresh profile", out)
        self.assertIn("box=desk member=alex role=operator", out)

    def test_init_heals_an_incomplete_profile_and_a_profile_of_another_checkout(self):
        # The doctor fails on both (load_runtime_profile) and names `seat.py init` as the repair: init must heal them.
        other = self.home / "another hub"
        cases = {"no box_name": {k: v for k, v in self.profile.items() if k != "box_name"},
                 "another checkout": {**self.profile, "hub_root": str(other)}}
        for name, broken in cases.items():
            with self.subTest(name):
                self.path.with_name(self.path.name + ".bad").unlink(missing_ok=True)
                self.path.write_text(json.dumps(broken), encoding="utf-8")  # save_profile would refuse these
                with patch.object(seat.getpass, "getuser", return_value="desk"):
                    status, out, _ = self.run_seat(["init"])
                    again = self.run_seat(["init"])
                self.assertEqual(0, status)
                self.assertIn("init: saved profile was invalid; moved it to", out)
                self.assertIn("init: wrote profile box=desk member=alex role=operator", out)
                self.assertEqual("alex", profile.load_runtime_profile(self.hub)["member_name"])
                self.assertEqual(broken, json.loads(self.path.with_name(self.path.name + ".bad").read_text(encoding="utf-8")))
                self.assertIn("init: profile exists", again[1])  # a rerun is safe and changes nothing

    def test_init_leaves_a_valid_profile_alone_with_no_bad_copy(self):
        profile.save_profile(self.path, self.profile)
        before = self.path.read_bytes()
        self.assertEqual(0, self.run_seat(["init"])[0])
        self.assertEqual(before, self.path.read_bytes())
        self.assertFalse(self.path.with_name(self.path.name + ".bad").exists())

    def test_init_takes_no_box_or_role_option(self):
        for argv in (["init", "--box", "air"], ["init", "--role", "operator"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit):
                seat.main(argv)


class BindGitTests(SeatFixture):
    def test_bind_git_runs_install_on_the_hub_and_prints_the_binding(self):
        calls, envs = [], []

        def run_command(args, *, cwd, env=None, capture=False, timeout=None):
            calls.append([str(item) for item in args])
            envs.append(env)
            return subprocess.CompletedProcess(args, 0, json.dumps({"result": "PASS", "branch": "main"}) + "\n", "")
        with self.sterile_env(GCM_INTERACTIVE="Never", GH_PROMPT_DISABLED="1"):
            status, out, err = self.run_seat(["bind-git"], run_command=run_command)
            # This session's own environment, as the probe found it, is the expected base.
            expected_env = {key: value for key, value in os.environ.items()
                            if key not in ("GCM_INTERACTIVE", "GH_PROMPT_DISABLED")}
        self.assertEqual(0, status, err)
        self.assertEqual("bind-git: ok branch=main\n", out)
        # The origin check runs first, then the sign-in probe with the credential manager allowed to open
        # its window, then the dry-run push that proves the push right.
        self.assertEqual(["git", "-C", str(self.hub), "remote", "get-url", "origin"], calls[0])
        self.assertEqual(["git", "-C", str(self.hub), "ls-remote", "--exit-code", "origin", "refs/heads/main"],
                         calls[1])
        self.assertEqual({**expected_env, "GIT_TERMINAL_PROMPT": "0"}, envs[1])
        self.assertEqual(["git", "-C", str(self.hub), "-c", f"core.hooksPath={os.devnull}", "push", "--dry-run",
                          "origin", "HEAD:refs/heads/main"], calls[2])
        self.assertEqual({**expected_env, "GIT_TERMINAL_PROMPT": "0"}, envs[2])
        argv = calls[3]
        # The policy needs only the repo: one human, one main branch, no box options.
        self.assertEqual([str(seat.interpreter(self.hub)), "-B",
                          str(self.hub / "Drawer" / "git-branch-policy.py"), "install", "--repo", str(self.hub)],
                         argv)
        self.assertEqual(4, len(calls))  # no product bound: no product hooks call

    # A bound product clone gets core.hooksPath = <main hub>/Drawer/product-hooks (PRODUCT-CONVENTIONS.md PRD-2,
    # Product hooks), so its first push runs the product push gate; a failed git config exits one.
    def test_bind_git_binds_the_product_clone_to_the_hub_product_hooks(self):
        product = self.home / "product"
        product.mkdir()
        (self.hub / "harness.toml").write_text(
            f'[project]\nstate_root = {json.dumps(str(self.state))}\n\n[product]\npath = {json.dumps(str(product))}\n',
            encoding="utf-8")
        hooks = self.hub / "Drawer" / "product-hooks"
        for config_rc, want in ((0, 0), (1, 1)):
            with self.subTest(config_rc=config_rc):
                calls = []

                def run_command(args, *, cwd, env=None, capture=False, timeout=None):
                    calls.append([str(item) for item in args])
                    rc = config_rc if "core.hooksPath" in calls[-1] else 0
                    return subprocess.CompletedProcess(args, rc, json.dumps(
                        {"result": "PASS", "branch": "main"}) + "\n", "error: no repo\n" if rc else "")
                with self.sterile_env():
                    status, out, err = self.run_seat(["bind-git"], run_command=run_command)
                self.assertEqual(want, status, err)
                self.assertEqual(["git", "-C", str(product.resolve()), "config", "core.hooksPath", str(hooks)], calls[4])
                if want:
                    self.assertEqual("bind-git: product hooks failed (exit 1): error: no repo\n", err)
                else:
                    self.assertTrue(out.endswith(f"bind-git: product hooks {hooks}\n"), out)

    def test_a_refused_binding_exits_one_with_the_policy_line(self):
        def run_command(args, *, cwd, env=None, capture=False, timeout=None):
            if args[0] == "git":
                return subprocess.CompletedProcess(args, 0, "", "")
            return subprocess.CompletedProcess(
                args, 1, "", "Git policy: the current server-derived identity disagrees\n")
        with self.sterile_env():
            status, out, err = self.run_seat(["bind-git"], run_command=run_command)
        self.assertEqual(1, status)
        self.assertEqual("", out)
        self.assertEqual("bind-git: failed (exit 1): Git policy: the current server-derived "
                         "identity disagrees\n", err)

    def test_a_timed_out_child_exits_one_with_one_line_not_a_traceback(self):
        def run_command(args, *, cwd, env=None, capture=False, timeout=None):
            if args[0] == "git":
                return subprocess.CompletedProcess(args, 0, "", "")
            # A named command, so the one line below carries the timeout the seat asked for.
            raise subprocess.TimeoutExpired("git-branch-policy.py install", timeout)
        with self.sterile_env():
            status, out, err = self.run_seat(["bind-git"], run_command=run_command)
        self.assertEqual(1, status)
        self.assertEqual("", out)
        self.assertEqual("seat: bind-git failed (TimeoutExpired: Command 'git-branch-policy.py install' "
                         "timed out after 300 seconds)\n", err)

    def test_a_missing_github_sign_in_prints_the_one_forwardable_line(self):
        def refused(args):
            return subprocess.CompletedProcess(args, 128, "", "fatal: could not read Username\n")

        def timed_out(args):
            raise subprocess.TimeoutExpired(args, 180)

        def probe(answer, args):
            """Only the sign-in probe answers as the case says; the origin check and the dry run pass."""
            if answer is not fetch_refused and "ls-remote" in args:
                return answer(args)
            return fetch_refused(args)

        def fetch_refused(args):
            if args[0] == "git":
                return subprocess.CompletedProcess(args, 0, "", "")
            return subprocess.CompletedProcess(args, 1, "", "Git policy: accepted main could not "
                                               "be fetched\n")
        for answer, expected_calls in ((refused, 2), (timed_out, 2), (fetch_refused, 4)):
            with self.subTest(answer.__name__):
                calls = []

                def run_command(args, *, cwd, env=None, capture=False, timeout=None):
                    calls.append(args)
                    return probe(answer, args)
                with self.sterile_env():
                    status, out, err = self.run_seat(["bind-git"], run_command=run_command)
                self.assertEqual(1, status)
                self.assertEqual("", out)
                self.assertEqual(
                    "bind-git: GitHub sign-in missing: Git on this computer is not signed in to GitHub "
                    "for this hub. Sign in when the GitHub window opens, then tell the agent to go "
                    "on. If no window opens (a Mac opens none), send this line to the human.\n", err)
                self.assertEqual(expected_calls, len(calls))


class OriginTests(SeatFixture):
    """bind-git names a missing origin and an origin the user cannot push to, apart from a missing sign-in."""

    def bind(self, failing):
        calls = []

        def run_command(args, *, cwd, env=None, capture=False, timeout=None):
            calls.append(" ".join(str(item) for item in args))
            hit = next((word for word in failing if word in calls[-1]), None)
            if hit:
                return subprocess.CompletedProcess(args, 1, "", failing[hit])
            return subprocess.CompletedProcess(args, 0, json.dumps({"result": "PASS", "branch": "main"}) + "\n", "")
        with self.sterile_env():
            status, out, err = self.run_seat(["bind-git"], run_command=run_command)
        return status, out, err, calls

    def test_no_origin_prints_its_own_line_and_runs_nothing_more(self):
        status, out, err, calls = self.bind({"get-url": "error: No such remote 'origin'\n"})
        self.assertEqual((1, "", seat.NO_ORIGIN + "\n"), (status, out, err))
        self.assertEqual(1, len(calls))

    def test_an_origin_the_user_cannot_push_to_prints_its_own_line(self):
        status, out, err, calls = self.bind({"push --dry-run": "remote: Permission denied to someone\n"})
        self.assertEqual((1, "", seat.ORIGIN_NOT_WRITABLE + "\n"), (status, out, err))
        self.assertEqual(3, len(calls))  # the install never ran

    def test_a_head_behind_the_remote_still_proves_the_push_right(self):
        status, out, err, _ = self.bind({"push --dry-run": " ! [rejected] HEAD -> main (non-fast-forward)\n"})
        self.assertEqual((0, "bind-git: ok branch=main\n"), (status, out))

    def empty_origin(self, push_code, push_text=""):
        """A bind-git run against an origin that answers but has no main (ls-remote exit 2); the seed
        push exits push_code. Returns the status, stdout, stderr and every command it ran."""
        calls = []

        def run_command(args, *, cwd, env=None, capture=False, timeout=None):
            calls.append([str(item) for item in args])
            if "ls-remote" in args:
                return subprocess.CompletedProcess(args, 2, "", "")
            if "push" in args and "--dry-run" not in args:
                return subprocess.CompletedProcess(args, push_code, "", push_text)
            return subprocess.CompletedProcess(args, 0, json.dumps({"result": "PASS", "branch": "main"}) + "\n", "")
        with self.sterile_env():
            status, out, err = self.run_seat(["bind-git"], run_command=run_command)
        return status, out, err, calls

    def test_an_origin_with_no_main_is_seeded_through_the_gates_then_bound(self):
        status, out, err, calls = self.empty_origin(0)
        self.assertEqual(0, status, err)
        self.assertEqual("bind-git: origin had no main; pushed main to it\nbind-git: ok branch=main\n", out)
        install = next(i for i, call in enumerate(calls) if "install" in call)
        push = [i for i, call in enumerate(calls) if "push" in call and "--dry-run" not in call]
        self.assertEqual([install + 1], push)  # the probe and the install come first, the push last
        # The seed push is a plain push: no hooks-off flag and no --no-verify, so pre-push and check-push run.
        self.assertEqual(["git", "-C", str(self.hub), "push", "origin", "HEAD:main"], calls[push[0]])

    def test_a_failed_seed_push_prints_the_origin_has_no_main_line_and_exits_one(self):
        status, out, err, calls = self.empty_origin(
            1, "Git policy: the checkout has no verified branch-policy binding; run install --repo first\n"
               "error: failed to push some refs to 'x'\n")
        self.assertEqual((1, ""), (status, out))
        self.assertEqual(1, len(err.splitlines()), err)
        self.assertTrue(err.startswith("bind-git: origin has no main: "), err)
        self.assertIn("Git policy: the checkout has no verified branch-policy binding; run install --repo first", err)
        self.assertIn("then run this step again", err)

    def test_the_seed_line_names_a_cause_and_the_next_step(self):
        self.assertTrue(seat.ORIGIN_NO_MAIN.startswith("bind-git: origin has no main: "))
        self.assertIn("{why}", seat.ORIGIN_NO_MAIN)
        self.assertIn("run this step again", seat.ORIGIN_NO_MAIN)

    def test_the_no_origin_line_leaves_the_push_of_main_to_bind_git(self):
        self.assertIn("git remote add origin <url>, then run this step again.", seat.NO_ORIGIN)
        self.assertNotIn("push main", seat.NO_ORIGIN)
        self.assertIn("A new hub: Drawer/new-hub.py --origin <url>.", seat.NO_ORIGIN)

    def test_bind_git_seeds_an_empty_origin_through_the_real_pre_push_gate(self):
        """A booted, unbound clone (core.hooksPath on the tracked hooks) and an empty bare origin:
        the seed push meets pre-push and check-push, and bind-git ends bound with main on origin."""
        env = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "GIT_TERMINAL_PROMPT": "0",
               "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}

        def git(*args, cwd=self.hub):
            return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()
        origin = self.home / "empty.git"
        with self.sterile_env(**env):
            git("init", "-q", "--bare", "-b", "main", str(origin), cwd=self.home)
            git("init", "-q", "-b", "main")
            drawer = DRAWER
            for name in ("git-branch-policy.py", "harness_config.py", "gatelib.py", "oscompat.py"):
                shutil.copy2(drawer / name, self.hub / "Drawer" / name)
            shutil.copytree(drawer / "git-hooks", self.hub / "Drawer" / "git-hooks")
            marker = harness_config.load(DRAWER.parent)["project"]["marker_file"]
            (self.hub / marker).write_text("", encoding="utf-8")
            # bind = true: a refusing gate blocks the push, as on a bound hub.
            (self.hub / "harness.toml").write_text(
                f'[project]\nstate_root = "{self.state}"\nmarker_file = "{marker}"\n\n[gates]\nbind = true\n',
                encoding="utf-8")
            venv = self.hub / ".venv" / "bin"
            venv.mkdir(parents=True)
            (venv / "python").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
            (venv / "python").chmod(0o755)
            (self.hub / ".gitignore").write_text(".venv/\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-q", "--no-verify", "-m", "hub")
            git("remote", "add", "origin", str(origin))
            git("config", "core.hooksPath", "Drawer/git-hooks")  # what the first session boot sets
            self.assertNotEqual(0, subprocess.run(["git", "config", "--get", "hub.bound"], cwd=self.hub).returncode)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                status = seat.main(["bind-git"])
            self.assertEqual(0, status, err.getvalue())
            self.assertIn("bind-git: origin had no main; pushed main to it\nbind-git: ok branch=main\n", out.getvalue())
            self.assertEqual(git("rev-parse", "HEAD"), git("--git-dir", str(origin), "rev-parse", "main"))
            self.assertEqual("true", git("config", "--get", "hub.bound"))

    def test_bind_git_names_a_missing_main_branch_not_the_sign_in(self):
        """An empty origin and a clone on a detached HEAD with no local main: install cannot make main, and the
        line says so and names the repair; the sign-in line would send the person the wrong way."""
        env = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "GIT_TERMINAL_PROMPT": "0",
               "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}

        def git(*args, cwd=self.hub):
            return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()
        origin = self.home / "empty.git"
        with self.sterile_env(**env):
            git("init", "-q", "--bare", "-b", "main", str(origin), cwd=self.home)
            git("init", "-q", "-b", "main")
            for name in ("git-branch-policy.py", "harness_config.py", "gatelib.py", "oscompat.py"):
                shutil.copy2(DRAWER / name, self.hub / "Drawer" / name)
            shutil.copytree(DRAWER / "git-hooks", self.hub / "Drawer" / "git-hooks")
            marker = harness_config.load(DRAWER.parent)["project"]["marker_file"]
            (self.hub / marker).write_text("", encoding="utf-8")
            (self.hub / "harness.toml").write_text(
                f'[project]\nstate_root = "{self.state}"\nmarker_file = "{marker}"\n', encoding="utf-8")
            venv = self.hub / ".venv" / "bin"
            venv.mkdir(parents=True)
            (venv / "python").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
            (venv / "python").chmod(0o755)
            (self.hub / ".gitignore").write_text(".venv/\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-q", "--no-verify", "-m", "hub")
            git("remote", "add", "origin", str(origin))
            git("checkout", "-q", "--detach")
            git("branch", "-q", "-D", "main")
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                status = seat.main(["bind-git"])
        self.assertEqual((1, ""), (status, out.getvalue()))
        self.assertEqual(seat.NO_MAIN_BRANCH + "\n", err.getvalue())
        self.assertIn("git switch -c main", seat.NO_MAIN_BRANCH)
        self.assertNotIn("sign-in", seat.NO_MAIN_BRANCH)

    def test_the_probe_answers_true_through_a_pre_push_hook_that_refuses(self):
        """A booted checkout has core.hooksPath set; its pre-push hook must not decide the probe."""
        root = Path(tempfile.mkdtemp(prefix="origin probe ", dir=self.home))
        hooks = root / "hooks"
        hooks.mkdir()
        hook = hooks / "pre-push"
        hook.write_text("#!/bin/sh\necho 'Git policy: no verified binding' >&2\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)

        def git(*args, cwd=root):
            subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)
        git("init", "-q", "--bare", "-b", "main", "origin.git")
        git("init", "-q", "-b", "main", "clone")
        clone = root / "clone"
        git("config", "user.email", "t@example.com", cwd=clone)
        git("config", "user.name", "t", cwd=clone)
        git("commit", "-q", "--allow-empty", "-m", "one", cwd=clone)
        git("remote", "add", "origin", str(root / "origin.git"), cwd=clone)
        git("config", "core.hooksPath", str(hooks), cwd=clone)
        with self.sterile_env():
            self.assertTrue(seat._origin_writable(clone, seat.run_command))

    def test_the_lines_name_a_remedy_and_differ_from_the_sign_in_line(self):
        lines = {seat.NO_ORIGIN, seat.ORIGIN_NOT_WRITABLE, seat.GITHUB_SIGN_IN_MISSING, seat.ORIGIN_NO_MAIN,
                 seat.NO_MAIN_BRANCH}
        self.assertEqual(5, len(lines))
        self.assertIn("new-hub.py --origin", seat.NO_ORIGIN)
        self.assertIn("Use this template", seat.ORIGIN_NOT_WRITABLE)


class CheckTests(SeatFixture):
    def setUp(self):
        super().setUp()
        self.records = [record("memory", "PASS", "the memory server answered"),
                        record("smoke", "SKIPPED", "no smoke command is configured"),
                        record("probe", "FAIL", "the probe never answered")]
        self.summary = {"PASS": 1, "SKIPPED": 1, "FAIL": 1, "BLOCKED": 0}
        self.notes = ["closed 1 child"]

    def mocked(self, records=None, summary=None, side_effect=None):
        self.closed = []

        def close_all(hub):
            self.closed.append(hub)
            return list(self.notes)
        return (patch.object(seat.components, "check_all",
                             side_effect=side_effect,
                             **({} if side_effect else {"return_value": records or self.records})),
                patch.object(seat.components, "summary", return_value=summary or self.summary),
                patch.object(seat.children, "close_all", close_all))

    def test_two_consecutive_runs_print_the_same_json(self):
        # components.summary is not patched here: the counts are its real work on these records.
        check_all, _summary, close_all = self.mocked()
        with check_all, close_all:
            first = self.run_seat(["check", "--json"])
            second = self.run_seat(["check", "--json"])
        self.assertEqual((0, 0), (first[0], second[0]))
        # The whole rendered text, key order included: one indent level, keys sorted.
        expected = ('{\n'
                    ' "records": [\n'
                    '  {\n'
                    '   "detail": null,\n'
                    '   "name": "memory",\n'
                    '   "reason": "the memory server answered",\n'
                    '   "status": "PASS"\n'
                    '  },\n'
                    '  {\n'
                    '   "detail": null,\n'
                    '   "name": "smoke",\n'
                    '   "reason": "no smoke command is configured",\n'
                    '   "status": "SKIPPED"\n'
                    '  },\n'
                    '  {\n'
                    '   "detail": null,\n'
                    '   "name": "probe",\n'
                    '   "reason": "the probe never answered",\n'
                    '   "status": "FAIL"\n'
                    '  }\n'
                    ' ],\n'
                    ' "summary": {\n'
                    '  "BLOCKED": 0,\n'
                    '  "FAIL": 1,\n'
                    '  "PASS": 1,\n'
                    '  "SKIPPED": 1\n'
                    ' }\n'
                    '}\n')
        self.assertEqual(expected, first[1])
        self.assertEqual(expected, second[1])  # the second run of the same seat reads the same
        self.assertEqual([self.hub, self.hub], self.closed)

    def test_only_passes_one_name_through(self):
        seen = []

        def record_check(hub, machine, *, only):
            seen.append((hub, tuple(machine), only))
            return [self.records[0]]
        check_all, summary, close_all = self.mocked(summary={"PASS": 1}, side_effect=record_check)
        with check_all, summary, close_all:
            status, _out, _err = self.run_seat(["check", "--only", "smoke"])
        self.assertEqual(0, status)
        self.assertEqual([(self.hub, ("posix", "claude"), ["smoke"])], seen)

    def test_only_a_family_name_passes_every_row_of_the_family_through(self):
        """--only probe must check every probe:<part> row, not one of them and not none."""
        items = [SimpleNamespace(name="memory"), SimpleNamespace(name="probe:one"),
                 SimpleNamespace(name="probe:two"), SimpleNamespace(name="probe:three")]
        seen = []

        def check_all(hub, machine, *, only):
            seen.append(list(only))
            return [self.records[0]]
        with patch.object(seat.components, "inventory", return_value=items), \
             patch.object(seat.components, "check_all", check_all), \
             patch.object(seat.children, "close_all", lambda hub: []):
            status, out, _err = self.run_seat(["check", "--only", "probe"])
        self.assertEqual(0, status)
        self.assertEqual([["probe:one", "probe:two", "probe:three"]], seen)
        self.assertEqual("PASS memory: the memory server answered\n", out)

    def test_only_a_name_no_component_carries_refuses_instead_of_checking_nothing(self):
        """An unmatched --only name must exit 1, not read as a passing re-check of nothing."""
        items = [SimpleNamespace(name="memory"), SimpleNamespace(name="probe:one")]
        with patch.object(seat.components, "inventory", return_value=items), \
             patch.object(seat.components, "check_all", _unavailable), \
             patch.object(seat.children, "close_all", _unavailable):
            status, out, err = self.run_seat(["check", "--only", "one"])
        self.assertEqual(1, status)
        self.assertEqual("", out)
        self.assertEqual("check: --only names no component; choose from memory, probe:one\n", err)

    def test_the_children_close_even_when_the_check_crashes(self):
        check_all, summary, close_all = self.mocked(side_effect=RuntimeError("the check exploded"))
        with check_all, summary, close_all, self.assertRaisesRegex(RuntimeError, "the check exploded"):
            self.run_seat(["check"])
        self.assertEqual([self.hub], self.closed)


class RepairTests(SeatFixture):
    def mocked(self, status):
        return (patch.object(seat.components, "check_all",
                             return_value=[record("smoke", status, "the smoke command failed")]),
                patch.object(seat.children, "close_all", return_value=[]))

    def test_a_passing_component_is_left_alone(self):
        check_all, close_all = self.mocked("PASS")
        with check_all, close_all, patch.object(seat.components, "repair", _unavailable):
            status, out, _err = self.run_seat(["repair", "smoke"])
        self.assertEqual(0, status)
        self.assertEqual("repair: smoke needs no repair\n", out)

    def test_a_failed_component_is_repaired_and_its_new_record_printed(self):
        check_all, close_all = self.mocked("FAIL")
        repairs = []

        def repair(hub, name, failed):
            repairs.append((hub, name, failed))
            return record("smoke", "PASS", "the smoke command passed")
        with check_all, close_all, patch.object(seat.components, "repair", repair):
            status, out, _err = self.run_seat(["repair", "smoke"])
        self.assertEqual(0, status)
        self.assertEqual("PASS smoke: the smoke command passed\n", out)
        # The failed record itself reaches the repair, not a fresh one.
        self.assertEqual([(self.hub, "smoke", record("smoke", "FAIL", "the smoke command failed"))],
                         repairs)

    def test_a_refused_repeat_exits_three(self):
        check_all, close_all = self.mocked("FAIL")
        with check_all, close_all, patch.object(seat.components, "repair",
                                                side_effect=seat.components.RepeatRefused("same failure")):
            status, out, _err = self.run_seat(["repair", "smoke"])
        self.assertEqual(3, status)
        # The refusal carries how to clear it, so the whole line is the contract.
        self.assertEqual("repair: smoke repeat refused (same failure after a repair). same failure\n", out)

    def test_clear_drops_the_refusal_without_checking_or_repairing(self):
        cleared = []

        def clear_refusals(hub, name):
            cleared.append((hub, name))
            return 2
        with patch.object(seat.components, "check_all", _unavailable), \
             patch.object(seat.components, "repair", _unavailable), \
             patch.object(seat.components, "clear_refusals", clear_refusals):
            status, out, _err = self.run_seat(["repair", "memory", "--clear"])
        self.assertEqual(0, status)
        self.assertEqual("repair: memory refusal cleared (2 entries)\n", out)
        self.assertEqual([(self.hub, "memory")], cleared)

    def test_an_unknown_component_is_reported(self):
        check_all, close_all = self.mocked("FAIL")
        with check_all, close_all:
            status, out, _err = self.run_seat(["repair", "nothing"])
        self.assertEqual(1, status)
        self.assertEqual("repair: nothing is not a checked component\n", out)

    def test_a_name_outside_the_pipe_encoding_prints_instead_of_raising(self):
        raw = io.BytesIO()
        pipe = io.TextIOWrapper(raw, encoding="cp1252", newline="\n")
        check_all, close_all = self.mocked("FAIL")
        with check_all, close_all, contextlib.redirect_stdout(pipe):
            status = seat.main(["repair", "名前"])
            pipe.flush()
        self.assertEqual(1, status)
        self.assertEqual(b'repair: \\u540d\\u524d is not a checked component\n', raw.getvalue())


class PathTests(unittest.TestCase):
    """A Dock-launched app keeps /usr/bin:/bin, so configure's spawns could not find uv or claude."""

    def test_a_mac_session_path_gains_only_the_tool_folders_that_exist(self):
        home_bin = "/srv/seat/.local/bin"
        present = {"/opt/homebrew/bin", home_bin}  # /usr/local/bin is the absent decoy
        with patch.object(os.path, "expanduser", return_value=home_bin), \
             patch.object(os.path, "isdir", lambda folder: folder in present), \
             mock.patch.dict(os.environ, {"PATH": f"/usr/bin{os.pathsep}/opt/homebrew/bin"}):
            seat._extend_path()
            # The folder already there keeps its place and is never repeated.
            self.assertEqual(f"/usr/bin{os.pathsep}/opt/homebrew/bin{os.pathsep}{home_bin}",
                             os.environ["PATH"])


if __name__ == "__main__":
    unittest.main()
