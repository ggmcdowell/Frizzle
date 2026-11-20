---
name: review
description: "Product-repo review advice: a milestone-close cold audit by two readers on two models; never a gate. Triggers: 'review', 'sprint audit', 'milestone audit', 'cold audit'."
---

# review — the cold audit

One review mode: the milestone-close cold audit of the product repo (`audit.md` beside this file). It is advice
that a worker or Flight runs by command. It is not a gate: no push, land or close waits on it. The risky-change
gate is the risky-review workflow (flight skill FLT-15).

Read `audit.md` before its first step.

## Common rules

1. Search memory on the subject first. A prior round, a rejected finding or a decision may bind.
2. Read-only. A review fixes nothing in the same pass. Fixes go to the Flight backlog.
3. A finding is a claim. Read its `file:line` yourself before you trust or report it.
4. Keep reports outside Git, under `[project] state_root`.
5. A run that returns no output is no review, not no findings. Never report it as a pass.

## Estate

This skill and `audit.md`. The readers run by the `one-leg` saved workflow. Owner: harness.
