# review: audit — the milestone-close cold read

A worker or Flight runs it on the product repo (`harness.toml` [product] path; empty: the hub) at milestone
close, by command. It is advice, never a gate. It finds what per-diff review cannot see: self-satisfying tests,
stale docs, parallel copies of one list, unstamped proof. These only show on an accumulated read.

**Read-only.** The audit fixes nothing. Do not validate and fix in one pass.

## 1. Derive the scope — never hand-pick it

The rings compute the scope. State what each ring covered; state each bound you set.

1. **Repo-wide drift checks** (every close). Duplicate lists, manifests and censuses (a guard test
   over a duplicate is a deferral, not a fix). The skip and ignore set against the gate's named
   allow-list. Gate integrity: a golden or approval suite still fails on a missing baseline, and no
   load-bearing test is unable to fail on the event it guards. Results provenance: each results
   artifact names its commit, time and run id.
2. **Files the milestone touched, read whole.** `git -C <product repo> log --name-only <milestone start>..HEAD`,
   deduplicated. Read each file whole, not as diff hunks, with its tests.
3. **One seam out.** The direct callers and callees of the ring-2 files.
4. **One rotating slice.** The least-recently-audited subsystem. Read the rotation state from the
   last audit (section 4). State which slice this close covers and which slices are still owed.

**Docs sweep** (same event). `PRODUCT-CONVENTIONS.md` and the product repo's own docs: compare each mechanism
claim with the code. Output a stale-claims table: location, claim, verified truth, fix route.

## 2. Chunk the runs

- One run per ring is not small enough, and the failure is silent. Chunk to about 12,000 lines (about
  60 files) per run, one artifact per run.
- Put a depth contract in every prompt: "Work file by file. Do not build a whole-tree type map first.
  If context runs low, stop and emit what you have, with a truthful COVERAGE line."
- Frame the prompt: read-only static audit, mechanism-focused, `file:line` evidence for each claim.
  Grade severity in reachability terms: can a user or a caller reach the defect?
- Give the readers the last validated table. A finding that sign-off rejected stays rejected unless
  new evidence comes. A finding already on a card is a re-file, not new.

## 3. Two readers, different pinned models

Both readers get the same prompt and the same chunks. They are independent cold reads, not a relay.

- Run each chunk through two `one-leg` runs, one with `model: 'sonnet'` and one with `model: 'haiku'` (Workflow args never name opus; fork-gate denies it). Each
  reader writes its full output to `<state_root>/audit/<milestone>/<chunk>-<model>.md` (its only write grant)
  and returns a summary. Run one chunk at a time. Read `modelMismatches` in each result.

**NO FINDINGS on a big scope is no result** until you check the reader's full output file:

- file coverage: how many scoped files the run touched at all;
- a COVERAGE line that stops short of the chunk;
- whether the run reached synthesis (a `severity` key outside the prompt echo).

A run that fails a check is abandoned. Keep its output as `<run>-ABANDONED`, re-chunk and run again.

## 4. Write the report

Write it outside Git, at `<state_root>/audit/<milestone>.md` (`[project] state_root` in
`harness.toml`). Name each reader output file in it as evidence.

1. One findings table: `file:line`, claim, severity, ring, reader (`sonnet`, `haiku` or `both`).
   When both readers flag one thing, keep one row marked `both`.
2. The scope statement: covered, not covered, rotation state.
3. The docs-sweep stale-claims table.

The close report carries the report path, the finding counts and the rotation state, so the next
audit finds them.

## 5. Sign-off, then fixes — a separate pass

- No finding is true by default. A separate sign-off pass validates each one against the live code,
  then accepts or rejects it out loud.
- A measured record in the repo beats a refutation from first principles.
- Two things with one name are two referents. Check which one a finding means before you rule it.
- Then Flight triages each accepted finding: this milestone, backlog or drop.
- Fixes go to the Flight backlog (`Flight/SCRATCHPAD.md`), only after sign-off and triage.
