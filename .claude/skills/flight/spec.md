# Spec case table

Part of the flight skill. Flight writes each task card by this table. Owner: harness.

A card-run leg builds from the card alone. A card with no answer for a restart, an empty state, a failure or a
neighbour leaves the leg to guess. Answer each case so the leg does not guess.

## Write the card

1. Have a Workflow leg (FLT-2) search memory on the work and read the code, rows or assets it touches. It returns the design cards, decisions, taste lines and open tasks on the same things, and names each neighbour by file or row kind.
2. Answer each case below in one short clause, from a design card, the code or the human's intent. Write `n/a` when a case cannot happen; never skip a case.
3. A case that no source answers: decide it, write a decision row (root `AGENTS.md` MEM-1), and put the answer in the card.
4. Write the card in this form, the mem text lint form with the spec before `Why:`:
   `<name>: <what>. Spec: state <...>; life <...>; breaks <...>; neighbours <...>; data <...>; proof <...>; sees <...>; keeps <...>. Why: <gap>. Done when: <end state>.`
5. Keep the card under 1800 characters; aim at 1600. A card over the cap is two tasks: split it, one card per task, the second `--depends-on` the first.
6. Add the card with `mem add --kind task` (memory skill). The text lint refuses a card out of the form or over the cap.

No dialog and no approval: Flight decides each case itself (FLT-8). A spec a human asks for takes spec-build rounds (root WRK-14).

## The cases

| Case | Code | Content | Assets |
|---|---|---|---|
| state | each state it writes, and each path that writes it | the rows and keys it adds | the files it adds |
| life | create, change, remove | where it shows in each state | the file or screen that holds it |
| breaks | restart, quit or stop midway, empty, full, fail, repeat | a missing row or key, a screen without it | a missing file, a failed load |
| neighbours | what it reads, what reads it, the contract it meets | the screen, the row kinds, the text table | the load path, the format, the budget |
| data | the test fixture or dev data it starts on | the dev row it starts on | the placeholder it starts on |
| proof | the test or check that shows done | the check that shows done | the check that shows it loads in place |
| sees | what a user sees and does | what a user reads and meets | what a user sees |
| keeps | what must not change | what must not change | what must not change |

Put the detail where a wrong guess costs most: what a user sees, the data shape, a step that cannot be undone.
`Done when:` is the end state a reviewer can check. The spec holds the cases, never the steps.

## Build to it

1. The card-run leg reads the `Spec:` part before its first edit. Its review leg reads it beside the diff.
2. A case the build finds that the spec lacks goes in the result row, with the answer the build took.
