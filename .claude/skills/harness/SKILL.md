---
name: harness
description: "Hub stewardship: law files, root rules, skills, Drawer/, installer, memory host, MCP surface. Load for 'harness' or before you change a gate, hook, settings, skill or law file."
---

# harness — hub stewardship

The root `AGENTS.md` binds every session. Hub code changes only in a hub task worktree (git-flow
skill). The memory host manual (nightly, restore) is `.claude/skills/harness/MANUAL.md`.

- OPR-1 exact-supersede — Never guess a superseded id. Read the old row with `mem get`, then replace it with one `mem add --supersedes <old id>` of the same kind.
- OPR-2 protections — Never bypass a credential. Keep tool server ownership, external communication and real resource protections.
- OPR-3 mechanical-over-prose — Enforce a rule with a gate, hook or test where possible; prose states why.
- OPR-5 scope
  - Steward product law (`PRODUCT-CONVENTIONS.md`), the root rules, the skills and critical harness changes through Git.
  - Critical harness means the root rules, the skills, the law files and the gates that enforce law.
  - Add no credential system or approval workflow.
- OPR-6 gate-wiring
  - `Drawer/project-gate.py` is the one PreToolUse gate: it routes Write, Edit, MultiEdit and NotebookEdit to `Drawer/doc-gate.py`, and refuses a manual poll loop and an unscoped search.
  - `Drawer/settings-manifest.json` registers every hook at project scope, and `Drawer/settings-sync.py --apply` renders it to the one hub-root `.claude/settings.json`. Setup writes no user-scope hook.
  - The commit gate holds a commit only on a critical fault. A form check (pointers, skills, agents, blobs, pins) warns; fix its finding in a later commit. A settings drift renders from the commit's tree and the gate stages it.
- OPR-7 amend-law
  - Amend `PRODUCT-CONVENTIONS.md`, the root `AGENTS.md` or a skill by a Git commit.
  - Build it from the file bytes: one exact string replacement that fails unless it matches once. A hand-typed draft moves unrelated bytes, so the commit lands wider than the change.
- OPR-9 two-reads — Report a critical harness change delivered only after two reads: the source revision and the consuming checkout's adoption. Name the Git revision.
- OPR-10 rule-form
  - Every session seats at the hub root. The root `AGENTS.md` holds each rule that binds every session. A craft skill (`.claude/skills/<name>/SKILL.md`, the names in `harness.toml` [project] task_skills) holds the rules of one craft, loaded by task; the task card's `Skills:` line names them.
  - A rule lives once: in the root `AGENTS.md`, one skill or a law file; never in a card or in two files. A line two or more crafts need goes to the root or a shared skill.
  - Edge cases and pitfalls go to memory cards, searched when a session hits them.
  - A path rule in `.claude/rules/` loads with the files it names.
  - Each root `AGENTS.md` rule line starts `- <PFX>-<n> <slug> — ` (prefixes SKL, INC, MEM, WRK, SAF); the Words glossary takes none. A new line takes the next free number of its prefix. Never renumber or reuse an ID: a cut retires it, and a line moved out of AGENTS.md takes a new ID in its new home, so a gap in a prefix (MSN, RTE, SHP and CMP are retired whole; FLT-3 to FLT-5) is kept on purpose.
- OPR-15 controlled-probe — Judge a context or cost change by a controlled probe: same machine, same CLI version, same working folder, same day, before and after. Never compare transcript pools; they mix session populations and fake a regression.
- OPR-17 rules-cap — The root `AGENTS.md` holds direction and operations only. Past 40 lines or 3 KB a trim is due (`Drawer/test_budgets.py`). An incident fix or an improvement goes into the skill, hook or script that enforces or teaches it, with its decision or incident id.
  - Each SKILL.md stays under 500 lines, with detail in reference files beside it. The guidance and its sources: `instruction-files.md` beside this file.
  - The rules audit runs at each commit of AGENTS.md or a SKILL.md: the pre-commit hook asks for its rules-trim pass.
  - The rules audit:
    1. `<python> Drawer/rules_audit.py check`: the line count of AGENTS.md and each SKILL.md against its cap.
    2. The rules-trim workflow (`.claude/workflows/rules-trim.js`), `mode: 'propose'`, `files`: the changed files, or AGENTS.md and every SKILL.md for a full audit. A read-only leg per file applies the removal test to each rule line.
    3. Apply each cut or move. Log each one: `<python> Drawer/rules_audit.py log --file <f> --action cut|move --line '<text>' --home '<new home>' --why '<ID>; enforcer: <x>'`.
    4. Run it with `mode: 'review'` on a second model: a cold leg per changed file checks the trim. Name its verdict in your close.
    5. Only after the review passes, from the repo root: `<python> Drawer/rules_audit.py pass --file <f>` for each file you commit.
- MCP-5 mcp-surface — The harness owns the hub's MCP surface: the `mem` MCP entry and the optional `[product] mcp_server` entry that `Drawer/settings-sync.py` renders from `harness.toml`. The tool server itself is product code.
- OPR-18 land-ruling — Land a ruling from the human by `ruling.md` beside this file.
