#!/usr/bin/env bash
# Claude Code status line. Four fields: model | effort | context | weekly usage.
# Portable: reads stdin JSON; no jq dependency or home-directory copy.
repo=$(cd -- "$(dirname -- "$0")/.." && pwd) || exit 1
PYTHON=""
for candidate in "$repo/.venv/bin/python" "$(command -v python3 2>/dev/null)"; do
  [ -n "$candidate" ] || continue
  if "$candidate" -c pass </dev/null >/dev/null 2>&1; then PYTHON=$candidate; break; fi
done
if test -z "$PYTHON"; then
  printf '%s\n' 'No working Python runtime; run the setup skill (.claude/skills/setup/SKILL.md).' >&2
  exit 1
fi
"$PYTHON" -c '
import sys, json, os, threading
# Claude Code can hold stdin open past its call; a bounded read ends this helper instead of leaking it.
# Every exit is os._exit: a normal shutdown aborts on the reader thread still blocked in stdin.
# The reader stops at EOF or at the first whole JSON value, so a held stdin still yields the fields.
got = [b""]
def read_stdin():
    while True:
        try:
            chunk = os.read(0, 65536)
        except OSError:  # stdin closed: the fields fall back to n/a
            return
        if not chunk:
            return
        got[0] += chunk
        try:
            json.loads(got[0])
            return
        except ValueError:
            pass
reader = threading.Thread(target=read_stdin, daemon=True)
reader.start()
reader.join(2)
try:
    d = json.loads(got[0])
except Exception:
    d = {}
if not isinstance(d, dict):
    d = {}
sys.excepthook = lambda *_: os._exit(1)  # a fault in the fields below must not hang on the reader

# 1. Model name.
model = (d.get("model") or {}).get("display_name") or "unknown"

# 2. Reasoning effort (present only when the model supports it).
effort = (d.get("effort") or {}).get("level") or "n/a"

# 3. Context size: percent used, else raw token count, else n/a.
cw = d.get("context_window") or {}
if cw.get("used_percentage") is not None:
    ctx = str(int(cw["used_percentage"])) + "%"
elif cw.get("total_input_tokens") is not None:
    ctx = str(int(cw["total_input_tokens"] / 1000)) + "k"
else:
    ctx = "n/a"

# 4. Weekly usage from the 7-day subscription rate limits (subscribers
#    only, after first API response). No network call; sourced from stdin JSON.
rl = (d.get("rate_limits") or {}).get("seven_day") or {}
if rl.get("used_percentage") is not None:
    wk = str(int(rl["used_percentage"])) + "%"
else:
    wk = "n/a"

# The hub palette.
def rgb(hexcode, text):
    r, g, b = (int(hexcode[i:i + 2], 16) for i in (0, 2, 4))
    return "\033[38;2;%d;%d;%dm%s\033[0m" % (r, g, b, text)
try:
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    sys.stdout.write(" ".join((rgb("e08a4f", model), rgb("a48fc0", "effort:" + effort),
                               rgb("9fb07a", "ctx:" + ctx), rgb("d8b46a", "wk:" + wk))))
    sys.stdout.flush()
finally:
    os._exit(0)
'
