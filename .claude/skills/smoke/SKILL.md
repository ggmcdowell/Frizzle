---
name: smoke
description: "Smoke one set-up seat in a fresh session; also the ship smoke before a hub code change is published. Triggers: 'smoke', 'smoke this seat', 'run the smoke', 'ship smoke'."
---

# smoke — prove one seat in a fresh session

## When it runs

- Seat smoke: the human types `/smoke` in the first new chat after setup (`/setup`) finished, or
  asks for a smoke. It runs at the hub root.
- Ship smoke: before you publish a code change, run only the ship smoke section below.

## Talking to the human

You run every command. Say in plain words what failed and what you do about it.

## Precondition

The session start printed `MEMORY DIGEST`, with no `UPGRADE REQUIRED`
notice. Otherwise stop and tell the human in plain words:

- `HUB SETUP REQUIRED` or `GIT FRESHNESS SETUP REQUIRED`: "Setup has not finished on this
  computer. Please type /setup."
- No `MEMORY DIGEST`: run the seat doctor
  (`<python> -B Drawer/seat-doctor.py`) and show the human its FAIL lines. Its SKIP lines are not
  failures.
- `UPGRADE REQUIRED` (Claude Code is older than 2.1.281 and reads no AGENTS.md): "Claude Code on
  this computer is too old. Please run claude update, then quit Claude completely and reopen it.";
  stop.

Every command below runs from the hub root under the prepared interpreter: `<python>` is
`.venv/bin/python -B`. Never a system `python`:
the probes import the locked `mcp` package, so another interpreter turns healthy components into
FAIL (`the check did not run (ModuleNotFoundError)`).

## 1. Python-side checks

```
<python> Drawer/runtime/seat.py check --json
```

It prints one JSON object: `records`, one per component in fixed order (`memory`, `smoke`), and
`summary`, the count per status. Each record is `{name, status, reason, detail}` with status PASS,
SKIPPED, FAIL or BLOCKED. `check` exits 0 whatever the records say: read the records.

- `memory`: starts the `mem` MCP server from this checkout's `.mcp.json` and reads its tool list.
- `smoke`: runs `[smoke] command` from `harness.toml` at the hub root, limited to
  `[smoke] timeout_seconds`; exit 0 passes. `Drawer/smoke.py` runs each hub test file first: one red
  file prints a `HUB RED` line and fails the smoke before the product step (`[product] gate_command`) starts. SKIPPED `no [smoke] command in harness.toml` is a
  supported seat: no product pack has set a command yet. BLOCKED means no test ran and a human
  must act; the reason says what. It is not a test failure.

The smoke row can run for many minutes. Give the command the longest command timeout, or run it in
the background and wait for its notice.

## 2. Settings and registrations

```
<python> Drawer/settings-sync.py
```

No flags: it checks this seat's generated files and `.mcp.json` and never writes. Exit 0 prints
`in sync: <n> generated files match the manifest`. Exit 1 prints one `DRIFT: <path>` line per
drifted file, then `DRIFT: <k> of <n> generated files`: report those files. Exit 2 prints an
`ERROR:` line: report it. Report any `ORPHAN` line, whatever the exit code. Never `--apply` here.

## 3. Repair loop

For each FAIL record whose reason is exactly `run repair <name>`:

```
<python> Drawer/runtime/seat.py repair <name>
<python> Drawer/runtime/seat.py check --json --only <name>
```

`--only` and `repair` take an exact record name. A name that matches nothing exits 1 and names the
valid set, so an empty result is never a clean re-check.

`repair` answers:

- `<STATUS> <name>: <reason>` (exit 0), then, when it did not pass, the record's detail with the
  repair's note first. A FAIL here means the repair did not fix it: report it, and do not repair it
  again.
- `repair: <name> repeat refused ...` (exit 3): this repair already failed today for the same
  reason. Stop that component and report it. The refusal ends with the day;
  `<python> Drawer/runtime/seat.py repair <name> --clear` ends it sooner. Run that only when the
  human asks, or in a later session once the cause is fixed.
- `seat: another seat command holds <path>` (exit 3, on stderr): another seat command on this
  checkout still ran after 10 s. Wait until none of your seat commands runs, then rerun once.
- `repair: <name> needs no repair`, or `BLOCKED <name>: no automated repair` and its detail line
  (exit 0).

The memory repair is `<python> Drawer/runtime/seat.py repair memory`. When it passed but this
chat's `mem` tools still fail, ask the human to quit the app completely, reopen it on the same
folder and type /smoke again. Otherwise no app restart. Never loop.

Report every other FAIL or BLOCKED record. A smoke FAIL belongs to the product pack that set the
command: report its tail, never edit the command.

## 4. Cleanup

```
<python> Drawer/runtime/seat.py cleanup
```

It ends only the children this checkout's smoke recorded, by verified process identity, and
removes only the scratch folder each recorded child names. It prints nothing when nothing was
recorded; that is normal, because `check` closes its own children. A record without a creation
identity prints `unverified` and is dropped, never killed; `uncertain, record kept` waits for the
next cleanup. Report any line it prints.

## 5. Report

First tell the human, in two or three plain sentences, whether this checkout is ready and what does not
work yet.

Then one table, component per row: status, reason, one line of detail. Every PASS names its
evidence (mem tool count, smoke exit and tail); state anything unverified as unverified.

## Ship smoke

Before you publish a code change:

```
<python> Drawer/runtime/seat.py check --json --only smoke
```

- PASS: publish. Put `smoke PASS`, the command and the last lines of its detail in the completion
  evidence.
- FAIL: do not publish. Fix the cause when the change caused it; otherwise report the record.
- SKIPPED (`no [smoke] command in harness.toml`): publish, and put `smoke not configured` in the
  completion evidence.
- BLOCKED: do not publish yet. Tell the human the reason line in plain words.
