"""Project gate -- the hub's one project-scope PreToolUse hook, and the one table
of gate registrations.

Claude Code starts an interpreter for every hook entry whose matcher fires. This
file is the single entry: it reads the hook JSON once, refuses a manual polling
loop on Bash (a Monitor command may poll), and on Bash or Monitor an unscoped search, a history rewrite, a HEAD move off a branch or
a hook bypass, a delete or kill outside the session's own work, a shell write to a law file (Drawer/git_guard.py) and a PR comment post with a placeholder card id. Then it routes by tool_name and imports the one gate that judges the call. A tool
that no gate claims returns before any gate module loads.

GATES below is the whole rule. One row per tool name:
  claude  - the Claude Code tool name
  policy  - the gate file under Drawer/ that judges the call, or None when only this
            file's own Bash refusals apply
  kg      - True when the memory MCP server exposes the tool, so the matcher also accepts
            the prefixed spellings (mcp__<server>__ with - or _, server name
            from [mem] server_name in harness.toml)

Everything else derives from the table: ROUTES and KG_TOOLS here, and
Drawer/settings-sync.py renders the Claude PreToolUse matcher from it.
Drawer/harness-conform.py already fails on generated-file drift.

Fail closed: hook JSON this file cannot read denies, because it cannot tell which
gate the call belonged to.

Cost: this file and the Claude manifest entry.
Maintainer: hub (whoever adds or retires a gate).
"""
import json
import os
import re
import sys
from collections import namedtuple

HERE = os.path.dirname(os.path.abspath(__file__))
PY = ".venv/bin/python -B"  # a new seat has no py launcher

Row = namedtuple("Row", "claude policy kg")

GATES = (
    #   claude tool        policy file               kg
    Row("Write",           "doc-gate.py",            False),
    Row("Edit",            "doc-gate.py",            False),
    Row("MultiEdit",       "doc-gate.py",            False),
    Row("NotebookEdit",    "doc-gate.py",            False),
    Row("Agent",           "fork-gate.py",           False),  # model routing: no leg on the session model
    Row("Workflow",        "fork-gate.py",           False),
    Row("Bash",            None,                     False),  # the poll and search refusals only
    Row("Monitor",         None,                     False),  # a watch: the Bash refusals but the poll one
    Row("add",             None,                     True),   # the memory MCP add: matched, judged by no gate file
)


def _server_name():
    """The hub memory's MCP server name from harness.toml ([mem] server_name)."""
    sys.path.insert(0, HERE)
    import harness_config
    return harness_config.server_name(harness_config.load(os.path.dirname(HERE)))


ROUTES = {row.claude: row.policy for row in GATES if row.claude and row.policy}
# The memory server reaches the same tool under two spellings of its own name.
SERVER = _server_name()
KG_PREFIXES = tuple(dict.fromkeys("mcp__%s__" % SERVER.replace(a, b) for a, b in (("_", "-"), ("-", "_"))))
KG_TOOLS = tuple(row.claude for row in GATES if row.claude and row.kg)
KG_MATCHER_PREFIX = "(mcp__%s__)?" % "".join("[-_]" if c in "-_" else re.escape(c) for c in SERVER)

UNREADABLE = (
    "project-gate: cannot read the hook JSON, so it denies (fail closed). "
    f"Fix: retry; if it repeats, run {PY} Drawer/project-gate.py < the-event.json."
)

# A live polling loop: a loop whose own span both sleeps and checks (a grep, a Select-String,
# or a background task's .output file). A one-off read of a task's output after its notice,
# and loop words in a heredoc body, are not polls.
POLL_SLEEP = re.compile(r"(?i)(?:^|[;&|({\n]|\bdo\b|\bthen\b|\belse\b)\s*(?:sleep|start-sleep)\b")  # in command position
POLL_CHECK = re.compile(
    r'''(?ix)\b(?:grep|select-string)\b'''
    r'''|(?:^|[\s'"`=:/\\(\[])tasks[\\/][^ \t\r\n'"`;&|()<>]*\.output(?=$|[\s'"`;&|()<>])'''
)
LOOP_WORD = re.compile(r"(?i)\b(?:while|until|for)\b")
SH_DO = re.compile(r"\b(do|done)\b")
# Since CLI 2.1.314 a Monitor command also matches a Bash hook matcher. The CLI runs one hook entry once per
# call, so this entry (matcher ...|^Bash$|^Monitor$) sees a Monitor call once, as tool_name Monitor (probed on
# 2.1.316). A poll loop is right in a Monitor command and wrong in a Bash call.
SHELL_TOOLS = ("Bash", "Monitor")
MANUAL_POLL_REASON = (
    "project-gate: manual polling is refused. Use Monitor for background jobs, "
    "or a foreground Workflow leg for bounded worker legs."
)

# A PR comment with a placeholder card id: the post was composed in the shell call that wrote the card, before its
# id existed. The body text is read whole, heredoc too.
# In command position only: a lesson or an incident line that quotes the commands is no post.
COMMAND_AT = r"(?:^|[;&|(\n]|\b(?:then|do|else)\b)\s*(?:[A-Za-z_]\w*=\S*\s+)*"
PR_POST = re.compile(COMMAND_AT + r"gh\s+(?:pr\s+comment\b|api\b(?=[^\n]*issues/(?:[^\s/]+/)?comments)"
                     r"(?=[^\n]*\s(?:-f|-F|--field|--raw-field|--input|-X\s*(?:POST|PATCH)|--method\s+(?:POST|PATCH))\b))")
