# Sourced by the git hooks; needs $repo and $python. Runs one copied gate script.
# [gates] bind = true in harness.toml: a failure blocks. Otherwise it is printed and the hook goes on.
# A missing script is skipped, so a scratch copy of the hub still commits.
bound_gate() {
  hook=$1; script=$2; shift 2
  test -f "$script" || return 0
  "$python" -B "$script" "$@" && return 0
  rc=$?
  bind=$("$python" -B "$repo/Drawer/harness_config.py" --get gates.bind 2>/dev/null)
  if test "$bind" = "true"; then return $rc; fi
  echo "$hook: $(basename "$script") failed (rc $rc); report only until [gates] bind = true." >&2
  return 0
}
