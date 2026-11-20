#!/bin/sh
# Godot pack product gate (PRD-1, PRD-2). Copy it into the product repo as tools/gate.sh.
# [product] gate_command runs it in the product checkout: sh tools/gate.sh [<Godot project folder>].
# Green needs all of: tests/smoke_test.gd exists, the headless import and the headless test exit 0, neither prints a
# SCRIPT ERROR line, and the test prints a SMOKE PASS line. A test that ran zero checks is red (PRD-1).
# Exit: 0 green, 1 red, 75 BLOCKED: no Godot here ([smoke] blocked_exit in the hub's harness.toml).
# Godot is GODOT, else godot on PATH, else the macOS app (GODOT_APP names another app binary).
# Windows (Git Bash): GODOT must name the console build (Godot_v..._console.exe), the one that writes the SMOKE PASS line to stdout.
# Green on a clean checkout also writes <repo>/.gate-verdict: the tree id of HEAD. .githooks/pre-push reads it (PRD-2).
set -u
project=${1:-.}
blocked=75
app=${GODOT_APP:-/Applications/Godot.app/Contents/MacOS/Godot}
red() { echo "gate: RED: $*" >&2; exit 1; }
find_godot() {
  if test -n "${GODOT:-}"; then command -v "$GODOT"; return; fi
  command -v godot || { test -x "$app" && echo "$app"; }
}
godot=$(find_godot 2>/dev/null) || godot=
test -n "$godot" || { echo "gate: BLOCKED: no Godot: set GODOT, put godot on PATH or run: brew install --cask godot (Windows: winget install GodotEngine.GodotEngine, then set GODOT to the _console.exe)" >&2; exit "$blocked"; }
top=$(git -C "$project" rev-parse --show-toplevel 2>/dev/null) || top=
test -z "$top" || : >"$top/.gate-verdict" # a stale verdict never covers a new run
test -f "$project/tests/smoke_test.gd" || red "$project/tests/smoke_test.gd is missing: no test ran"
out=$("$godot" --headless --path "$project" --import 2>&1); code=$?
printf '%s\n' "$out"
test "$code" -eq 0 || red "the import exited $code"
case $out in *"SCRIPT ERROR"*) red "the import printed SCRIPT ERROR" ;; esac
out=$("$godot" --headless --path "$project" --script res://tests/smoke_test.gd 2>&1); code=$?
printf '%s\n' "$out"
test "$code" -eq 0 || red "the smoke test exited $code"
case $out in *"SCRIPT ERROR"*) red "the smoke test printed SCRIPT ERROR" ;; esac
printf '%s\n' "$out" | grep -q '^SMOKE PASS' || red "the smoke test printed no SMOKE PASS line"
if test -n "$top" && git -C "$top" diff --quiet HEAD -- 2>/dev/null && tree=$(git -C "$top" rev-parse 'HEAD^{tree}' 2>/dev/null); then
  printf '%s\n' "$tree" >"$top/.gate-verdict"
else
  echo "gate: green, but no verdict written: tracked files differ from HEAD, or there is no commit" >&2
fi
exit 0
