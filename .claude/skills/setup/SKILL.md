---
name: setup
description: "Set up a fresh hub checkout: session start prints HUB SETUP REQUIRED, or a fresh clone has no MEMORY DIGEST. Triggers: 'setup', 'set up this machine', 'fresh clone'."
---

# setup — prepare a fresh checkout on this machine

## When it runs

The human types `/setup` at the hub root. Run this skill when:

- the session boot printed `HUB SETUP REQUIRED`;
- that boot prints no `MEMORY DIGEST`: this clone has no `.venv/bin/python` or no saved seat profile;
- the boot printed `GIT FRESHNESS SETUP REQUIRED`, or the human asks to finish setup.

Run this skill, then nothing else, until the hand-off.

## Talking to the human

You run every command. Ask the human only to:

- enter their Mac password in an installer or permission window;
- sign Git in to GitHub when step 5 asks for it;
- pick a product in step 6: none, or a Godot game and its folder;
- allow the setup commands for this chat;
- quit the app completely and reopen it at the hand-off.

Say in one plain sentence what will appear before each of these. Say in plain words what failed
and what you do about it. Print no secret: never paste a token into the chat.

## Rules

- Run every command from the hub root (the folder that holds `harness.toml` and `AGENTS.md`),
  exactly as written.
- Before step 1, check the hub checkout and the product checkout (`[product] path`, when set): neither
  sits under Desktop, Documents or Downloads. macOS privacy can revoke the agent's write access to
  files there in the middle of a session. One does: tell the human to move it, and stop.
- `<python>` is the hub environment that step 1 builds: `.venv/bin/python`.

## 0. Stamp a new hub

Skip this step on a clone of an existing hub. For a new hub, stamp it from the template hub first. The stamp
needs no product checkout: set `[product]` later with
`<python> -B Drawer/product-init.py --path <dir> --local-origin [--pack godot]`, or leave it empty and the hub is the product.
Run it from the template hub root:

```
<python> -B Drawer/new-hub.py --dest <new hub folder> --name <slug> --display "<Display Name>" [--origin <url>]
```

It prints `STAMPED <folder> <sha>`; `REFUSED <why>` writes nothing. Then run steps 1 to 7 in the new
folder. Harness manual: `.claude/skills/harness/MANUAL.md`, New hub.

## 1. Build the environment

```
uv sync
```

