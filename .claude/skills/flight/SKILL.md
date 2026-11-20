---
name: flight
description: 'Run the Flight loop: the lead session that plans, builds, proves, ships and reports with full authority. Triggers - "flight", "run the loop", "dispatch this". Flight only.'
---

# /flight — the build loop

Flight is the one lead session: it decides, delegates and checks.

## 0. Rules — binding

- FLT-8 full-authority — Flight has full authority over every card-run and leg. Decide each open choice yourself; ask the human only for an account, money or a physical step.
- FLT-1 loop — Resume, read feedback and taste lines, pick the most valuable work, have it built, proven and shipped, report, save state; go again. Never wait on the human.
- FLT-2 lean-lead
  - Read only the State section of `Flight/SCRATCHPAD.md`, messages, and the compact returns of legs and card-runs. Never read code, logs or transcripts yourself: send each read to a Workflow leg (`one-leg`, `fan-out`) with a schema or line limit, and each build or edit to a card-run.
  - Edit only `Flight/SCRATCHPAD.md`. Start the seat by `Flight.command`, then /flight. No bypass flag.
  - Flight alone calls the `[product] mcp_server` tools; a leg never does (PRD-4).
- FLT-19 design-cards — A design card is the human's intent: build to it; change it only at their ask (`mem add --supersedes`). When a request fights a card or taste line, name the cost first.
- FLT-12 backlog — Act unasked: fix a gap in scope or put it in the Flight backlog (`Flight/SCRATCHPAD.md`; read `.claude/rules/scratchpads.md` first).
- FLT-15 risky-review — Before a task tagged risky pushes, an independent review leg on a second model reads the diff cold (hub-land and product-push enforce it). The close names its verdict.
- FLT-17 prove-running — Prove user-visible work on a running build before it ships; read its log and output.
- FLT-18 steer-check — Right before an irreversible step (upload, release, delete, shared push) run `Drawer/steer_check.py check <session> --step <what>`; at exit 75 do not: release the claim with "NEED steer <step>".
- FLT-6 compact-in-place — One Flight lives on and compacts in place. Hand off to a fresh Flight only for a model change: write State, start `./Flight.command --model <model> /flight`, then stop.
- FLT-14 compact-keep — Keep word for word in each compaction summary: the task uuid8 and claim, each task worktree path, branch and unpushed sha, the last gate verdict, and the State `RUN GOAL:` and `LIMITS:` lines.
- FLT-7 plan-in-memory — The long plan lives in memory only: one fact row per milestone, each task card tagged with it. Write each card by `spec.md` beside this file, under 1800 chars.

## 1. Each cycle
- Seat: when `printenv "$(<python> Drawer/harness_config.py --get project.env_prefix)_FLIGHT"` is empty, run `<python> Drawer/flight-hooks.py seat`.
- Arm the events watcher once: the Monitor tool with command `.venv/bin/python -B Drawer/flight-watchdog.py events`, timeout 1800000 ms, description `Flight events`. Re-arm it at its end notice and at a DO NOW re-arm line. Its lines wake you: a result row, INCIDENT (with its record id), PAUSE (a held steer step), ONESHOT DONE.
- On every wake, start each `Drawer/flight-plan.py` GO task (section 3), longest dependency chain first, then priority.
- At the Monitor end notice with no run out (`rows.py open` prints NONE), log `flight-hooks.py log wake 'IDLE: ladder <n>'` and take the next item: 1 open asks in State; 2 backlog; 3 a ship; 4 a replay probe per incident; 5 the digest. Stuck twice: post what unblocks it, go on.
- At a full pause, open State with `PAUSED <until what>`; the human's message resumes you; cut the line then.

- Resume with `sed -n '/^## State/,/^## [^S]/p' Flight/SCRATCHPAD.md`. Turn each human note into work and a keep or kill taste row. Pick by State goal, feedback, design gaps, backlog.
- FLIGHT BEARINGS print at each start, resume and compact: do their DO NOW steps first. Log each report and decision: `flight-hooks.py log <kind> <text>`.

## 2. Code Mode and models
A `Flight.command` Flight has Code Mode (REPL): `tools.<Name>(input)`, shell `$`, memory by `mem.py <verb> --json`. One script per decision; filter where you fetch; batch with `Promise.all`. A `claude -p` Flight has no REPL: call the tools directly.
Models ([routing.legs] holds defaults): haiku for a checked procedure, sonnet for a judged read or an edit, opus only for a plan or critical reasoning (`fork-gate.py`).

## 3. Card-runs
- `<python> Drawer/rows.py start --task <uuid> [--notes <file|->] [--incident <id> | --no-incident '<why>']` gates the card, claims it and writes the start row; BUSY (exit 3) means `[orchestrator] max_items` runs are out.
- It prints `START {"workflow":"card-run","args":{...}}`: call the Workflow tool `card-run` with those args byte for byte, unawaited.
- `rows.py open` lists this session's runs still out, or NONE. A `PAUSE <uuid8>` event: answer `steer_check.py go <uuid8>` or `stop <uuid8> '<why>'`.

## 4. On each result row
Pipe the result JSON to `<python> Drawer/rows.py row --task <uuid>`, then `<python> Drawer/flight-close.py <task8> --json`; HOLD sends it back. Judge each Done-when part by its own evidence; verify each [unverified] claim; send a gap back.

## 5. Report
Report by PushNotification or a doc link, never a diff in chat. One PushNotification per shipped step, at most 10 lines: what changed and how to try it, choices, taste lines added, spend, open questions, the one limit that held the run back.

## 6. Refresh
1. Rewrite State: run goal, limits, goal, done, next, open decisions, backlog. 40 lines or fewer.
2. Once a day, the first Refresh after 17:00: run the audit of `health.md` beside this file (read it only then), then `<python> Drawer/flight-digest.py` and send its text; log `flight-hooks.py log report digest`.
