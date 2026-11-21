"""Smoke-owned children: the record, the temp-root fence, close, and verified reclaim."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
import unittest.mock
from unittest.mock import patch

DRAWER = Path(__file__).resolve().parents[1]
if str(DRAWER) not in sys.path:
    sys.path.insert(0, str(DRAWER))
from runtime import children, profile  # noqa: E402

SLEEPER = "import time; time.sleep(60)"
# The prepared interpreter's python.exe is a launcher: it runs the real interpreter as its
# own child, which survives a terminate of the pid spawn recorded and keeps the scratch log
# open. Every child here is the base interpreter, so one spawn is one process on any seat.
PYTHON = getattr(sys, "_base_executable", None) or sys.executable
# The contract this suite pins, written out instead of read back from children.py.
SMOKE_PREFIX = "hub-smoke-"
RECORD_NAME = "smoke-children.json"
LOCK_NAME = "smoke-children.lock"
# The creation identity a live process carries here (children.process_identity).
IDENTITY_KIND = "proc-start"
# A terminated child's exit code: SIGTERM on POSIX, taskkill /F on Windows.
TERMINATED_EXIT = 1 if os.name == "nt" else -15
# A living child holds its log open: POSIX removes the scratch anyway, Windows cannot.
HELD = "; scratch still in use" if os.name == "nt" else "; scratch removed"
# No seat variable reaches a child of this suite.
CHILD_ENV = {}


class ChildrenFixture(unittest.TestCase):
    """A temp home, so the record and its lock land beside a temp runtime profile."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="runtime children ")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.hub = self.home / "a hub" / "Hub"
        self.hub.mkdir(parents=True)
        home = patch.object(Path, "home", staticmethod(lambda: self.home))
        home.start()
        self.addCleanup(home.stop)
        self.addCleanup(self.disown)

    def scratch(self, prefix=SMOKE_PREFIX):
        root = Path(tempfile.mkdtemp(prefix=prefix))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        return root

    def sleeper(self, name="sleeper", temp_root=None):
        root = temp_root if temp_root is not None else self.scratch()
        process = children.spawn(self.hub, name, [PYTHON, "-c", SLEEPER],
                                 self.hub, CHILD_ENV, root)
        self.addCleanup(self.stop, process)
        return process, root

    def stop(self, process):
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)

    def records(self):
        return json.loads(children.record_path(self.hub).read_text(encoding="utf-8"))

    def rewrite_identity(self, identity):
        entries = self.records()
        for entry in entries:
            entry["process_identity"] = identity
        children._write(self.hub, entries)

    @staticmethod
    def stale(**values):
        """A previous run's record entry; ``values`` override its fields."""
        entry = {"name": "orphan", "pid": 11, "process_identity": None,
                 "started_at": "2026-01-01T00:00:00+00:00", "temp_root": None}
        entry.update(values)
        return entry

    def disown(self):
        """Forget the handles, so the next call sees only a previous run's record."""
        children._OWNED.clear()

    def held_scratch(self):
        """A live child holds its log: stop waiting for a removal that cannot happen."""
        budget = patch.object(children, "REMOVE_SECONDS", 0.0)
        budget.start()
        self.addCleanup(budget.stop)


