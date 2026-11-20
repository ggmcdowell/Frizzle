# memory part: meeting — a spoken design session into mem

The memory rules are in `.claude/skills/memory/SKILL.md`; this part holds the meeting intake.

Land what a meeting means, never what it said. Rows land at once, with no approval step: the human reads one
review doc per meeting and their reply supersedes rows. Only a reversal asks first.
The session that runs this part makes the review doc and asks the reversal question; a Workflow leg does neither.

## Files

- The human's transcript or recording text goes to `<state_root>/meetings/raw/` (`harness.toml` [project] state_root), never into the hub.
- The cleaned copy goes to `<state_root>/meetings/<YYYY-MM-DD>-<slug>.md`. Neither is committed:
  `Drawer/meeting_clean.py` refuses a dst inside the hub checkout.

## Clean

1. Read the raw text once for names and mishearings.
2. `<python> Drawer/meeting_clean.py <raw> <cleaned copy> --speaker <the human's name>`, with one `--replace "<old>=><new>"`
   per misheard name (`[<reading>?]` for a likely mishearing) and per other person's name (a bracketed role).
   It reads timed `[h:mm:ss] Name: words` lines and untimed text; unlabeled text is the `--speaker`'s,
   and untimed text gets stamps estimated by word pace. It drops filler-only turns, joins side-by-side lines,
   opens a `## hh:mm:ss` heading each minute or more and prints one JSON line.
3. Read `check_names`: rerun with a `--replace` for each person's name it lists, until none is left.
   A replacement that matches nothing is refused.

A row's anchor is the nearest heading before its line. Its source is `feedback:meeting <cleaned basename>#<hh:mm:ss>`.

## Speakers

- The human owns the design. A row lands as a mantra or an intent only when the human says it or commits to it.
- Another speaker's idea that the human does not commit to lands as an exploration, credited to that speaker's role.
- Agreement words alone ("I love it") commit no one. Name no person but the human: use a role.

## Classes

All three land as kind `design`, `--tag mantra|intent|exploration`, `--topic meeting` plus a subject
topic (the thing the row is about, never a person), and the source above. A body is under 600 characters, with no date:

- mantra: `Mantra (<credit>): <line>. Why: <lesson>.`
- intent: `Intent (<credit>): <what>.` A concrete call the human commits to lands as an intent.
- exploration: `Exploration (<credit>): <idea>; open: <question>.`

A war story never lands; its lesson does, as a mantra or an intent. An action goes to the Flight backlog.
Drop filler, banter and a line you cannot read.

## Extract

1. Run the saved `fan-out` workflow (`.claude/workflows/fan-out.js`), no model arg (fan-out's own route applies), `maxItems` 40: one leg
   per chunk of about 20 minutes, at most 5 legs. Each brief names the cleaned copy path, its line range,
   the block before it as context only, and the Speakers, Classes and Extract sections of this file by path.
2. A leg returns one item per row and per dropped story:
   `<class> | <credit> | <topic> | <hh:mm:ss> | <text> | <story or reason>`. The result caps each leg's
   items: read every leg's full items from the run's `journal.jsonl`.
3. Read the whole cleaned copy yourself, by line range; skip no range. Merge the legs' rows with your own:
   one row per distinct lesson or idea, each checked against its line.

## Merge

Per row, `mem search "<subject>" --kind design` first.

- Same meaning: write nothing.
- A repeat said tighter: `mem get` the old row, then `mem add --supersedes <id>` (harness OPR-1).
- An exploration now committed lands as an intent that supersedes it only when nothing open is left.
- A row that contradicts a live design, taste or decision row is a reversal: do not land it. Ask the human
  by a doc link with the old row, the new row and three options:
  Evolution, Scope change, Keep the old. This is the only question.

## Review doc

After the rows land, make one review doc per meeting and send its link: each landed row (id8, class, credit, text, anchor),
each supersede, each dropped story and why, and each reversal asked. Return one line:

`meeting intake: <meeting> · <n> mantras, <n> intents, <n> explorations · <n> superseded · <n> reversals · <n> dropped`