It builds `.venv` from `uv.lock`. Rerunning it is safe. `uv` not found: on a Mac with Homebrew
run `brew install uv`, then rerun; with no Homebrew, tell the human that uv is missing
(<https://docs.astral.sh/uv/>) and stop. Every commit needs gitleaks: run `brew install gitleaks` when step 2 fails on it.

## 2. Detect

```
<python> -B Drawer/seat-doctor.py --source-only
```

The doctor prints one PASS or FAIL line per item (sources, python, git, uv, gitleaks, claude). A FAIL: install
what it names (git, gitleaks, claude 2.1.316 or newer), then rerun step 2; when you cannot, show the human the line and stop.

## 3. Copy the local files

Copy the deny list and render `.mcp.json` (the `mem` server, with this clone's real path). Never print the deny
list's content, and never commit either file.

```
DENY=$(<python> -B Drawer/harness_config.py --get names.denylist); DENY="${DENY/#\~/$HOME}"
mkdir -p "$(dirname "$DENY")"; [ -e "$DENY" ] || cp name-denylist.example "$DENY"; chmod 600 "$DENY"
<python> -B Drawer/settings-sync.py --mcp
```

Add the human's real names and private words to the deny list, one per line. Without the list, or with an
empty one, the commit gate fails closed. A file that exists already stays.

## 4. Configure this checkout

```
<python> -B Drawer/runtime/seat.py init
<python> -B Drawer/runtime/seat.py configure
```

`init` writes this checkout's seat profile once. It prints `init: wrote profile ...` or `init: profile exists ...`; either way, continue.

`configure` first runs `Drawer/settings-sync.py --apply`: the generated project settings and the
local, git-ignored `.mcp.json` (the `mem` server, launched by `<python>`). Then it syncs the hub
environment when `uv.lock` moved, and stamps what it reached. A later boot reruns it on drift and
names the result in its SEAT STEPS line. Read the lines:

- `in sync: ...` or `wrote: ...` and exit 0 (a `configure: ... synced` line may follow) — continue.
- `configure: hub-uv-lock sync failed ...` (exit 1) —
  everything before it stands. Continue to step 5 and report it at the end.
- `seat: another seat command holds <path>` (exit 3) — wait one minute, then rerun once.
- `configure: settings-sync.py failed ...`, `seat: configure failed (...)` or any other exit 1
  line — rerun once; it is safe to rerun. The same line again: show the human the line; stop.

Then check memory and load the starter cards:

```
<python> -B Drawer/mem/mem.py doctor
<python> -B Drawer/mem/mem.py seed
```

`doctor` makes the DB, loads sqlite-vec and prints one line. `OK ...` — continue; `search keyword-only`
is fine with no OpenAI key. `WARN ...` (exit 0) names a stale or missing nightly export, or an OpenAI key the API
refused: show the human the line, then continue. `FAIL mem doctor: db ... belongs to the hub at <path>` — another hub on this machine
owns these roots: write this hub's own roots into `harness.local.toml` (README Setup step 2), then rerun from
step 3. Any other `FAIL ...` (exit 1) — show the human the line; stop. `seed` loads the starter
cards only into an empty DB; a DB that holds cards prints that it skipped.

## 5. Bind Git

Tell the human first: "A GitHub sign-in window may open: please sign in with the account that can
see this hub's repository."

```
<python> -B Drawer/runtime/seat.py bind-git
```

Read the last line:

- `bind-git: ok branch=main` — continue.
- `seat: another seat command holds <path>` (exit 3) — wait one minute, then rerun once.
- `bind-git: GitHub sign-in missing: ...` (exit 1) — ask the human to sign Git in to GitHub (for
  example `brew install gh`, then `! gh auth login` in this chat), then rerun step 5 once. The same line again: show the
  human the line; stop.
- `bind-git: no origin: ...` (exit 1) — ask the human for their own repository URL, run
  `git remote add origin <url>`, then rerun step 5. `bind-git` pushes main to an empty origin itself.
- `bind-git: origin has no main: ...` (exit 1) — that push failed, and the line names the cause. Fix the
  cause, then rerun step 5.
- `bind-git: no main branch: ...` (exit 1) — origin is empty and the checkout is on no main: run `git switch -c main` at the hub root, then rerun step 5.
- `bind-git: origin not writable: ...` (exit 1) — the human cannot push to origin (a clone of the public
  template): they make their own copy with Use this template on GitHub, or create a repository; run `git remote set-url origin <url>`, then rerun step 5.
- `bind-git: failed (exit <n>): <reason>` or `seat: bind-git failed (...)` — show the human the
  line; stop.

## 6. Product

Skip this step when `[product] path` is set. Ask the human one question: no product (the hub builds itself), or a
Godot game. For a game, ask for its folder (default `~/Projects/<Display Name>Game`, outside Desktop, Documents and
Downloads) and their own empty repository URL, or none.

```
<python> -B Drawer/product-init.py --path <folder> --pack godot (--origin <url> | --local-origin)
```

`--local-origin` (no URL) makes a bare repo beside the folder. Read `state` in the JSON line:

- `ready` — continue.
- `blocked` (exit 75) — Godot is missing. Tell the human a password window may open, run
  `brew install --cask godot`, then rerun once.
- `red` — the first gate failed: show the human the line, continue to step 7 and report it.
- `failed` — show the human `step` and `output`; stop.

## 7. Hand off

A running app keeps the settings and PATH it started with. So the hand-off is a full quit of the
app, never a computer restart. Tell the human, with the hub path:

- Claude app: "Setup is done. Now quit Claude completely (Claude > Quit Claude). Then open Claude
  again, open the same folder (<hub path>), start a new chat, and type /smoke."
- Terminal session: "Close this terminal, start `claude` in a new terminal in <hub path>, and type
  /smoke."
- Both: "After the smoke passes, double-click `Flight.command` in <hub path>. It opens Claude
  with Code Mode on and waits: type /flight to start Flight. Its first start asks one Code Mode
  question: answer it yourself."

