# Hub health — audit and sweep

Part of the flight skill. Owner: harness. Refresh step 2 runs it once a day, in the first Refresh after
17:00 local, before the digest. Flight runs each mode as one REPL script and reads only the compact
returns of its legs (FLT-2): keep the `transcript.py list` output and each leg row in variables, and return only
the distilled report. It has no feedback mode: `Drawer/incident.py` covers faults.

## Audit — the last 10 sessions

1. In the script, run `<python> Drawer/transcript.py list` into a variable. Take the newest 10 top-level hub
   transcripts; drop your own session id. Put them in 3 to 5 groups by size, about 1 to 2 MB each (a Flight
   transcript can pass 30 MB: give it its own group).
2. Run the `fan-out` workflow: one reader leg per group. The reader brief: run
   `<python> Drawer/session_digest.py <file>` for each file; never print a whole transcript. Return rows
   `{session8, class, line, evidence}`, class failure, waste, delegation or candidate. The reader asks:
   - failure: tool errors, denials, retries, dead ends, a wrong path, a failed or abandoned leg.
   - waste: oversized results, re-reads, redundant legs, token use from `message.usage`.
   - delegation: solo grind (long shell runs, a retry past two failures, a wide sweep with no Workflow), a
     `run_in_background` shell or an Agent spawn, a Workflow whose result the turn did not read.
   - candidate: a repeated sequence of steps that a script or a skill could do.
3. Add one more leg to the same fan-out. It runs `<python> Drawer/transcript.py codemode --exclude <your session id>`
   and `codemode --legs --exclude <your session id>`, and returns both JSON lines.
4. Run the `one-leg` workflow for the distiller. It dedupes the rows and ranks them by sessions hit, then by
   count. It writes the long form to `Drawer/project-health/project-health-<YYYY-MM-DD>.txt` (git-ignored,
   local). It returns the transcript ids read, at most 10 audit rows, the codemode top category and its
   root fix, the leg types below the 90% REPL bar, and the findings that are already fixed.
5. Log the codemode top category and root fix: `<python> Drawer/flight-hooks.py log decision '<category>: <root fix>'`.
   Order the root fix, and order a profile or brief fix for each leg type below the bar (SKILL.md section 2).

## Sweep — stale files, dead pointers, logs with secrets

The sweep is read-only. Run the `fan-out` workflow with one hunt leg per ground; a hunt leg writes nothing and
returns a candidate list: path, mtime, one-line signal. A candidate is a lead, not a verdict.
- Stale files: tracked hub files with a stale signal (plan text with a past date, superseded text, a dead
  task id, a retired skill name, a copy of a canonical home); untracked or ignored loose files in `Drawer/` and
  the hub root older than 30 days. A tracked file needs a stale signal: age alone is not enough.
- Dead pointers: `<python> Drawer/harness-conform.py pointers`.
- Logs with secrets: `gitleaks dir --redact --no-banner -c .gitleaks.toml --report-format json --report-path <temp>/<uuid8>-leaks.json <path>`
  on `Drawer/*.log`, `Flight/log.jsonl` and `Drawer/project-health/`. Return the path, line and
  rule id only, never the match. Put the report in a temp folder outside the hub and trash it after the leg.

Then one cold validate leg (`one-leg`) reads each candidate and sorts it: stale, sensitive, pointer-only or
live. For each one it runs `<python> Drawer/mem/mem.py search "<exact path>" --json`: a current row that names
the path makes it `standing <id>`, never a Delete. A `Keep verdict` row for the path drops it.

Never touch (copy this list word for word into each hunt brief and the validate brief): `AGENTS.md`,
`PRODUCT-CONVENTIONS.md`, both SCRATCHPAD.md pads, `.claude/` (pointer fixes only), the mem database,
`Flight/log.jsonl`, `Flight/compactions/`, a gate log that is a live append target (scan it; delete it only by
verdict), a test that is the only check of a risk, any task worktree or other files of another session, the
product repo, and each file changed in the last 7 days.

## Delete rule

The health run deletes nothing. For each validated stale or sensitive candidate, Flight decides Delete or
Keep, per file or per group, and records it as one row: `mem add --kind decision 'Delete verdict <path|group>: <why>'`
or `'Keep verdict <path|group>: <why>'`. A Keep row stops the next sweep from asking again. A Delete runs only
as an ordered hub task, never in the health run:
- A tracked file: `git rm` in a hub task worktree, then fix each pointer to it; a re-grep finds zero.
- An untracked or ignored file: trash, only after a dead-target proof: no live session or claim owns it,
  `lsof` shows no process that holds it (on Windows, a rename of the file is refused while it is open: that is the proof), and it is older than 7 days. A missing lock is no proof.

## Close

- Put each finding (audit row, stale candidate, dead pointer, leak path) on its own line under
  `## Flight health <YYYY-MM-DD>: backlog` at the end of `Flight/SCRATCHPAD.md`. The State backlog gets one
  count line that points to that heading.
- Write the two report rows:
  `<python> Drawer/flight-hooks.py log report 'health audit <date>: <n> transcripts, <n> rows, top <category>'`
  `<python> Drawer/flight-hooks.py log report 'health sweep <date>: <n> stale, <n> dead pointers, <n> leak paths, <n> deleted'`
