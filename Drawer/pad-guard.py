"""Pad guard -- a PostToolUse hook on Edit, Write, MultiEdit and Bash: block once when a tool call cut a pad.

Both pads (hub-root SCRATCHPAD.md, Flight/SCRATCHPAD.md) are git-ignored, so no git check sees a cut.
Flight's regex on its SHIPPED line cut the State BACKLOG line for ~12 h; a worker
edit can cut another session's notes the same way.

After each call it compares each pad to the copy it kept after the last call, from any session, under
state_root/pad-guard/. Hooks run right after their call, so a change is near always this call's own.
It blocks once, and names the copy to restore from, when:
  - a level-2 heading the session does not own lost lines, or was cut;
  - a labelled line (one that starts with a word in capitals and a colon, such as BACKLOG:) was cut, or
    kept under half its length, in any heading, own ones too;
  - the pad shrank by more than SHRINK of its characters.
A session owns each heading that holds its session uuid8; a Flight owns all of Flight/SCRATCHPAD.md
(it curates the backlog) and its own heading in the hub-root pad. Then the new text becomes the copy,
so the same cut does not block again. It keeps the newest KEEP copies of each pad.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

DRAWER = Path(__file__).resolve().parent
sys.path.insert(0, str(DRAWER))
import oscompat  # noqa: E402

PADS = {"hub": "SCRATCHPAD.md", "flight": "Flight/SCRATCHPAD.md"}
KEEP = 6
SHRINK = 0.3
HEADING_RE = re.compile(r"^## (?!#)", re.M)
LABEL_RE = re.compile(r"^([A-Z][A-Z ]{2,}):(.*)$", re.M)
REASON = ("pad-guard: {pad} lost text since the last tool call (this call, or another session's edit just "
          "before it): {faults}. Both pads are git-ignored, so no git check sees a cut. "
          "Restore it from {copy} with an exact edit, and keep the new text. If the cut was on purpose, go on.")


def sections(text):
    """{heading line: body} for each level-2 heading; the text before the first one is '(top)'."""
    parts = {}
    starts = [m.start() for m in HEADING_RE.finditer(text)]
    parts["(top)"] = text[:starts[0]] if starts else text
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        heading, _, body = text[start:end].partition("\n")
        parts[heading] = parts.get(heading, "") + body
    return parts


def lines(body):
    return [line for line in body.splitlines() if line.strip()]


def faults(old, new, owns):
    """The cuts from old to new that block, as short phrases."""
    found = []
    if len(old) and len(new) < len(old) * (1 - SHRINK):
        found.append(f"the pad shrank from {len(old)} to {len(new)} characters")
    before, after = sections(old), sections(new)
    for heading, body in before.items():
        name = heading[3:] if heading.startswith("## ") else heading
        now = after.get(heading)
        labels = dict(LABEL_RE.findall(now or ""))
        for label, rest in LABEL_RE.findall(body):
            if label not in labels:
                found.append(f"heading '{name}' lost its line that starts '{label}:'")
            elif len(labels[label]) < len(rest) / 2:
                found.append(f"heading '{name}' cut its '{label}:' line from {len(rest)} to {len(labels[label])} characters")
        if owns(heading):
            continue
        if now is None:
            found.append(f"heading '{name}' was cut")
        else:
            gone = [line for line in lines(body) if line not in set(lines(now))]
            if gone:
                found.append(f"heading '{name}' lost {len(gone)} line(s), first: {gone[0][:80]!r}")
    return found


def owner(session, flight, pad):
    tag = (session or "")[:8].lower()

    def owns(heading):
        if flight and pad == "flight":
            return True
        return bool(tag) and tag in heading.lower()
    return owns


def check(event, hub, store, flight):
    """The block reply for this call, or None."""
    store.mkdir(parents=True, exist_ok=True)
    with open(store / "lock", "w") as lock:
        oscompat.lock(lock, blocking=True)
        replies = []
        for key, rel in PADS.items():
            pad = hub / rel
            try:
                new = pad.read_text(encoding="utf-8")
            except OSError:
                continue
            copies = sorted(store.glob(f"{key}-*.md"))
            old = copies[-1].read_text(encoding="utf-8") if copies else None
            if old == new:
                continue
            if old is not None:
                cut = faults(old, new, owner(event.get("session_id"), flight, key))
                if cut:
                    replies.append(REASON.format(pad=pad, faults="; ".join(cut), copy=copies[-1]))
            stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 10**9:09d}"
            (store / f"{key}-{stamp}.md").write_text(new, encoding="utf-8")
            for stale in sorted(store.glob(f"{key}-*.md"))[:-KEEP]:
                stale.unlink(missing_ok=True)
    return {"decision": "block", "reason": " ".join(replies)} if replies else None


def main():
    try:
        event = json.loads(sys.stdin.buffer.read())
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(event, dict):
        return 0
    sys.path.insert(0, str(DRAWER))
    import harness_config
    cfg = harness_config.load(DRAWER.parent)
    store = harness_config.path(cfg["project"]["state_root"]) / "pad-guard"
    flight = bool(os.environ.get(harness_config.env_name("FLIGHT", cfg), "").strip())
    reply = check(event, DRAWER.parent, store, flight)  # the hook runs from the hub root, where the pads live
    if reply:
        print(json.dumps(reply))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
