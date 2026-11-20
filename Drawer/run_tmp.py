"""The per-run temp folder of a hub test run.

Drawer/smoke.py and Drawer/hub-land.py run their test processes with TMPDIR, TMP and TEMP set to
<cache>/<state_root name>-<hash>-test-tmp/<run id>, so the temp repos the hub tests make stay out of the system TMPDIR
(test fixture repos there drove fseventsd to 100% CPU). The run folder goes to the trash when the run ends,
red or green. Each run first trashes the run folders whose owner pid is gone (a killed run). A run started inside
another run's folder (hub-land runs smoke.py) uses that folder and leaves its cleanup to the outer run.
<cache> is the macOS per-user cache folder (getconf DARWIN_USER_CACHE_DIR, beside the box TMPDIR); on another
box, or when getconf fails, the temp folder (one entry). It sits outside the home folder, the state root and every
delete-guard temp root: a temp path under state_root reads as a portable state_root: path
(a tool test), a fixture under the home folder finds the box's own Claude files
(Drawer/test_rules_check.py), and a fixture under /tmp passes every delete-guard peer case
(Drawer/test_project_gate.py). The hash of the resolved state root keeps two hubs (a stamped hub, a test fixture)
apart.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gatelib  # noqa: E402
import harness_config  # noqa: E402
import oscompat  # noqa: E402

TEMP_ROOT: Path | None = None  # set by a test; None: temp_root() reads the box
OWNER = ".owner"  # the pid of the process that owns the run folder
YOUNG_SECONDS = 600  # a run folder with no owner file this young may be one a peer run just made
TRASH_TIMEOUT = 120
TEMP_VARS = ("TMPDIR", "TMP", "TEMP")


def temp_root() -> Path:
    """<cache>: TEMP_ROOT when set, else the macOS per-user cache folder, else the temp folder."""
    if TEMP_ROOT is not None:
        return TEMP_ROOT
    if sys.platform == "darwin":
        try:
            ran = subprocess.run(["getconf", "DARWIN_USER_CACHE_DIR"], capture_output=True, text=True, timeout=10,
                                 check=False)
            if ran.returncode == 0 and ran.stdout.strip() and Path(ran.stdout.strip()).is_dir():
                return Path(ran.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            pass
    return Path(tempfile.gettempdir())


def base(cfg) -> Path:
    """<cache>/<state_root name>-<hash>-test-tmp: the parent of every run folder of this hub."""
    state = harness_config.path(cfg["project"]["state_root"]).resolve()
    digest = hashlib.sha256(str(state).encode("utf-8")).hexdigest()[:8]
    return temp_root() / f"{state.name}-{digest}-test-tmp"


def trash(path: Path) -> str | None:
    """Move path to the trash (root AGENTS.md: trash, not rm); None when it went, else why it stays."""
    if oscompat.WINDOWS:
        try:
            oscompat.recycle(path)
        except OSError as exc:
            return f"Recycle Bin: {exc}"
        return None
    tool = shutil.which("trash") or next((t for t in ("/usr/bin/trash", "/opt/homebrew/bin/trash")
                                          if os.access(t, os.X_OK)), None)
    if not tool:
        return "no trash command on this box"
    try:
        ran = subprocess.run([tool, str(path)], capture_output=True, text=True, check=False, timeout=TRASH_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"trash did not run: {exc}"
    if ran.returncode or os.path.lexists(path):
        return f"trash exit {ran.returncode}: {(ran.stderr or ran.stdout).strip()[:200]}"
    return None


def dead(folder: Path) -> bool:
    """Whether no live process owns the run folder: its owner pid is gone, or it has no readable owner and is
    not young."""
    try:
        pid = int((folder / OWNER).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        try:
            return time.time() - folder.stat().st_mtime > YOUNG_SECONDS
        except OSError:
            return False
    return gatelib.pid_state(pid) == "gone"


def sweep(parent: Path) -> list[str]:
    """Trash each run folder under parent that no live process owns; one line per folder that stays."""
    kept = []
    try:
        folders = [p for p in parent.iterdir() if p.is_dir() and not p.is_symlink()]
    except OSError:
        return kept
    for folder in folders:
        if dead(folder):
            why = trash(folder)
            if why:
                kept.append(f"run_tmp: {folder} stays: {why}")
    return kept


@contextmanager
def run_folder(cfg):
    """Yield the environment for the run's test processes: os.environ with each temp variable set to the run
    folder. Inside another run's folder: that folder, with no sweep and no trash."""
    parent = base(cfg)
    outer = os.environ.get("TMPDIR", "")
    if outer and Path(outer).resolve().parent == parent.resolve():
        yield dict(os.environ)
        return
    for line in sweep(parent):
        print(line, file=sys.stderr, flush=True)
    for attempt in range(3):  # a peer run's end may remove the empty parent between mkdir and mkdtemp
        parent.mkdir(parents=True, exist_ok=True)
        try:
            folder = Path(tempfile.mkdtemp(prefix=f"{os.getpid()}-", dir=parent))
            break
        except FileNotFoundError:
            if attempt == 2:
                raise
    (folder / OWNER).write_text(str(os.getpid()), encoding="utf-8")
    try:
        yield {**os.environ, **{name: str(folder) for name in TEMP_VARS}}
    finally:
        why = trash(folder)
        if why:
            print(f"run_tmp: {folder} stays: {why}", file=sys.stderr, flush=True)
        try:
            parent.rmdir()  # only when empty: the last run of this hub leaves no folder behind
        except OSError:
            pass
