#!/usr/bin/env python3
"""Remove what one Drawer/e2e.sh run left in the user's Claude Code config.

    /usr/bin/python3 Drawer/e2e_clean.py <e2e temp folder>

Two traces, both keyed by the temp folder: the projects[<folder>] trust rows in the client config
($CLAUDE_CONFIG_DIR/.claude.json, else ~/.claude.json) and the transcript folders in <config dir>/projects whose
name starts with the folder path with every non-alphanumeric character turned into a dash. The transcript folders
go to the trash. It takes only a folder named frz-e2e.*, and it stops with exit 1 when trash is missing.
Standard library only: e2e.sh runs it with the system Python.
"""
from __future__ import annotations

import glob
import json
import ntpath
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import oscompat  # noqa: E402  (standard library only, like this file)

TRASH = ("/usr/bin/trash", "/opt/homebrew/bin/trash")


def trash_command():
    return shutil.which("trash") or next((p for p in TRASH if os.path.exists(p)), None)


def config_paths(env=None):
    """(the client config file, the folder of the transcript projects)."""
    env = os.environ if env is None else env
    folder = env.get("CLAUDE_CONFIG_DIR")
    home = os.path.expanduser("~")
    return (os.path.join(folder or home, ".claude.json"), os.path.join(folder or os.path.join(home, ".claude"), "projects"))


def under(key, temp):
    """Whether a projects key is temp or a path below it. Windows writes either slash and any drive-letter case."""
    if oscompat.WINDOWS:
        key, temp = (ntpath.normcase(ntpath.normpath(p)) for p in (key, temp))
        return key == temp or key.startswith(temp + "\\")
    return key == temp or key.startswith(temp + "/")


def clean(temp, config, projects, trash):
    """Drop the trust rows under temp from config and trash the transcript folders of temp. Returns (rows, folders)."""
    base = os.path.basename(os.path.normpath(temp))
    if not base.startswith("frz-e2e."):
        raise ValueError("not an e2e temp folder: " + temp)
    temp = os.path.realpath(oscompat.native_path(temp))
    rows = 0
    if os.path.isfile(config):
        with open(config, encoding="utf-8") as handle:
            value = json.load(handle)
        held = value.get("projects") if isinstance(value, dict) else None
        if isinstance(held, dict):
            gone = [k for k in held if under(k, temp)]
            for key in gone:
                del held[key]
            rows = len(gone)
            if gone:
                mode = os.stat(config).st_mode & 0o777
                fd, staged = tempfile.mkstemp(dir=os.path.dirname(config), prefix=".claude.json.")
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(value, handle, indent=2)
                    handle.write("\n")
                os.chmod(staged, mode)
                try:
                    oscompat.replace(staged, config)
                except PermissionError:  # Windows: a running client holds the file
                    os.unlink(staged)
                    raise RuntimeError("the client config %s is in use; close Claude Code and run it again" % config)
    folders = sorted(glob.glob(os.path.join(glob.escape(projects), glob.escape(re.sub(r"[^A-Za-z0-9]", "-", temp)) + "*")))
    if folders and oscompat.WINDOWS:
        for folder in folders:
            oscompat.recycle(folder)
    elif folders:
        done = subprocess.run([trash, *folders], capture_output=True, text=True)
        if done.returncode:
            raise RuntimeError("trash failed: " + (done.stderr or done.stdout).strip())
    return rows, len(folders)


def main(argv):
    if len(argv) != 1:
        print(__doc__.strip())
        return 0 if argv in (["-h"], ["--help"]) else 2
    trash = trash_command()
    if not trash and not oscompat.WINDOWS:  # Windows recycles through oscompat
        print("e2e_clean: no trash command on this box (macOS: /usr/bin/trash); the run's traces stay", file=sys.stderr)
        return 1
    config, projects = config_paths()
    try:
        rows, folders = clean(argv[0], config, projects, trash)
    except (OSError, ValueError, RuntimeError) as exc:
        print("e2e_clean: " + str(exc), file=sys.stderr)
        return 1
    print("e2e_clean: removed %d trust row(s) and %d transcript folder(s)" % (rows, folders))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
