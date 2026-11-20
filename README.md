# Spec-driven agentic hub

Status: experimental. macOS only. No support is promised.

This hub is a spec-driven agentic development harness, built on Claude Code. You say what to build; one lead
session (Flight) writes each feature as a spec card, has agents build and test it, and pushes it. Gates, hooks
and tests enforce the rules in `AGENTS.md`.

## Quick start

```sh
git clone <your copy: Use this template on GitHub> my-hub && cd my-hub
Drawer/e2e.sh        # optional, paid: one headless run in a temp clone, the starter card to done (about 8 minutes, about 2 USD)
claude               # type /setup; quit and start again; type /smoke; then double-click Flight.command and type /flight
```

The first-run order is: `Drawer/e2e.sh` (optional), `/setup`, quit and start again, `/smoke`, `/flight`.
`Drawer/e2e.sh` is a paid headless test run: about 2 USD and about 8 minutes. Skip it on a first try. It runs
before `/setup`, so it installs nothing: it needs `uv`, `claude` (signed in) and `trash` on PATH.

## Start a Godot game

1. Install [Homebrew](https://brew.sh), the Xcode command line tools (`xcode-select --install`) and
   [Claude Code](https://code.claude.com/docs) on a Mac, and sign in to Claude Code. Then run `brew install gh`
   and `gh auth login` to sign Git in to GitHub. Setup does not do this. Setup installs uv and gitleaks, and
   Godot for a game.
2. On GitHub, press `Use this template` to make your own copy (it can be private). Clone your copy with `git clone`
   into a folder outside Desktop, Documents and Downloads. Do not use Download ZIP: the hub needs a Git clone
   that can push.
3. In that folder, run `claude` and type `/setup`.
4. When setup asks for a product, pick a Godot game and give its folder, for example `~/Projects/MyGame`.
   Setup installs Godot when it is missing (you approve), makes the game repo with a passing smoke test and
   connects it to the hub. With no repository URL, the game's origin is a local bare repo beside it: point it
   at your own GitHub repo later with `git remote set-url origin <url>` in the game folder.
5. Quit Claude Code fully. Start `claude` again in the hub folder and type `/smoke`.
6. Double-click `Flight.command` and type `/flight`.
7. Tell Flight what to build, for example "a player that moves with the arrow keys". Flight writes the spec,
   builds it, runs the Godot gate and pushes. It does not wait for you: play each build and type feedback in
   the Flight window.

To add the game later, from the hub folder:
`.venv/bin/python -B Drawer/product-init.py --path ~/Projects/MyGame --pack godot --local-origin`.
The pack: `examples/godot-pack/README.md`.

## What is here

- `AGENTS.md`: the rules every session loads.
- `.claude/skills/`: one skill per craft (flight, memory, git-flow, review, harness, ...).
- `.claude/workflows/`: the saved Workflow scripts (card-run, fan-out, find-verify, ...).
- `Drawer/`: the hub tools: memory (`mem`), gates, hooks, the run rows (`rows.py`) and their tests.
- `harness.toml`: every name, path, port and model the code reads.
- `PRODUCT-CONVENTIONS.md`: the product law template: the gate, the push gate and owners.
- `examples/`: an opt-in example product pack for a game engine. The core never loads it.
- `docs/REFERENCE.md`: full setup, memory, Flight, the scripted run, the environment variables and the tests.

Not included: art or asset tools, and an editor tool server for Godot. Add your own as a product tool.

## Requirements

- macOS, with Homebrew and the Xcode command line tools (git)
- [Claude Code](https://code.claude.com/docs) 2.1.316 or newer, signed in. Check with `claude --version`; update
  with `claude update`. `/setup` checks the version and asks you to update below it.
  The CLI is needed; the desktop app alone is not enough. Every `/flight` run spends tokens on your Claude plan
  or API account, and Flight plans on Opus.
- A GitHub account that can push to your copy (`gh auth login`, or a Git credential helper)
- [uv](https://docs.astral.sh/uv/) 0.12 or newer (it installs Python 3.12 or newer)
- [gitleaks](https://github.com/gitleaks/gitleaks) (the pre-commit secret scan)
- Optional: a product repo with a test command (its gate)
- Optional: Godot 4 for a Godot game (setup installs it with `brew install --cask godot`)
- Optional: `trash` for `Drawer/e2e.sh` (`/usr/bin/trash`, or `brew install trash`)
- Optional: an OpenAI API key (memory embeddings). With a key, card text and search queries go to the OpenAI API.

## Safety

- Agents commit and push to your copy's `main` without asking. Run the hub on a machine and account you trust.
- The gates and hooks are guardrails, not a sandbox. The local allow list lets an agent run any git, uv or hub
  Python command in the hub. Nothing runs with permissions bypassed.
- Setup writes outside the clone. It marks folders trusted in `~/.claude.json` (or `$CLAUDE_CONFIG_DIR`): the
  hub, `<hub>_wt` and, with a game, `<game>_wt`. A `_wt` folder stands beside its clone and holds the task
  worktrees (clone `~/Projects/my-hub` gives `~/Projects/my-hub_wt`). It keeps memory and settings in the
  `config_root` and `state_root` folders that `harness.toml` names.
- To remove the hub, delete the clone, `<hub>_wt`, and the two root folders (`config_root` and `state_root` in
  `harness.toml`; `harness.local.toml` can change them). Then delete the `projects` rows of those folders in
  `~/.claude.json`. A game also has its own folder, `<game>_wt` and a local `<game>-origin.git`.
- Each session start pulls your copy's `main`: work in your own copy, never in a plain clone of this repository.
- Report a security problem with GitHub private vulnerability reporting (`SECURITY.md`).

Not affiliated with Anthropic, OpenAI or the Godot Foundation. Names belong to their owners. Pull requests may
not be reviewed: make your own copy and change it freely.

## License

MIT. See `LICENSE`. Third-party programs and services: `THIRD-PARTY.md`.
