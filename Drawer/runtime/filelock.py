"""One exclusive file lock with a bounded wait, shared by the seat and the child record."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
import time

DEFAULT_SECONDS = 1200.0


class LockBusy(RuntimeError):
    """Another process holds the lock file named by ``path``."""

    def __init__(self, path):
        self.path = Path(path)
        super().__init__(f"another process holds {self.path}")


class FileLock:
    """An exclusive lock on one byte of one file, with a bounded wait.

    flock conflicts across separate open handles, so threads and processes serialize the
    same way.
    """

    def __init__(self, path, timeout: float = DEFAULT_SECONDS):
        self.path = Path(path)
        self.timeout = float(timeout)
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self.handle.seek(0)
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError as exc:
                if time.monotonic() >= deadline:
                    self.handle.close()
                    self.handle = None
                    raise LockBusy(self.path) from exc
                time.sleep(0.05)

    def __exit__(self, _kind, _value, _traceback):
        if self.handle is None:
            return
        self.handle.seek(0)
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        self.handle = None
