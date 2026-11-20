"""The hub probe that git-branch-policy.py land runs at the branch tip: a smoke check, then one replay test per past incident.

A replay is Drawer/probes/replay_<incident>.py. It holds INCIDENT, one line on what went wrong, and
replay(hub, product), which reproduces the incident against this hub's code and returns None when the
protection holds, else why it went red. A replay goes red when its incident's fix is undone.
A new incident gets a new replay from the session that fixes it. A replay that calls a script's main
passes it an argv (main([])), never the probe's own; a SystemExit from a replay is red.

The smoke compiles every Python file under Drawer/ and parses harness.toml. The product replays use
--product, else the session's product folder (harness_config.product_path); with neither, they are red:
a skipped replay proves nothing.

    <python> Drawer/probes/probe.py [--product <product folder>] [--only <name>] [--json]

Exit 0 when every check passes, else 1. --json prints one last line {"pass": n, "total": n, "red": [...]}.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import sys
import tomllib

HERE = Path(__file__).resolve().parent
HUB = HERE.parents[1]
sys.path.insert(0, str(HUB / "Drawer"))
import harness_config  # noqa: E402


def smoke(hub, product):
    for path in sorted((hub / "Drawer").rglob("*.py")):
        try:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        except (SyntaxError, UnicodeDecodeError) as error:
            return f"{path.relative_to(hub)} does not compile: {error}"
    try:
        tomllib.loads((hub / "harness.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        return f"harness.toml does not parse: {error}"
    return None


def replays():
    for path in sorted(HERE.glob("replay_*.py")):
        spec = importlib.util.spec_from_file_location(path.stem, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield path.stem[len("replay_"):], module


def product_project(given):
    if given:
        return Path(given)
    _, value = harness_config.product_path(harness_config.load(HUB))
    return harness_config.path(value) if value else None


def main(argv):
    p = argparse.ArgumentParser(prog="probe.py")
    p.add_argument("--product")
    p.add_argument("--only", action="append")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    product = product_project(a.product)
    checks = [("smoke", "the hub's Python compiles and harness.toml parses", smoke)]
    checks += [(name, module.INCIDENT, module.replay) for name, module in replays()]
    if a.only:
        checks = [c for c in checks if c[0] in a.only]
    red = []
    for name, incident, check in checks:
        try:
            why = check(HUB, product)
        except (Exception, SystemExit) as error:  # a replay that breaks is red, never skipped
            why = f"the replay broke: {type(error).__name__}: {error}"
        print(f"PASS {name}" if why is None else f"FAIL {name}: {why} (incident: {incident})", flush=True)
        if why is not None:
            red.append(name)
    print(f"PROBE {'GREEN' if not red else 'RED'}: {len(checks) - len(red)}/{len(checks)} pass")
    if a.json:
        print(json.dumps({"pass": len(checks) - len(red), "total": len(checks), "red": red}))
    return 1 if red else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
