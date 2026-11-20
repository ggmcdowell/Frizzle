---
paths:
  - "SCRATCHPAD.md"
  - "Flight/SCRATCHPAD.md"
---
# Scratchpads

- Both pads are git-ignored and live only in the main hub checkout. Never write one in a task worktree: worktree-remove deletes it.
- `SCRATCHPAD.md` holds working notes. Write under one heading, `## <session name> <date>`, and edit no other session's heading.
- `Flight/SCRATCHPAD.md` is the Flight backlog. Only Flight edits its `## State` section.
- A leg puts each backlog entry under its own heading, `## <session name> <date>: backlog`, at the end of the file.
- Flight reads only `## State` and the result rows, so a leg names each new backlog entry in its result row (extras).
