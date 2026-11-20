"""Fork gate: the lean model routing rule for Workflow legs and Agent calls.

PreToolUse hook on Agent and Workflow; Drawer/project-gate.py routes both here (its GATES table).

Rule (operator ruling): most work runs on haiku and sonnet. Opus is for planning and
critical reasoning only. No leg inherits the session model or the subagent model.

It refuses:
1. An Agent call whose model is not a non-opus word of harness.toml [gates] allowed_models.
2. A Workflow script with an agent() call that has no model entry in a literal options object.
3. A Workflow script or args that name a model outside [gates] allowed_models.
   Workflow args never name opus: an opus leg runs on its route row, not on a pick in args.
4. A Workflow script line that names opus with no route:<role> tag of an opus row of [routing.legs]
   (plan, review-other, tier-design).

The script comes from scriptPath, then script, then name, the Workflow tool's own precedence. A saved hub
template (.claude/workflows/<name>.js) called by name passes: Drawer/settings-sync.py routes its lines.
A script the gate cannot read passes. The scan blanks strings and comments before it looks for calls, so
prompt text that mentions agent() does not count. It guards against mistakes, not a determined bypass.

Set FORK_GATE=off to disable, FORK_GATE=dry to judge without logging.

Cost: this file, two GATES rows (Agent, Workflow) in Drawer/project-gate.py.
Maintainer: hub (whoever changes the routing rules).
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gatelib
import harness_config

GATE = gatelib.Gate("fork-gate", HERE, "FORK_GATE")
HUB = os.path.dirname(HERE)
WORKFLOWS = os.path.join(HUB, ".claude", "workflows")
CALL = re.compile(r"(?<![\w$.])agent(?![\w$])")
MODEL_NAME = re.compile(r"\s*(?:claude-)?(opus|sonnet|haiku|fable|mythos|astra)(?:[-.][-.\w]*)?\s*", re.IGNORECASE)
MODEL_KEY = re.compile(r"(model|(['\"])model\2)\s*:")
ROUTE_TAG = re.compile(r"route:([a-z0-9-]+)")
QUOTES = "'\"`"
LEAN = harness_config.load(HUB)["gates"]["lean_model"]  # the one allowed model that needs a route

AGENT_REASON = ("fork-gate: Agent call refused: model %s. Fix: set model to %s, or run opus work as a "
                "Workflow leg tagged route:plan.")
NO_MODEL_REASON = ("fork-gate: agent() call on script line %d has no model. Fix: give each agent() a literal "
                   "options object with model: %s ([routing.legs]).")
BAD_MODEL_REASON = "fork-gate: the workflow names the model %s. Fix: use %s; a leg never runs on the session model."
OPUS_ARG_REASON = ("fork-gate: Workflow args name opus. Fix: remove it; a leg runs on its [routing.legs] route, "
                  "and only these rows use opus: %s.")
OPUS_REASON = ("fork-gate: script line %d names opus with no route. Fix: use haiku or sonnet, or tag the line "
               "// route:%s for planning or critical reasoning.")


def routing(hub=HUB):
    """(allowed models, the [routing.legs] roles whose model is opus)."""
    cfg = harness_config.load(hub)
    allowed = tuple(cfg.get("gates", {}).get("allowed_models") or ())
    legs = cfg.get("routing", {}).get("legs", {})
    return allowed, tuple(sorted(n for n, row in legs.items() if isinstance(row, dict) and row.get("model") == LEAN))


def scan(src):
    """(code, strings): src with string and comment bodies blanked, and each string as (offset, text)."""
    out, strings, i, n = list(src), [], 0, len(src)
    while i < n:
        c, nxt = src[i], src[i + 1:i + 2]
        if c == "/" and nxt in ("/", "*"):
            end = src.find("\n" if nxt == "/" else "*/", i + 2)
            end = n if end < 0 else end + (0 if nxt == "/" else 2)
            body = range(i, end)
        elif c in QUOTES:
            end = skip_string(src, i)
            body, end = (range(i + 1, n), n) if end is None else (range(i + 1, end - 1), end)
            strings.append((i, src[body.start:body.stop]))
        else:
            i += 1
            continue
        for k in body:
            if out[k] != "\n":
                out[k] = " "
        i = end
    return "".join(out), strings


def skip_string(src, i):
    """The index just past the string or template literal that opens at src[i]; None if unclosed."""
    quote, n, k = src[i], len(src), i + 1
    while k < n:
        if src[k] == "\\":
            k += 2
        elif src[k] == quote:
            return k + 1
        elif quote == "`" and src.startswith("${", k):
            depth, k = 1, k + 2
            while k < n and depth:
                if src[k] in QUOTES:
                    k = skip_string(src, k)
                    if k is None:
                        return None
                    continue
                depth += (src[k] == "{") - (src[k] == "}")
                k += 1
        else:
            k += 1
    return None


def closing(code, opened):
    """The index of the bracket that closes code[opened], or -1."""
    depth = 0
    for k in range(opened, len(code)):
        if code[k] in "([{":
            depth += 1
        elif code[k] in ")]}":
            depth -= 1
            if depth == 0:
                return k
    return -1


def split_top(code, start, stop):
    """The (start, stop) spans of the comma-separated items in code[start:stop], depth 0 only, trimmed."""
    spans, depth, begin = [], 0, start
    for k in range(start, stop):
        if code[k] in "([{":
            depth += 1
        elif code[k] in ")]}":
            depth -= 1
        elif code[k] == "," and depth == 0:
            spans.append((begin, k))
            begin = k + 1
    spans.append((begin, stop))
    out = []
    for a, b in spans:
        while a < b and code[a].isspace():
            a += 1
        while b > a and code[b - 1].isspace():
            b -= 1
        if a < b:
            out.append((a, b))
    return out


def line_of(src, offset):
    return src.count("\n", 0, offset) + 1


def has_model(src, code, args):
    """True when the call's second argument is a literal options object with a model entry."""
    if len(args) < 2:
        return False
    a, b = args[1]
    if code[a] != "{" or closing(code, a) != b - 1:
        return False
    return any(MODEL_KEY.match(src[x:y]) or code[x:y] == "model" for x, y in split_top(code, a + 1, b - 1))