class RecordTests(ChildrenFixture):
    def test_the_record_lands_beside_the_profile_with_a_real_creation_identity(self):
        process, root = self.sleeper()
        record = children.record_path(self.hub)
        self.assertEqual(RECORD_NAME, record.name)
        self.assertEqual(LOCK_NAME, children.lock_path(self.hub).name)
        self.assertEqual(profile.default_profile_path(self.hub).parent, record.parent)
        self.assertEqual(record.parent, children.lock_path(self.hub).parent)
        self.assertIn(self.home, record.parents)
        entry, = self.records()
        self.assertEqual(["name", "pid", "process_identity", "started_at", "temp_root"],
                         sorted(entry))
        self.assertEqual("sleeper", entry["name"])
        self.assertEqual(process.pid, entry["pid"])
        self.assertEqual(str(root.resolve()), entry["temp_root"])
        kind, _colon, stamp = str(entry["process_identity"]).partition(":")
        self.assertEqual(IDENTITY_KIND, kind, entry["process_identity"])
        self.assertTrue(stamp.isdigit(), entry["process_identity"])

    def test_this_process_has_a_readable_identity(self):
        identity = children.process_identity(os.getpid())
        self.assertEqual(IDENTITY_KIND, str(identity).partition(":")[0], identity)
        # One process keeps one identity: a value that moves would never verify a leftover.
        self.assertEqual(identity, children.process_identity(os.getpid()))

    def test_a_reaped_process_and_a_non_pid_have_no_identity(self):
        gone = subprocess.Popen([PYTHON, "-c", "pass"], env=CHILD_ENV)
        gone.wait(20)
        self.assertIsNone(children.process_identity(gone.pid))
        self.assertIsNone(children.process_identity(0))

    def test_the_child_writes_both_streams_to_one_log_in_its_scratch(self):
        root = self.scratch()
        talker = [PYTHON, "-c",
                  "import sys; print('out'); sys.stdout.flush(); print('err', file=sys.stderr)"]
        # A spawn that raises, or a log that lands elsewhere, is this test's answer too:
        # both are recorded as values, so the defect FAILs here instead of ERRORing.
        seen = {}
        try:
            process = children.spawn(self.hub, "talker", talker, self.hub, CHILD_ENV, root)
            self.addCleanup(self.stop, process)
            seen["exit"] = process.wait(timeout=30)
            log = children.log_path(root, "talker")
            seen["log"] = str(log)
            seen["lines"] = log.read_text(encoding="utf-8").splitlines() if log.is_file() else []
        except Exception as exc:
            seen["error"] = repr(exc)
        self.assertEqual({"exit": 0, "log": str(root / "talker.log"),
                          "lines": ["out", "err"]}, seen)

    def test_a_damaged_record_reads_as_the_entries_it_can_trust(self):
        path = children.record_path(self.hub)
        path.parent.mkdir(parents=True, exist_ok=True)
        damaged = [
            ('[{"name": "half"', []),
            ('{"name": "one entry, not a list"}', []),
            ('"a string"', []),
            ('[1, null, "text", {"name": "kept", "pid": 7}]', [{"name": "kept", "pid": 7}]),
        ]
        for text, expected in damaged:
            with self.subTest(record=text):
                path.write_text(text, encoding="utf-8")
                self.assertEqual(expected, children._read(self.hub))
        path.unlink()
        self.assertEqual([], children._read(self.hub))


class TempRootFenceTests(ChildrenFixture):
    def test_spawn_refuses_a_temp_root_outside_the_smoke_prefix(self):
        refused = [
            self.hub,
            Path(tempfile.gettempdir()),
            self.scratch(prefix="unrelated-"),
            Path(self.hub.anchor) / (SMOKE_PREFIX + "outside-the-temp-root"),
            None,
            "",
        ]
        for root in refused:
            with self.subTest(root=root):
                with self.assertRaisesRegex(
                        ValueError,
                        r"a smoke temp root must be a hub-smoke-\* directory under "):
                    children.spawn(self.hub, "refused", [PYTHON, "-c", "pass"],
                                   self.hub, CHILD_ENV, root)
        self.assertEqual([], children._read(self.hub))
        # The boundary the fence accepts: a smoke directory under the system temp root.
        accepted = self.scratch()
        process = children.spawn(self.hub, "accepted", [PYTHON, "-c", "pass"],
                                 self.hub, CHILD_ENV, accepted)
        self.addCleanup(self.stop, process)
        self.assertEqual(["accepted"], [entry["name"] for entry in self.records()])


class CloseTests(ChildrenFixture):
    def test_close_all_stops_the_child_removes_the_scratch_and_clears_the_record(self):
        process, root = self.sleeper()
        (root / "artifact.txt").write_text("scratch", encoding="utf-8")
        # A previous run's child shares the record: close_all drops its own rows, never the file.
        foreign = self.stale(name="another run")
        children._write(self.hub, [foreign, *self.records()])
        notes = children.close_all(self.hub)
        self.assertEqual(["sleeper: closed pid %d (exit %d); scratch removed"
                          % (process.pid, TERMINATED_EXIT)], notes)
        self.assertEqual(TERMINATED_EXIT, process.poll())
        self.assertFalse(root.exists())
        self.assertEqual([foreign], self.records())

    def test_close_all_without_owned_children_is_a_no_op(self):
        self.assertEqual([], children.close_all(self.hub))
        self.assertFalse(children.record_path(self.hub).exists())


