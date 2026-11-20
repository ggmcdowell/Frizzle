#!/bin/sh
# Hub hook wrapper: run one hook under the first working Python, or answer for it.
#
#   sh run-hook.sh <boot|gate|pass> <script.py> [args...]
#
# Interpreter ladder: the prepared hub environment (.venv at the hub root: Scripts/python.exe on Windows,
# bin/python elsewhere), then a python3, then a python, each 3.12 or newer. Every argument passes
# through unchanged, so --part and --answer reach the hook. With no interpreter at all the
# checkout is not set up on this machine: a boot announces HUB SETUP REQUIRED, a gate
# denies everything except the exact setup install commands (fail closed), and a pass exits 0.
mode=$1
# sh runs a script one command at a time, so this trap is set before a later syntax error is read.
# A wrapper that breaks before its gate answers exits non-zero with no JSON, a bare hook error; in
# gate mode the trap turns that into a loud allow naming the repair. A broken gate never locks the
# hub: a deny on every call leaves no session able to repair it. Keep this block above anything
# that can break.
trap '[ $? -ne 0 ] && [ "$mode" = gate ] && { printf "%s\n" "{\"systemMessage\": \"HUB HOOK WRAPPER BROKEN: Drawer/hooks/run-hook.sh stopped before its gate answered, so this call is ALLOWED UNCHECKED. Repair it now: git checkout -- Drawer/hooks/run-hook.sh in the hub, or git pull when main carries the break.\"}"; exit 0; }' EXIT
# The gate budget below uses read -t, which dash (a Linux /bin/sh) lacks: a gate killed on every call allowed
# every call unchecked. Run under bash when this shell is not bash and bash is there; a Mac's sh is bash.
if [ -z "$BASH_VERSION" ] && command -v bash >/dev/null 2>&1; then exec bash "$0" "$@"; fi
script=$2
shift 2

# The mode decides what an answer means, so an unknown mode is refused here, above the interpreter
# ladder and the gate: a hook registered with a typo must never exec its script as a silent pass.
case "$mode" in
    boot|gate|pass) ;;
    *)
        echo "run-hook.sh: unknown mode '$mode' (boot|gate|pass)" >&2
        exit 2
        ;;
esac
# A pass hook whose script is gone (a live session keeps the hook list it started with) is a
# no-op, never a Python error on every prompt or tool call. A gate stays fail closed below.
if [ "$mode" = pass ] && [ ! -f "$script" ]; then exit 0; fi

# A backslash path (C:\x\hub\Drawer\hooks\run-hook.sh) has no slash for dirname to split: map it to the
# msys form when cygpath is there (it exists on Git for Windows only).
self=$0
case "$self" in *\\*) command -v cygpath >/dev/null 2>&1 && self=$(cygpath -u "$self") ;; esac
here=$(dirname -- "$self")
hub=$(cd "$here/../.." 2>/dev/null && pwd) || hub="$here/../.."

# A rung counts only when it runs and is Python 3.12 or newer (pyproject requires-python): a Mac
# without the command line tools keeps a /usr/bin/python3 stub, and stock macOS python3 is 3.9,
# which cannot import tomllib. A dead or old rung must fall through to the no-Python answers,
# because an exec into a crash prints no banner at all.
runs() { [ -n "$1" ] && "$1" -c 'import sys; sys.exit(sys.version_info < (3, 12))' </dev/null >/dev/null 2>&1; }

python=""
# The prepared environment is trusted on its executable bit, not probed, because this is every
# hook call's hot path; probe it too if a half-deleted .venv ever reaches a fresh clone.
# On Windows uv puts the interpreter at .venv/Scripts/python.exe (-f: msys has no useful x bit), and
# .venv/bin/python is a shim for command text; the real file comes first.
if [ -f "$hub/.venv/Scripts/python.exe" ]; then python="$hub/.venv/Scripts/python.exe"
elif [ -x "$hub/.venv/bin/python" ]; then python="$hub/.venv/bin/python"; fi
notice="HUB SETUP REQUIRED (no Python 3.12): this checkout is not set up on this machine. Read .claude/skills/setup/SKILL.md and follow it (brew on a Mac, winget on Windows). Do nothing else."
reason="HUB SETUP REQUIRED (no Python 3.12): fail-closed denies this call. Only uv sync, or brew or winget install of uv or gitleaks, pass alone. Read .claude/skills/setup/SKILL.md."
if [ -z "$python" ]; then
    for name in python3 python; do
        found=$(command -v "$name" 2>/dev/null)
        if runs "$found"; then python=$found; break; fi
    done
