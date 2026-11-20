#!/usr/bin/env python
r"""The mechanical onboarding check for one seat.

A seat is this checkout's working environment: the hub root, the local
.mcp.json that launches the memory server, the distributed inputs and the tools a session
needs. This prints PASS or FAIL for each item and exits 1 when any item fails, so onboarding
is checked by running one command rather than by reading a document.

    .venv/bin/python -B Drawer/seat-doctor.py
    --source-only   a fresh checkout, before setup: the distributed inputs and the tools
    --json          the same items, structured

Every item is pure over an injected environment dict, config and command runner, so the
checks are testable off any particular machine.
"""
from __future__ import annotations

import os
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

HUB = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HUB / "Drawer"))
import harness_config  # noqa: E402

CFG = harness_config.load(HUB)
MIN_PYTHON = (3, 12)
# The first Claude Code the hub's Workflow legs and hooks need: Subagent and Workflow leg settings
# (settings-sync.py, CLI 2.1.316) and the Monitor hook match (project-gate.py, 2.1.314). README Requirements names it.
MIN_CLI = (2, 1, 316)


class Item:
    def __init__(self, name: str, ok: bool, detail: str, skipped: bool = False):
        self.name = name
        self.ok = ok
        self.detail = detail
        self.skipped = skipped


def seat_keys(cfg=None) -> tuple[str, ...]:
    """The seat values a launching client can carry into this process: hub and box."""
    cfg = cfg or CFG
    return tuple(harness_config.env_name(suffix, cfg) for suffix in ("HUB", "BOX"))


def sources(cfg=None) -> tuple[str, ...]:
    """The distributed inputs a checkout carries before any setup step runs."""
    cfg = cfg or CFG
    return ("harness.toml", cfg["project"]["marker_file"], "AGENTS.md", ".gitattributes",
            "pyproject.toml", "uv.lock", "Drawer/settings-manifest.json",
            "Drawer/statusline-command.sh", "Drawer/mem/mem.py")


def _resolved(value: str) -> Path:
    return Path(value).expanduser().resolve(strict=False)


def _same_path(left: Path, right: Path) -> bool:
    """A default macOS volume ignores case; compare case-insensitively after resolving."""
    return str(left).casefold() == str(right).casefold()


def run_command(args: list[str], timeout: int = 30):
    """(returncode, output). (None, reason) when the command could not run at all."""
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return done.returncode, (done.stdout or "") + (done.stderr or "")
    except FileNotFoundError as exc:
        return None, str(exc)
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout}s"
    except OSError as exc:  # pragma: no cover - platform-specific spawn failures
        return None, str(exc)


def _nonempty_item(name: str, env) -> Item:
    value = (env.get(name) or "").strip()
    if not value:
        return Item(name, False, "unset or empty")
    return Item(name, True, value)


def _mcp_json_item(hub: Path, cfg) -> Item:
    """The local .mcp.json (settings-sync.py writes it) must register the memory server."""
    server = harness_config.server_name(cfg)
    path = hub / ".mcp.json"
    if not path.is_file():
        return Item("mcp_json", False, "missing; run Drawer/settings-sync.py --apply")
    try:
        servers = json.loads(path.read_text(encoding="utf-8")).get("mcpServers") or {}
    except (ValueError, OSError, AttributeError) as exc:
        return Item("mcp_json", False, f"does not parse ({type(exc).__name__})")
    if server not in servers:
        return Item("mcp_json", False, f"registers no {server} server: {sorted(servers)}")
    return Item("mcp_json", True, f"registers {server}")


def _sources_item(hub: Path, cfg) -> Item:
    missing = [name for name in sources(cfg) if not (hub / name).is_file()]
    if missing:
        return Item("sources", False, "missing: " + ", ".join(missing))
    return Item("sources", True, f"all {len(sources(cfg))} distributed inputs present")


