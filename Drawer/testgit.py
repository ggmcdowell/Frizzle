"""Light git fixtures for the hub tests.

One full smoke built about 1,341 git repos in TMPDIR, and fseventsd ran at 100%+ CPU on their writes. A test file
that builds git repos calls arm() at import: each git init and clone that test process and its children start then
copies no template (no sample hooks, description or info/exclude). A child env built from a list of names takes ENV
too. slim() packs a template repo that cases copy, so each copy writes a few files, not hundreds. seed() packs a set of
files once, so a repo that names it in objects/info/alternates writes no loose object for them.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # oscompat, beside this file

# An empty GIT_TEMPLATE_DIR: git init and git clone copy no template folder (git-init(1) TEMPLATE DIRECTORY).
ENV = {"GIT_TEMPLATE_DIR": ""}
# What a repo that is copied per case does not need: git writes each again when a case needs it.
DROPPED = ("description", "COMMIT_EDITMSG", "ORIG_HEAD", "FETCH_HEAD", "logs")


def _own_python_first() -> None:
    """The folder of the interpreter that runs the tests leads PATH, once: a fixture repo has no .venv, so its git
    hooks take python3 from PATH. A direct unittest run has no conftest or smoke.py to set it (harness_config.test_home)."""
    own = os.path.dirname(sys.executable)
    rest = [part for part in os.environ.get("PATH", "").split(os.pathsep) if part and part != own]
    os.environ["PATH"] = os.pathsep.join([own, *rest])


_own_python_first()


def system_path() -> str:
    """A PATH for a fixture that must not see the box's own tools: git's folder and the POSIX system folders. On
    Windows Git for Windows' sh sits in its own folders, which git needs to run a hook."""
    import oscompat
    folders = {os.path.dirname(shutil.which("git") or ""), "/usr/bin", "/bin"}
    if oscompat.WINDOWS:
        folders.add(os.path.dirname(oscompat.git_shell("sh")))
    return os.pathsep.join(sorted(f for f in folders if f))


def arm() -> None:
    """Every git init and clone this process and its children start copies no template."""
    os.environ.update(ENV)


def _git(git_dir: Path, *arguments: str, stdin: str | None = None) -> None:
    subprocess.run(["git", "--git-dir", str(git_dir), *arguments], check=True, capture_output=True, text=True,
                   input=stdin, env={**os.environ, **ENV})


def slim(git_dir: Path) -> None:
    """Pack git_dir's refs and objects, and drop its sample hooks, description, reflogs and message files."""
    git_dir = Path(git_dir)
    _git(git_dir, "pack-refs", "--all")
    _git(git_dir, "repack", "-adq")
    _git(git_dir, "prune-packed")
    for sample in git_dir.glob("hooks/*.sample"):
        sample.unlink()
    for name in DROPPED:
        path = git_dir / name
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()


def _writable(function, path, _info) -> None:
    """shutil.rmtree onerror: clear the read-only bit Git sets on pack and object files (Windows refuses to
    remove or overwrite them), then retry."""
    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    function(path)


def rmtree(path) -> None:
    """Remove a folder tree that holds Git's read-only files; a missing folder is no error."""
    if os.path.lexists(path):
        shutil.rmtree(path, onerror=_writable)


def _copy_writable(source, target, *, follow_symlinks=True):
    """shutil.copy2 whose copy is writable: a Git pack or bitmap file is read-only, and a copy of it blocks the
    next case's overwrite and the cleanup on Windows."""
    copied = shutil.copy2(source, target, follow_symlinks=follow_symlinks)
    if os.name == "nt" and not os.path.islink(copied):
        os.chmod(copied, stat.S_IWRITE | stat.S_IREAD)
    return copied


def copytree(source, target) -> None:
    """Copy a Git fixture tree into target (which may exist), with writable copies."""
    shutil.copytree(source, target, symlinks=True, dirs_exist_ok=True, copy_function=_copy_writable)


def seed(folder: Path, files: list[Path]) -> Path:
    """A bare repo under folder that holds each file's blob in one pack; returns its objects folder."""
    bare = Path(folder) / "seed.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, capture_output=True,
                   env={**os.environ, **ENV})
    _git(bare, "hash-object", "-w", "--stdin-paths", "--no-filters", stdin="".join(f"{f}\n" for f in files))
    _git(bare, "repack", "-adq")
    _git(bare, "prune-packed")
    return bare / "objects"


def borrow(git_dir: Path, objects: Path) -> None:
    """git_dir reads objects from that objects folder too (gitrepository-layout(5) objects/info/alternates)."""
    info = Path(git_dir) / "objects" / "info"
    info.mkdir(parents=True, exist_ok=True)
    # LF, not the platform's newline (a CRLF line names an objects folder that ends in a carriage return), and
    # forward slashes, which git reads on Windows too.
    with open(info / "alternates", "w", encoding="utf-8", newline="\n") as alternates:
        alternates.write(f"{Path(objects).as_posix()}\n")
