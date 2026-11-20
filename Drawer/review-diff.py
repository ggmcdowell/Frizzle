"""review-diff -- the review args for one task worktree's change: the diff, its hash and its files.

    <python> Drawer/review-diff.py <hub or product worktree>

Prints `none` when HEAD changes no file against its merge base with origin/main. Otherwise it
writes that diff at <[project] state_root>/review/<head12>.diff and prints one JSON line: repo, head, base, hash
(the SHA-256 of the diff bytes), diffPath and files. A risky task's review reads that line; the push then binds to
it: product-push.py and hub-land.py --reviewed <hash> recompute the hash after each rebase and push nothing on a
different hash (state review, exit 3).
The diff and the hash hold every file the change touches, with no path filter: a filter let a risky review pass
a diff that left out Art/MANUAL.md. The diff form is fixed: no user diff setting (noprefix,
color, context, algorithm, order, renames, submodule form, an external diff, textconv or a user attributes file)
changes the bytes, and hunk headers lose their function text. A clone with no origin/main diffs against the empty
tree.
Exit 0 on a line or none, 1 on a git fault, 2 on bad arguments.
Why: a rebase after review can reach main unreviewed.
Owner: Harness.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

DRAWER = Path(__file__).resolve().parent
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
HUNK = re.compile(r"(?m)^(@@ -[0-9,]+ \+[0-9,]+ @@).*$")
DIFF = ["-c", "core.quotePath=true", "-c", "diff.suppressBlankEmpty=false", "-c", "diff.orderFile=" + os.devnull,
        "-c", "core.attributesFile=" + os.devnull, "diff", "--submodule=short", "--no-ext-diff", "--no-textconv", "--no-renames", "--no-relative", "--full-index", "--no-color",
        "--src-prefix=a/", "--dst-prefix=b/", "--unified=3", "--inter-hunk-context=0", "--diff-algorithm=myers",
        "--indent-heuristic", "--ignore-submodules=none"]
USAGE = "usage: <python> Drawer/review-diff.py <hub or product worktree>\n"


class Refused(Exception):
    pass


def git(repo, *args):
    """git's stdout as bytes decoded by hand: text mode would turn a lone CR into a newline."""
    run = subprocess.run(["git", *args], cwd=str(repo), capture_output=True)
    if run.returncode != 0:
        raise Refused("git %s failed: %s" % (" ".join(args), run.stderr.decode("utf-8", "replace").strip()[:300]))
    return run.stdout.decode("utf-8", "replace")


def merge_base(repo, head):
    """The merge base with origin/main, else the empty tree when the clone has no origin/main."""
    try:
        origin = git(repo, "rev-parse", "--verify", "-q", "refs/remotes/origin/main").strip()
    except Refused:
        return EMPTY_TREE
    return git(repo, "merge-base", head, origin).strip()


def build(repo, state_root):
    """The review args for HEAD of repo against its merge base with origin/main; None when the change touches no
    file. The diff file goes under state_root/review."""
    repo = git(os.path.abspath(str(repo)), "rev-parse", "--show-toplevel").strip()
    head = git(repo, "rev-parse", "HEAD").strip()
    base = merge_base(repo, head)
    files = sorted(p for p in git(repo, "diff", "--name-only", "-z", "--no-renames", "--no-relative",
                                  "--ignore-submodules=none", base, head).split("\0") if p)
    if not files:
        return None
    diff = HUNK.sub(r"\1", git(repo, *DIFF, base, head))
    digest = hashlib.sha256(diff.encode("utf-8")).hexdigest()
    out = Path(state_root).expanduser() / "review"
    out.mkdir(parents=True, exist_ok=True)
    path = out / (head[:12] + ".diff")
    path.write_text(diff, encoding="utf-8", newline="\n")
    return {"repo": repo, "head": head, "base": base, "hash": digest, "diffPath": str(path), "files": files}


def main(argv=None):
    args = sys.argv[1:] if argv is None else list(argv)
    if args in (["--help"], ["-h"]):
        print(__doc__)
        return 0
    if len(args) != 1 or args[0].startswith("-") or not os.path.isdir(args[0]):
        sys.stderr.write(USAGE)
        return 2
    sys.path.insert(0, str(DRAWER))
    import harness_config
    try:
        found = build(args[0], harness_config.path(harness_config.load(DRAWER.parent)["project"]["state_root"]))
    except Refused as exc:
        sys.stderr.write("review-diff: %s\n" % exc)
        return 1
    print("none" if found is None else json.dumps(found))
    return 0


if __name__ == "__main__":
    sys.exit(main())