class PosixCompareTests(unittest.TestCase):
    """An identity the OS hides never authorizes a kill; only a proven absence is a mismatch."""

    def test_hidden_identity_is_uncertain_and_a_missing_process_is_a_mismatch(self):
        # The pid probe is gatelib.pid_state on every seat: "live" is a process that runs or
        # runs as another user (POSIX PermissionError), "gone" is no such process.
        with patch.object(children, "process_identity", return_value=None):
            with patch.object(children.gatelib, "pid_state", return_value="live"):
                self.assertEqual("uncertain", children._posix_compare(4242, "proc-start:1"))
            with patch.object(children.gatelib, "pid_state", return_value="gone"):
                self.assertEqual("mismatch", children._posix_compare(4242, "proc-start:1"))
            with patch.object(children.gatelib, "pid_state", return_value="unsure"):
                self.assertEqual("uncertain", children._posix_compare(4242, "proc-start:1"))
        with patch.object(children, "process_identity", return_value="proc-start:2"):
            self.assertEqual("mismatch", children._posix_compare(4242, "proc-start:1"))
        # A strong identity that now reads pid-only proves nothing either way.
        with patch.object(children, "process_identity", return_value="pid-only:4242"):
            self.assertEqual("uncertain", children._posix_compare(4242, "proc-start:1"))
        with patch.object(children, "process_identity", return_value="proc-start:1"):
            self.assertEqual("same", children._posix_compare(4242, "proc-start:1"))


class PosixTerminateTests(unittest.TestCase):
    """The POSIX route, driven on every seat: children.os and the oscompat group signals are patched."""

    def route(self, identity, killpg):
        """The POSIX branch of _terminate_verified over a fake OS, with the signals it sent."""
        sent = []

        def sender(number):
            def send(pid):
                sent.append((pid, number))
                killpg()
            return send

        fake_os = types.SimpleNamespace(
            name="posix", kill=self.gone,
            waitpid=lambda pid, flags: (0, 0), WNOHANG=1)
        with patch.object(children, "os", fake_os), \
             patch.object(children.oscompat, "WINDOWS", False), \
             patch.object(children.oscompat, "term_tree", sender(15)), \
             patch.object(children.oscompat, "kill_tree", sender(9)), \
             patch.object(children, "process_identity", identity):
            return children._terminate_verified(4242, "proc-start:1"), sent

    @staticmethod
    def gone(pid, number):
        """The fake OS answers "no such process" for the identity-hidden compare."""
        raise ProcessLookupError

    @staticmethod
    def changing(*values):
        """An identity that reads each value in turn, then keeps answering the last."""
        reads = []

        def identity(pid):
            reads.append(pid)
            return values[min(len(reads) - 1, len(values) - 1)]

        return identity

    def test_the_posix_route_signals_the_group_and_waits_for_the_process_to_go(self):
        reads = []

        def identity(pid):
            reads.append(pid)
            # The recorded child is still there for the first two reads after the signal.
            return "proc-start:1" if len(reads) <= 3 else None

        outcome, sent = self.route(identity, lambda: None)
        self.assertEqual("terminated", outcome)
        self.assertEqual([(4242, 15)], sent)
        self.assertEqual([4242, 4242, 4242, 4242], reads)

    def test_the_posix_route_escalates_or_stands_down_when_sigterm_did_not_finish(self):
        cases = {
            "the group ignores SIGTERM": (
                self.changing("proc-start:1"), lambda: None,
                "terminated", [(4242, 15), (4242, 9)]),
            "the pid was recycled while we waited": (
                self.changing("proc-start:1", "proc-start:2"), lambda: None,
                "mismatch", [(4242, 15)]),
            "the group is not ours to signal": (
                self.changing("proc-start:1"), self.refuse, "uncertain", [(4242, 15)]),
        }
        # No wait: the verdict must not depend on how fast this seat runs.
        with patch.object(children, "STOP_SECONDS", 0.0):
            seen = {name: self.route(identity, killpg)
                    for name, (identity, killpg, _expected, _signals) in cases.items()}
        self.assertEqual({name: (expected, signals)
                          for name, (_identity, _killpg, expected, signals) in cases.items()}, seen)

    @staticmethod
    def refuse():
        raise PermissionError(1, "not your process group")