fi
# Every hub Python runs in UTF-8 mode: a Windows pipe defaults to the ANSI code page.
export PYTHONUTF8=1
# A gate answers deny on stdout as JSON and exits 0. A gate never execs: the wrapper keeps the
# process and holds a budget under the harness timeout. A crash (non-zero exit, no JSON) or a run
# past the budget ALLOWS the call with a loud warning that names the gate and its error: a gate
# that denied on its own crash once locked every hub session out, with no session able to repair it.
if [ -n "$python" ] && [ "$mode" = "gate" ]; then
    payload=$(cat)
    work=$(mktemp -d 2>/dev/null)
    status=1
    detail=""
    if [ -n "$work" ]; then
        printf '%s' "$payload" > "$work/in"
        # The budget is a timed read, never a sleep process: the left side runs the gate, holds
        # the pipe open until the gate ends, then writes one line, so a fast gate's read returns
        # at once and the call leaves no process. A budget sleep the wrapper killed lived on
        # whenever msys dropped the kill (222 were once live). A read that gets no line in the
        # budget kills the gate, which the left records as 137. The line, not the read status,
        # tells the two apart: a Mac's bash 3.2 answers 1 for a timeout and for EOF alike, and
        # reads a whole number of seconds only, so the budget drops any fraction.
        budget=${HOOK_GATE_BUDGET:-150}
        budget=${budget%%.*}
        case "$budget" in ""|*[!0-9]*) budget=150 ;; esac
        { "$python" -B "$script" "$@" < "$work/in" > "$work/out" 2> "$work/err" &
          echo "$!"; wait "$!"; echo "$?" > "$work/status"; echo ended; } | {
            read -r child
            # On Windows a venv python.exe is a launcher over the real interpreter: kill -9 stops only the
            # msys pid, so taskkill ends the whole Windows tree (/proc/<pid>/winpid exists on msys only).
            read -r -t "$budget" _ || { winpid=$(cat "/proc/$child/winpid" 2>/dev/null)
                kill -9 "$child"
                [ -z "$winpid" ] || taskkill //F //T //PID "$winpid" >/dev/null 2>&1; }; }
        status=$(cat "$work/status" 2>/dev/null)
        [ -n "$status" ] || status=1
        cat "$work/err" >&2
        if [ "$status" -eq 0 ]; then cat "$work/out"; rm -rf "$work"; exit 0; fi
        # The gate's last stderr lines ride in the warning, so the agent reads why without a rerun:
        # printable ASCII only, cut before escaping, so the JSON string always closes.
        detail=$(tail -n 5 "$work/err" | tr '\r\n\t' '   ' | tr -cd ' -~' | cut -c1-600 \
            | sed 's/\\/\\\\/g; s/"/\\"/g')
        rm -rf "$work"
    fi
    warning="HUB GATE CRASHED (exit $status): $(basename "$script" | tr -cd ' -~' | sed 's/\\/\\\\/g; s/"/\\"/g') did not finish, so this call is ALLOWED UNCHECKED. Fix the gate now."
    [ -n "$detail" ] && warning="$warning Gate stderr: $detail"
    printf '%s\n' "$warning" >&2
    printf '{"systemMessage": "%s"}\n' "$warning"
    exit 0
fi
if [ -n "$python" ]; then
    exec "$python" -B "$script" "$@"
fi

# The shell commands a gate passes with no Python are the setup install commands the setup skill
# names: uv sync (with at most --frozen), brew install uv and brew install gitleaks, and on Windows
# winget install --id astral-sh.uv -e and winget install --id Gitleaks.Gitleaks -e, each alone.
# Anything else is denied.
prep='^(uv sync( --frozen)?|brew install (uv|gitleaks)|winget install --id (astral-sh\.uv|Gitleaks\.Gitleaks) -e)$'
# The shape is not enough: uv sync prepares the project above the working directory, and a
# session outside this checkout would prepare another one. Keep it only when the hook runs from
# inside this checkout (the hub root or a folder below it).
here_is_the_checkout() {
    dir=$(pwd -P) || return 1
    root=$(cd "$hub" 2>/dev/null && pwd -P) || return 1
    case "$dir/" in "$root"/*) return 0 ;; esac
    return 1
}
case "$mode" in
    boot)
        payload=$(cat)
        event=SessionStart
        case "$payload" in *SubagentStart*) event=SubagentStart ;; esac
        printf '{"hookSpecificOutput": {"hookEventName": "%s", "additionalContext": "%s"}}\n' "$event" "$notice"
        ;;
    gate)
        payload=$(cat)
        # Without a JSON parser: exactly one "command" key and one "tool_name" key may appear, so
        # neither match can land on a nested decoy.
        keys=$(printf '%s' "$payload" | grep -o '"command"[[:space:]]*:' | wc -l | tr -d ' ')
        tools=$(printf '%s' "$payload" | grep -o '"tool_name"[[:space:]]*:' | wc -l | tr -d ' ')
        command=$(printf '%s' "$payload" | sed -n 's/.*"command"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)
        if [ "$keys" = "1" ] && [ "$tools" = "1" ] \
            && printf '%s' "$payload" | grep -Eq '"tool_name"[[:space:]]*:[[:space:]]*"Bash"' \
            && [ -n "$command" ]; then
            if printf '%s\n' "$command" | grep -Eqx "$prep" && here_is_the_checkout; then
                exit 0
            fi
        fi
        printf '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "%s"}}\n' "$reason"
        ;;
    pass)
        exit 0
        ;;
esac
exit 0
