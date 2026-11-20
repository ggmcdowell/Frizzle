# Hub rules

- WRK-1 binds-all — Every session and leg seats at the hub root and keeps each rule here, whatever skills it loads.
- WRK-2 harness-toml — Names, paths, ports and models come from `harness.toml`.
- WRK-3 code-wins — Read state from the live system and behavior from code. When prose and code disagree, fix the prose.
- SAF-1 sentinel — Rules sentinel: slate-finch-2093. A session that cannot read this line stops (`Drawer/rules_check.py`): a leg releases its claim with the reason "NEED rules missing".

## Words
- `<python>` — `.venv/bin/python -B`, run from the hub root.
- hub — this checkout: rules, skills, `Drawer/`, `harness.toml`. Flight is the lead session (flight skill).
- product repo — the checkout at `harness.toml` [product] path (`PRODUCT-CONVENTIONS.md`); empty means the hub.
- leg — one bounded Workflow worker. It returns a summary, never raw output.

## Rules
- SKL-1 card-skills — Load each skill your task card's `Skills:` line names, and each skill whose craft the work needs, before your first action.
- MEM-1 search-memory — Search memory (`mem`) before a design, content, behavior or UI choice. Decide an open choice yourself and write a decision row (memory skill).
- WRK-4 evidence-label — A claim of a critical issue (crash, data loss, blocker; a gate, hook or tool fault) carries [device], [sim], [test] or [unverified]; other lines carry none. A fact a leg read from a file, log, git or the PR counts as checked.
- WRK-5 verified-claims — Claim only what you read and verified. Never state a guess as fact.
- WRK-6 simplest — Build the simplest complete solution. Add nothing speculative.
- WRK-7 tests-honest — Never weaken a test to make it pass. A skipped test proves nothing.
- WRK-8 finish — Commit each green change. Leave no branch, worktree or unpushed commit behind.
- WRK-13 found-work — Name each piece of discovered work you do not fix in your return, or as Flight in its backlog.
- WRK-10 worktree — Edit and commit hub code only in your hub task worktree, product code only in your product worktree (git-flow skill).
- WRK-14 spec-build — Build a spec a human asks for in spec-build workflow rounds; the human decides each finding.
- INC-1 incident-record — At a fault (data loss risk, a broken live resource, a harness tool fault), first run `<python> Drawer/incident.py '<what broke>; impact: <x>; cause: <y>'` and cite its id.
- INC-2 fix-with-test — Fix a fault with a test that goes red without the fix. Close the record: `mem add --kind lesson --closes <id>`.
- SAF-4 trash — Delete with `trash`, not `rm`. Prove a cleanup target dead first; a missing lock is no proof.
- SAF-5 others-work — Never reset, stash, check out or discard another session's work, or remove another's worktree.
- SAF-6 secrets — Keep secrets, keys and real names out of commits, comments, docs and chat.
- SAF-7 no-bypass — Never bypass a hook or a gate: no --no-verify, no hand-typed push --force.
