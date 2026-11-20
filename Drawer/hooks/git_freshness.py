"""Shared SessionStart adapter for the verified Hub Git branch policy."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType

DRAWER_DIR = Path(__file__).resolve().parents[1]
if str(DRAWER_DIR) not in sys.path:
    sys.path.insert(0, str(DRAWER_DIR))
import harness_config

# The settings render after an accepted refresh: it has taken over 20 s.
SETTINGS_SECONDS = 45
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def active_env() -> str:
    """The recursion guard variable: <env_prefix>_GIT_FRESHNESS_ACTIVE, from harness.toml."""
    return harness_config.env_name("GIT_FRESHNESS_ACTIVE", harness_config.load(DRAWER_DIR.parent))


def _load_policy(hub_root: Path) -> ModuleType:
    path = hub_root / "Drawer" / "git-branch-policy.py"
    spec = importlib.util.spec_from_file_location("hub_git_branch_policy", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("the tracked Git branch policy could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def notice(hub_root: Path, source: str) -> str:
    """Return one truthful SessionStart line; never let Git hide the memory digest."""
    return check(hub_root, source)[0]


def check(hub_root: Path, source: str) -> tuple[str, bool]:
    """(the SessionStart text, True when the checkout is current or rebased).

    After an accepted refresh it renders this checkout's .mcp.json from the new tree (settings-sync
    --mcp; the tracked settings are Git's), so a pulled manifest change reaches the checkout. A render
    fault adds one line and never blocks: the freshness verdict and the digest stay.
    """
    text, fresh = _notice(hub_root, source)
    if fresh:
        fault = render_mcp(hub_root)
        if fault:
            text += "\n" + fault
    return text, fresh


def render_mcp(hub_root: Path) -> str:
    """'' when settings-sync --mcp exits 0, else one SETTINGS SYNC FAILED line."""
    try:
        done = subprocess.run(
            [sys.executable, "-B", str(hub_root / "Drawer" / "settings-sync.py"), "--mcp"],
            cwd=str(hub_root), capture_output=True, text=True, timeout=SETTINGS_SECONDS,
            check=False, creationflags=NO_WINDOW)
        if not done.returncode:
            return ""
        lines = [line.strip() for line in f"{done.stdout}\n{done.stderr}".splitlines() if line.strip()]
        why = f"exit {done.returncode}: {lines[-1] if lines else 'no output'}"
    except subprocess.TimeoutExpired:
        why = f"no answer in {SETTINGS_SECONDS} s"
    except OSError as exc:
        why = str(exc)
    return (f"SETTINGS SYNC FAILED — {' '.join(why.split())[:300]}; this checkout's .mcp.json may be stale. "
            "Run <python> Drawer/settings-sync.py --mcp.")


def _notice(hub_root: Path, source: str) -> tuple[str, bool]:
    source = str(source or "").strip().casefold()
    if source not in {"startup", "resume", "clear"}:
        return "", False
    try:
        guard = active_env()
    except Exception as exc:
        return (
            "GIT FRESHNESS BLOCKED — the hub config is unreadable: "
            f"{' '.join(str(exc).split())[:300]}. Do not claim this checkout is current."
        ), False
    if os.environ.get(guard) == "1":
        return (
            "GIT FRESHNESS BLOCKED — recursive session-start freshness invocation was refused. "
            "Do not claim this checkout is current."
        ), False
    previous = os.environ.get(guard)
    os.environ[guard] = "1"
    try:
        policy = _load_policy(hub_root)
        result = policy.freshness(hub_root, source)
        message = result.get("message") if isinstance(result, dict) else None
        if not isinstance(message, str) or not message.strip():
            raise RuntimeError("the Git branch policy returned no freshness message")
        return " ".join(message.split()), result.get("state") in {"current", "rebased"}
    except Exception as exc:
        return (
            "GIT FRESHNESS BLOCKED — the verified freshness check failed: "
            f"{' '.join(str(exc).split())[:300]}. Do not claim this checkout is current or begin "
            "new code work; read-only diagnosis remains available."
        ), False
    finally:
        if previous is None:
            os.environ.pop(guard, None)
        else:
            os.environ[guard] = previous