def _tool_item(name: str, runner) -> Item:
    code, out = runner([name, "--version"])
    if code is None:
        return Item(name, False, f"not found: {(out or '').strip()[:120]}")
    if code != 0:
        return Item(name, False, f"--version exited {code}: {(out or '').strip()[:120]}")
    return Item(name, True, ((out or "").strip().splitlines() or [""])[0][:120])


def _claude_item(runner) -> Item:
    code, out = runner(["claude", "--version"], timeout=10)
    match = re.search(r"\d+\.\d+\.\d+", out or "") if code == 0 else None
    wanted = ".".join(map(str, MIN_CLI))
    if not match:
        return Item("claude", False, f"claude is missing or gave no version; install Claude Code {wanted} or newer")
    if tuple(int(part) for part in match.group(0).split(".")) < MIN_CLI:
        return Item("claude", False, f"Claude Code {match.group(0)} is older than {wanted}; run claude update")
    return Item("claude", True, f"Claude Code {match.group(0)}")


def _denylist_item(hub: Path, cfg) -> Item:
    """The name deny list ([names] denylist) the commit hook reads: present, with a rule of its own."""
    path = harness_config.path(cfg["names"]["denylist"])

    def rules(file: Path) -> list[str]:
        lines = (line.strip() for line in file.read_text(encoding="utf-8").splitlines())
        return [line for line in lines if line and not line.startswith("#")]
    try:
        found = rules(path)
    except OSError:
        return Item("denylist", False, f"missing or unreadable: {path}; setup step 3 copies name-denylist.example there")
    if not found:
        return Item("denylist", False, f"no rule in {path}; the commit hook refuses an empty list")
    example = hub / "name-denylist.example"
    if example.is_file() and found == rules(example):
        return Item("denylist", True, f"{path} still holds only the example rules; add your own names")
    return Item("denylist", True, f"{len(found)} rule(s) in {path}")


def check_seat(env=None, runner=run_command, hub: Path | None = None, cfg=None, *,
               source_only: bool = False, python=None) -> list[Item]:
    """Every seat item in report order. source_only drops the profile items (a fresh checkout)."""
    env = os.environ if env is None else env
    cfg = cfg or CFG
    hub = _resolved(str(HUB if hub is None else hub))
    python = sys.version_info if python is None else python
    items: list[Item] = []

    if not source_only:
        hub_key, box_key = seat_keys(cfg)
        declared = (env.get(hub_key) or "").strip()
        if not declared:
            items.append(Item(hub_key, False, "unset"))
        elif not _same_path(_resolved(declared), hub):
            items.append(Item(hub_key, False, f"{_resolved(declared)} is not the hub root {hub}"))
        else:
            items.append(Item(hub_key, True, str(hub)))
        items.append(_nonempty_item(box_key, env))
        items.append(_mcp_json_item(hub, cfg))
        items.append(_denylist_item(hub, cfg))

    items.append(_sources_item(hub, cfg))
    shown = ".".join(str(part) for part in tuple(python)[:3])
    wanted = ".".join(map(str, MIN_PYTHON))
    items.append(Item("python", tuple(python)[:2] >= MIN_PYTHON,
                      f"Python {shown}" + ("" if tuple(python)[:2] >= MIN_PYTHON else f" is older than {wanted}")))
    items.append(_tool_item("git", runner))
    items.append(_tool_item("uv", runner))
    items.append(_tool_item("gitleaks", runner))  # every commit runs it (Drawer/git-hooks/pre-commit)
    items.append(_claude_item(runner))
    return items


