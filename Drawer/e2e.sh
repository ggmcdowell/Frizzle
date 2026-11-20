#!/bin/bash
# e2e.sh -- the one-shot acceptance test: a fresh clone runs the starter task (hub-hello) to done through Flight.
# Usage: Drawer/e2e.sh [--keep] [--pack]. Exit 0 = PASS. It tests the checkout it sits in (its HEAD becomes origin main).
# --pack: the clone applies the whole Godot pack through Drawer/product-init.py (a scratch product repo with a local origin,
# every [product] line, gate.sh, the product hooks) with a stub godot on GODOT, so no Godot is needed. The land smoke must
# run the pack gate; after the run one product change lands through Drawer/product-push.py; the settings check stays green.
# Topology (P2): Flight runs headless (claude -p) with --permission-mode acceptEdits, never with permissions bypassed;
# it runs the task as a card-run Workflow in its own session. settings-sync --mcp renders .claude/settings.local.json (the
# folders and the short allow list) and .mcp.json; claude reads that file alone (--strict-mcp-config --mcp-config), so
# no user or plugin MCP server of this box reaches the run. It needs no GUI. A check fails the run when the session mode is not acceptEdits.
# On PASS (no --keep) it removes the temp folder, its projects rows in ~/.claude.json and its transcript folders
# (Drawer/e2e_clean.py); it stops at once when uv, claude or trash is missing.
# Tokens: the sum over every transcript of the run (Flight and its legs); E2E_TIMEOUT caps claude (default 3600 s).
# Windows (Git Bash): env -i would also drop what uv, git, claude and Python need to find the profile and start, so pass it on.
if [ -z "${E2E_CLEAN:-}" ]; then
  winenv=()
  case "$(uname -s)" in MINGW*|MSYS*) for v in SYSTEMROOT USERPROFILE APPDATA LOCALAPPDATA TEMP TMP ProgramFiles ProgramData COMSPEC USERNAME; do
    [ -n "${!v:-}" ] && winenv+=("$v=${!v}"); done ;; esac
  exec env -i E2E_CLEAN=1 HOME="$HOME" USER="${USER:-${USERNAME:-}}" PATH="$PATH" TERM="${TERM:-xterm}" PYTHONUTF8=1 \
    LANG="${LANG:-en_US.UTF-8}" E2E_TIMEOUT="${E2E_TIMEOUT:-3600}" ${winenv[@]+"${winenv[@]}"} /bin/bash "$0" "$@"
fi
set -u
WIN=; case "$(uname -s)" in MINGW*|MSYS*) WIN=1 ;; esac
for tool in uv claude trash; do  # trash removes the temp folder and the run's traces: a PASS must never leave them
  command -v "$tool" >/dev/null 2>&1 || { echo "FAIL e2e needs $tool on PATH (macOS: /usr/bin/trash)"; exit 2; }
done
SRC=$(cd "$(dirname "$0")/.." && pwd); T=$(cd "$(mktemp -d /tmp/frz-e2e.XXXXXX)" && pwd -P); H="$T/hub"
if [ -n "$WIN" ]; then  # native Windows tools read C:/..., not /tmp/...; uv puts the interpreter in .venv/Scripts; no /usr/bin/python3
  SRC=$(cygpath -m "$SRC"); T=$(cygpath -m "$T"); H="$T/hub"; PYBIN="$H/.venv/Scripts/python.exe"; SYSPY="$PYBIN"
else
  PYBIN="$H/.venv/bin/python"; SYSPY=/usr/bin/python3
fi
PY="$PYBIN -B"
CARD=4f638e9e; FAILS=0; KEEP=; PACK=
for a in "$@"; do case "$a" in --keep) KEEP=1 ;; --pack) PACK=1 ;; *) echo "usage: e2e.sh [--keep] [--pack]"; exit 2 ;; esac; done
fail() { echo "FAIL $*"; FAILS=$((FAILS + 1)); }
step() { echo "== $*"; "$@" >>"$T/setup.log" 2>&1 || { echo "FAIL setup: $* (log $T/setup.log)"; exit 1; }; }
git init -q --bare -b main "$T/origin.git" && git -C "$T/origin.git" fetch -q "$SRC" HEAD:refs/heads/main && git clone -q "$T/origin.git" "$H" || exit 1
cat >"$H/harness.local.toml" <<EOF
[project]
config_root = "$T/config"
state_root = "$T/state"
[mem]
db = "$T/state/hub.db"
env_file = "$T/config/mem.env"
export_dir = "$T/state/mem-export"
[names]
denylist = "$T/config/name-denylist.txt"
EOF
if [ -n "$PACK" ]; then  # a stub godot: the pack gate runs for real; the stub logs each call and prints the SMOKE PASS line
  mkdir -p "$T/bin" || exit 1
  if [ -n "$WIN" ]; then  # CreateProcess runs no #! script and finds no bare name: a godot.cmd that runs a python stub
    cat >"$T/bin/godot_stub.py" <<EOF || exit 1
