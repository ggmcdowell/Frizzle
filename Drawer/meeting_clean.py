"""Clean a spoken design meeting into the meeting intake form (.claude/skills/memory/meeting.md).

    <python> Drawer/meeting_clean.py <src> <dst> [--speaker NAME] [--replace "OLD=>NEW" ...] [--dry-run]

Reads "[h:mm:ss] Name: words" or "[m:ss] Name: words" lines (brackets optional) and untimed text. A
timed line with no label, or untimed text with no label, is words of --speaker. An untimed line is a
labeled turn only when the text before ":" is 1 or 2 capitalized words of 24 characters or fewer that
open 2 or more untimed lines; so a lone "Note: ..." in a memo stays with --speaker. An untimed line gets
an estimated stamp: the last real stamp (00:00:00 when there is none) plus the words since it at 150
words a minute; the head says so.

Drops filler-only turns, joins a speaker's lines that sit side by side, names each speaker by first
name, opens a "## hh:mm:ss" heading at the first turn a minute or more after the last heading, writes
the head, then applies each --replace (a misheard name to its person, another person's name to a
bracketed role). Words are otherwise kept as transcribed.

Prints one JSON line: {result, blocks, bytes, dst, estimated, replaced, check_names}; check_names are the
capitalized words left for a person's-name check. --dry-run writes no file and first prints one row stub
per block: {kind, source: "feedback:meeting <dst name>#<hh:mm:ss>", speakers, words}. A fault prints
{error} and exits 1 with no file written: no turn found, unlabeled text and no --speaker, a replacement
that matches nothing, or a dst inside this hub checkout (transcripts stay outside git).
"""
import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

HUB = Path(__file__).resolve().parents[1]
FILLER = {"mhm", "mm", "hmm", "uh", "um", "uhoh", "so", "yeah", "yes", "okay", "ok", "right", "and",
          "but", "well", "no", "sorry", "heat", "hello", "yep", "oh", "i", "the", "ex", "dude", "it's",
          "its", "like"}
STAMP = re.compile(r"^\[?(?:(\d+):)?(\d{1,2}):(\d\d)\]?\s+(.*)$")
LABEL = re.compile(r"^([^:\s][^:]{0,23}): ?(.*)$")
CAPITAL = re.compile(r"^[A-Z][\w.'-]*$")
DATE = re.compile(r"\d{4}-\d\d-\d\d")
BLOCK_SECONDS = 60
WORDS_PER_MINUTE = 150


class Fault(Exception):
    pass


