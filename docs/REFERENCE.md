# Reference

The detail behind the root `README.md`: the full setup, memory, Flight, the scripted run, the environment
variables and the tests. `Drawer/test_readme.py` checks these sections with the README.

## Setup

1. Clone the hub into any folder outside Desktop, Documents and Downloads. It must be a Git clone
   with an `origin` that has `main` and that you can push to: every land pushes to `origin main`. Make your own
   copy with `Use this template` on GitHub (or push a copy to a new repository of your own), clone that, or point
   a clone at it with `git remote set-url origin <your repo URL>` (setup pushes `main` to an empty repository
   itself). A plain clone of the public template has a read-only `origin`, and `bind-git` stops with `origin not writable`. To start a new hub from this one, stamp it with
   `Drawer/new-hub.py --origin <your repository url>` plus `--dest`, `--name` and `--display` (setup step 0).
2. Give this hub its own roots first when another hub with the same slug is on this machine (a second
   clone or a stamp of this template). The roots in `harness.toml` (`[project] config_root` and
   `state_root`) hold one hub's memory DB, state and deny list, and `/setup` opens the DB there.
   Create `harness.local.toml` (git-ignored; it overrides any `harness.toml` value, and task worktrees
   read the main checkout's copy):

   ```toml
   [project]
   config_root = "~/.config/my-hub"
   state_root = "~/.local/share/my-hub"

   [mem]
   db = "~/.local/share/my-hub/hub.db"
   env_file = "~/.config/my-hub/mem.env"
   export_dir = "~/.local/share/my-hub/mem-export"

   [names]
   denylist = "~/.config/my-hub/name-denylist.txt"

   ```

   `mem doctor` records which hub owns a DB. It fails when another hub owns it.
3. Start Claude Code at the hub root and type `/setup`. The setup skill runs each command: it builds
   `.venv` with `uv sync`, checks the tools, copies the deny list, renders `.mcp.json`, configures the
   seat, checks and seeds memory and binds Git. Add your own private names to the deny list it copies.
   The seat step also writes to your global Claude Code config, `~/.claude.json` (or
   `$CLAUDE_CONFIG_DIR/.claude.json`): it marks the hub root and its task worktree folders as trusted,
   so a session there does not stop at the folder trust dialog.
4. Quit Claude Code fully, start it again at the hub root and type `/smoke`. The smoke skill checks
   the seat.
5. Optional: a separate product repo (empty `[product] path`: the hub is the product). One command makes the
   folder and its Git repo, writes the `[product]` lines into `harness.local.toml`, binds the product hooks and
   runs the first product gate: `.venv/bin/python -B Drawer/product-init.py --path ~/Projects/MyProduct
   --local-origin` (`--origin <url>` names your own origin). Add `--pack godot` for the Godot pack
   (`examples/godot-pack/README.md`): it finds Godot, or prints `brew install --cask godot` and exits 75. With
   your own repo and `[product] gate_command`, commit a push gate `.githooks/pre-push` to it: a push is refused
   without one (`PRODUCT-CONVENTIONS.md` PRD-2; `examples/godot-pack/hooks/pre-push` is a template).

## Memory

`mem` (`Drawer/mem/mem.py`) is one local SQLite file (`[mem] db`). It holds design cards,
decisions, taste lines, lessons, facts and tasks. Search fuses SQLite FTS5 keywords with
OpenAI embeddings. The key is optional: put `OPENAI_API_KEY` in `[mem] env_file`. With no key
or no network, search runs on keywords alone; after you add the key, `mem reembed` gives the earlier
cards their vectors. The clone can sit in any folder.

Backup: session start runs `mem nightly --if-stale`, at most once in 24 hours. It makes a snapshot of the database
and commits a `cards.jsonl` export in its own Git repo (`[mem] export_dir`, never the hub repo). `mem doctor` warns
when the export is older than 24 hours. Install no scheduler. Set `[mem] export_remote` in `harness.local.toml` to a
bare repo or folder on another disk, so the export survives this disk.

Each machine has its own memory. The export only pushes; no command pulls or merges it, so cards edited on two
machines do not merge. To restore, or to start a hub on another machine: clone the hub, run `/setup`, then run
`.venv/bin/python -B Drawer/mem/mem.py import <export_dir or export_remote>/cards.jsonl` into the fresh database
(import skips ids it already holds). Pick one machine as the memory host. The restore steps are in
`.claude/skills/harness/MANUAL.md`.

## Start Flight

```sh
./Flight.command     # or double-click Flight.command in Finder
```

`Flight.command` at the hub root opens `claude` there with Code Mode on and waits; type `/flight` to start the lead session. The first
start on a machine shows the Code Mode prompt once; answer it yourself. Claude Code takes the Code Mode switch only from
the shell it starts in, a `--settings` file or your user settings, never from the project `.claude/settings.json`: a plain
`claude` then `/flight` runs Flight with plain tools unless your own settings turn Code Mode on. A `claude -p` run has
none. On a checkout that setup has not finished, the launcher starts `/setup`. Flight plans tasks from memory and runs each GO task as a card-run Workflow,
at most `harness.toml` [orchestrator] max_items at once. Nothing runs with permissions bypassed: setup renders
`.claude/settings.local.json` (git-ignored) with the extra folders and a short allow list.

## Scripted run

One run, then exit: set `<PREFIX>_FLIGHT_ONESHOT=1` for a headless Flight, where `<PREFIX>` is `harness.toml`
[project] env_prefix.

```sh
PREFIX=$(.venv/bin/python -B Drawer/harness_config.py --get project.env_prefix)
env "${PREFIX}_FLIGHT=Flight" "${PREFIX}_FLIGHT_ONESHOT=1" claude --permission-mode acceptEdits -p '/flight run the starter task'
```

The Flight bearings then carry a ONE-SHOT RUN line: Flight runs the GO task(s) as card-runs and takes no new
work. It writes each result row, checks and closes each task, reports and ends its turn without a TaskStop of
the watcher. The watcher prints `ONESHOT DONE` and exits when it saw a run out and now none is out. The Stop
hook lets the stop pass when no run of this session is out and the watcher has printed `ONESHOT DONE`.
Without the variable, Flight loops as before. `Drawer/e2e.sh` is the one-shot acceptance test: it runs this
command and needs no GUI. `Drawer/e2e.sh --pack` applies the whole `examples/godot-pack/` pack to a scratch product repo (all
`[product]` lines, `gate.sh` with a stub Godot, the product hooks) and lands one product change through
`Drawer/product-push.py`.

## Environment variables

| Variable | Effect |
|---|---|
| `<PREFIX>_MEM_DB` | Path of the memory database; replaces `[mem] db`. Tests set it to a scratch file. A bare `MEM_DB` is no longer read: mem stops and names the new variable. |
| `<PREFIX>_MEM_EMBED` | `off`: no embedding calls; search uses keywords only. A bare `MEM_EMBED` stops mem the same way. |
| `MEM_LIVE_WRITE` | `1`: code outside the live hub may write and migrate the live database. |
| `OPENAI_API_KEY` | The embeddings key; else the `OPENAI_API_KEY=` line in `[mem] env_file`. |
| `DOC_GATE`, `FORK_GATE` | `off` or `dry`: switch the doc gate or the fork gate off, or judge without logging. |
| `HARNESS_CONFORM` | `off` or `dry`: the conform gate of a commit. The Bash gate refuses a git command that sets `off`. |
| `HARNESS_NO_LOCAL_TOML` | `1`: ignore `harness.local.toml`. The test runners set it. |
| `<PREFIX>_FLIGHT`, `<PREFIX>_FLIGHT_ONESHOT` | Mark a Flight session; one-shot run (Scripted run). `<PREFIX>` is `[project] env_prefix`. |
| `E2E_TIMEOUT` | Seconds before `Drawer/e2e.sh` stops claude (default 3600). |
| `CLAUDE_CONFIG_DIR` | Claude Code config folder; the seat step writes its trust rows there. |

## Tests

```sh
.venv/bin/python -B Drawer/smoke.py
.venv/bin/python -B -m pytest Drawer -q
```
