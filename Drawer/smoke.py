"""Smoke: the hub's own test files, then the product step, ``[product] gate_command``, run from the hub.

``[smoke] command`` in harness.toml runs it from the hub root, and hub-land.py runs
``[smoke] land_command`` before a land (git-flow skill WRK-11).
The product step runs ``[product] gate_command`` (split like a shell line, with forward slashes in paths; a ``{python}``
word is the hub interpreter, and on Windows a leading ``sh`` or ``bash`` is Git for Windows' own) in the product checkout: the nearest folder at or above the session's product folder
that holds ``.git``, else the project folder. The project is ``[product] path`` (the local
(git-ignored) harness.local.toml names it); a card-run leg's task product worktree (``<env_prefix>_PRODUCT_PATH``)
outranks it. With an empty gate_command or no project, the smoke prints a SKIP line and runs only the hub tests.
The product command's legs are its own; this wrapper only maps its exit.

With ``--impact <base>`` (hub-land.py runs ``[smoke] land_command``, which passes it) the smoke runs only the hub test files
the diff from the merge base of <base> and HEAD can reach: each changed test file, each test file
under Drawer/ that names a changed file or imports its module, on through each file that imports
a file reached (a script that imports a changed module, a test that borrows a test file's fixture), each test file
that names a file reached (runs it by path), the test file named for each file reached, and each test file in
[smoke] always_run (a test that walks the whole tree) (Drawer/hub-land.py: Drawer/test_hub_land.py). A change to a core file (CORE below, or a
pattern in [smoke] full_on), a changed file no test file reaches, an empty or unreadable diff, or a diff that
reaches no test file runs every hub test file, with a FULL line that says why. With no flag (Flight at milestone close,
nightly, the seat smoke) it runs every hub test file.

First it runs each hub test file of its own checkout (Drawer/test_*.py, Drawer/*/test_*.py), each in its own
`python -B -m unittest` process, at most [smoke] max_parallel files at once (0 or unset: a quarter of
the logical cores, at least 1), so several hub lands at once do not starve the machine: one red file fails the smoke with a HUB RED line and no product
step (two red hub files once passed a ship smoke that ran the product gate alone). It prints one summary line: files, tests
run, skipped, red. A box with no gitleaks fails the smoke when Drawer/test_precommit.py would run: its secret-scan tests skip without it.

Exit codes:
  0    the hub tests pass and the product step passed or was skipped
  1    a hub test file is red, the product step is red, or the run broke
  2    the config is incomplete: [product] path names no folder, or [product] gate_command does not parse
  75   ([smoke] blocked_exit) the product step says a human must act

The hub tests and the product step run with the run's own temp folder outside the system TMPDIR (Drawer/run_tmp.py): it
goes to the trash when the run ends, and a killed run's folder goes at the next run.

    <python> Drawer/smoke.py [--impact <base>]
"""

from __future__ import annotations

import argparse
import fnmatch
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

HUB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HUB / "Drawer"))
from concurrent.futures import ThreadPoolExecutor  # noqa: E402

import harness_config  # noqa: E402
import run_tmp  # noqa: E402

HUB_TEST_GLOBS = ("Drawer/test_*.py", "Drawer/*/test_*.py")
# Core files: a change to one runs every hub test file, since most tests load them.
CORE = ("harness.toml", "Drawer/harness_config.py", "Drawer/runlib.py", "Drawer/gatelib.py", "Drawer/oscompat.py", "Drawer/smoke.py",
        "Drawer/runtime/__init__.py")
RAN = re.compile(r"(?m)^Ran (\d+) tests? in ")
SKIPPED = re.compile(r"(?m)^(?:OK|FAILED) \([^)]*\bskipped=(\d+)")
SCAN_TEST = "Drawer/test_precommit.py"  # its secret-scan tests skip when gitleaks is missing
HUB_TEST_TIMEOUT = 1800  # seconds for one hub test file: test_hub_land.py, the slowest, takes under 2 min on POSIX and 10 to 15 on a loaded Windows box (every test spawns git and python children)