def hms(secs):
    return "%02d:%02d:%02d" % (secs // 3600, secs // 60 % 60, secs % 60)


def label(text, timed):
    """(name, words) when text opens with a speaker label, else None."""
    m = LABEL.match(text)
    if not m or len(m.group(1).split()) > 2:
        return None
    if not timed and not all(CAPITAL.match(w) for w in m.group(1).split()):
        return None
    return m.group(1).strip(), m.group(2)


def parse(text):
    """Turns as (secs, name or None, words, estimated); untimed labels count only when one opens 2+ lines."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    untimed = Counter(lab[0] for ln in lines if not STAMP.match(ln) for lab in [label(ln, False)] if lab)
    turns, base, since = [], 0, 0
    for ln in lines:
        stamp = STAMP.match(ln)
        if stamp:
            h, m, s, rest = stamp.groups()
            base, since = int(h or 0) * 3600 + int(m) * 60 + int(s), 0
            lab = label(rest, True)
            who, words = lab if lab else (None, rest)
            turns.append((base, who, words.strip(), False))
            continue
        lab = label(ln, False)
        who, words = lab if lab and untimed[lab[0]] >= 2 else (None, ln)
        turns.append((base + since * 60 // WORDS_PER_MINUTE, who, words.strip(), True))
        since += len(words.split())
    return turns


def clean(turns, speaker):
    """Blocks: a list of [stamp, [[speaker, words], ...]], one per minute or more of talk."""
    blocks, opened = [], None
    for secs, who, words, _ in turns:
        who = (who or speaker).split()[0]
        if all(w in FILLER for w in re.sub(r"[^a-z' ]", " ", words.lower()).split()):
            continue
        if opened is None or secs - opened >= BLOCK_SECONDS:
            opened = secs
            blocks.append([hms(secs), []])
        said = blocks[-1][1]
        if said and said[-1][0] == who:
            said[-1][1] += " " + words
        else:
            said.append([who, words])
    return blocks


def render(date, blocks, estimated):
    speakers = sorted({who for _, said in blocks for who, _ in said})
    span = "%s to %s" % (blocks[0][0], blocks[-1][0])
    how = ("Cleaned from a transcript with untimed text; its stamps are estimated at %d words a minute "
           "from the last real stamp, and its turns run about %s." % (WORDS_PER_MINUTE, span)
           if estimated else "Cleaned from a timed transcript whose turns run %s." % span)
    out = ["# Design meeting: %s, %s" % (" and ".join(speakers), date), "", how,
           "Edits: filler-only turns are dropped, and a speaker's lines that sit side by side are joined.",
           "Any other person's name is a bracketed role. A bracketed word with a question mark is a likely reading of a misheard word.",
           "Every other word is as transcribed; speech-to-text mishears some words.", ""]
    for stamp, said in blocks:
        out += ["## " + stamp, ""]
        for who, words in said:
            out += ["**%s:** %s" % (who, words), ""]
    return "\n".join(out).rstrip() + "\n"


def rows(text, name):
    """One design row stub per "## hh:mm:ss" block of the final text."""
    out = []
    for part in re.split(r"^## ", text, flags=re.M)[1:]:
        stamp, _, body = part.partition("\n")
        said = re.findall(r"^\*\*(.+?):\*\* (.*)$", body, re.M)
        out.append({"kind": "design", "source": "feedback:meeting %s#%s" % (name, stamp.strip()),
                    "speakers": sorted({who for who, _ in said}), "words": sum(len(w.split()) for _, w in said)})
    return out


def run(a):
    dst = Path(a.dst).resolve()
    if dst == HUB or HUB in dst.parents:
        raise Fault("dst %s is inside the hub checkout %s; keep transcripts outside git" % (dst, HUB))
    turns = parse(Path(a.src).read_text(encoding="utf-8"))
    unlabeled = sum(1 for t in turns if t[1] is None)
    if unlabeled and not a.speaker:
        raise Fault("%d line(s) have no speaker label; give --speaker NAME" % unlabeled)
    blocks = clean(turns, a.speaker or "")
    if not blocks:
        raise Fault("no speaker turn found in %s" % a.src)
    date = DATE.search(Path(a.src).name)
    text = render(date.group(0) if date else "an unknown date", blocks, any(t[3] for t in turns))
    replaced = {}
    for pair in a.replace:
        old, sep, new = pair.partition("=>")
        n = text.count(old) if sep and old else 0
        if not n:
            raise Fault("replacement %r matches nothing" % pair)
        replaced[old] = n
        text = text.replace(old, new)
    body = text[text.index("\n## "):]
    known = set(re.findall(r"^\*\*(\w+):\*\*", body, re.M))
    lower = set(re.findall(r"\b[a-z]+\b", body))
    names = Counter(w for w in re.findall(r"\b[A-Z][a-z]{2,}\b", body) if w not in known and w.lower() not in lower)
    if a.dry_run:
        for row in rows(text, dst.name):
            print(json.dumps(row))
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(text, encoding="utf-8", newline="\n")
    return {"result": "ok", "blocks": len(blocks), "bytes": len(text.encode()), "dst": str(dst),
            "estimated": any(t[3] for t in turns), "replaced": replaced, "dry_run": a.dry_run,
            "check_names": ["%s(%d)" % kv for kv in sorted(names.items())]}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--speaker", help="the name of unlabeled or untimed text")
    ap.add_argument("--replace", action="append", default=[], metavar="OLD=>NEW")
    ap.add_argument("--dry-run", action="store_true", help="write no file; print one design row stub per block")
    a = ap.parse_args(argv)
    try:
        print(json.dumps(run(a)))
    except (Fault, OSError, UnicodeDecodeError) as e:
        print(json.dumps({"error": "meeting_clean: %s; nothing written" % e}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
