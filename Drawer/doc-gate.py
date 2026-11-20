"""Doc gate: no hiding places in the hub, no write in the live hub, no Claude instruction file, no test edit
during a product gate run, no write in the main product checkout from a session that holds a product worktree.

PreToolUse hook on Write|Edit|MultiEdit|NotebookEdit. Refuses:
  1. a hub write to a HIDING PLACE -- a path segment named attic, archive, backup, rejected,
     old, copy, or a file name carrying .bak, .orig, -old, -copy, -rejected, sync-conflict --
     and any zip/7z/tar inside the hub. The hub and its linked task worktrees are judged alike;
  2. in the live hub -- the main checkout, not a task worktree -- a write to any file Git does not
     ignore: every session's hooks run from its files, so a half-done edit there breaks every
     session. Edit in a task worktree and land it. Git-ignored files (pads, local config) pass;
  3. in a product checkout, a write under a tests/ folder while its gate-verdict.json, written by the
     product gate ([product] gate_command), says running: a gate that lists the tests at its start reds on an
     edit mid-run.
     A running mark older than GATE_STALE_HOURS is a dead gate and passes;
  4. a Claude instruction file (CLAUDE.md, CLAUDE.local.md) in the hub, a task worktree, the product
     repo or a product worktree, or one in the hub root or a parent folder of it (.claude/CLAUDE.md
     too): it turns AGENTS.md off (Drawer/rules_check.py). ~/.claude/CLAUDE.md passes;
  5. in the main product checkout (not a linked worktree of it), any write from a session that a product
     worktree lock names: a task lock or a spawn session lock (Drawer/product_worktree.py). Product code is
     edited only in the session's own product worktree (WRK-10). No session_id in the event, or a Git
     fault listing the trees, passes; a shell write is not judged, as for rule 2.
Every other write passes. Every deny is appended to Drawer/doc-gate.log (box-local). Every reason is at most
gatelib.REASON_CAP characters: the gate name, the problem and the fix.
The law-file shell fence: a Bash write skips rules 2 and 4, so Drawer/project-gate.py refuses a
shell write to a law file (AGENTS, SKILL, PRODUCT-CONVENTIONS, CLAUDE or CLAUDE.local, each .md) -- a redirect,
tee, sed -i or perl -i, cp, mv, ln or install onto one, or an inline python, node, perl or ruby script that names
one and writes (Drawer/git_guard.py law_write_reason). Reads pass; so do a write under ~/.claude and one under a
temp folder outside any checkout.
Set DOC_GATE=off to disable, DOC_GATE=dry to judge without logging.

Drawer/project-gate.py routes the write tools here; this file stays a standalone hook and CLI
(`<python> Drawer/doc-gate.py --check <path>`).

Cost: this file, routed by one hook line in Drawer/settings-manifest.json.
Maintainer: hub (whoever changes the doc rules).
"""
from datetime import datetime, timedelta, timezone
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import product_worktree
import gatelib
import harness_config
import oscompat
import rules_check

HUB = os.path.dirname(HERE)
GATE = gatelib.Gate("doc-gate", HERE, "DOC_GATE")
CFG = harness_config.load(HUB)
WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
GATE_VERDICT = "gate-verdict.json"
GATE_STALE_HOURS = 2

HIDE_SEGMENT = re.compile(
    r"(^|[\\/])(attic|archives?|_archive|backups?|_backup|rejected|old|copy|copies)([\\/]|$)", re.I)
HIDE_NAME = re.compile(
    r"([-_.](bak|orig|old|backup|copy|rejected)\d*([-_.]\d{2,})*|sync-conflict)(\.[A-Za-z0-9]+)?$", re.I)
ARCHIVE_EXT = re.compile(r"\.(zip|7z|rar|tar|tgz|tar\.gz|tar\.bz2|tar\.xz)$", re.I)
ALLOW_HIDE = [  # hub-relative paths where a hiding-place name is the file's own job
    re.compile(r"^Drawer[\\/]never-copy\.txt$", re.I),  # the copy exclude list (Phase 0)
]


def log(kind, tool, what):
    GATE.log("%s %s %s" % (kind, tool, what[:300]))


def default_base():
    """The folder a relative path names when the event carries no cwd."""
    return os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())


def absolute(path, base=None):
    """The absolute path a tool path names from base; None when a variable hides it."""
    path = os.path.expandvars(path)
    if "$" in path:
        return None  # a variable this hook cannot expand resolves at run time, not inside the hub
    path = os.path.expanduser(oscompat.native_path(path))
    return os.path.abspath(path if os.path.isabs(path) else os.path.join(base or default_base(), path))


