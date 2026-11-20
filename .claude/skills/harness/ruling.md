# Land a ruling

A ruling is the human's standing call on the harness: the rules, a gate, a hook, a skill, the tooling or a
law file. A product design, content or product-code call is not a ruling: land it as a design or decision row
(memory skill). A ruling is landed only when every surface that should carry it does. Run the steps in
order. Skip a step only when its condition does not apply, and name the skip in your close.

## 1. Check it against the law
1. Read the ruling against the root `AGENTS.md`, `PRODUCT-CONVENTIONS.md` and this skill.
2. At a conflict, push back once: name the line, the conflict and a compliant option.
3. The human's explicit call then wins. The commit message names the line it overrides.

## 2. Land it in its lowest home
A ruling is a line in a file, never only a mem row. Try each home in this order:
1. A hook, a script or a gate that enforces it (OPR-3).
2. Else the skill loaded at the act the ruling governs.
3. Else the root `AGENTS.md`, only as a pointer or critical operational information.

Product law goes in `PRODUCT-CONVENTIONS.md`. A minor standing call is a decision row. A ruling with no file
home goes back to the human to name one.

## 3. Write it in rule form
- One home: a rule lives once (OPR-10). The new line replaces the line it supersedes in the same commit.
- Imperative: an order in the present tense ("Run X", "Never Y"), never a wish or a history.
- Atomic: one rule per line; a second idea is a second line.
- Ranked: a hard stop sits under Never; a line keeps its section's order and its ID.
- Channel: the line names the hook, script, skill or file that delivers it.
- Small: the line serves a present need. Cut the paths it makes dead.

## 4. Update each skill the ruling touches
When the ruling changes a loop, grep `.claude/skills/` for that loop. Update every skill it touches in
the same commit. A skill that contradicts a fresh ruling is worse than no skill.

## 5. Commit and land
1. Build the edit from the file bytes (OPR-7). Run the rules audit on each changed AGENTS.md or SKILL.md (OPR-17).
2. Commit in a hub task worktree and land it (git-flow skill). Run the smoke skill first.
3. Read the new line back on hub main: `git -C <hub> grep -n '<new words>' main -- <file>` (OPR-9).

## 6. Fix the rows it makes wrong
After the commit, run `mem search` for rows the ruling makes wrong. Replace each with one
`mem add --supersedes <old id>` of the same kind (OPR-1). The new row states the corrected fact; it
holds no rule text.

## Close
One line per surface: the commit and file of each line landed, each row superseded, and the hub main
read-back. Name each skipped step and why.