def blocked_exit(cfg) -> int:
    code = cfg.get("smoke", {}).get("blocked_exit")
    return code if isinstance(code, int) and not isinstance(code, bool) else 75


def max_parallel(cfg) -> int:
    """[smoke] max_parallel when it is a whole number over 0; else a quarter of the logical cores, at least 1."""
    value = cfg.get("smoke", {}).get("max_parallel")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return max(1, (os.cpu_count() or 4) // 4)


def hub_tests_all(root: Path = HUB) -> list[str]:
    """Each hub test file under root, root-relative, sorted."""
    return sorted({p.relative_to(root).as_posix() for g in HUB_TEST_GLOBS for p in root.glob(g)})


def changed_files(root: Path, base: str) -> list[str] | None:
    """The files the diff from the merge base of base and HEAD touches (a rename gives both names); None when git
    cannot read it."""
    try:
        run = subprocess.run(["git", "diff", "--name-only", "--no-renames", f"{base}...HEAD", "--"], cwd=root,
                             capture_output=True, text=True, check=False, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if run.returncode != 0:
        return None
    return sorted({line.strip() for line in run.stdout.splitlines() if line.strip()})


SCAN_ROOTS = ("Drawer",)  # the hub code and tests the impact map reads
SCAN_MAX_BYTES = 2_000_000


def _imports(text: str) -> set[str]:
    """Each module name part a Python text imports: import a.b as c, from a.b import (x, y) on many lines,
    importlib.import_module('a.b'), and a mock target patch('a.b.f')."""
    names: set[str] = set()
    for line in re.findall(r"(?m)^[ \t]*import[ \t]+([^\n#;]+)", text):
        for item in line.split(","):
            names.update(item.split(" as ")[0].strip().split("."))
    for module, rest in re.findall(r"(?m)^[ \t]*from[ \t]+([\w.]+)[ \t]+import[ \t]+(\([^)]*\)|[^\n#;]*)", text):
        names.update(module.split("."))
        names.update(word for word in re.findall(r"\w+", rest) if word != "as")
    for module in re.findall(r"(?:import_module|patch|patch\.dict|patch\.object)\(\s*['\"]([\w.]+)['\"]", text):
        names.update(module.split("."))
    names.discard("")
    return names


def _index(root: Path) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """(file name -> the hub files whose text names it, module name -> the hub Python files that import it)."""
    by_name: dict[str, set[str]] = {}
    by_import: dict[str, set[str]] = {}
    for top in SCAN_ROOTS:
        for path in (root / top).rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            try:
                if path.stat().st_size > SCAN_MAX_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rel = path.relative_to(root).as_posix()
            for token in set(re.findall(r"[\w.-]+", text)):
                by_name.setdefault(token, set()).add(rel)
            if path.suffix == ".py":
                for name in _imports(text):
                    by_import.setdefault(name, set()).add(rel)
    return by_name, by_import


def _named_test(rel: str) -> str:
    """The test file named for rel: Drawer/hub-land.py -> Drawer/test_hub_land.py."""
    return (Path(rel).parent / f"test_{Path(rel).stem.replace('-', '_')}.py").as_posix()


def impact(root: Path, changed: list[str], full_on: list[str], always: list[str] = ()) -> tuple[list[str] | None, str]:
    """(the hub test files the changed files reach, why); None for the full set: a core file, an unmapped
    change, no changed file or no test file reached. A changed file reaches each hub file that names it or imports
    its module; each file reached reaches each file that imports its module and its named test, on to the end."""
    if not changed:
        return None, "no changed file"
    core = [rel for rel in changed if any(fnmatch.fnmatch(rel, pat) for pat in (*CORE, *full_on))]
    if core:
        return None, f"core file changed: {', '.join(core)}"
    tests = set(hub_tests_all(root))
    by_name, by_import = _index(root)
    picked, unmapped = set(), []
    for rel in changed:
        seen, frontier = {rel}, [rel]
        while frontier:
            target = Path(frontier.pop())
            # A name in a module's text links the changed file only: past it, a docstring that names a script
            # would pull in most of the hub. A test that names a reached file (runs it by path) links at any hop.
            named = by_name.get(target.name, set())
            users = ({_named_test(target.as_posix())} | named) & tests
            if target.as_posix() == rel:
                users |= named
            if target.suffix == ".py":
                users |= by_import.get(target.stem, set())
            for user in users - seen:
                seen.add(user)
                frontier.append(user)
        hits = seen & tests
        if not hits and not (Path(rel).name.startswith("test_") and rel.endswith(".py")):
            unmapped.append(rel)
        picked |= hits
    if unmapped:
        return None, f"unmapped change: {', '.join(unmapped)}"
    if not picked:
        return None, "no test file reached"
    picked |= set(always) & tests  # a test that walks the whole tree checks every file
    return sorted(picked), f"{len(changed)} changed file(s)"


def scanner_missing(files: list[str] | None, root: Path = HUB) -> str:
    """A HUB RED line when gitleaks is not installed and the secret-scan tests would run (they skip without it, and
    a skip is no proof); else ''. files None: every hub test file."""
    if shutil.which("gitleaks") or (files is not None and SCAN_TEST not in files) or not (root / SCAN_TEST).is_file():
        return ""
    return f"smoke: HUB RED {SCAN_TEST}: gitleaks is not installed (brew install gitleaks; winget install gitleaks on Windows); its secret-scan tests would skip"


def hub_tests(root: Path = HUB, workers: int = 1, files: list[str] | None = None,
              env: dict[str, str] | None = None, stats: dict | None = None) -> list[str]:
    """One HUB RED line per hub test file under root that fails, times out or does not start; [] when all pass.
    files: only these (root-relative); None: every hub test file. At most workers files run at once. env: the
    test processes' environment (None: this one), always with HARNESS_NO_LOCAL_TOML=1: a hub test never reads
    the checkout's harness.local.toml, whose roots would send its writes into live state, and with a temp HOME
    (harness_config.test_home): no hub test reaches the real state or config roots. The Flight session vars a
    card-run leg inherits are dropped, as conftest.py drops them under pytest (incident 091c2450). stats, when given,
    gets files, ran (tests run) and skipped, read from each file's unittest summary."""
    files = hub_tests_all(root) if files is None else files
    # ignore_cleanup_errors: on Windows a sqlite file or a git child may still hold a handle when the tests end.
    home = tempfile.TemporaryDirectory(prefix="hub-test-home-", ignore_cleanup_errors=True)
    flight = {harness_config.env_name(name, harness_config.load(root)) for name in ("FLIGHT", "FLIGHT_ONESHOT")}
    env = {k: v for k, v in (os.environ if env is None else env).items() if k not in flight}
    env = harness_config.test_home({**env, harness_config.NO_LOCAL_VAR: "1"}, home.name)

    def red(rel: str) -> tuple[str | None, int, int]:
        """(the HUB RED line or None, tests run, tests skipped) of one file."""
        try:
            run = subprocess.run([sys.executable, "-B", "-m", "unittest", rel], cwd=root, capture_output=True,
                                 text=True, check=False, timeout=HUB_TEST_TIMEOUT, env=env)
        except (OSError, subprocess.SubprocessError) as error:
            return f"smoke: HUB RED {rel}: did not run ({error})", 0, 0
        text = run.stderr + run.stdout
        ran, skipped = RAN.findall(text)[-1:], SKIPPED.findall(text)[-1:]
        counts = (int(ran[0]) if ran else 0, int(skipped[0]) if skipped else 0)
        if run.returncode == 0:
            return None, *counts
        tail = [line for line in text.splitlines() if line.strip()][-1:] or ["no output"]
        return f"smoke: HUB RED {rel}: exit {run.returncode} ({tail[0][:200]})", *counts

    with home, ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(red, files))
    if stats is not None:
        stats.update(files=len(files), ran=sum(r[1] for r in results), skipped=sum(r[2] for r in results))
    return [line for line, _, _ in results if line]


def checkout(project: Path) -> Path:
    """The product checkout: the nearest folder at or above the project that holds .git, else the project."""
    return next((folder for folder in (project, *project.parents) if (folder / ".git").exists()), project)


def product_step(cfg) -> tuple[list[str], Path] | int | str:
    """(argv, folder) of the product step; a SKIP line when none is set up; 2 when the config is broken."""
    command = str(cfg.get("product", {}).get("gate_command") or "").strip()
    if not command:
        return "smoke: SKIP product step: [product] gate_command is empty; only the hub tests ran"
    source, value = harness_config.product_path(cfg)
    if not value:
        return "smoke: SKIP product step: [product] path is unset; only the hub tests ran"
    project = harness_config.path(value)
    if not project.is_dir():
        print(f"smoke: {source} {project} is not a folder")
        return 2
    try:
        argv = harness_config.gate_argv(command)
    except ValueError as exc:
        print(f"smoke: [product] gate_command does not parse: {exc}: {command}")
        return 2
    except OSError as exc:
        print(f"smoke: [product] gate_command cannot start: {exc}: {command}")
        return 2
    if not argv:
        return "smoke: SKIP product step: [product] gate_command is empty; only the hub tests ran"
    return argv, checkout(project)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="smoke.py")
    parser.add_argument("--impact", metavar="BASE",
                        help="run only the hub test files the diff from BASE can reach (core or unmapped: all)")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    cfg = harness_config.load(HUB)
    step = product_step(cfg)
    if isinstance(step, int):
        return step
    files = None
    if args.impact:
        changed = changed_files(HUB, args.impact)
        full_on = cfg.get("smoke", {}).get("full_on") or []
        full_on = [full_on] if isinstance(full_on, str) else [str(p) for p in full_on]
        always = cfg.get("smoke", {}).get("always_run") or []
        always = [always] if isinstance(always, str) else [str(p) for p in always]
        files, why = impact(HUB, changed, full_on, always) if changed is not None else (None, f"no diff against {args.impact}")
        if files is None:
            print(f"smoke: FULL: {why}", flush=True)
        else:
            print(f"smoke: IMPACT {len(files)} of {len(hub_tests_all(HUB))} hub test file(s) for {why} against "
                  f"{args.impact}: {' '.join(files) or 'none'}", flush=True)
    with run_tmp.run_folder(cfg) as env:
        return run_steps(cfg, step, files, env)


def run_steps(cfg, step, files, env) -> int:
    """The hub tests, then the product step, each child with env (the run folder as its temp folder)."""
    missing = scanner_missing(files)
    stats: dict = {}
    reds = [missing] if missing else hub_tests(workers=max_parallel(cfg), files=files, env=env, stats=stats)
    # One summary line, so a green run shows what it ran and what it skipped.
    print(f"smoke: {stats.get('files', 0)} files, {stats.get('ran', 0)} tests run, "
          f"{stats.get('skipped', 0)} skipped, {len(reds)} red", flush=True)
    if reds:
        print("\n".join(reds))
        print(f"smoke: {len(reds)} hub test file(s) red; the product step did not start")
        return 1
    if isinstance(step, str):
        print(step)
        return 0
    argv, root = step
    try:
        code = subprocess.run(argv, cwd=root, check=False, env=env).returncode
    except OSError as exc:
        print(f"smoke: the product step did not start: {exc}")
        return 1
    if code == 0:
        return 0
    if code == blocked_exit(cfg):
        return code
    print(f"smoke: the product step exited {code} in {root}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
