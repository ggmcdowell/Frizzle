---
name: memory
description: "Search and write hub memory: design, decisions, taste, lessons, facts, tasks. Triggers: 'remember this', 'mem add', 'what do we know about', 'meeting transcript', 'audio memo'."
---

# memory — search and write mem

One SQLite file on this machine holds the hub memory. `Drawer/mem/mem.py` is the one tool. Use the
MCP server `mem` (search, get, add, list_cards, set_status) or the CLI:

`.venv/bin/python -B Drawer/mem/mem.py <command>`

No approval, no judge.

## Parts

| Part | Read it when |
|---|---|
| `.claude/skills/memory/meeting.md` | you take in a meeting transcript or audio memo of a design talk |

## Search first

Search before a design, content, behavior, UI or process choice.

1. `mem search "<query>"` (add `--kind K` to narrow).
2. `mem get <id>` for the full row; `--follow` returns the live card at the end of its supersede chain.
3. `mem list --ready [--tag T] --json` for work to take: open, unclaimed tasks with every depends_on closed, each with `chain` (open tasks that wait on it), chain-first then priority.

## Kinds

- `design` — the human's intent. Needs `--source feedback:<the human's note>`.
- `decision` — a choice the design left open, made by a session.
- `taste` — one keep or kill line from the human's feedback. The body starts `KEEP:` or `KILL:`; `--topic` names its subject; `--source` quotes the note.
- `lesson` — a pitfall or edge case that a later session can hit.
- `fact` — a present-tense truth about the hub or the product.
- `task` — work: `--priority p0-p4`, `--status`, `--depends-on <id>`. The body takes the form
  `<name>: <what>. Why: <gap>. Done when: <end state>`.
- incident record — a `task` with `--tag incident`: what broke, impact, suspected cause. Put the tag
  incident only on an incident record. The fix's lesson closes it:
  `mem add --kind lesson --closes <incident id> --body "<text>"`.
  For a record on git history or a delete, put a `LOST-WORK CHECK: <command or audit> -> <result>`
  line in the lesson (a record that names filter-branch, a force push, reset --hard, refs/original, rm, prune or gc); mem refuses the close without it.

## JSON contract

Every verb takes `--json`: one compact JSON line on stdout (`add` and `status` print the row). A refusal
with `--json` prints `{"error": "..."}` on stdout and exits 2. Without `--json`, text and stderr stay as
they are: `add` prints `<id8> <kind>` first, which `Drawer/incident.py` reads.

## Add

- LIB-6 plan-first — Plan the clean final state of memory before the first mutation.

`mem add --kind K --body "<text>" [--title T] [--topic T ...] [--source S]`

- Write one thing per row. Name its topics.
- A task may carry `--model`, `--effort` and `--area`: a word from harness.toml [gates] allowed_models, [routing] rungs and [project] task_skills. Another word is refused.
- A card's words are its writer's: write your own rows; Flight orders and links them after.
- A near-duplicate is refused. Supersede it, or pass `--new` when the two rows differ.
- The text lint refuses a new row (a supersede too): a task not in the form, a body over 1800 chars (task)
  or 600 (other kinds), an ISO date, a user-home or volume path (a `~` path passes), a `File:123` cite.
  An incident record skips it.

## Change

A body is never edited. Write a new row that replaces the old one:

`mem add --kind K --body "<new text>" --supersedes <old id>`

- Read the old row with `mem get` first. Never guess an id.
- Replace a row that your work proves wrong at once.
- Change a `design` row only when the human's feedback asks for it. Name the change in the build report.

## Task status

`mem status <id> <todo|doing|parked|done|dropped>`

## Begin and close a task

`Drawer/task-lifecycle.py` is the one checked path. Each verb prints one JSON line; a refusal is `{"error"}`, non-zero.

1. `task-lifecycle.py begin <id>`: claims the task; prints `claim_id`, the card and `done_when`.
2. Push each commit to origin/main first.
3. `task-lifecycle.py complete <id> --claim-id C --evidence '<4+ words naming the sha>' --commit <repo>=<full sha> [--path P] [--lesson L]`.
   `<repo>` is `hub` (this hub checkout), `product` (the product repo of `[product] path`) or a checkout path; another bare name is refused.
   A close with no file change takes `--no-file-changes '<why>'` instead of `--commit`.
   It refuses a wrong claim_id, a sha not on origin/main and a lesson that names a commit id; the task stays doing.
4. `task-lifecycle.py check-close <id>`: CLOSED, or `{error}` while a claim is held or a done task has `delivered` 0.

`release`, `interrupt` and `note` wrap the mem verbs below.

## Claims, liveness and notes

A claim is live while its session has a live `~/.claude/sessions` row on this machine.

- `mem interrupt <id>` parks a task whose claim session is dead: the claim clears, the worktree stays. A live claim is refused.
- `mem reap [--dry-run] --json` interrupts each dead claim. `--dry-run` lists them and changes nothing.
  It fails closed: a claim it cannot judge (no session, no sessions folder, an unreadable row) is `state: unknown`, never parked.
- `mem note <id> "<text>" [--stop]` steers a running task. `--stop` tells it to halt.
- `mem notes <id> [--since N] --json` lists notes after row `n` = N; each row has `stop`. Read it at each step.
- `mem log <id> --json` prints every task_log row of a task.