def inside(p, root):
    """p relative to root with forward slashes, or None when p is outside root."""
    try:
        r = os.path.relpath(p, root)
        if r.startswith(".."):  # a checkout reached through a link (a linked cwd) is still it
            r = os.path.relpath(os.path.realpath(p), os.path.realpath(root))
    except ValueError:
        return None
    if r.startswith(".."):
        return None
    return r.replace("\\", "/")


def worktrees(repo=None):
    """Roots of the linked worktrees of repo (default: the hub's task worktrees), longest first.

    When Git cannot list them the answer is none, so a worktree path passes as outside the hub,
    as every path outside it does: failing closed here would block every write outside the hub
    whenever Git breaks.
    """
    repo = repo or HUB
    try:
        out = subprocess.run(["git", "-C", repo, "worktree", "list", "--porcelain"],
                             capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode:
        return []
    roots = [line[len("worktree "):] for line in out.stdout.decode("utf-8", "replace").splitlines()
             if line.startswith("worktree ")]
    main = os.path.normcase(os.path.realpath(repo))
    return sorted((os.path.normpath(r) for r in roots
                   if os.path.normcase(os.path.realpath(r)) != main), key=len, reverse=True)


def product_root():
    """The Git checkout that holds [product] path, or None when it is unset or in no checkout."""
    project = (CFG.get("product") or {}).get("path") or ""
    if not project:
        return None
    p = os.path.abspath(str(harness_config.path(project)))
    while not os.path.exists(os.path.join(p, ".git")):
        if os.path.dirname(p) == p:
            return None
        p = os.path.dirname(p)
    return p


PRODUCT = product_root()
# The hub is the live main checkout when it holds the .git folder; a task worktree holds a .git file.
LIVE_HUB = os.path.isdir(os.path.join(HUB, ".git"))


def rel(path, base=None):
    """(checkout root, checkout-relative path with forward slashes[, product checkout]), or None outside them all.

    A checkout is the hub, one of its linked task worktrees, or the product repo (PRODUCT). An edit in a
    worktree meets the rules an edit in the hub meets, measured against the worktree's own files.
    """
    if not path:
        return None
    p = absolute(path, base)
    if p is None:
        return None
    r = inside(p, HUB)
    if r is not None:
        return HUB, r
    for root in worktrees():  # only off the hub, so a hub edit spawns no extra Git
        r = inside(p, root)
        if r is not None:
            return root, r
    r = inside(p, PRODUCT) if PRODUCT else None
    if r is not None:
        return PRODUCT, r, PRODUCT
    for root in worktrees(PRODUCT) if PRODUCT else []:  # a task product worktree
        r = inside(p, root)
        if r is not None:
            return PRODUCT, r, root
    return None


def where(root, r):
    """r as a refusal names it: a worktree path carries its worktree."""
    if root == HUB:
        return r
    try:
        label = os.path.relpath(root, os.path.dirname(HUB))  # ValueError: another drive on Windows
    except ValueError:
        label = root
    return "%s: %s" % (label.replace("\\", "/"), r)


def hiding_place(r):
    if any(a.search(r) for a in ALLOW_HIDE):
        return None
    if HIDE_SEGMENT.search(r):
        return "a hiding place (attic, archive, backup, rejected, old, copy)"
    base = r.rsplit("/", 1)[-1]
    if HIDE_NAME.search(base):
        return "a backup-named file (.bak, .orig, -old, -copy, -rejected, sync-conflict)"
    if ARCHIVE_EXT.search(base):
        return "an archive file inside the hub"
    return None


def reason_hide(r, why):
    return "doc-gate: %s is %s. Fix: delete it, or put what lasts in memory (mem add)." % (gatelib.clip(r, 40), why)


def git_ignored(r):
    """True when Git ignores r in the hub. A Git fault reads as ignored: the gate never locks the hub."""
    try:
        done = subprocess.run(["git", "-C", HUB, "check-ignore", "-q", "--", r], capture_output=True,
                              env=harness_config.git_env(), timeout=20)
    except (OSError, subprocess.SubprocessError):
        return True
    return done.returncode != 1


def git_new(r):
    """True when Git does not track r in the hub (a file the write would create). A Git fault reads as tracked."""
    try:
        done = subprocess.run(["git", "-C", HUB, "ls-files", "--error-unmatch", "--", r], capture_output=True,
                              env=harness_config.git_env(), timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 1


def reason_live_hub(r, new=False):
    return ("doc-gate: %s is %s file in the live hub; every session runs from it. Fix: edit in a task worktree "
            "(git-flow skill), then land it." % (gatelib.clip(r, 48), "a new" if new else "a Git"))


def gate_running(checkout):
    """True when checkout's product gate verdict says running and the mark is fresh."""
    try:
        with open(os.path.join(checkout, GATE_VERDICT), encoding="utf-8") as stream:
            doc = json.load(stream)
        if doc.get("status") != "running":
            return False
        started = datetime.fromisoformat(str(doc.get("startedAtUtc", "")).replace("Z", "+00:00"))
    except (OSError, ValueError, AttributeError):
        return False
    return datetime.now(timezone.utc) - started < timedelta(hours=GATE_STALE_HOURS)


def reason_gate_running(checkout, r):
    return ("doc-gate: %s is a test file and the product gate is running in %s. Fix: wait for its verdict; a mark over "
            "%d h old clears." % (gatelib.clip(r, 40), gatelib.clip(checkout, 32), GATE_STALE_HOURS))


def reason_shadow(what):
    return ("doc-gate: %s is a Claude instruction file; Claude Code then skips AGENTS.md. Fix: put the rule in "
            "AGENTS.md or a skill." % gatelib.clip(what, 48))


def judge_path(r, root=HUB, checkout=None):
    if rules_check.is_shadow_name(r):
        return reason_shadow(where(root, r))
    if root == PRODUCT:
        if "tests" in (r.lower() if oscompat.WINDOWS else r).split("/") and gate_running(checkout or PRODUCT):
            return reason_gate_running(checkout or PRODUCT, r)
        return None
    why = hiding_place(r)
    if why:
        return reason_hide(where(root, r), why)
    if root == HUB and LIVE_HUB and not git_ignored(r):
        return reason_live_hub(r, git_new(r))
    return None


def held_product_trees(session):
    """Sorted roots of the product worktrees whose lock names session; [] on no session, no product or a Git fault."""
    if not session or not PRODUCT:
        return []
    try:
        listed = product_worktree.trees(PRODUCT)
    except (OSError, subprocess.SubprocessError):
        return []  # fail open, as worktrees() does: a Git fault never blocks every product write
    return sorted(str(t["worktree"]) for t in listed.values()
                  if (product_worktree.lock_fields(t["locked"]).get("session") or "").lower() == session.lower())


def reason_main_checkout(r, trees):
    return ("doc-gate: %s is in the main product checkout; you hold a product worktree (WRK-10). Fix: edit %s."
            % (gatelib.clip(where(PRODUCT, r), 40), gatelib.clip(os.path.join(trees[0], r), 64)))


def main_checkout_reason(found, session):
    """Rule 5: the refusal for a write in the main product checkout from a session that holds a product worktree."""
    if len(found) != 3 or not PRODUCT or found[0] != PRODUCT or found[2] != PRODUCT:
        return None
    trees = held_product_trees(session)
    return reason_main_checkout(found[1], trees) if trees else None


def run(data):
    """Judge one already-parsed PreToolUse event. The dispatcher calls this."""
    tool = data.get("tool_name", "")
    if GATE.off() or tool not in WRITE_TOOLS:
        return
    inp = data.get("tool_input") or {}
    cwd = data.get("cwd")
    base = cwd if isinstance(cwd, str) and os.path.isabs(cwd) else None  # the session's own folder
    what = inp.get("file_path") or inp.get("notebook_path") or ""
    found = rel(what, base)
    verdict = judge_path(found[1], found[0], *found[2:]) if found else None
    if not verdict and found:
        verdict = main_checkout_reason(found, str(data.get("session_id") or ""))
    p = absolute(what, base) if what else None
    if not verdict and p and rules_check.shadowing(p, HUB):
        verdict = reason_shadow(p)
    if verdict:
        log("deny", tool, what)
        gatelib.deny(verdict)


def main():
    if "--check" in sys.argv:
        target = sys.argv[sys.argv.index("--check") + 1]
        found = rel(target)
        print((judge_path(found[1], found[0], *found[2:]) if found else None) or "passes")
        return
    if GATE.off():
        return
    try:
        data = json.loads(sys.stdin.buffer.read())
    except (json.JSONDecodeError, ValueError):
        return
    run(data)


if __name__ == "__main__":
    main()