CARD_WORD = r"(?:lesson|task|card|decision|fact|design|taste|incident|record|row|id|mem)"
PLACEHOLDER_ID = re.compile(r"(?i)(?:\b" + CARD_WORD + r"\s+[0-9a-f]{1,7}|(?<![\w-])(?=[0-9a-f]{0,6}\d)[0-9a-f]{1,7})"
                            r"(?:\.\.\.|\u2026)|\bid\s+below\b|\bin\s+the\s+next\s+line\b|\bTBD\s+id\b")
MEM_ADD = re.compile(COMMAND_AT + r"(?:\S*python\S*\s+(?:-\S+\s+)*)?\S*\bmem(?:\.py)?\s+add\b")
PLACEHOLDER_POST_REASON = (
    "project-gate: a PR comment post with a placeholder card id is refused. "
    "Fix: create the card in its own shell call, read its printed id, then post the full 8-char id."
)


def placeholder_post_reason(event):
    """Refuse a gh PR comment post whose body holds a placeholder card id, or that runs with a mem add."""
    tool_input = event.get("tool_input") if event.get("tool_name") in SHELL_TOOLS else None
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not PR_POST.search(command):
        return None
    if PLACEHOLDER_ID.search(command) or MEM_ADD.search(command):
        return PLACEHOLDER_POST_REASON
    return None


def guard_reason(command, event):
    """Refuse a history rewrite, a hook bypass, or a delete or kill outside the session's own work
    in the product clone or the hub: Drawer/git_guard.py holds the rules."""
    # Every shell call loads the guard: a load-word list missed feeds such as a=r; b=m; bash -c "$a$b".
    if command is None:
        return None
    sys.path.insert(0, HERE)
    import git_guard
    try:
        return git_guard.reason(command, event)
    except Exception as exc:  # a crash passes the call: refuse a line with a delete or kill word, else crash as before
        if git_guard.DELETE_TEXT.search(command):
            return (git_guard.DELETE_REFUSED + f"the gate faulted ({type(exc).__name__[:24]}) on a delete or kill "
                    f"line; cannot place it." + git_guard.DELETE_REMEDY)
        raise


def matcher_term(tool, kg):
    """One anchored regex alternative for a tool name."""
    return "^%s%s$" % (KG_MATCHER_PREFIX if kg else "", tool)


def claude_matcher():
    """The Claude PreToolUse matcher: every routed tool, anchored per name."""
    return "|".join(matcher_term(row.claude, row.kg) for row in GATES if row.claude)


def accepted_tools(policy):
    """Every tool name that reaches this policy file. A gate reads this instead of
    restating its own tool set."""
    return {row.claude for row in GATES if row.policy and row.policy == policy and row.claude}


def gate_for(tool):
    """The gate file that judges this tool, or None. Keep this cheap: every tool
    call that no gate claims must return without importing a gate."""
    gate = ROUTES.get(tool)
    if gate:
        return gate
    for prefix in KG_PREFIXES:
        if tool.startswith(prefix) and tool[len(prefix):] in KG_TOOLS:
            return ROUTES.get(tool[len(prefix):])
    return None


def loop_spans(command):
    """The text of each loop: a shell keyword through its matching done."""
    for m in LOOP_WORD.finditer(command):
        rest = command[m.end():]
        do = SH_DO.search(rest)
        if do and do.group(1) == "do":
            depth = 0
            for d in SH_DO.finditer(rest, do.start()):
                depth += 1 if d.group(1) == "do" else -1
                if depth == 0:
                    yield command[m.start():m.end() + d.end()]
                    break
            else:
                yield command[m.start():]  # an unclosed loop runs to the end of the text


def shell_command(event):
    """A shell tool's command (Bash or Monitor) with every document body cut out (a body is data), else None."""
    if event.get("tool_name") not in SHELL_TOOLS:
        return None
    tool_input = event.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return None
    sys.path.insert(0, HERE)
    import git_guard
    for m in reversed(git_guard.documents(command)):  # a quoted '<<EOF' is text, not a document
        command = command[:m.start()] + m.group(0).split("\n", 1)[0] + command[m.end():]  # the opener line stays
    return command


def manual_poll_reason(event):
    """Refuse a live polling loop in a Bash call: a loop that sleeps and checks within its own span.
    A Monitor command is a watch, so its poll loop passes."""
    command = shell_command(event)
    if command is None or event.get("tool_name") == "Monitor":
        return None
    for span in loop_spans(command):
        if POLL_SLEEP.search(span) and POLL_CHECK.search(span):
            return MANUAL_POLL_REASON
    return None


def load(filename):
    """Import one gate module by path; its file name is not an identifier. A module loads once per process."""
    import importlib.util
    name = "gate_" + filename[:-3].replace("-", "_")
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main():
    if sys.argv[1:] in (["--help"], ["-h"]):
        print(__doc__)
        return 0
    try:
        event = json.loads(sys.stdin.buffer.read())
        ok = isinstance(event, dict)
    except (json.JSONDecodeError, ValueError):
        ok = False
    if not ok:
        sys.path.insert(0, HERE)
        import gatelib
        gatelib.deny(UNREADABLE)
        return 0
    command = shell_command(event)
    refusal = manual_poll_reason(event) or guard_reason(command, event) or placeholder_post_reason(event)
    if refusal or command is not None:
        sys.path.insert(0, HERE)
        import gatelib
        refusal = refusal or gatelib.unscoped_search_reason(command)
        if refusal:
            gatelib.deny(refusal)
            return 0
    gate = gate_for(str(event.get("tool_name") or ""))
    if gate is None:
        return 0
    load(gate).run(event)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
