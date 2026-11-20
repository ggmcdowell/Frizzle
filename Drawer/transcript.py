#!/usr/bin/env python3
"""transcript.py -- one CLI for mining Claude Code session transcripts.

Stdlib only.

Layout facts this tool bakes in (previously undocumented, rediscovered by hand
in at least four sessions):
  - Per-project transcripts live in  ~/.claude/projects/<mangled>/<session-id>.jsonl
    where <mangled> is the project path with every non-alphanumeric character
    replaced by '-'  (C:\\src\\Hub -> C--src-Hub).
  - Subagent transcripts are NOT in the parent .jsonl (isSidechain finds
    nothing). They live in  <project-dir>/<session-id>/subagents/agent-<id>.jsonl
    with an agent-<id>.meta.json beside each (agentType, spawnDepth, model).

Counting rules (what the numbers mean):
  - A record is one JSONL line. Only records with type 'user' or 'assistant'
    are conversation; every other type (attachment, mode, system,
    file-history-*, ai-title, last-prompt, queue-operation, ...) is metadata.
  - An ASSISTANT TURN is one unique message.id among type=assistant records
    (streaming/multi-block messages emit several records sharing one id;
    usage is counted once per unique id, from the record where it appears).
  - A USER PROMPT is a type=user record whose content is a plain string or a
    text block, isMeta not set -- tool_result-only user records are plumbing.
  - Token figures come from message.usage: input_tokens, output_tokens,
    cache_creation_input_tokens, cache_read_input_tokens, summed per model.
  - peak-ctx is the largest context of any turn: the LAST executor iteration of
    message.usage.iterations (top-level usage sums them all). Advisor iterations
    (type advisor_message) are other models' calls; classify prints them on an
    'advisor <model>' line under the session row, never in the totals.
  - 'likely chat-only' label in `list`: zero tool_use blocks AND <= 3
    assistant turns. A label to aid filtering -- never an auto-exclusion.

Usage:
  <python> Drawer/transcript.py [--project PATH | --dir PATH] <cmd> [session] [options]

  session: full id, any unique substring of the filename, 'latest', or a
           direct path to a .jsonl file.

Subcommands:
  list                       all sessions: date, size, turns, first prompt
  classify [session]         all sessions (or one): model, effort, tokens, span
  tokens   <session>         token totals by model + per-turn averages
  gaps     <session> [--threshold MIN]   idle gaps with surrounding events
  tools    <session>         tool histogram + errors (tool_use <-> tool_result)
  subagents <session>        locate + summarize the session's subagent files
  extract  <session> [--out DIR]   recover Write contents; Edits listed only
  codemode [--hours N] [--exclude SID]   one JSON line: Code Mode (REPL) errors by category
                             in the hub's project dirs (subagents too), top category, root fix
  codemode --legs [--hours N] [--exclude SID]   one JSON line: per leg agentType, the REPL
                             share of top-level tool calls (nested isVirtual calls left out),
                             the 90% bar and the types below it

extract writes to the device-local temp dir by default and REFUSES to write
into the hub unless --out explicitly points there (transcripts can contain
project content; the hub checkout must not receive it by default).
"""

import argparse
import datetime
import glob
import json
import os
import re
import sys

DRAWER = os.path.dirname(os.path.abspath(__file__))
if DRAWER not in sys.path:
    sys.path.insert(0, DRAWER)
import harness_config

# A new seat has no py launcher: printed commands name this host's prepared interpreter.
PY = ".venv/bin/python -B"
__doc__ = __doc__.replace("<python>", PY)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HUB = os.environ.get(harness_config.env_name("HUB")) or os.path.dirname(DRAWER)
CLAUDE_PROJECTS = os.path.join(os.path.expanduser("~"), ".claude", "projects")
SESSION_NAME_RE = re.compile(
    r"^(?:(?P<box>.+)-)?(?P<sid>[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})$",
    re.IGNORECASE)


def mangle(path):
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(path))


