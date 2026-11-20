---
name: 'git-flow'
description: "Move work through Git: session close, hub task branch, product worktree. Triggers: 'git flow', 'commit and publish', 'close out git', 'task worktree', 'land the branch'."
---

# /git-flow — hub work forward through Git

`Drawer/git-branch-policy.py` does every check itself: the branch, no upstream, merged
before delete. `<hub>` is the main checkout root; `<wt>` is a task worktree root.

- WRK-11 hub-worktree — Hub code: edit and commit only in a hub task worktree; doc-gate refuses an edit in the live hub. Run the smoke before you land: hub-land runs it (§2 step 4).
- WRK-12 keep-tree — A released, need or died task keeps its tree for its next run; a sweep takes it only once the card is done or dropped.

Every session that edits hub files makes its own task worktree (§2) before its first edit, and
edits and commits only there. Sessions share one `<hub>` index, so a commit there can carry a
peer's staged work.

## 1. Close — every session

Close with §2 step 4. Ignore or delete generated files; never commit one by hand.

Session start and each land run freshness: it takes accepted main and carries every session's
uncommitted work. Act only on a conflict, and resolve it yourself; never ask the human.

## 2. Task worktree — every session

The session stays at the `<hub>` root; only the files move. Never run `git worktree add`,
`git merge` or `git push` for a task branch by hand.

1. In `<hub>`: `<python> Drawer/git-branch-policy.py worktree-add --name <uuid8>-<slug>`.
   `<uuid8>` is the first 8 characters of `$CLAUDE_CODE_SESSION_ID`; worktree-add refuses a name without it.
   A leg shares its session id, so a task tree may take the task form `<task8>-<slug>`: `<task8>` is the card id's first 8 characters, and your session must hold a live `mem` claim on that card.
   worktree-remove refuses a `<task8>-*` tree while its claim is live; own-cleanup and the session-start sweep remove it once the card is done or dropped with no claim.
   `<slug>` is lower-case: letters, digits, `.`, `_`, `-`. That full name is `<name>` below. Read `path` from the JSON line; that is `<wt>`.
2. Edit hub files only under `<wt>`, by absolute path. Doc-gate judges them as it judges `<hub>`.
   Keep `SCRATCHPAD.md` and every other git-ignored file in `<hub>`: worktree-remove deletes them in `<wt>` unasked.
3. Run every hub tool with `<wt>` as the working directory: tests and
   `Drawer/settings-sync.py --apply`.
4. Close with one call, from `<hub>` or `<wt>`:
   `<python> Drawer/hub-land.py --name <name> --task <task8> -m '<message>' -- <paths>`.
   `--task` names your card (spoken work has none); a card tagged risky lands only with `--reviewed <Pass hash>`.
   It stages `<paths>` (relative to `<wt>`; a deleted path as a removal) and commits them in `<wt>`, runs freshness, then `[smoke] land_command` (the impact smoke; else `[smoke] command`) in
   `<wt>` at the tip it pushes, lands (`git-branch-policy.py land`: freshness, probe, steer check, push, main checkout moves),
   then runs worktree-remove. With no message and no paths it lands the commits `<wt>` holds.
   It prints one JSON line; read `state`:
   - `landed`: on origin/main. `removed: false`: run `<python> Drawer/git-branch-policy.py worktree-remove --name <name>` later; `remove_output` says why.
   - `already`: origin/main and `<hub>` held the task before the call; the worktree is gone.
   - `need` (exit 75): a Flight steer STOP; nothing pushed. Release the claim with the reason "NEED steer push" and return status need.
   - `review` (exit 3): the diff is not the one reviewed, or a risky card has no stored Pass; nothing pushed. Run the risky-review workflow on `args`, then land again with `--reviewed <hash>`.
   - `failed`, `step` `stage`: a named path is unknown, or `<wt>` holds uncommitted work. Name it, commit it or delete it.
   - `failed`, `step` `remove`: landed before, but worktree-remove refused; `output` says why.
   - `failed`, `step` `smoke`: the smoke is red (or `blocked: true`: it cannot run here now); nothing pushed. Fix, commit, run the call again.
   - `failed`, `step` `land`: `land_state` and `reason` are the land's own; its output names the fix.
   Run it again after each fix; a step already done is not done twice.

## 3. Product worktree — product repo work

A separate product repo (`harness.toml` [product] path) takes its own worktree per task. An empty path
means the hub is the product: use §2. The `[product] mcp_server` tool server stays one per machine, on
[product] path; a product worktree has none (PRD-4).

1. `<python> Drawer/product_worktree.py add --task <task8>`: `<product repo>_wt/<task8>`, branch `task/<task8>`,
   from origin/main, locked to your task claim. A second add returns the same tree. Read `path` (`<wt>`) and
   `project` (the product folder in it) from the JSON line. A smoke there reads `<env_prefix>_PRODUCT_PATH`: set it to `project`.
2. Edit, build, test and commit product code only in `<wt>` (PRODUCT-CONVENTIONS.md).
3. Push with `<python> Drawer/product-push.py <wt> --task <task8>`: it rebases on origin/main, runs the
   product gate unless a proof covers HEAD (PRD-2), runs the steer check and pushes HEAD to main. A card tagged
   risky pushes only with `--reviewed <hash>`. It prints one JSON line; read `state`:
   - `pushed` or `already`: on origin/main.
   - `need` (exit 75): a Flight steer STOP; nothing pushed. Release the claim with the reason "NEED steer push" and return status need.
   - `review` (exit 3): run the risky-review workflow on `args`, then push again with `--reviewed <hash>`.
   - `failed`: `step` names it. Fix, commit, run the call again.
   A gate can run 10 minutes: start the call through `Drawer/long-job.py`.
4. `<python> Drawer/product_worktree.py remove --task <task8>` removes a clean tree, and its branch once
   origin/main holds its patches; the session-start sweep removes the trees of done tasks.
