"""SessionStart hook: Git freshness, the worktree sweep, the rules check, seat steps, scratch pad seeds and the memory digest.

The context it adds opens with MEMORY DIGEST. Each part fails soft into one line that names the
fault; nothing here blocks a session.

Timing (the manifest gives this hook 240 s): after freshness and its settings render, the digest,
the sweep and the rules check run beside the seat steps. They start after freshness so none reads
a half-rebased tree. test_session_start checks the sum. The memory backup (mem nightly --if-stale) runs beside them.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
DRAWER_DIR = HOOK_DIR.parent
HUB_ROOT = DRAWER_DIR.parent
for entry in (HOOK_DIR, DRAWER_DIR):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))
import oscompat  # noqa: E402

OUTPUT_BUDGET = 1500  # characters of the printed JSON; it is ASCII, so bytes too (it once printed 4102)
# The digest parts to cut first, least important first: a whole line by its lead, else the rows under a header,
# newest kept. Taste rows go last: they steer the work. The freshness, sweep and rules lines are never cut.
CUT_ORDER = ("TOPICS:", "ROWS:", "NEWEST DECISIONS:", "OPEN TASKS", "TASTE:")
KEEP_TOP = ("OPEN TASKS", "TASTE:")  # these sections keep their header and top row; only the last cut takes more

SCRATCHPAD_SEED = (
    "# {name} scratchpad\n\n"
    "This pad is per user and ignored by Git. It is not law. Big plans and working notes\n"
    "belong here; it survives close.\n"
)


SETUP_NOTICE = (
    "HUB SETUP REQUIRED: this checkout has no hub environment (.venv) or no saved seat profile on this machine. "
    "Read .claude/skills/setup/SKILL.md and follow it from step 1. Do nothing else.")
FLIGHT_STATE = "\n## State\nBACKLOG: none.\n"  # the pad a fresh Flight needs: a State section with its backlog line

DIGEST_SECONDS = 45
SWEEP_SECONDS = 60  # the product sweep subprocess, and the deadline the hub task sweep shares with it
RULES_SECONDS = 30  # rules_check's claude --version timeout
NIGHTLY_SECONDS = 60  # mem nightly --if-stale: a snapshot and an export, when the last one is over 24 h old


def run(argv: list[str], timeout: int) -> tuple[int, str, str]:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)
    return done.returncode, done.stdout.strip(), done.stderr.strip()


def setup_needed() -> bool:
    """True when this checkout has no prepared interpreter or no saved seat profile (setup skill steps 1 to 4)."""
    if not oscompat.venv_python(HUB_ROOT).is_file():
        return True
    try:
        from runtime import profile
        return profile.setup_required(HUB_ROOT)
    except Exception:
        return False  # a profile fault is repair, never setup


def bind_hooks() -> None:
    """Before setup, bind core.hooksPath to the tracked hooks, so the first commit meets the secret scan and the
    name check. bind-git later sets the rest; a bound path stays as it is."""
    try:
        if not (HUB_ROOT / ".git").exists():
            return
        if not run(["git", "-C", str(HUB_ROOT), "config", "--get", "core.hooksPath"], 10)[1]:
            run(["git", "-C", str(HUB_ROOT), "config", "core.hooksPath", "Drawer/git-hooks"], 10)
    except Exception:
        pass


def digest() -> str:
    code, out, err = run([sys.executable, "-B", str(DRAWER_DIR / "mem" / "mem.py"), "digest"], DIGEST_SECONDS)
    if code:
        return f"MEMORY DIGEST UNAVAILABLE: {(err.splitlines() or ['exit %d' % code])[-1]}"
    return "MEMORY DIGEST\n" + out


def nightly() -> str:
    """Run the memory backup (mem nightly --if-stale; a no-op while the last export is under 24 h old). '' when it
    ran, was not due or failed soft; the NIGHTLY line only names a fault, and never blocks the boot."""
    try:
        code, out, err = run([sys.executable, "-B", str(DRAWER_DIR / "mem" / "mem.py"), "nightly", "--if-stale"],
                             NIGHTLY_SECONDS)
    except Exception as exc:
        return f"NIGHTLY EXPORT FAILED: {exc}"
    if code:
        return f"NIGHTLY EXPORT FAILED: {(err.splitlines() or ['exit %d' % code])[-1]}"
    return ""


def freshness(source: str) -> tuple[str, bool]:
    """(the freshness text, True when the checkout is current or rebased)."""
    try:
        import git_freshness
        return git_freshness.check(HUB_ROOT, source)
    except Exception as exc:
        return f"GIT FRESHNESS FAILED: {exc}", False


def seat_steps() -> str:
    """The SEAT STEPS line: each stale seat step (a moved uv.lock runs configure), '' when none."""
    try:
        from runtime import seat
        return seat.boot_steps(HUB_ROOT)
    except Exception as exc:
        return f"SEAT STEPS FAILED: {exc}"


def policy():
    """Drawer/git-branch-policy.py, loaded as a module: the sweep runs its task_sweep."""
    spec = importlib.util.spec_from_file_location("git_branch_policy", DRAWER_DIR / "git-branch-policy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sweep() -> str:
    """The SWEEP line when it removed or kept anything, else "". Two passes share SWEEP_SECONDS: the product task
    trees (Drawer/product_worktree.py sweep, a subprocess under that timeout) and the hub task trees
    (git-branch-policy task_sweep, which holds each tree it reaches past the deadline)."""
    deadline = time.monotonic() + SWEEP_SECONDS
    try:
        code, out, err = run([sys.executable, "-B", str(DRAWER_DIR / "product_worktree.py"), "sweep"], SWEEP_SECONDS)
        if code:
            return f"SWEEP FAILED: product sweep exit {code}: {(err.splitlines() or [''])[-1]}"
        product = out.removeprefix("PRODUCT SWEEP:").strip().rstrip(".")
        reports = [product] if product and product != "nothing to remove" else []
        import harness_config
        repo = harness_config.main_checkout(HUB_ROOT)
        if (repo / ".git").exists():
            removed, held = policy().task_sweep(repo, deadline=deadline)
            reports += [f"hub task tree {b} removed" for b in removed]
            reports += [f"hub {h['item']} kept: {h['reason']}" for h in held]
    except Exception as exc:
        return f"SWEEP FAILED: {(str(exc).strip().splitlines() or [type(exc).__name__])[0]}"
    return f"SWEEP: {'; '.join(reports)}." if reports else ""


def rules() -> str:
    try:
        import rules_check
        return rules_check.notice()
    except Exception as exc:
        return f"RULES CHECK FAILED: {exc}. Stop all work and send NEED rules fault."


def seed_scratchpads() -> None:
    for folder, name in ((HUB_ROOT, "Hub"), (HUB_ROOT / "Flight", "Flight")):
        try:
            if name == "Flight":
                folder.mkdir(exist_ok=True)  # a fresh clone has no Flight folder; its first Flight needs a pad
            if folder.is_dir():
                with (folder / "SCRATCHPAD.md").open("x", encoding="utf-8", newline="\n") as handle:
                    handle.write(SCRATCHPAD_SEED.format(name=name) + (FLIGHT_STATE if name == "Flight" else ""))
        except Exception:
            continue


def render(context: str) -> str:
    return json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}})


def fit(digest_text: str, rest: list[str]) -> str:
    """The context within OUTPUT_BUDGET. Cut digest parts in CUT_ORDER (a section's rows from its end), then the
    digest's last characters, and name each cut. The parts in rest are never cut."""
    lines = digest_text.split("\n")
    cuts = {}

    def build():
        note = (f"DIGEST CUT to fit {OUTPUT_BUDGET} chars: "
                f"{', '.join(f'{name} {n}' if n > 1 else name for name, n in cuts.items())}; "
                "run mem digest for the full one.") if cuts else ""
        return "\n\n".join(p for p in ["\n".join(lines).strip(), note, *rest] if p)

    def over():
        return len(render(build())) - OUTPUT_BUDGET

    for lead in CUT_ORDER:
        while over() > 0:
            at = next((i for i, line in enumerate(lines) if line.startswith(lead)), None)
            if at is None:
                break
            end = at + 1
            while end < len(lines) and lines[end].startswith("- "):
                end += 1
            if lead in KEEP_TOP and end <= at + 2:
                break
            if end > at + 1:  # a section: cut its last row
                del lines[end - 1]
                name = lead.rstrip(":") + " rows"
            else:             # a one-line part: cut it whole
                del lines[at]
                name = lead.rstrip(":")
            cuts[name] = cuts.get(name, 0) + 1
    while over() > 0 and any(lines):
        cuts.setdefault("TRUNCATED digest", 1)
        text = "\n".join(lines)
        lines = text[:max(0, len(text) - over() - 1)].split("\n")
    return build()


def main() -> None:
    try:
        raw = sys.stdin.buffer.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}
    source = str(payload.get("source") or "")
    if setup_needed():  # before any digest: mem opens (and creates) the memory DB
        bind_hooks()
        print(render(SETUP_NOTICE))
        return
    if source == "compact":
        print(render(fit(digest(), [])))
        return
    fresh_text, fresh = freshness(source)
    with ThreadPoolExecutor(max_workers=4) as pool:
        memory, swept, ruled = pool.submit(digest), pool.submit(sweep), pool.submit(rules)
        backup = pool.submit(nightly)
        steps = seat_steps() if fresh else ""  # a blocked checkout may hold a half-moved uv.lock
        parts = [fresh_text, swept.result(), ruled.result(), steps, backup.result()]
        digest_text = memory.result()
    seed_scratchpads()
    print(render(fit(digest_text, [p for p in parts if p])))


if __name__ == "__main__":
    main()
