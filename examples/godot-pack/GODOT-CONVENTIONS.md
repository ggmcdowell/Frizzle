# Godot conventions

The Godot pack law. `Drawer/product-init.py --pack godot` copies it into the game repo. It binds every session that
edits the game repo. The core PRD rules map: ENG-13 = PRD-1, ENG-15 = PRD-2, ENG-11 = PRD-3, ENG-8 = PRD-4,
ENG-20 = PRD-5.

Needs: a Godot 4 binary (`GODOT`, `godot` on PATH, or /Applications/Godot.app; `brew install --cask godot`) and a game
Git repo with `origin` on `main`. The pack ships no editor MCP server, so no rule here tells a session to call an editor tool.

## Two roots
- ENG-1 two-roots — Keep rules, docs and tools in the hub; keep only buildable code and assets in the game repo.
  Put game values (project path, editor MCP name and URL) in `harness.local.toml`. Read versions from the game
  repo (`project.godot`), never from a doc.

## Import
- ENG-6 refresh-first — Import after a file write made outside the editor: `godot --headless --path <project> --import`
  (ENG-10).

## One driver
- ENG-8 seat-driver — Drive the editor only from the session that holds the Flight seat. The machine has one
  editor, on `[product] path`; a product worktree has none. Every editor tool call is driving: a read, a test
  run, play mode, a quit. A Workflow leg never calls an editor tool: that makes a second driver.
- ENG-10 project-lock — A headless Godot run is a driver: it writes `.godot/` and imports. Beside the open
  editor it is safe; Godot takes no project lock.

## Git and the gate
- ENG-11 pull-first — Fetch and fast-forward the game repo before your first edit in a session.
- ENG-13 gate-green — Green only when `[product] gate_command` (`gate.sh`) passes and the proof holds. A leg
  that runs zero tests is red. A new skip fails the gate. A golden with no committed key fails; it never
  regenerates its key. Any change under a gate path after the run makes the proof stale. A lint finding
  warns; the `hooks/pre-commit` gdformat fixer does style.
- ENG-15 push-gate — The game repo's tracked `.githooks/pre-push` refuses a push without a green verdict:
  `.gate-verdict`, written by `tools/gate.sh`, names the tree of each pushed commit (PRODUCT-CONVENTIONS.md PRD-2).
  A verdict that cannot be read is red. The hub hook `Drawer/product-hooks/pre-push` runs it.

## Data and owners
- ENG-16 integer-data — Never let a float become a hash input, an ordering key or a dictionary key. Keep a
  value compared across a save boundary as an integer; write a fraction as a scaled integer.
- ENG-20 name-an-owner — Give new behavior an owner class. Never add a policy domain to a hotspot coordinator.
- ENG-21 data-driven — Keep gameplay values in data only, never also in code as a constant, default, fallback
  or shadow copy. Code keeps five bands: determinism pins, structural invariants, vocabulary, bootstrap
  pointers, engine policy. Formulas stay code; their weights are data. A live path that cannot resolve
  content fails loudly, never falls back to a fixture.
- ENG-26 serialized-data — Never rename a serialized field or an exported property without keeping its old
  key readable, or a migration of every `.tscn` and `.tres` that stores it.
