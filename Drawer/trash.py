"""trash for Windows: move each named file or folder to the Recycle Bin (root AGENTS.md SAF-4).

macOS and Linux have a trash command. On Windows, seat.py shims writes ~/.local/bin/trash, a Git Bash
script that runs this file, so `trash <path>` works in every session there.

  <python> Drawer/trash.py [-rfv] <path> [<path> ...]

The options of rm and of the macOS trash are accepted and ignored. Exit 0 when every path went, 1 when one
stayed (each failure is named on stderr), 2 on no path.
Cost: this file. Maintainer: hub (whoever ports a tool to a new platform).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import oscompat  # noqa: E402


def main(argv):
    paths, literal = [], False
    for arg in argv:
        if not literal and arg == "--":
            literal = True
        elif not literal and arg.startswith("-") and len(arg) > 1:
            continue
        else:
            paths.append(arg)
    if not paths:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    failed = 0
    for path in paths:
        try:
            oscompat.recycle(path)
        except OSError as exc:
            print(f"trash: {path}: {exc}", file=sys.stderr)
            failed = 1
    return failed


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