class WindowsRouteTests(unittest.TestCase):
    """The Windows branches, driven on every seat with oscompat.WINDOWS patched."""

    def test_identity_is_the_creation_stamp_and_a_hidden_one_falls_back_to_pid_only(self):
        with patch.object(children.oscompat, "WINDOWS", True):
            with patch.object(children.oscompat, "process_started", return_value=1234567), \
                 patch.object(children.gatelib, "pid_state", return_value="live"):
                self.assertEqual("proc-start:1234567", children.process_identity(4242))
            # An exited process whose handle is still open keeps its stamp but has no identity.
            with patch.object(children.oscompat, "process_started", return_value=1234567), \
                 patch.object(children.gatelib, "pid_state", return_value="gone"):
                self.assertIsNone(children.process_identity(4242))
            with patch.object(children.oscompat, "process_started", return_value=None), \
                 patch.object(children.gatelib, "pid_state", return_value="live"):
                self.assertEqual("pid-only:4242", children.process_identity(4242))

    def test_reap_does_nothing_on_windows(self):
        with patch.object(children.oscompat, "WINDOWS", True), \
             patch.object(children.os, "waitpid", side_effect=AssertionError("waitpid on Windows"), create=True):
            children._reap(4242)

    def test_terminate_stops_the_tree_and_reports_terminated_once_the_stamp_changes(self):
        stopped = []
        reads = iter(["proc-start:1", "proc-start:1", "proc-start:2"])
        with patch.object(children.oscompat, "WINDOWS", True), \
             patch.object(children.oscompat, "term_tree", side_effect=stopped.append), \
             patch.object(children, "process_identity", side_effect=lambda pid: next(reads)):
            self.assertEqual("terminated", children._terminate_verified(4242, "proc-start:1"))
        self.assertEqual([4242], stopped)

    def test_stop_owned_kills_the_whole_tree_on_windows_and_falls_back_to_the_child(self):
        process = unittest.mock.MagicMock(pid=4242)
        with patch.object(children.oscompat, "WINDOWS", True), \
             patch.object(children.oscompat, "kill_tree") as tree:
            children._signal_owned(process, force=False)
        tree.assert_called_once_with(4242)
        process.terminate.assert_not_called()
        with patch.object(children.oscompat, "WINDOWS", True), \
             patch.object(children.oscompat, "kill_tree", side_effect=OSError("taskkill")):
            children._signal_owned(process, force=True)
        process.kill.assert_called_once_with()


