#!/bin/sh
# Flight.command: double-click it in Finder (or run ./Flight.command) to open the hub session with Code Mode on.
# The session then waits for input: type /flight to start the lead session.
#
# Claude Code takes CLAUDE_CODE_EXPERIMENTAL_CODE_MODE only from the shell it starts in, a --settings file or the
# user settings: the env of a project .claude/settings.json cannot turn Code Mode on. This file sets it for this one
# start, so no user setting changes. The first start on a machine shows the Code Mode prompt once; the human answers.
# The model and effort come from the project .claude/settings.json.
# A checkout that setup has not finished (no .venv, or no hub.bound from setup step 5) starts /setup with plain
# tools instead: the setup gates are proven on them.
# Arguments go to claude: ./Flight.command --model opus /flight runs claude --model opus /flight.
cd "$(dirname "$0")" || exit 1
if ! command -v claude >/dev/null 2>&1; then
  echo "Claude Code is not installed on this computer."
  echo "Install it (https://docs.claude.com/en/docs/claude-code), then double-click this file again."
  exit 1
fi
if [ ! -x .venv/bin/python ] || [ "$(git config --get hub.bound 2>/dev/null)" != true ]; then
  exec claude "$@" /setup
fi
export CLAUDE_CODE_EXPERIMENTAL_CODE_MODE=1
# The marker a plain-claude Flight lacks: flight-hooks.py seat reads it to say Code Mode is off.
export HARNESS_FLIGHT_LAUNCHER=1
exec claude "$@"
