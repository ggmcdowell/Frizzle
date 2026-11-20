# Harness manual

## New hub

`Drawer/new-hub.py` stamps a new hub from this template's committed head:

```
.venv/bin/python -B Drawer/new-hub.py --dest <folder> --name <slug> --display "<Display Name>" [--origin <url>] \
  [--state-root <folder>] [--config-root <folder>] [--copy-secrets]
```

- The slug is 2 to 16 lowercase letters and digits. The env prefix is the slug in capitals.
- The stamp keeps the template's Git history. Remote `template` points to the template's origin; `--origin`
  sets `origin`. Nothing is pushed.
- Rewritten: harness.toml [project], [mem] db, env_file, export_dir, [names] denylist; the
  pyproject and uv.lock name `<slug>-hub`; the marker file; the generated settings. One commit holds them.
- --state-root and --config-root set the two roots (default `~/.local/share/<slug>-hub` and `~/.config/<slug>-hub`).
  [mem] db and export_dir sit under the state root; env_file and the name deny list under the config root.
- By default the stamp writes only under --dest: the stamp prints one `skip <path>` line per private file. With
  `--copy-secrets` the template's name deny list and mem env_file go to the new paths at mode 600 (Mac and Linux; on Windows the profile ACL protects them). They are never printed.
- Refused, with no write: a bad slug, the template slug, a dest inside the template, a used dest, a used
  state_root or config_root.
- Next, in the stamp: the setup skill, steps 1 to 7. The stamp gets its own memory DB.

## Upgrade a stamped hub

`Drawer/hub-upgrade.py` takes the template's newer commits into a stamped hub. The stamp keeps remote `template`.
Run it in a clean task worktree of the stamp (git-flow skill, step 2), never in the live main checkout:

```
.venv/bin/python -B Drawer/hub-upgrade.py
```

- It fetches `template/main` and merges it with no commit. A conflict in `.claude/settings.json` only (both sides
  re-rendered it) is settled by `settings-sync.py --apply`. Any other conflict aborts the merge and prints
  `REFUSED conflict: <files>`: nothing changes; settle that file in the template or the stamp, then run it again.
- The stamp's `[project]` name, env_prefix and marker_file must survive, else the same abort.
- It commits the merge and prints `UPGRADED <sha>`. Land the branch as any other (git-flow skill, step 2.4).
- Refused with no write: the live main checkout, a dirty tree, no `template` remote, the template itself, and a stamp
  that already holds `template/main`.

## Memory host

One local file holds the memory: `[mem] db`, by default `<[project] state_root>/hub.db` (SQLite, WAL).
Each hub takes its paths from its own `harness.toml` [project] name and state_root.
`Drawer/mem/mem.py` is the one tool. Run each command from the hub root.

```
.venv/bin/python -B Drawer/mem/mem.py digest     # what the session start prints
.venv/bin/python -B Drawer/mem/mem.py snapshot   # one snapshot now
.venv/bin/python -B Drawer/mem/mem.py doctor     # make the DB, load sqlite-vec, name the search mode: one OK or FAIL line
.venv/bin/python -B Drawer/mem/mem.py seed       # the starter cards (Drawer/mem/seed/starter.jsonl), only into an empty DB
.venv/bin/python -B Drawer/mem/mem.py reembed    # embed the cards that have no vector (added with no key)
```

The OpenAI key is optional. With none, search runs on keywords alone; after you add the key to `[mem] env_file`,
`reembed` gives the earlier cards their vectors.

The live hub is the main worktree of the Git checkout that holds `mem.py`, when it holds the marker file. A clone
in any folder is its own live hub; `mem.py` in a task worktree of it hands each live-DB command to the main clone.

### Nightly

Session start runs `mem.py nightly --if-stale` (a no-op while the last export is under 24 h old), and `mem doctor` warns when
the export is older. Run `.venv/bin/python -B Drawer/mem/mem.py nightly` by hand, from the hub root, at any time. It does three things:

1. `VACUUM INTO` a snapshot in `<state_root>/mem-snapshots`.
2. Export every row to `cards.jsonl` in its own Git repo, `[mem] export_dir`
   (by default `<state_root>/mem-export`), and commit it there.
3. When `[mem] export_remote` is set (optional, in `harness.local.toml`), push that repo to it: a bare repo, for example
   `~/<[project] name>-mem-export.git` written as an absolute path, outside the hub data folder. Never use another
   hub's export repo.

The export never goes in the hub repo: the name check refuses it.

### Restore

Never restore over the live file. Move it aside first.

1. Stop the sessions that write memory.
2. Import the export into a fresh file: `.venv/bin/python -B Drawer/mem/mem.py import <export_dir>/cards.jsonl`.
3. Read it back: `mem list --limit 5` and `mem search "<a known card>"`.

A snapshot in `mem-snapshots` is a full SQLite file. Copy one to the db path to restore it whole.
