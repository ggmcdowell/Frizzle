# Instruction files: the guidance the rules audit applies

Reference for harness skill OPR-17. Read it before a trim or a rules audit. Pages read 2026-10-31.

## The guidance

1. Keep each instruction file short. Each line loads into every session and costs attention on every task.
   - "Claude Code loads CLAUDE.md into every session, which means every line in it takes a little of Claude's
     attention on *every* task, whether it's relevant or not." (best practices, Keep CLAUDE.md short)
   - "Longer files consume more context and reduce adherence." (memory, Write effective instructions)
   - "If the line is getting lost in a long file, the file needs to be shorter." (best practices, What you can stop doing)
2. Apply the removal test to each line: would removing it cause a session to make a mistake? Keep a line only
   when the answer is yes. The current pages state the test as what the file should hold: "only what Claude
   couldn't work out from the code itself": the commands that confirm work, places where the code misleads,
   decisions that are not in the repo, and "anything you've had to correct twice already" (best practices).
   `/doctor` "cuts content Claude can derive from the codebase ... and keeps pitfalls, rationale, and
   conventions that differ from tool defaults" (memory, My CLAUDE.md is too large).
3. CLAUDE.md under 200 lines: "target under 200 lines per CLAUDE.md file" (memory, Write effective
   instructions). The root AGENTS.md is our CLAUDE.md. Its cap is 40 lines and 3 KB (`Drawer/test_budgets.py`).
4. SKILL.md under 500 lines, detail in reference files: "Keep `SKILL.md` under 500 lines. Move detailed
   reference material to separate files." (skills, Add supporting files). "Keep the body itself concise. Once
   a skill loads, its content stays in context across turns, so every line is a recurring token cost."
   (skills, Types of skill content)
5. A rule that cannot bend is a hook, not a line: "Claude treats them as context, not enforced configuration.
   To block an action regardless of what Claude decides, use a PreToolUse hook instead." (memory)
6. Remove outdated or conflicting lines: "Review your CLAUDE.md files ... periodically to remove outdated or
   conflicting instructions." (memory, Write effective instructions)

## How the hub applies it

- A line fails the removal test when a session could work it out from the code or a tool's own output; when a
  gate, hook or script enforces it and prints its own refusal; when it repeats a rule that lives in another
  file; or when it narrates history a session does not act on.
- An incident fix or an improvement moves to the skill, hook, script or manual that enforces or teaches it,
  with its decision or incident id. A rule lives once (OPR-10).
- `/doctor prompt-audit [<path>]` (claude 2.1.283 or later) finds outdated and conflicting lines. Only a human
  can start it, so the rules audit runs the same checks through the rules-trim workflow.

## Sources

- https://code.claude.com/docs/en/best-practices (Keep CLAUDE.md short; What you can stop doing)
- https://code.claude.com/docs/en/memory (Write effective instructions; Audit your instruction files;
  My CLAUDE.md is too large)
- https://code.claude.com/docs/en/skills (Types of skill content; Add supporting files)
