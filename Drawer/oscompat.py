"""oscompat -- the one home of each operating-system difference the hub meets: macOS and Linux, and
native Windows with Git for Windows (Claude Code runs the Bash tool and every hook through Git Bash).

- venv_python: the hub interpreter a process runs. uv puts it at .venv/bin/python on POSIX and at
  .venv/Scripts/python.exe on Windows.
- ensure_venv_shim: on Windows, a two-line sh script at .venv/bin/python, so every command the rules and
  skills print (`<python>` is `.venv/bin/python -B`) runs in Git Bash. Python never runs the shim: a
  process starts venv_python.
- lock / unlock: one exclusive, non-blocking file lock. POSIX takes flock on the whole file. Windows
  takes msvcrt.locking on one byte far past the end, so a reader of the file's data never meets the lock.
- popen_group / kill_tree: start a child as the head of its own group, and stop that whole tree.
  Never os.kill on Windows: Python maps every signal but CTRL_*_EVENT to TerminateProcess.
- process_started: a pid-reuse-resistant creation stamp of a Windows process (GetProcessTimes).
- replace: os.replace with a short retry on Windows, where a file open in another process (an editor, a
  scan, Claude Code itself) refuses a replace for a moment.
- native_path: a Git Bash path (/c/work/x) as Windows reads it (C:/work/x).
- recycle: move a path to the Windows Recycle Bin (root AGENTS.md SAF-4 trash, not rm).
- git_shell: Git for Windows' own sh or bash, never the WSL launcher in System32.

Standard library only: the seat and the hooks import it before the hub environment exists.
Cost: this file. Maintainer: hub (whoever ports a tool to a new platform).
"""
from __future__ import annotations

import errno
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

WINDOWS = os.name == "nt"
# The shim at .venv/bin/python on Windows. Git Bash runs a #! script; CreateProcess does not.
VENV_SHIM = ("#!/bin/sh\n"
             "# Windows: uv puts the interpreter in .venv/Scripts. Hub commands name .venv/bin/python (Drawer/oscompat.py).\n"
             'exec "$(dirname "$0")/../Scripts/python.exe" "$@"\n')
# The errors of a lock another handle holds: flock EWOULDBLOCK, msvcrt EACCES or EDEADLOCK. Any other is a fault.
BUSY = {errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES, errno.EDEADLK}
LOCK_OFFSET = 1 << 30  # the byte Windows locks: past any lock file's data
REPLACE_TRIES = 20
REPLACE_PAUSE = 0.05


def venv_python(root) -> Path:
    """The hub interpreter under root that a process can start."""
    venv = Path(root) / ".venv"
    return venv / "Scripts" / "python.exe" if WINDOWS else venv / "bin" / "python"


def venv_bin(root) -> Path:
    """The folder of the hub environment's console scripts (gdformat, pytest)."""
    return venv_python(root).parent


def ensure_venv_shim(root) -> bool:
    """On Windows, write .venv/bin/python as VENV_SHIM when the interpreter exists and the shim differs.
    True when it wrote. A uv sync that rebuilds .venv drops the shim; seat configure writes it again."""
    if not WINDOWS or not venv_python(root).is_file():
        return False
    shim = Path(root) / ".venv" / "bin" / "python"
    try:
        if shim.read_bytes() == VENV_SHIM.encode():
            return False
    except OSError:
        pass
    shim.parent.mkdir(parents=True, exist_ok=True)
    with open(shim, "w", encoding="utf-8", newline="\n") as f:
        f.write(VENV_SHIM)
    return True


def _fd(handle) -> int:
    return handle if isinstance(handle, int) else handle.fileno()


def lock(handle, blocking: bool = False, timeout: float | None = None) -> None:
    """Take the exclusive lock on an open file (an object or a descriptor). A busy lock raises OSError
    (BlockingIOError on POSIX) unless blocking, which waits up to timeout seconds (None: no limit). Any
    other lock fault raises at once."""
    fd = _fd(handle)
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        try:
            if WINDOWS:
                import msvcrt
                where = os.lseek(fd, 0, os.SEEK_CUR)
                os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                finally:
                    os.lseek(fd, where, os.SEEK_SET)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in BUSY or not blocking or (deadline is not None and time.monotonic() >= deadline):
                raise
            time.sleep(0.05)


def unlock(handle) -> None:
    """Release the lock lock() took on the same open file."""
    fd = _fd(handle)
    if WINDOWS:
        import msvcrt
        where = os.lseek(fd, 0, os.SEEK_CUR)
        os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.lseek(fd, where, os.SEEK_SET)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