class ReclaimTests(ChildrenFixture):
    def test_a_verified_leftover_is_terminated_and_its_scratch_removed(self):
        process, root = self.sleeper(name="leftover")
        self.disown()
        notes = children.reclaim(self.hub)
        self.assertEqual(["leftover: terminated pid %d; scratch removed" % process.pid], notes)
        self.assertIsNotNone(process.poll(), "terminated must mean the child is already gone")
        self.assertFalse(root.exists())
        self.assertEqual([], self.records())

    def test_a_forged_creation_identity_never_terminates(self):
        process, root = self.sleeper(name="forged")
        self.rewrite_identity(IDENTITY_KIND + ":1")
        self.disown()
        self.held_scratch()
        notes = children.reclaim(self.hub)
        # The scratch goes on every branch, but this child is alive and holds its own log,
        # so the removal reports the truth instead of claiming it.
        self.assertEqual(["forged: pid reused or exited, untouched" + HELD], notes)
        self.assertIsNone(process.poll(), "a forged identity must not authorize a kill")
        self.assertEqual([], self.records())

    def test_a_pid_only_identity_reports_unverified_and_never_terminates(self):
        process, root = self.sleeper(name="weak")
        self.rewrite_identity("pid-only:%d" % process.pid)
        self.disown()
        self.held_scratch()
        notes = children.reclaim(self.hub)
        self.assertEqual(["weak: unverified: no creation identity" + HELD], notes)
        self.assertIsNone(process.poll(), "a bare PID must not authorize a kill")
        self.assertEqual([], self.records())

    def test_a_record_without_a_usable_pid_and_identity_is_never_terminated(self):
        attempts = []
        cases = {
            "no identity at all": {"process_identity": None},
            "a bare pid": {"process_identity": "pid-only:11"},
            "an identity without its stamp": {"process_identity": "proc-start"},
            "an identity that is not text": {"process_identity": 11},
            "a flag for a pid": {"pid": True},
            "a pid written as text": {"pid": "11"},
            "pid zero": {"pid": 0},
            "a negative pid": {"pid": -11},
        }
        for name, override in cases.items():
            with self.subTest(case=name):
                entry = self.stale(**{"process_identity": IDENTITY_KIND + ":1", **override})
                children._write(self.hub, [entry])
                with patch.object(children, "_terminate_verified",
                                  side_effect=lambda *call: attempts.append(call)):
                    notes = children.reclaim(self.hub)
                self.assertEqual(["orphan: unverified: no creation identity"], notes)
                self.assertEqual([], self.records())
        self.assertEqual([], attempts)

    def test_a_dropped_record_takes_its_scratch_when_nothing_holds_it(self):
        root = self.scratch()
        (root / "leftover.log").write_text("old run", encoding="utf-8")
        children._write(self.hub, [self.stale(temp_root=str(root))])
        self.assertEqual(["orphan: unverified: no creation identity; scratch removed"],
                         children.reclaim(self.hub))
        self.assertFalse(root.exists())
        self.assertEqual([], self.records())

    def test_a_scratch_root_outside_the_smoke_prefix_is_left_alone(self):
        outside = self.scratch(prefix="unrelated-")
        (outside / "keep.txt").write_text("another tool's directory", encoding="utf-8")
        children._write(self.hub, [self.stale(name="stray", pid=-1, temp_root=str(outside))])
        self.assertEqual(["stray: unverified: no creation identity"
                          "; scratch left in place (not a smoke temp root)"],
                         children.reclaim(self.hub))
        self.assertEqual("another tool's directory",
                         (outside / "keep.txt").read_text(encoding="utf-8"))

    def test_an_interrupted_reclaim_keeps_the_entry_it_never_tried(self):
        entries = [self.stale(name=name) for name in ("first", "second", "third")]
        children._write(self.hub, entries)

        def reclaim_one(entry):
            if entry.get("name") == "second":
                raise OSError("the reclaim host vanished")
            return "%s: unverified" % entry.get("name"), False

        with patch.object(children, "_reclaim_one", side_effect=reclaim_one), \
             self.assertRaisesRegex(OSError, "the reclaim host vanished"):
            children.reclaim(self.hub)
        # The first was reclaimed and written out; the one it died on and the untried one stay.
        self.assertEqual(["second", "third"], [entry["name"] for entry in self.records()])

    def test_an_uncertain_outcome_keeps_the_record_and_its_scratch(self):
        _process, root = self.sleeper(name="doubtful")
        self.disown()
        with patch.object(children, "_terminate_verified", return_value="uncertain"):
            notes = children.reclaim(self.hub)
        self.assertEqual(["doubtful: uncertain, record kept for the next cleanup"], notes)
        self.assertEqual(["doubtful"], [entry["name"] for entry in self.records()])
        self.assertTrue(root.exists(), "an uncertain outcome keeps the scratch too")

    def test_reclaim_without_a_record_is_a_no_op(self):
        self.assertEqual([], children.reclaim(self.hub))
        self.assertFalse(children.record_path(self.hub).exists())

    def test_an_exited_pid_is_untouched(self):
        # Above the Darwin and Linux PID range, so no process can hold it: only a proven exit.
        root = self.scratch()
        children._write(self.hub, [self.stale(name="gone", pid=99_999_999,
                                              process_identity=IDENTITY_KIND + ":1",
                                              temp_root=str(root))])
        self.assertEqual(["gone: pid reused or exited, untouched; scratch removed"],
                         children.reclaim(self.hub))
        self.assertFalse(root.exists())
        self.assertEqual([], self.records())


if __name__ == "__main__":
    unittest.main()
