# Product conventions

The law of the product repo: the Git checkout at `harness.toml` [product] path. It binds every session that
edits the product repo. An empty [product] path means the hub is the product: then these rules apply to the hub.
This file is a template: add your product's own rules under each part. Land each change as a Git commit.

## The gate
- PRD-1 gate-green — Call a change green only when [product] gate_command passes in the product checkout
  (`{python}` is the hub interpreter). A runner exit code that ran zero tests is not green. An empty
  gate_command means no product gate: the hub tests are the gate.
- PRD-3 pull-first — Fetch and fast-forward the product repo before your first edit in a session. Other
  sessions push to it.

## The push gate
- PRD-2 push-gate — The gate binds at push. The product repo's tracked `.githooks/pre-push` refuses a push
  without a green proof that covers the pushed files.
  - `Drawer/product-hooks/pre-push` (the hub hook on `core.hooksPath`) runs it. With gate_command set and
    that file missing, the hub hook refuses the push.
  - Push only by `<python> Drawer/product-push.py <product worktree> --task <task8>`: it rebases, runs the
    gate when no proof covers HEAD, runs the steer check and pushes.
  - Proof format: a green gate_command writes `.gate-verdict` at the product repo root (git-ignored): one line, the
    tree id of the commit it passed (`git rev-parse HEAD^{tree}`), written only when tracked files match HEAD. The
    `.githooks/pre-push` passes a push only when that line equals the tree of each pushed commit (the pushed ids
    arrive on stdin). `examples/godot-pack/` ships the gate and the hook.

## Owners
- PRD-5 name-an-owner — Give new behavior an owner: a module, a class or a file, old or new. Never add an
  unrelated domain to a large coordinator file; move old policy out slice by slice, when real work next
  touches it.
- PRD-4 one-driver — One driver per [product] mcp_server tool server: only the Flight seat calls it, never a leg.