import sys
args = " ".join(sys.argv[1:])
with open("$T/godot.log", "a") as log:
    log.write(args + "\n")
if "--version" in args:
    print("4.0.stub")
elif "--script" in args:
    print("SMOKE PASS")
EOF
    printf '@"%s" "%%~dp0godot_stub.py" %%*\r\n' "$PYBIN" >"$T/bin/godot.cmd" || exit 1
    export GODOT="$T/bin/godot.cmd"
  else
    cat >"$T/bin/godot" <<EOF && chmod +x "$T/bin/godot" || exit 1
#!/bin/sh
echo "\$*" >>"$T/godot.log"
case "\$*" in *--version*) echo 4.0.stub; exit 0 ;; *--script*) echo "SMOKE PASS" ;; esac
exit 0
EOF
    export GODOT="$T/bin/godot"
  fi
fi
mkdir -p "$T/config"&& cp "$H/name-denylist.example" "$T/config/name-denylist.txt" && cd "$H" || exit 1
step uv sync -q
[ -z "$PACK" ] || { step $PY Drawer/product-init.py --path "$T/product" --pack godot --local-origin; INIT_RUNS=$(wc -l <"$T/godot.log" | tr -d ' '); }
for s in "settings-sync.py --mcp" "runtime/seat.py init" "runtime/seat.py configure" "mem/mem.py doctor" "mem/mem.py seed" "runtime/seat.py bind-git"; do
  step $PY Drawer/$s
