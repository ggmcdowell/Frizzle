# Godot pack

An example product pack: it points the hub at a Godot game repo. The core never loads it. Nothing here runs
until you opt in.

| File | What it is |
|---|---|
| `GODOT-CONVENTIONS.md` | the Godot rules (the ENG ids), condensed |
| `gate.sh` | the product gate: a headless import, then the headless smoke test. Green needs a `SMOKE PASS` line and no `SCRIPT ERROR`. No Godot exits 75 (BLOCKED). Green on a clean checkout writes `.gate-verdict` |
| `hooks/pre-push` | the push gate (PRD-2): refuses a push unless `.gate-verdict` names the tree of the pushed commit |
| `hooks/pre-commit` | the gdformat fixer for staged `.gd` files; it never refuses a commit, and warns when gdformat is missing |
| `template/` | a minimal `project.godot`, a passing `tests/smoke_test.gd` and the `.gitignore` lines |
| `harness.pack.toml` | the `[product]` lines for a Godot game |

## Opt in

One command, from the hub root, after setup:

```sh
.venv/bin/python -B Drawer/product-init.py --path ~/Projects/MyGame --pack godot --local-origin
```

`--origin <url>` names your own origin in place of `--local-origin` (a bare repo beside the folder). The command:

1. finds Godot (`GODOT`, then `godot` on PATH, then /Applications/Godot.app) and prints its version. With none it
   prints `brew install --cask godot` and exits 75 (BLOCKED) before it changes anything. It installs nothing;
   run it again after you install.
2. makes the folder and its Git repo on `main` with the origin, copies the pack into it (`tools/gate.sh`,
   `.githooks/pre-commit`, `.githooks/pre-push`, `GODOT-CONVENTIONS.md`, `project.godot`, `tests/smoke_test.gd`,
   `.gitignore` lines), commits and pushes `main`. A file already there is kept.
3. writes the `[product]` lines of `harness.pack.toml` into the main hub checkout's `harness.local.toml`.
4. binds the product hooks (`core.hooksPath`) and renders the box settings.
5. runs the first gate and prints one JSON line: `state` is `ready`, `red`, `blocked` or `failed`.

A second run changes nothing (`changed` is empty). Then run `/smoke`: it runs the hub tests, then the gate.

The hub environment holds gdformat: `uv.lock` pins gdtoolkit, and the hub product hook puts the hub `.venv/bin` first on
PATH. The pre-commit hook warns while gdformat is missing.
The pack ships no editor MCP server. `mcp_server` names one; `--mcp-url <url>` registers one you run.