def records(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def parse_ts(r):
    ts = r.get("timestamp")
    if not ts:
        return None
    try:
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def content_blocks(r):
    c = (r.get("message") or {}).get("content")
    if isinstance(c, list):
        return c
    return []


def tool_result_bytes(block):
    """UTF-8 size of one tool_result block's content (str or list of blocks)."""
    c = block.get("content")
    if isinstance(c, list):
        c = "".join(b.get("text", "") for b in c if isinstance(b, dict))
    return len(str(c or "").encode("utf-8"))


def first_prompt(path):
    for r in records(path):
        if r.get("type") != "user" or r.get("isMeta"):
            continue
        c = (r.get("message") or {}).get("content")
        texts = []
        if isinstance(c, str):
            texts = [c]
        elif isinstance(c, list):
            texts = [b.get("text", "") for b in c if b.get("type") == "text"]
        for t in texts:
            t = t.strip()
            if t and not t.startswith("<"):
                return re.sub(r"\s+", " ", t)[:100]
    return "(no plain user prompt)"


def session_stats(path):
    """(assistant_turn_count, tool_use_count) in one pass."""
    ids = set()
    tools = 0
    for r in records(path):
        if r.get("type") != "assistant":
            continue
        mid = (r.get("message") or {}).get("id")
        if mid:
            ids.add(mid)
        tools += sum(1 for b in content_blocks(r) if b.get("type") == "tool_use")
    return len(ids), tools


def turn_context(usage):
    """(context, advisors) of one message.usage. Top-level usage sums every executor
    iteration, so a turn's context is its LAST executor iteration (type 'message');
    each 'advisor_message' iteration is another model's call, returned apart as
    {"model", "input", "output"}. Without iterations the top-level counts stand."""
    iterations = usage.get("iterations") or []
    executor = [i for i in iterations if i.get("type", "message") == "message"]
    advisors = [{"model": i.get("model") or "?", "output": i.get("output_tokens", 0),
                 "input": i.get("input_tokens", 0) + i.get("cache_creation_input_tokens", 0)
                 + i.get("cache_read_input_tokens", 0)}
                for i in iterations if i.get("type") == "advisor_message"]
    last = executor[-1] if executor else usage
    context = (last.get("input_tokens", 0) + last.get("cache_creation_input_tokens", 0)
               + last.get("cache_read_input_tokens", 0))
    return context, advisors


def classify_stats(path):
    """Facts for one classify row, using the standard turn/token rules."""
    turn_ids = set()
    advisor = {}
    usage_ids = set()
    model_turns = {}
    efforts = []
    tools = 0
    errors = 0
    totals = {"in": 0, "out": 0, "cc": 0, "cr": 0}
    peak = 0
    out_bytes = 0
    first_ts = None
    last_ts = None

    for r in records(path):
        ts = parse_ts(r)
        if ts is not None:
            first_ts = ts if first_ts is None or ts < first_ts else first_ts
            last_ts = ts if last_ts is None or ts > last_ts else last_ts

        rtype = r.get("type")
        message = r.get("message") or {}

        if rtype == "user":
            for block in content_blocks(r):
                if block.get("type") == "tool_result":
                    out_bytes += tool_result_bytes(block)
                    errors += bool(block.get("is_error"))
            continue

        if rtype != "assistant":
            continue

        effort = r.get("effort")
        if effort and effort not in efforts:
            efforts.append(effort)

        for block in content_blocks(r):
            if block.get("type") != "tool_use":
                continue
            tools += 1

        mid = message.get("id")
        model = message.get("model")
        if mid and mid not in turn_ids:
            turn_ids.add(mid)
            if model:
                model_turns[model] = model_turns.get(model, 0) + 1

        usage = message.get("usage")
        if usage and not (mid and mid in usage_ids):
            usage_ids.add(mid)
            totals["in"] += usage.get("input_tokens", 0)
            totals["out"] += usage.get("output_tokens", 0)
            totals["cc"] += usage.get("cache_creation_input_tokens", 0)
            totals["cr"] += usage.get("cache_read_input_tokens", 0)
            context, advisors = turn_context(usage)
            peak = max(peak, context)
            for call in advisors:
                row = advisor.setdefault(call["model"], {"calls": 0, "input": 0, "output": 0})
                row["calls"] += 1
                row["input"] += call["input"]
                row["output"] += call["output"]

    models = "-"
    if len(model_turns) == 1:
        models = next(iter(model_turns))
    elif model_turns:
        models = ",".join("%s:%d" % item for item in model_turns.items())

    span = "-"
    if first_ts is not None and last_ts is not None:
        span = format_span(int((last_ts - first_ts).total_seconds()))

    return {
        "turns": len(turn_ids), "tools": tools, "tool_errors": errors, "models": models,
        "effort": ",".join(efforts) or "-",
        "input": totals["in"], "out": totals["out"],
        "cc": totals["cc"], "cr": totals["cr"],
        "span": span, "peak_context": peak, "tool_output_bytes": out_bytes,
        "advisor": advisor,
    }


def session_transcripts(sid):
    """Every transcript of one session (a full id, or a uuid8 prefix) on this box: the main file in any
    project folder, then each subagent and workflow leg file under <session-id>/ (agent-*.jsonl)."""
    found = []
    for main in sorted(glob.glob(os.path.join(glob.escape(CLAUDE_PROJECTS), "*", glob.escape(sid) + "*.jsonl"))):
        found.append(main)
        found += sorted(glob.glob(os.path.join(glob.escape(main[:-6]), "**", "agent-*.jsonl"), recursive=True))
    return found


def guard_counts(paths):
    """The guard metrics summed over transcripts (flight-digest.py route measure), by the classify rules:
    tool_calls (tool_use blocks), tool_errors (tool_result blocks with is_error) and tokens
    (input, cache write, cache read and output, once per message id)."""
    out = {"tool_calls": 0, "tool_errors": 0, "tokens": 0}
    for path in paths:
        stats = classify_stats(path)
        out["tool_calls"] += stats["tools"]
        out["tool_errors"] += stats["tool_errors"]
        out["tokens"] += stats["input"] + stats["cc"] + stats["cr"] + stats["out"]
    return out


def format_span(seconds):
    days, seconds = divmod(max(0, seconds), 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return (("%dd " % days) if days else "") + "%02d:%02d:%02d" % (hours, minutes, seconds)


def session_identity(path):
    stem = os.path.basename(path)[:-6]
    match = SESSION_NAME_RE.match(stem)
    if not match:
        return stem, "-"
    return match.group("sid"), match.group("box") or "-"


def resolve_dir(args):
    if args.dir:
        return args.dir
    return os.path.join(CLAUDE_PROJECTS, mangle(args.project))


def list_sessions(d):
    if not os.path.isdir(d):
        sys.exit("transcript.py: no such directory: %s" % d)
    fs = [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".jsonl")]
    return sorted(fs, key=os.path.getmtime, reverse=True)


def resolve_session(args):
    if os.path.isfile(args.session):
        return args.session
    d = resolve_dir(args)
    fs = list_sessions(d)
    if not fs:
        sys.exit("transcript.py: no transcripts in %s" % d)
    if args.session == "latest":
        return fs[0]
    hits = [f for f in fs if args.session in os.path.basename(f)]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        sys.exit("transcript.py: no session matches '%s' in %s" % (args.session, d))
    sys.exit("transcript.py: '%s' is ambiguous:\n  %s" %
             (args.session, "\n  ".join(os.path.basename(h) for h in hits)))


def brief(r):
    t = r.get("type", "?")
    if t == "assistant":
        for b in content_blocks(r):
            if b.get("type") == "tool_use":
                return "assistant tool_use %s" % b.get("name")
            if b.get("type") == "text" and b.get("text", "").strip():
                return "assistant text: %s" % re.sub(r"\s+", " ", b["text"])[:60]
        return "assistant"
    if t == "user":
        c = (r.get("message") or {}).get("content")
        if isinstance(c, str):
            return "user: %s" % re.sub(r"\s+", " ", c)[:60]
        for b in (c or []):
            if b.get("type") == "tool_result":
                return "tool_result for %s" % b.get("tool_use_id", "?")[:16]
            if b.get("type") == "text":
                return "user: %s" % re.sub(r"\s+", " ", b.get("text", ""))[:60]
    return t


# ------------------------------------------------------------- subcommands

def cmd_list(args):
    d = resolve_dir(args)
    print("Sessions in %s (newest first):\n" % d)
    for f in list_sessions(d):
        turns, tools = session_stats(f)
        label = "  [likely chat-only]" if (tools == 0 and turns <= 3) else ""
        mtime = datetime.datetime.fromtimestamp(os.path.getmtime(f))
        print("%s  %s  %7.1f KB  %3d turns  %s%s" % (
            os.path.basename(f)[:44].ljust(44),
            mtime.strftime("%Y-%m-%d %H:%M"),
            os.path.getsize(f) / 1024.0, turns, first_prompt(f), label))


def cmd_classify(args):
    if args.session:
        files = [resolve_session(args)]
        print("Session classification of %s:\n" % files[0])
    else:
        d = resolve_dir(args)
        files = list_sessions(d)
        print("Session classification in %s (newest first):\n" % d)
    print("%-36s %5s %-31s %-11s %5s %12s %12s %10s %12s %5s %9s %10s  %s" % (
        "session", "box", "model(s)", "effort", "turns",
        "cache-read", "cache-write", "output", "span", "tools", "peak-ctx", "tool-out-B", "flag"))
    for f in files:
        sid, box = session_identity(f)
        x = classify_stats(f)
        label = "[likely chat-only]" if (x["tools"] == 0 and x["turns"] <= 3) else ""
        print("%-36s %5s %-31s %-11s %5d %12d %12d %10d %12s %5d %9s %10s  %s" % (
            sid, box, x["models"], x["effort"], x["turns"],
            x["cr"], x["cc"], x["out"], x["span"], x["tools"],
            x.get("peak_context", "-"), x.get("tool_output_bytes", "-"), label))
        for model, row in sorted(x.get("advisor", {}).items()):
            print("%-36s advisor %s: %d calls, input %d, output %d" % (
                "", model, row["calls"], row["input"], row["output"]))


def cmd_tokens(args):
    path = resolve_session(args)
    per = {}
    seen = set()
    for r in records(path):
        if r.get("type") != "assistant":
            continue
        m = r.get("message") or {}
        u = m.get("usage")
        mid = m.get("id")
        if not u or (mid and mid in seen):
            continue
        seen.add(mid)
        row = per.setdefault(m.get("model", "?"),
                             {"turns": 0, "in": 0, "out": 0, "cc": 0, "cr": 0})
        row["turns"] += 1
        row["in"] += u.get("input_tokens", 0)
        row["out"] += u.get("output_tokens", 0)
        row["cc"] += u.get("cache_creation_input_tokens", 0)
        row["cr"] += u.get("cache_read_input_tokens", 0)
    print("Token usage -- %s\n" % os.path.basename(path))
    print("%-28s %6s %10s %10s %12s %14s" %
          ("model", "turns", "input", "output", "cache-write", "cache-read"))
    for model, x in sorted(per.items()):
        print("%-28s %6d %10d %10d %12d %14d" %
              (model, x["turns"], x["in"], x["out"], x["cc"], x["cr"]))
        if x["turns"]:
            print("%-28s %6s %10.0f %10.0f %12.0f %14.0f  (avg/turn)" %
                  ("", "", x["in"] / x["turns"], x["out"] / x["turns"],
                   x["cc"] / x["turns"], x["cr"] / x["turns"]))


def cmd_gaps(args):
    path = resolve_session(args)
    thr = datetime.timedelta(minutes=args.threshold)
    prev = None
    found = 0
    for r in records(path):
        ts = parse_ts(r)
        if ts is None:
            continue
        if prev is not None and ts - prev[0] > thr:
            found += 1
            mins = (ts - prev[0]).total_seconds() / 60
            print("GAP %6.1f min  %s -> %s" % (
                mins, prev[0].strftime("%m-%d %H:%M:%S"), ts.strftime("%H:%M:%S")))
            print("   before: %s" % brief(prev[1]))
            print("   after:  %s" % brief(r))
        prev = (ts, r)
    if not found:
        print("No gaps over %g min in %s" % (args.threshold, os.path.basename(path)))


def cmd_tools(args):
    path = resolve_session(args)
    hist = {}
    id2name = {}
    errors = []
    for r in records(path):
        for b in content_blocks(r):
            if r.get("type") == "assistant" and b.get("type") == "tool_use":
                hist[b.get("name", "?")] = hist.get(b.get("name", "?"), 0) + 1
                id2name[b.get("id")] = b.get("name", "?")
            elif r.get("type") == "user" and b.get("type") == "tool_result" and b.get("is_error"):
                c = b.get("content")
                if isinstance(c, list):
                    c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                errors.append((id2name.get(b.get("tool_use_id"), "?"),
                               re.sub(r"\s+", " ", str(c or ""))[:120]))
    print("Tool calls -- %s\n" % os.path.basename(path))
    for name, n in sorted(hist.items(), key=lambda kv: -kv[1]):
        ne = sum(1 for e in errors if e[0] == name)
        print("%-40s %4d%s" % (name, n, ("   (%d errors)" % ne) if ne else ""))
    if errors:
        print("\nErrors:")
        for name, msg in errors:
            print("  %-20s %s" % (name, msg))
    else:
        print("\nNo tool errors.")


def cmd_subagents(args):
    path = resolve_session(args)
    sid = os.path.basename(path)[:-6]
    sub = os.path.join(os.path.dirname(path), sid, "subagents")
    # staged copies may carry a box prefix (<box>-<uuid>); try the bare uuid too
    if not os.path.isdir(sub):
        m = re.search(r"[0-9a-f]{8}-[0-9a-f-]{27}", sid)
        if m:
            alt = os.path.join(os.path.dirname(path), m.group(0), "subagents")
            if os.path.isdir(alt):
                sub = alt
    if not os.path.isdir(sub):
        print("No subagents directory for this session (looked at %s)." % sub)
        return
    print("Subagents in %s:\n" % sub)
    for f in sorted(os.listdir(sub)):
        if not f.endswith(".jsonl"):
            continue
        full = os.path.join(sub, f)
        turns, tools = session_stats(full)
        models = set()
        for r in records(full):
            if r.get("type") == "assistant":
                mm = (r.get("message") or {}).get("model")
                if mm:
                    models.add(mm)
        meta = full[:-6] + ".meta.json"
        atype = ""
        if os.path.isfile(meta):
            try:
                with open(meta, encoding="utf-8") as fh:
                    j = json.load(fh)
                atype = j.get("agentType", "")
            except Exception:
                pass
        print("%-34s %7.1f KB  %3d turns  %3d tool calls  %s  %s" % (
            f, os.path.getsize(full) / 1024.0, turns, tools,
            ",".join(sorted(models)) or "?", atype))


def cmd_extract(args):
    path = resolve_session(args)
    out = args.out or os.path.join(
        os.environ.get("TEMP", "."), "claude-transcript-extract",
        os.path.basename(path)[:-6])
    hub_norm = os.path.normcase(os.path.abspath(HUB))
    out_norm = os.path.normcase(os.path.abspath(out))
    if out_norm.startswith(hub_norm) and not args.out:
        sys.exit("transcript.py: refusing to extract into the hub by default "
                 "(transcripts can contain project content). Pass --out explicitly.")
    os.makedirs(out, exist_ok=True)

    writes = {}   # tool_use id -> (file_path, content)
    edits = {}    # tool_use id -> (file_path, old, new)
    ok_ids = set()
    for r in records(path):
        for b in content_blocks(r):
            if r.get("type") == "assistant" and b.get("type") == "tool_use":
                inp = b.get("input") or {}
                if b.get("name") == "Write" and "file_path" in inp:
                    writes[b.get("id")] = (inp["file_path"], inp.get("content", ""))
                elif b.get("name") == "Edit" and "file_path" in inp:
                    edits[b.get("id")] = (inp["file_path"],
                                          inp.get("old_string", ""),
                                          inp.get("new_string", ""))
            elif r.get("type") == "user" and b.get("type") == "tool_result":
                if not b.get("is_error"):
                    ok_ids.add(b.get("tool_use_id"))

    n = 0
    for tid, (fp, content) in writes.items():
        if tid not in ok_ids:
            continue
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", fp)[-120:]
        dest = os.path.join(out, safe)
        with open(dest, "w", encoding="utf-8") as f:
            f.write(content)
        n += 1
        print("recovered Write -> %s  (%d chars, original: %s)" % (safe, len(content), fp))
    manifest = os.path.join(out, "_edits.txt")
    ne = 0
    with open(manifest, "w", encoding="utf-8") as f:
        for tid, (fp, old, new) in edits.items():
            if tid not in ok_ids:
                continue
            ne += 1
            f.write("EDIT %s\n  old: %s\n  new: %s\n\n" % (
                fp, re.sub(r"\s+", " ", old)[:200], re.sub(r"\s+", " ", new)[:200]))
    print("\n%d successful Write(s) recovered in full; %d Edit(s) LISTED as "
          "old->new fragments in _edits.txt (fragments, not whole files)." % (n, ne))
    print("Output: %s" % out)


# Code Mode error categories: the first rule whose pattern finds the error text names it.
CODE_MODE_RULES = [
    ("tool search", r"is not a function|tools\.mcp__mem__|mcp__mem__\w+ is not defined|No such tool|[Uu]nknown tool"),
    ("file read", r"cannot read binary files|File does not exist|modified since read|EISDIR|ENOENT|"
                  r"exceeds maximum allowed tokens"),
    ("oversized return", r"too large|exceeds the maximum|[Oo]utput (was|is) truncated"),
    ("shell quoting", r"ShellError[\s\S]*(Unexpected token|syntax error|unexpected EOF|ambiguous argument|"
                      r"unterminated|bad substitution|command not found|provide a value expression)"),
    ("shell exit", r"ShellError"),
    ("bad argument", r"InputValidationError|[Ii]nvalid (input|argument)|invalid_type|old and new text are the same|"
                     r"not found in|matches \d+ times|[Rr]equired (parameter|argument)"),
    ("reply shape", r"TypeError: (undefined|null) is not an object|Cannot read propert"),
    ("script", r"The script did not run|ReferenceError|SyntaxError|EvalError|Module loading|TypeError"),
]
# The root fix to look for per category: a hub CLI with JSON out, a tool default or the environment.
CODE_MODE_ROOT = {
    "tool search": "a hub CLI: memory calls go through Drawer/mem/mem.py --json, never a mem tool named in a script",
    "file read": "the environment: give the brief the fact the read was after (a path checked, a size known)",
    "oversized return": "a hub CLI that prints JSON in place of the raw reply",
    "shell quoting": "a hub CLI that takes plain arguments in place of the shell text",
    "shell exit": "the hub CLI's exit code and JSON, read with .nothrow()",
    "bad argument": "the tool default or a hub CLI that checks the argument",
    "reply shape": "a hub CLI that exits non-zero on a refusal (Drawer/mem/mem.py get exits 2) so the call throws",
    "script": "the script itself: no fix outside it",
    "other": "read the examples",
}
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


def code_mode_category(text):
    for name, pattern in CODE_MODE_RULES:
        if re.search(pattern, text):
            return name
    return "other"


LEG_REPL_BAR = 0.9  # a REPL leg type makes 90% or more of its top-level calls by REPL


def codemode_roots(args):
    if args.dir:
        return [args.dir]
    prefix = mangle(args.project).lower()
    return [os.path.join(CLAUDE_PROJECTS, d) for d in (os.listdir(CLAUDE_PROJECTS)
            if os.path.isdir(CLAUDE_PROJECTS) else []) if d.lower().startswith(prefix)]


def cmd_codemode_legs(args, roots, cut):
    """One JSON line: per agentType, the REPL share of the legs' top-level tool calls.
    A call made inside a REPL script lands as an isVirtual record: it is not top-level."""
    exclude = (args.exclude or "").lower()
    by = {}
    for root in roots:
        for dirpath, _dirs, names in os.walk(root):
            for name in names:
                path = os.path.join(dirpath, name)
                if not (name.startswith("agent-") and name.endswith(".jsonl")) or os.path.getmtime(path) < cut:
                    continue
                sid = (UUID_RE.search(os.path.relpath(path, root)) or [""])[0].lower()
                if exclude and sid.startswith(exclude):
                    continue
                try:
                    with open(path[:-len(".jsonl")] + ".meta.json", encoding="utf-8") as f:
                        agent_type = json.load(f).get("agentType") or "unknown"
                except (OSError, ValueError):
                    agent_type = "unknown"
                row = by.setdefault(agent_type, {"legs": 0, "calls": 0, "repl": 0})
                row["legs"] += 1
                for r in records(path):
                    if r.get("type") != "assistant" or r.get("isVirtual"):
                        continue
                    for b in content_blocks(r):
                        if isinstance(b, dict) and b.get("type") == "tool_use":
                            row["calls"] += 1
                            row["repl"] += b.get("name") == "REPL"
    for row in by.values():
        row["repl_share"] = round(row["repl"] / row["calls"], 3) if row["calls"] else 0.0
    below = sorted(t for t, row in by.items() if row["calls"] and row["repl_share"] < LEG_REPL_BAR)
    print(json.dumps({"hours": args.hours, "bar": LEG_REPL_BAR, "by_agent_type": dict(sorted(by.items())),
                      "below_bar": below}))


def cmd_codemode(args):
    """One JSON line: the Code Mode (REPL) errors of the last --hours of transcripts, by category."""
    roots = codemode_roots(args)
    cut = datetime.datetime.now().timestamp() - args.hours * 3600
    if args.legs:
        return cmd_codemode_legs(args, roots, cut)
    exclude = (args.exclude or "").lower()
    counts, hit, examples, sessions, cm_sessions = {}, {}, {}, set(), set()
    calls = 0
    for root in roots:
        for dirpath, _dirs, names in os.walk(root):
            for name in names:
                path = os.path.join(dirpath, name)
                if not name.endswith(".jsonl") or os.path.getmtime(path) < cut:
                    continue
                rel = os.path.relpath(path, root)
                sid = (UUID_RE.search(rel) or [rel])[0].lower()
                if exclude and sid.startswith(exclude):
                    continue
                sessions.add(sid)
                repl = set()
                for r in records(path):  # a line still being written fails to parse and is skipped
                    ts = parse_ts(r)
                    if ts and ts.timestamp() < cut:  # the window holds records, not whole files
                        continue
                    for b in content_blocks(r):
                        if not isinstance(b, dict):
                            continue
                        if b.get("type") == "tool_use" and b.get("name") == "REPL":
                            repl.add(b.get("id"))
                            calls += 1
                            cm_sessions.add(sid)
                        elif b.get("type") == "tool_result" and b.get("tool_use_id") in repl:
                            c = b.get("content")
                            if isinstance(c, list):
                                c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                            text = str(c or "")
                            m = re.search(r"<error>([\s\S]*?)(</error>|$)", text)
                            # a script that caught a failed inner call says so in its error part, not its result
                            if not b.get("is_error") and not (m and "and then failed" in m.group(1)):
                                continue
                            cat = code_mode_category(m.group(1) if m else text)
                            counts[cat] = counts.get(cat, 0) + 1
                            hit.setdefault(cat, set()).add(sid)
                            examples.setdefault(cat, "%s %s" % (sid[:8], re.sub(r"\s+", " ", (m.group(1) if m else text))[:160]))
    # Rank by sessions hit, then by count: one session's mistake repeated through a file is one failure.
    order = sorted(counts, key=lambda cat: (-len(hit[cat]), -counts[cat]))
    ranked = {cat: {"count": counts[cat], "sessions": len(hit[cat])} for cat in order}
    top = order[0] if order else None
    print(json.dumps({"hours": args.hours, "sessions": len(sessions), "code_mode_sessions": len(cm_sessions),
                      "repl_calls": calls, "errors": sum(counts.values()), "by_category": ranked,
                      "top": top, "root_fix": CODE_MODE_ROOT.get(top) if top else None,
                      "examples": examples}))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project", default=HUB,
                   help="project path (mangled to find its transcript dir); default: the hub")
    p.add_argument("--dir", help="explicit transcript directory (overrides --project)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sp = sub.add_parser("classify")
    sp.add_argument("session", nargs="?", help="one session; default: every session in the dir")
    for name in ("tokens", "tools", "subagents"):
        sp = sub.add_parser(name)
        sp.add_argument("session")
    sp = sub.add_parser("gaps")
    sp.add_argument("session")
    sp.add_argument("--threshold", type=float, default=15,
                    help="minimum gap in minutes (default 15)")
    sp = sub.add_parser("extract")
    sp.add_argument("session")
    sp.add_argument("--out", help="output dir (default: device-local temp; hub refused unless explicit)")
    sp = sub.add_parser("codemode")
    sp.add_argument("--hours", type=float, default=24, help="transcripts written in the last N hours (default 24)")
    sp.add_argument("--exclude", help="a session id (or its start) to leave out: the audit's own session")
    sp.add_argument("--legs", action="store_true",
                    help="instead: REPL share of top-level tool calls per leg agentType, against the 90%% bar")
    args = p.parse_args()
    {"list": cmd_list, "classify": cmd_classify,
     "tokens": cmd_tokens, "gaps": cmd_gaps,
     "tools": cmd_tools, "subagents": cmd_subagents, "extract": cmd_extract,
     "codemode": cmd_codemode}[args.cmd](args)


if __name__ == "__main__":
    main()