def seat_source_item(carried, applied, main_hub: Path | None = None) -> Item:
    """Name where the checked values came from: this process, or the saved profile.

    Activation replaces this process environment with the saved profile, so every other
    item reads the profile. A process that carried its own seat values and disagrees with
    the profile is the dangerous seat: the client launches with values nothing checked.
    main_hub is the checkout this one is a linked task worktree of: a session seated at the
    main checkout carries its hub into the worktree, whose profile names the worktree, so a
    carried hub that names main_hub matches too.
    """
    hub_key = seat_keys()[0]
    named = {name: value for name, value in carried.items() if (value or "").strip()}
    if not named:
        return Item("seat_source", True,
                    "this process carried no seat; every item is from the saved profile")

    def matches(name, value):
        value = (value or "").strip()
        if value == (applied.get(name) or "").strip():
            return True
        return name == hub_key and main_hub is not None and _same_path(_resolved(value), _resolved(str(main_hub)))

    differing = sorted(name for name, value in named.items() if not matches(name, value))
    if differing:
        shown = ", ".join(f"{name}={named[name].strip()!r}" for name in differing)
        return Item("seat_source", False,
                    f"this process carries seat values the saved profile does not match: {shown}")
    return Item("seat_source", True,
                f"this process carries the seat and it matches the saved profile: "
                f"{', '.join(sorted(named))}")


SETUP_REQUIRED = (
    'HUB SETUP REQUIRED: this checkout has no saved runtime profile on this machine. '
    'Read .claude/skills/setup/SKILL.md and follow it; the seat doctor is repair, not setup.'
)
PROFILE_INVALID = (
    "saved profile is invalid; run `<python> Drawer/runtime/seat.py init` at the hub root "
    "(it moves the bad file aside and writes a fresh profile), then rerun the doctor."
)


def _blocked(as_json: bool, name: str, detail: str) -> int:
    """One named remedy before any seat item: a doctor with nothing to read must still say why."""
    if as_json:
        print(json.dumps({'status': 'FAIL', 'items': [
            {'name': name, 'ok': False, 'detail': detail, 'skipped': False}]}))
    else:
        print(f'FAIL  {name}: {detail}')
    return 1


def _report(as_json: bool, source: Item | None, items: list[Item]) -> int:
    shown = [source, *items] if source else list(items)
    checked = [item for item in shown if not item.skipped]
    failed = [item.name for item in checked if not item.ok]
    if as_json:
        document = {'status': 'FAIL' if failed else 'PASS'}
        if source:
            document['seat_source'] = {'ok': source.ok, 'detail': source.detail}
        document['items'] = [{'name': item.name, 'ok': item.ok, 'detail': item.detail,
                              'skipped': item.skipped} for item in items]
        print(json.dumps(document))
        return 1 if failed else 0
    for item in shown:
        print(f"{'SKIP' if item.skipped else 'PASS' if item.ok else 'FAIL'}  "
              f"{item.name}: {item.detail}")
    skipped = len(shown) - len(checked)
    tail = f", {skipped} skipped" if skipped else ""
    print()
    if failed:
        print(f"RESULT: FAIL - {len(failed)} of {len(checked)} seat items failed{tail}: "
              f"{', '.join(failed)}")
        return 1
    print(f"RESULT: PASS - all {len(checked)} seat items clean{tail}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--json', action='store_true', help='Print structured checks for onboarding acceptance')
    parser.add_argument('--source-only', action='store_true',
                        help='Check the distributed inputs and tools only, before setup')
    args = parser.parse_args(argv)
    if args.source_only:
        return _report(args.json, None, check_seat(source_only=True))
    keys = seat_keys()
    carried = {name: os.environ.get(name, "") for name in keys}
    try:
        from runtime.profile import activate, setup_required
        if setup_required(HUB):
            return _blocked(args.json, 'setup_required', SETUP_REQUIRED)
        activate(HUB)
    except (ValueError, OSError, RuntimeError):
        return _blocked(args.json, 'runtime_profile', PROFILE_INVALID)
    applied = {name: os.environ.get(name, "") for name in keys}
    main_hub = harness_config.main_checkout(HUB).resolve()
    main_hub = None if _same_path(main_hub, HUB.resolve()) else main_hub
    # The seat source stays beside items, never in it: acceptance reads the items by name.
    return _report(args.json, seat_source_item(carried, applied, main_hub), check_seat())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
