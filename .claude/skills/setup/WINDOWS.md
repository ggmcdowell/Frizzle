# Windows setup

Read by the setup skill (step 1) on Windows. Supported: Windows 10 or 11 on x64. Windows on ARM64 is not
supported: `sqlite-vec` has no `win_arm64` wheel, so `uv sync` fails there. Claude Code runs the Bash tool,
the hooks and the statusline through Git Bash, so every command in this hub runs as written in Git Bash.

## Install

1. Git for Windows (Claude Code needs it): `winget install --id Git.Git -e`
2. Claude Code, native installer, in PowerShell: `irm https://claude.ai/install.ps1 | iex`
3. The tools: `winget install --id astral-sh.uv -e`, `winget install --id Gitleaks.Gitleaks -e` and
   `winget install --id GitHub.cli -e` (for the GitHub sign-in). Open a new terminal afterwards so the PATH updates.
4. Long paths: `git config --global core.longpaths true` (the branch policy install sets it too on Windows).
   Optionally turn on `LongPathsEnabled` in Windows. Task worktrees sit at `<hub>_wt/<uuid8>-<slug>`: a long path
   can pass the 260 character limit.
5. Folders: keep the hub, `<hub>_wt`, the product repo and the two roots (`config_root`, `state_root`) out of
   OneDrive-synced folders (sync locks open files, which breaks file replace and the SQLite memory DB) and out
   of Desktop, Documents and Downloads. Use a short path, for example `C:\dev\<hub>`.
6. Godot product only: `winget install --id GodotEngine.GodotEngine -e`, then set the `GODOT` environment
   variable to the `*_console.exe` of that install.

## First run

1. In Git Bash at the hub root: `uv sync`.
2. Once after `uv sync`: `.venv/Scripts/python.exe -B Drawer/runtime/seat.py shims`. It writes the
   `.venv/bin/python` Git Bash shim and `~/.local/bin/trash` (the `trash` command: the Recycle Bin). After that
   every command in the docs and skills runs as written, in Git Bash, with `.venv/bin/python` and `trash`.
   `seat.py configure` rewrites the shim when a later `uv sync` rebuilds `.venv`.
3. Start Claude Code at the hub root and type `/setup`, then continue with the steps of the setup skill.

## Launch Flight

Double-click `Flight.cmd` at the hub root: it runs `Flight.command` through Git Bash. Or run `./Flight.command`
in Git Bash. Type `/flight`.

## Differences from Mac and Linux

- `<python>` stays `.venv/bin/python -B` in every command text; on Windows it is the shim above.
- The name deny list and `mem.env` are not set to mode 600 on Windows (NTFS has no mode bits): the ACL of your
  user profile protects them.
- `~/.claude.json` is `%USERPROFILE%\.claude.json`.
- A file that is open cannot be deleted or renamed on Windows. A refused rename is the dead-target proof for
  a cleanup (`lsof` does not exist there).