done
BASE=$(git -C "$T/origin.git" rev-parse main); PREFIX=$($PY Drawer/harness_config.py --get project.env_prefix)
# Windows: MSYS kill does not stop a native claude.exe or the MCP servers it spawned; taskkill takes the tree by its Windows pid.
stop() { if [ -n "$WIN" ]; then w=$(ps -p "$1" 2>/dev/null | awk 'NR==2{print $4}'); [ -n "$w" ] && taskkill //PID "$w" //T //F >/dev/null 2>&1; else kill "$1" 2>/dev/null; fi; }
echo "== claude one-shot (timeout ${E2E_TIMEOUT}s)"; START=$(date +%s)
env "${PREFIX}_FLIGHT=Flight" "${PREFIX}_FLIGHT_ONESHOT=1" claude --permission-mode acceptEdits \
  --strict-mcp-config --mcp-config "$H/.mcp.json" -p '/flight run the starter task' --output-format stream-json --verbose >"$T/stream.jsonl" 2>"$T/claude.err" &
CPID=$!; (sleep "$E2E_TIMEOUT"; stop "$CPID" && touch "$T/timed-out") & KPID=$!
wait "$CPID"; RC=$?; kill "$KPID" 2>/dev/null; wait "$KPID" 2>/dev/null; WALL=$(( $(date +%s) - START ))
[ -e "$T/timed-out" ] && fail "claude killed at the ${E2E_TIMEOUT}s timeout"
[ "$RC" = 0 ] || fail "claude exit $RC"
MODE=$(grep -m1 '"subtype":"init"' "$T/stream.jsonl" | "$SYSPY" -c 'import json,sys; print(json.loads(sys.stdin.read() or "{}").get("permissionMode"))' 2>/dev/null)
[ "$MODE" = acceptEdits ] || fail "session permission mode is $MODE, not acceptEdits"
$PY Drawer/mem/mem.py get "$CARD" | head -1 | grep -q ' done ' || fail "card $CARD is not done: $($PY Drawer/mem/mem.py get $CARD | head -1)"
HEAD=$(git -C "$T/origin.git" rev-parse main)
[ "$HEAD" != "$BASE" ] || fail "origin main did not move"
for f in Drawer/hub-hello.py Drawer/test_hub_hello.py; do git -C "$T/origin.git" cat-file -e "main:$f" 2>/dev/null || fail "$f not on origin main"; done
[ "$(git rev-parse HEAD)" = "$HEAD" ] || fail "the main checkout is not at origin main"
$PY -m pytest Drawer/test_hub_hello.py -q -p no:cacheprovider >"$T/hello.log" 2>&1 || fail "test_hub_hello.py red ($T/hello.log)"
[ "$(git worktree list | wc -l | tr -d ' ')" = 1 ] || fail "worktree left: $(git worktree list | tail -n +2 | tr '\n' ' ')"
[ "$(git for-each-ref --format=x refs/heads | wc -l | tr -d ' ')" = 1 ] || fail "local branch left: $(git branch --format='%(refname:short)' | tr '\n' ' ')"
[ "$(git -C "$T/origin.git" for-each-ref --format=x refs/heads | wc -l | tr -d ' ')" = 1 ] || fail "origin branch left"
if [ -n "$PACK" ]; then
  [ "$(wc -l <"$T/godot.log" | tr -d ' ')" -gt "$INIT_RUNS" ] || fail "--pack: the land smoke did not run the pack gate"
  $PY Drawer/harness-conform.py settings >"$T/conform.log" 2>&1 || fail "--pack: conform settings red ($T/conform.log)"
  PWT="$T/product_wt/e2e"
  { git -C "$T/product" worktree add -q -b task/e2e "$PWT" origin/main && echo e2e >"$PWT/e2e-note.txt" &&
    git -C "$PWT" add e2e-note.txt && git -C "$PWT" -c user.name=e2e -c user.email=e2e@example.invalid commit -qm "e2e product change"; } ||
    fail "--pack: cannot make the product change"
  PUSHED=$($PY Drawer/product-push.py "$PWT" --session e2e00000 2>"$T/product-push.err" | tail -1)
  case "$PUSHED" in *'"state": "pushed"'*) ;; *) fail "--pack: product-push said: $PUSHED (log $T/product-push.err)" ;; esac
  [ "$(git -C "$T/product-origin.git" rev-parse main 2>/dev/null)" = "$(git -C "$PWT" rev-parse HEAD)" ] ||
    fail "--pack: the product change is not on the product origin"
fi
TOK=$("$SYSPY" - "$T" <<'PY'
import glob, json, os, re, sys
t = sys.argv[1]; seen = {}  # the run's project folders: ~/.claude/projects/<the temp path, munged>*
for f in glob.glob(os.path.expanduser("~/.claude/projects/") + re.sub(r"[^A-Za-z0-9]", "-", t) + "*/**/*.jsonl", recursive=True):
    for line in open(f, errors="replace"):
        try: m = json.loads(line).get("message") or {}
        except ValueError: continue
        if isinstance(m, dict) and m.get("usage") and m.get("id"): seen[m["id"]] = m["usage"]
s = lambda k: sum(u.get(k) or 0 for u in seen.values())
print(f"messages={len(seen)} input={s('input_tokens')} cache_read={s('cache_read_input_tokens')} "
      f"cache_write={s('cache_creation_input_tokens')} output={s('output_tokens')}")
PY
)
COST=$(grep '"type":"result"' "$T/stream.jsonl" | tail -1 | "$SYSPY" -c 'import json,sys; d=json.loads(sys.stdin.read() or "{}"); print(d.get("total_cost_usd"), d.get("num_turns"), len(d.get("permission_denials") or []))' 2>/dev/null)
echo "E2E${PACK:+ (pack)} wall=${WALL}s tokens: $TOK flight_cost_usd/turns/denials=$COST mode=$MODE sha=$HEAD dir=$T"
if [ "$FAILS" = 0 ]; then
  echo "E2E PASS"
  if [ -z "$KEEP" ]; then  # the temp folder, its trust rows in ~/.claude.json and its transcript folders
    "$SYSPY" "$SRC/Drawer/e2e_clean.py" "$T" && trash "$T" || { echo "FAIL e2e cleanup; kept $T"; exit 1; }
  fi
  exit 0
fi
echo "E2E FAIL ($FAILS checks; kept $T)"; exit 1
