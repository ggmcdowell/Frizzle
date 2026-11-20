"""Claude UserPromptSubmit: announce a context that costs more than the work.

Cost is turns times context. Every turn re-bills the whole prompt, and an idle
lapse past the prompt-cache TTL rewrites it at the creation price instead of the
read price. This hook reads the session's own transcript and speaks only on the
turn that crosses a line: past BIG_CONTEXT, or after an idle gap longer than the
TTL the last turn actually used. It announces; it never blocks a prompt, so any
failure is silent.

Claude only: the transcript rows and usage fields are Claude Code's.
Cost: this file. Maintainer: hub.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import sys

BIG_CONTEXT = 500_000
DEFAULT_TTL = 3600  # the 1-hour prompt cache; a turn's own usage overrides it


def prompt_tokens(usage):
    """Everything the model re-read this turn: the whole prompt, however it was billed."""
    return sum(int(usage.get(key) or 0) for key in
               ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))


def cache_ttl(rows):
    """The TTL this session's cache writes actually bought, newest write wins."""
    for _, usage in reversed(rows):
        created = usage.get("cache_creation")
        if isinstance(created, dict):
            if int(created.get("ephemeral_1h_input_tokens") or 0):
                return 3600
            if int(created.get("ephemeral_5m_input_tokens") or 0):
                return 300
    return DEFAULT_TTL


def turns(path):
    """Main-thread assistant turns, oldest first, as (timestamp, usage) pairs.

    A sidechain turn is a subagent's own context and bills separately, so it says
    nothing about this session's size.
    """
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue  # a half-written tail line is not worth failing the turn over
            if not isinstance(row, dict) or row.get("type") != "assistant" or row.get("isSidechain"):
                continue
            usage = (row.get("message") or {}).get("usage")
            stamp = row.get("timestamp")
            if not isinstance(usage, dict) or not isinstance(stamp, str):
                continue
            try:
                at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except ValueError:
                continue
            rows.append((at, usage))
    return rows


def notices(rows, now):
    """The lines this turn earns: the crossing turn only, never a standing banner."""
    if not rows:
        return []
    out = []
    size = prompt_tokens(rows[-1][1])
    before = prompt_tokens(rows[-2][1]) if len(rows) > 1 else 0
    if before < BIG_CONTEXT <= size:
        out.append(
            f"CONTEXT {size // 1000} K tokens - every further turn re-bills all of it. "
            "Keep working; a Flight compacts in place, no session hands off."
        )
    ttl = cache_ttl(rows)
    idle = (now - rows[-1][0]).total_seconds()
    if idle > ttl:
        lapse = f"{idle / 60:.0f} min" if idle < 7200 else f"{idle / 3600:.1f} h"
        out.append(
            f"IDLE {lapse} since the last turn, past the {ttl // 60} min cache TTL - "
            "this turn rewrites the whole context at the creation price."
        )
    return out


def run(event, now):
    """Judge one already-parsed UserPromptSubmit event. The text becomes turn context."""
    if not isinstance(event, dict) or event.get("hook_event_name") != "UserPromptSubmit":
        return ""
    path = event.get("transcript_path")
    if not isinstance(path, str) or not path:
        return ""
    return "\n".join(notices(turns(path), now))


def main():
    if sys.argv[1:] in (["--help"], ["-h"]):
        print(__doc__)
        return 0
    try:
        text = run(json.loads(sys.stdin.buffer.read()), datetime.now(timezone.utc))
    except Exception:  # a nudge that breaks a prompt costs more than the nudge saves
        return 0
    if text:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