def popen_group() -> dict:
    """Popen keywords that make the child the head of its own group, so kill_tree reaches its tree and a
    Ctrl-C in the parent's console does not."""
    if WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def _exited(pid: int) -> bool:
    """Windows: True when pid has ended or cannot be opened. A handle held elsewhere keeps an ended process
    openable, so its exit code decides."""
    try:
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k.OpenProcess.restype = wintypes.HANDLE
        k.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        k.GetExitCodeProcess.restype = wintypes.BOOL
        k.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = k.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return True
        try:
            code = wintypes.DWORD()
            return bool(k.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value != 259  # STILL_ACTIVE
        finally:
            k.CloseHandle(handle)
    except (OSError, AttributeError, ValueError):
        return False


def kill_tree(pid: int) -> None:
    """Stop pid and every process below it at once. POSIX: SIGKILL to the group pid heads (popen_group).
    Windows: taskkill /T /F. A process already gone raises ProcessLookupError on both."""
    if WINDOWS:
        done = subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"], capture_output=True,
                              text=True, encoding="utf-8", errors="replace", check=False)
        if done.returncode == 128:  # no such process
            raise ProcessLookupError(pid)
        # A venv launcher exits by itself once taskkill has stopped its child: it is gone before taskkill reaches
        # it, which taskkill reports as an error ("no running instance"). The tree is stopped: not a fault.
        if done.returncode != 0 and _exited(pid):
            return
        if done.returncode != 0:
            raise OSError("taskkill %d: %s" % (pid, (done.stderr or done.stdout).strip()))
        return
    import signal
    os.killpg(int(pid), signal.SIGKILL)


def term_tree(pid: int) -> None:
    """Ask pid's group to stop (SIGTERM on POSIX). Windows has no polite stop for a console tree:
    it stops the tree as kill_tree does."""
    if WINDOWS:
        kill_tree(pid)
        return
    import signal
    os.killpg(int(pid), signal.SIGTERM)


def process_started(pid: int) -> int | None:
    """The creation time (FILETIME ticks) of Windows process pid, or None when it cannot be read."""
    if not WINDOWS:
        return None
    try:
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k.OpenProcess.restype = wintypes.HANDLE
        k.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
        k.GetProcessTimes.restype = wintypes.BOOL
        k.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = k.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not k.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return None
            return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        finally:
            k.CloseHandle(handle)
    except (OSError, AttributeError, ValueError):
        return None


def replace(src, dst) -> None:
    """os.replace(src, dst). Windows retries a PermissionError for about a second before it raises."""
    tries = REPLACE_TRIES if WINDOWS else 1
    for attempt in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == tries - 1:
                raise
            time.sleep(REPLACE_PAUSE)


def native_path(text: str) -> str:
    """On Windows, a Git Bash drive path (/c/work/x) as Windows reads it (C:/work/x). Else text."""
    if WINDOWS and len(text) >= 2 and text[0] == "/" and text[1].isalpha() and (len(text) == 2 or text[2] == "/"):
        return text[1].upper() + ":/" + text[3:]
    return text


def recycle(path) -> None:
    """Move path (a file or a folder) to the Windows Recycle Bin. OSError when it fails or off Windows."""
    if not WINDOWS:
        raise OSError("the Recycle Bin is Windows only")
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    full = os.path.abspath(native_path(os.fspath(path))).replace("/", "\\")
    if not os.path.lexists(full):
        raise FileNotFoundError(full)
    op = SHFILEOPSTRUCTW(None, 3, full + "\0", None, 0x40 | 0x10 | 0x4 | 0x400 | 0x4000, False, None, None)
    # FO_DELETE; FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI | FOF_WANTNUKEWARNING: an item the
    # Recycle Bin cannot take (a network drive, an oversize item) asks first, never goes for good unasked.
    # pFrom ends in two NULs.
    code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if code or op.fAnyOperationsAborted or os.path.lexists(full):
        raise OSError("Recycle Bin refused %s (code %d)" % (full, code))


def git_shell(name: str = "sh") -> str:
    """The shell that runs a hub or product script. Windows: Git for Windows' own sh.exe or bash.exe
    beside git.exe, never C:/Windows/System32/bash.exe (the WSL launcher). POSIX: the bare name."""
    if not WINDOWS:
        return name
    git = shutil.which("git")
    if git:
        top = Path(git).resolve().parent.parent  # <Git>/cmd/git.exe or <Git>/bin/git.exe
        for found in (top / "bin" / f"{name}.exe", top / "usr" / "bin" / f"{name}.exe"):
            if found.is_file():
                return str(found)
    found = shutil.which(name)
    if found and "system32" not in found.lower():
        return found
    raise OSError(f"no Git for Windows {name}.exe: install Git for Windows (winget install Git.Git)")


def account_home() -> Path:
    """The account's home folder whatever HOME or USERPROFILE say (POSIX: the passwd entry; Windows: the profile
    folder the shell reports). Raises OSError when the system names none."""
    if WINDOWS:
        import ctypes
        buffer = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0x28, None, 0, buffer) == 0:  # CSIDL_PROFILE
            return Path(buffer.value)
        raise OSError("the system names no profile folder")
    import pwd
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except KeyError as exc:
        raise OSError("the system names no home for this account") from exc


def utf8_stdio() -> None:
    """UTF-8 on stdin, stdout and stderr whatever the locale (Windows pipes default to the ANSI code page)."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
