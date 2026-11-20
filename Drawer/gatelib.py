"""gatelib -- the one body the PreToolUse gates share.

One copy of the box-local log line, the PreToolUse deny JSON, the unscoped-search
refusal and clip(), which keeps a deny reason within REASON_CAP characters. Drawer/doc-gate.py
holds a Gate; Drawer/project-gate.py calls deny() and unscoped_search_reason().

Cost: this file, imported by gates that already ran this code inline.
Maintainer: hub (whoever changes a gate's logging).
"""
import json
import os
import re
import shlex
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import oscompat  # noqa: E402

# A Claude shell grep -r, find or ls -t over a whole generated tree ([product] broad_roots), or over the
# product root ([product] path), runs past the 120 s tool timeout. A narrower path passes. Claude Code
# only: Drawer/project-gate.py calls this.
SEGMENT = re.compile(r"&&|\|\||[;|\n]")
GREP_ARG_FLAGS = ("-e", "-f", "-m", "-A", "-B", "-C", "-d", "-D")
UNSCOPED_SEARCH_REASON = (
    "project-gate: unscoped search refused: %s over %s runs past the tool timeout. "
    "Fix: use the Grep tool with a narrower path and a glob.")
REASON_CAP = 200  # every PreToolUse deny reason fits this (Drawer/test_deny_reasons.py)


def clip(text, size=32):
    """text cut to size characters, keeping its tail (a path's useful end), so a deny reason stays short."""
    text = str(text)
    return text if len(text) <= size else "..." + text[-(size - 3):]


def _search_paths(words):
    """(shape, path arguments) of one grep -r, find or ls -t command, else (None, [])."""
    while words and re.match(r"^[A-Za-z_]\w*=", words[0]):
        words = words[1:]  # VAR=value prefix
    if not words:
        return None, []
    prog, args = os.path.basename(words[0]).lower(), words[1:]
    if oscompat.WINDOWS:
        prog = re.sub(r"\.(exe|cmd|bat)$", "", prog)  # grep.exe, C:\Git\usr\bin\find.exe
    if prog in ("grep", "egrep", "fgrep"):
        recursive, positional, has_pattern, skip = False, [], False, False
        for a in args:
            if skip:
                skip = False
            elif a in ("--recursive", "--dereference-recursive"):
                recursive = True
            elif a.startswith("--"):
                pass
            elif a.startswith("-") and len(a) > 1:
                recursive |= "r" in a[1:] or "R" in a[1:]
                has_pattern |= a[:2] in ("-e", "-f")
                skip = a in GREP_ARG_FLAGS
            else:
                positional.append(a)
        return ("grep -r" if recursive else None), positional[0 if has_pattern else 1:]
    if prog == "find":
        paths = []
        for a in args:
            if a.startswith(("-", "(", "!")):
                break
            paths.append(a)
        return "find", paths
    if prog == "ls":
        timed = any(a.startswith("-") and not a.startswith("--") and "t" in a[1:] for a in args)
        return ("ls -t" if timed else None), [a for a in args if not a.startswith("-")]
    return None, []


def _trimmed(path):
    """The path less its trailing slashes and any /* or /. tail."""
    path = path.rstrip("/")
    while path.endswith(("/*", "/.")):
        path = path[:-2].rstrip("/")
    return path


def _same(path):
    """path as one absolute spelling to compare: a Git Bash /c/x is C:/x and Windows case is folded."""
    return os.path.normcase(os.path.abspath(os.path.expanduser(oscompat.native_path(path))))


