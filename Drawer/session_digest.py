"""Compact digest of one Claude Code transcript for a reader agent. Usage: <python> Drawer/session_digest.py <file.jsonl>  (prints <= ~450 lines)"""
import json, sys, collections
from transcript import turn_context
# A new seat has no py launcher: printed commands name this host's prepared interpreter.
PY = ".venv/bin/python -B"
USAGE = __doc__.replace("<python>", PY)
if sys.argv[1:] in (["-h"], ["--help"]):
    print(USAGE)
    sys.exit(0)
if len(sys.argv) != 2:
    sys.exit(USAGE)
p = sys.argv[1]
ents = []
for line in open(p, encoding="utf-8", errors="replace"):
    try: ents.append(json.loads(line))
    except Exception: pass
def txt(c, n=280):
    if isinstance(c, str): return c[:n].replace("\n", " ")
    if isinstance(c, list):
        out = []
        for b in c:
            if isinstance(b, dict) and b.get("type") == "text": out.append(b.get("text", ""))
        return " ".join(out)[:n].replace("\n", " ")
    return ""
users = []; tools = []; results = {}; errors = []; effort = collections.Counter(); models = collections.Counter(); usage = collections.Counter()
advisor = {}; seen = set(); bg = 0; spawns = []; wf = 0; assistant_text = []
for e in ents:
    t = e.get("type"); m = e.get("message") or {}; c = m.get("content")
    ts = (e.get("timestamp") or "")[5:16]
    if t == "user":
        if isinstance(c, str) or (isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "text" for b in c)):
            s = txt(c, 300)
            if s and not s.startswith("<") and "tool_use_id" not in s: users.append((ts, s))
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    cc = b.get("content"); n = len(cc) if isinstance(cc, str) else sum(len(x.get("text", "")) for x in cc if isinstance(x, dict)) if isinstance(cc, list) else 0
                    results[b.get("tool_use_id")] = (n, bool(b.get("is_error")), txt(cc, 240))
    elif t == "assistant":
        if e.get("effort"): effort[e["effort"]] += 1
        if m.get("model"): models[m["model"]] += 1
        u = m.get("usage") or {}; k = e.get("requestId") or m.get("id")
        if u and k and k not in seen:
            seen.add(k)
            for f in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"): usage[f] += u.get(f, 0) or 0
            usage["requests"] += 1
            context, calls = turn_context(u)
            usage["peak_context"] = max(usage["peak_context"], context)
            for call in calls:
                row = advisor.setdefault(call["model"], collections.Counter())
                row["calls"] += 1; row["input"] += call["input"]; row["output"] += call["output"]
        if isinstance(c, list):
            for b in c:
                if not isinstance(b, dict): continue
                if b.get("type") == "text" and len(b.get("text", "")) > 40: assistant_text.append((ts, b["text"][:200].replace("\n", " ")))
                if b.get("type") == "tool_use":
                    name = b.get("name", "?"); inp = b.get("input") or {}
                    desc = inp.get("command") or inp.get("description") or inp.get("file_path") or inp.get("pattern") or inp.get("prompt") or inp.get("skill") or json.dumps(inp)[:120]
                    if name == "Bash" and inp.get("run_in_background"): bg += 1
                    if name == "Agent": spawns.append((ts, str(inp.get("subagent_type", "")), str(inp.get("description", ""))[:80]))
                    if name == "Workflow": wf += 1
                    tools.append((ts, name, str(desc)[:150].replace("\n", " "), b.get("id")))
print("FILE", p.split("/")[-1].split("\\")[-1])
print("MODELS", dict(models), "EFFORT", dict(effort))
print("USAGE requests=%d uncached=%d cache_write=%d cache_read=%d output=%d" % (usage["requests"], usage["input_tokens"], usage["cache_creation_input_tokens"], usage["cache_read_input_tokens"], usage["output_tokens"]))
print("PEAK_CONTEXT %d (last executor iteration of the largest turn)" % usage["peak_context"])
for model, row in sorted(advisor.items()): print("ADVISOR %s calls=%d input=%d output=%d" % (model, row["calls"], row["input"], row["output"]))
print("DELEGATION main_tool_calls=%d agent_spawns=%d workflow_calls=%d background_runs=%d" % (len(tools), len(spawns), wf, bg))
print("\n== USER TURNS (%d) ==" % len(users))
for ts, s in users[:60]: print(ts, "|", s)
print("\n== TOOL CALLS by name ==")
cnt = collections.Counter(n for _, n, _, _ in tools); print(dict(cnt.most_common(25)))
errs = [(ts, n, d, results[i][2]) for ts, n, d, i in tools if i in results and results[i][1]]
print("\n== TOOL ERRORS (%d) ==" % len(errs))
for ts, n, d, r in errs[:30]: print(ts, "|", n, "|", d[:100], "=>", r[:160])
big = sorted([(results[i][0], ts, n, d) for ts, n, d, i in tools if i in results], reverse=True)[:15]
print("\n== LARGEST TOOL RESULTS (bytes) ==")
for n_, ts, n, d in big: print(n_, "|", ts, "|", n, "|", d[:110])
rep = collections.Counter((n, d) for _, n, d, _ in tools)
print("\n== REPEATED IDENTICAL CALLS (>=3) ==")
for (n, d), k in rep.most_common(40):
    if k >= 3: print(k, "x", n, "|", d[:120])
print("\n== SUBAGENT SPAWNS (%d) ==" % len(spawns))
for ts, t_, d in spawns[:30]: print(ts, "|", t_, "|", d)
print("\n== TOOL CALL TIMELINE (first 120) ==")
for ts, n, d, _ in tools[:120]: print(ts, "|", n, "|", d[:100])
if len(tools) > 120:
    print("... %d more; last 40:" % (len(tools) - 120))
    for ts, n, d, _ in tools[-40:]: print(ts, "|", n, "|", d[:100])
print("\n== ASSISTANT TEXT SAMPLE (every 5th, max 40) ==")
for ts, s in assistant_text[::5][:40]: print(ts, "|", s)
