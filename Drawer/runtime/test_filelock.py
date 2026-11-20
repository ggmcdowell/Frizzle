"""The shared file lock: one holder at a time, a bounded wait, and release on exit."""
from pathlib import Path
import sys
import tempfile
import threading
import unittest

DRAWER = Path(__file__).resolve().parents[1]
if str(DRAWER) not in sys.path:
    sys.path.insert(0, str(DRAWER))
from runtime import filelock  # noqa: E402


class FileLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="file lock ")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "nested dir" / "seat.lock"

    def test_the_lock_creates_its_folder_and_one_byte_file(self):
        with filelock.FileLock(self.path, 5):
            self.assertTrue(self.path.is_file())
        self.assertEqual(b"0", self.path.read_bytes())

    def test_a_held_lock_refuses_a_second_holder_after_the_wait(self):
        with filelock.FileLock(self.path, 5):
            with self.assertRaises(filelock.LockBusy) as caught:
                with filelock.FileLock(self.path, 0.1):
                    self.fail("a second holder entered a held lock")
        self.assertEqual(self.path, caught.exception.path)
        self.assertIn(str(self.path), str(caught.exception))

    def test_release_lets_the_next_holder_in(self):
        with filelock.FileLock(self.path, 5):
            pass
        with filelock.FileLock(self.path, 0.1) as lock:
            self.assertIsNotNone(lock.handle)
        self.assertIsNone(lock.handle)

    def test_two_threads_serialize_on_one_lock(self):
        held, release, order, failures = threading.Event(), threading.Event(), [], []

        def first():
            try:
                with filelock.FileLock(self.path, 5):
                    order.append("first in")
                    held.set()
                    release.wait(10)
                    order.append("first out")
            except BaseException as exc:  # noqa: BLE001 - reported by the assert below
                failures.append(f"first: {exc}")

        def second():
            try:
                with filelock.FileLock(self.path, 10):
                    order.append("second in")
            except BaseException as exc:  # noqa: BLE001 - reported by the assert below
                failures.append(f"second: {exc}")

        one = threading.Thread(target=first)
        one.start()
        self.addCleanup(one.join, 20)
        self.assertTrue(held.wait(10), "the first holder never took the lock")
        two = threading.Thread(target=second)
        two.start()
        self.addCleanup(two.join, 20)
        # The second thread waits on the held lock; only the release below lets it in.
        two.join(0.3)
        self.assertTrue(two.is_alive(), "the second holder entered while the first held the lock")
        release.set()
        one.join(20)
        two.join(20)
        self.assertEqual([], failures)
        self.assertEqual(["first in", "first out", "second in"], order)


if __name__ == "__main__":
    unittest.main()