def _product():
    """([product] path absolute or "", [product] broad_roots as a tuple); ("", ()) when the config does not read."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import harness_config
        cfg = harness_config.load()
    except (OSError, KeyError, SystemExit, ValueError):
        return "", ()
    product = cfg.get("product") or {}
    project = str(product.get("path") or "")
    roots = tuple(str(r).strip("/") for r in product.get("broad_roots") or () if str(r).strip("/"))
    return (_same(project) if project else ""), roots


def unscoped_search_reason(command):
    """The refusal for a shell grep -r, find or ls -t over a broad tree, else None. The hub config
    ([product] path and broad_roots) is read only when a search names a path."""
    searches = []
    for segment in SEGMENT.split(command):
        if oscompat.WINDOWS:
            segment = segment.replace("\\", "/")  # a backslash path is no escape on Windows
        try:
            words = shlex.split(segment)
        except ValueError:
            words = segment.split()
        shape, paths = _search_paths(words)
        if shape:
            searches += [(shape, path) for path in paths]
    if not searches:
        return None
    project, roots = _product()
    for shape, path in searches:
        trimmed = _trimmed(path)
        if os.path.normcase(trimmed.rsplit("/", 1)[-1]) in {os.path.normcase(r) for r in roots}:
            return UNSCOPED_SEARCH_REASON % (shape, clip(path))
        if project and _same(trimmed or "/") == project:
            return UNSCOPED_SEARCH_REASON % (shape, clip(path))
    return None


def append(path, text, mkdir=False):
    """Append one timestamped line. Best effort: a log never blocks a gate."""
    try:
        if mkdir:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), text))
    except OSError:
        pass


def _kernel32():
    """(kernel32, get_last_error, dword, byref) on Windows, typed for 64-bit handles. A test swaps
    this out: ctypes will not import on a Mac that poses as Windows."""
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k.OpenProcess.restype = wintypes.HANDLE
    k.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    k.GetExitCodeProcess.restype = wintypes.BOOL
    k.CloseHandle.argtypes = (wintypes.HANDLE,)
    return k, ctypes.get_last_error, wintypes.DWORD, ctypes.byref


def _pid_state_windows(pid):
    """pid_state on Windows by OpenProcess and GetExitCodeProcess. Never os.kill: Python maps any
    signal but CTRL_C_EVENT and CTRL_BREAK_EVENT to TerminateProcess, so a probe would end the process.
    Only ERROR_INVALID_PARAMETER (no such pid) or a read exit code proves the process gone."""
    try:
        k, last_error, dword, byref = _kernel32()
        handle = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            err = last_error()
            if err == 87:  # ERROR_INVALID_PARAMETER: no such pid
                return "gone"
            return "live" if err == 5 else "unsure"  # ERROR_ACCESS_DENIED: it runs
        try:
            code = dword(0)
            if not k.GetExitCodeProcess(handle, byref(code)):
                return "unsure"
            return "live" if code.value == 259 else "gone"  # STILL_ACTIVE
        finally:
            k.CloseHandle(handle)
    except (OSError, AttributeError, ValueError):
        return "unsure"


def pid_state(pid):
    """The one pid probe of the hub: "live" (it runs, or it runs as another user), "gone" (no such
    process) or "unsure" (no answer, or no int pid). Windows takes _pid_state_windows.
    test_pid_probe refuses an os.kill signal-0 probe anywhere else in Drawer/."""
    if not isinstance(pid, int) or isinstance(pid, bool):
        return "unsure"
    if os.name == "nt":
        return _pid_state_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "gone"
    except PermissionError:
        return "live"
    except (OSError, OverflowError):
        return "unsure"
    return "live"


def pid_alive(pid):
    """Whether process `pid` runs on this box. A missing pid, or an unsure answer, counts as running:
    a caller fails closed."""
    return pid_state(pid) != "gone"


def live_sessions(home, own=""):
    """(live, unread) from <home>/.claude/sessions/<pid>.json, one row per Claude Code session: live is
    the uuid8 of each session whose pid runs (pid_alive), by its sessionId and by the uuid8 suffix of its
    name; unread names each row of a running pid whose session cannot be read twice. A row with no pid
    in it or its file name is no session row. The row whose sessionId starts with own (a uuid8) is the
    caller's: none of its ids count, so a name suffix kept past a /clear stays the caller's own.
    Shared by git-branch-policy worktree-remove and the session-start sweep."""
    live, unread = set(), []
    folder = os.path.join(str(home), ".claude", "sessions")
    try:
        names = sorted(n for n in os.listdir(folder) if n.endswith(".json"))
    except OSError:
        names = []
    for name in names:
        stem, data = name[:-len(".json")], None
        for attempt in (0, 1):
            try:
                with open(os.path.join(folder, name), encoding="utf-8") as f:
                    data = json.load(f)
                break
            except (OSError, ValueError):
                time.sleep(0.2 * (1 - attempt))
        data = data if isinstance(data, dict) else {}
        pid = data.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool):
            pid = int(stem) if stem.isdigit() else None
        if pid is None or not pid_alive(pid):
            continue
        session = data.get("sessionId")
        if not isinstance(session, str) or not session:
            unread.append(name)
            continue
        if own and session[:8].lower() == own:
            continue
        live.add(session[:8].lower())
        suffix = re.search(r"-([0-9a-f]{8})$", str(data.get("name") or ""))
        if suffix:
            live.add(suffix.group(1))
    return live, unread


def deny(reason):
    """Print the PreToolUse deny the harness reads. The caller returns after it."""
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))


class Gate:
    """One named gate: its box-local files and its off|dry environment switch.

    name is the gate's file stem (doc-gate), so the log and every logged line carry
    the same name the operator types on the CLI.
    """

    def __init__(self, name, here, env):
        self.name = name
        self.env = env
        self.log_path = os.path.join(here, name + ".log")

    def mode(self):
        return os.environ.get(self.env, "").lower()

    def off(self):
        return self.mode() == "off"

    def log(self, text):
        if self.mode() == "dry":
            return
        append(self.log_path, text)
