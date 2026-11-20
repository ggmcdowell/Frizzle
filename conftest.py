"""pytest at the hub root: the hub tests never read this checkout's harness.local.toml or reach the real roots,
and never see the Flight session vars (<env_prefix>_FLIGHT, <env_prefix>_FLIGHT_ONESHOT) a card-run leg inherits.

A local toml can move the state roots to absolute folders; the tests isolate by a temp HOME, so they would write
into live state. Drawer/smoke.py sets the same variable for its child test processes
(Drawer/test_local_toml_isolation.py).

The run gets a temp HOME (harness_config.isolate_test_env), so the ~-rooted [project] state_root, config_root and [mem]
db, here and in each child process, resolve under it. An audit hook fails any open in this process under the
real roots (Drawer/test_hermetic_roots.py).
"""
import os
import sys
from pathlib import Path

HUB = Path(__file__).resolve().parent
sys.path.insert(0, str(HUB / "Drawer"))
import harness_config  # noqa: E402

harness_config.isolate_test_env()  # NO_LOCAL_VAR, no Flight vars (incident befb286b), a temp HOME unless nested

_REAL = tuple(os.path.realpath(p) for p in harness_config.real_roots(harness_config.load(HUB)))


def _guard(event, args):
    if event not in ("open", "sqlite3.connect") or not args or not isinstance(args[0], (str, bytes, os.PathLike)):
        return
    try:
        target = os.path.realpath(os.fsdecode(args[0]))
    except (TypeError, ValueError):
        return
    for root in _REAL:
        if target == root or target.startswith(root + os.sep) or target.startswith(root + "-"):
            raise PermissionError(f"a hub test opened {target} under the real root {root}")


sys.addaudithook(_guard)
