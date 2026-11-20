"""Name check: refuses a commit that adds a denied name or an absolute home path.

The deny list lives outside git, one per hub, at [names] denylist in harness.toml. One rule per
line; a line starting with # is a comment. Matching is case-insensitive.
  word          a word rule: no ASCII letter or digit may touch either end; a space, -, _ or .
                inside the rule matches any run of those (so "a b" also finds a_b, a-b, ab)
  sub:text      a substring rule: matches anywhere, even inside a longer identifier
  re:pattern    a raw regex
A missing, unreadable or empty deny list is a failure, never a pass.

  <python> Drawer/name-check.py                 what the next commit adds: the staged added lines,
                                                and the path of each added, copied or renamed file
  <python> Drawer/name-check.py --all           the whole index (an audit; an old hit shows here only)
  <python> Drawer/name-check.py --files F...    these files on disk
The pre-commit hook runs the default form, so an old hit in a file the commit leaves alone never
blocks an unrelated commit.
  --denylist PATH                               another deny list (tests)
Exit: 0 clean, 1 hits, 2 deny list missing, unreadable or empty.
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_config  # noqa: E402

SEP = re.compile(r"[\s_.\-]+")
HOME_PATH = re.compile(r"(/Users/|/home/|[A-Za-z]:\\Users\\)[A-Za-z0-9._-]+")


def compile_rule(line):
    if line.startswith("re:"):
        return re.compile(line[3:], re.I)
    if line.startswith("sub:"):
        return re.compile(re.escape(line[4:]), re.I)
    parts = [re.escape(p) for p in SEP.split(line) if p]
    return re.compile(r"(?<![A-Za-z0-9])" + r"[\s_.\-]*".join(parts) + r"(?![A-Za-z0-9])", re.I)


def load_rules(path):
    """[(line, regex)], or raise SystemExit(2) when the list is missing, unreadable or empty."""
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8")
    except OSError as err:
        raise SystemExit(f"name-check: deny list unreadable: {path}: {err.strerror}") from None
    rules = []
    for raw in text.splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            try:
                rules.append((line, compile_rule(line)))
            except re.error as err:
                raise SystemExit(f"name-check: bad rule {line!r}: {err}") from None
    if not rules:
        raise SystemExit(f"name-check: deny list is empty: {path}")
    return rules


def index_blobs(root):
    """(path, text) for every file in the index. Binary blobs are skipped."""
    listing = subprocess.run(["git", "-C", str(root), "ls-files", "-s", "-z"],
                             capture_output=True, check=True).stdout.decode()
    entries = [e.split("\t", 1) for e in listing.split("\0") if e]
    shas = [meta.split()[1] for meta, _ in entries]
    batch = subprocess.run(["git", "-C", str(root), "cat-file", "--batch"], input="\n".join(shas).encode(),
                           capture_output=True, check=True).stdout
    pos = 0
    for meta, path in entries:
        header_end = batch.index(b"\n", pos)
        size = int(batch[pos:header_end].split()[2])
        blob = batch[header_end + 1:header_end + 1 + size]
        pos = header_end + 1 + size + 1
        yield path, None if b"\0" in blob else blob.decode("utf-8", "replace")


HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def staged_changes(root):
    """(path, path_is_new, [(line number, text)]) for each file the index changes against HEAD.
    Only the added lines; a binary file gives no lines."""
    git = ["git", "-C", str(root), "-c", "core.quotePath=false"]
    status = subprocess.run([*git, "diff", "--cached", "--name-status", "-z", "--no-renames", "--diff-filter=ACMRT"],
                            capture_output=True, check=True).stdout.decode("utf-8", "replace").split("\0")
    new_paths = {status[i + 1] for i in range(0, len(status) - 1, 2) if status[i] in ("A", "C", "R")}
    diff = subprocess.run([*git, "diff", "--cached", "--no-renames", "--diff-filter=ACMRT", "-U0", "--no-color",
                           "--no-ext-diff", "--src-prefix=a/", "--dst-prefix=b/"],
                          capture_output=True, check=True).stdout.decode("utf-8", "replace")
    files, path, lines, n, header = [], None, [], 0, False
    for line in diff.split("\n"):
        if line.startswith("diff --git "):
            if path is not None:
                files.append((path, path in new_paths, lines))
            path, lines, header = None, [], True
        elif header and line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("@@"):
            m = HUNK.match(line)
            n, header = (int(m.group(1)) if m else 0), False
        elif not header and line.startswith("+") and path is not None:
            lines.append((n, line[1:]))
            n += 1
    if path is not None:
        files.append((path, path in new_paths, lines))
    return files


def scan_changes(changes, rules):
    hits = []
    for path, path_is_new, lines in changes:
        if path_is_new:
            hits += [f"{path}: path matches {rule!r}" for rule, rx in rules if rx.search(path)]
        for n, line in lines:
            hits += [f"{path}:{n}: matches {rule!r}" for rule, rx in rules if rx.search(line)]
            if HOME_PATH.search(line):
                hits.append(f"{path}:{n}: absolute home path")
    return hits


def disk_files(paths):
    for p in paths:
        blob = Path(p).read_bytes()
        yield p, None if b"\0" in blob else blob.decode("utf-8", "replace")


def scan(files, rules):
    hits = []
    for path, text in files:
        for rule, rx in rules:
            if rx.search(path):
                hits.append(f"{path}: path matches {rule!r}")
        if text is None:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for rule, rx in rules:
                if rx.search(line):
                    hits.append(f"{path}:{n}: matches {rule!r}")
            if HOME_PATH.search(line):
                hits.append(f"{path}:{n}: absolute home path")
    return hits


def main(argv):
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--denylist")
    ap.add_argument("--files", nargs="+")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("-h", "--help", action="store_true")
    args = ap.parse_args(argv)
    if args.help:
        print(__doc__.strip())
        return 0
    cfg = harness_config.load()
    try:
        rules = load_rules(args.denylist or harness_config.path(cfg["names"]["denylist"]))
    except SystemExit as stop:
        print(stop, file=sys.stderr)
        return 2
    root = cfg["derived"]["root"]
    if args.files:
        hits = scan(disk_files(args.files), rules)
    elif args.all:
        hits = scan(index_blobs(root), rules)
    else:
        hits = scan_changes(staged_changes(root), rules)
    for hit in hits:
        print(hit, file=sys.stderr)
    if hits:
        print(f"name-check: {len(hits)} hit(s). Replace each name with a config value.", file=sys.stderr)
        return 1
    print(f"name-check: clean ({len(rules)} rules)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