def unmodelled(src, code):
    """The line of the first agent() call with no model entry, or None."""
    for m in CALL.finditer(code):
        opened = m.end()
        while opened < len(code) and code[opened].isspace():
            opened += 1
        end = closing(code, opened) if code[opened:opened + 1] == "(" else -1
        if end >= 0 and not has_model(src, code, split_top(code, opened + 1, end)):
            return line_of(src, m.start())
    return None


def arg_strings(value):
    """Every string inside the Workflow args, at any depth."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from arg_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from arg_strings(v)


def script_reason(src, allowed, opus_roles):
    """The refusal for one Workflow script, or None."""
    code, strings = scan(src)
    line = unmodelled(src, code)
    if line:
        return NO_MODEL_REASON % (line, " or ".join(repr(m) for m in allowed if m != LEAN))
    lines = src.split("\n")
    for offset, text in strings:
        if not MODEL_NAME.fullmatch(text):
            continue
        if text.strip() not in allowed:
            return BAD_MODEL_REASON % (gatelib.clip(text.strip(), 32), ", ".join(allowed))
        at = line_of(src, offset)
        if text.strip() == LEAN and not any(tag in opus_roles for tag in ROUTE_TAG.findall(lines[at - 1])):
            return OPUS_REASON % (at, opus_roles[0] if opus_roles else "plan")
    return None


def workflow_source(inp):
    """(source or None, saved): the Workflow call's script; saved is True for a hub template called by name."""
    try:
        if inp.get("scriptPath"):
            with open(str(inp["scriptPath"]), encoding="utf-8-sig") as f:
                return f.read(), False
        if inp.get("script"):
            return str(inp["script"]), False
        name = str(inp.get("name") or "")
        if name and not re.search(r"[\\/]|\.\.", name):
            with open(os.path.join(WORKFLOWS, name + ".js"), encoding="utf-8-sig") as f:
                return f.read(), True
    except (OSError, ValueError):  # unreadable or not UTF-8: the gate passes it
        pass
    return None, False


def reason(data, hub=HUB):
    """The refusal for one PreToolUse event, or None."""
    inp = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    allowed, opus_roles = routing(hub)
    cheap = [m for m in allowed if m != LEAN]
    if data.get("tool_name") == "Agent":
        model = str(inp.get("model") or "").strip()
        if model in cheap:
            return None
        return AGENT_REASON % (gatelib.clip(model, 24) if model else "missing", " or ".join(cheap))
    for text in arg_strings(inp.get("args")):
        named = MODEL_NAME.fullmatch(text)
        if named and named.group(1).lower() == LEAN:
            return OPUS_ARG_REASON % gatelib.clip(", ".join(opus_roles) or "none", 80)
        if named and text.strip() not in allowed:
            return BAD_MODEL_REASON % (gatelib.clip(text.strip(), 32), ", ".join(allowed))
    src, saved = workflow_source(inp)
    if src is None or saved:
        return None
    return script_reason(src, allowed, opus_roles)


def run(data):
    """Judge one already-parsed PreToolUse event. The dispatcher calls this."""
    if GATE.off() or data.get("tool_name") not in ("Agent", "Workflow"):
        return
    verdict = reason(data)
    if verdict:
        GATE.log("deny %s %s" % (data.get("tool_name"), verdict[:200]))
        gatelib.deny(verdict)


def main():
    if sys.argv[1:] in (["--help"], ["-h"]):
        print(__doc__)
        return
    if GATE.off():
        return
    try:
        data = json.loads(sys.stdin.buffer.read())
    except (json.JSONDecodeError, ValueError):
        return
    if isinstance(data, dict):
        run(data)


if __name__ == "__main__":
    main()
