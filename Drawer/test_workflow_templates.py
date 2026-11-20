"""The saved workflows in .claude/workflows/ run as committed.

Each template runs under node with a fake agent(): its legs pin the requested model on the
workflow-leg profile, and its result shape holds. No Workflow tool runs.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import harness_config  # noqa: E402

# harness.toml [routing.legs]: each template's default model and effort (Drawer/settings-sync.py renders them).
LEGS = harness_config.load(os.path.dirname(HERE))["routing"]["legs"]


def route_of(name, label):
    """The [routing.legs] row of one template leg: find-verify's verify legs take their own row."""
    if name == "find-verify":
        return LEGS["find-verify-verifier" if label.startswith("verify:") else "find-verify-finder"]
    return LEGS[name]
WORKFLOWS = os.path.join(os.path.dirname(HERE), ".claude", "workflows")
TEMPLATES = ("one-leg", "fan-out", "find-verify", "chain", "rules-trim")
SAVED = TEMPLATES + ("risky-review", "card-run", "spec-build")  # risky-review: the risky-change review; card-run: one claimed task on legs; spec-build: a spec by draft and cold attack


# Runs one template body as the Workflow tool does: the meta
# export, then an async function body over args, agent, parallel, pipeline, phase and log.
# The fake agent() records each call's options and answers from a per-label table: a default
# leg that asserts the requested model, overlaid by the table row; a null row is a failed leg;
# a row's $drop list removes those default fields. The fake workflow() answers each call from cfg.workflows[name]
# in order: a null entry returns null, a {$throw} entry throws, and an empty list returns null.
HARNESS = r"""
const fs = require('fs')
const [path, cfgText] = process.argv.slice(-2)
const cfg = JSON.parse(cfgText)
const src = fs.readFileSync(path, 'utf8').replace('export const meta =', 'const meta =')
const calls = [], logs = [], wcalls = []
async function agent(prompt, opts) {
  calls.push({ label: opts.label, model: opts.model, effort: opts.effort ?? null, agentType: opts.agentType,
               required: opts.schema ? opts.schema.required : null,
               tiers: opts.schema && opts.schema.properties && opts.schema.properties.tier ? opts.schema.properties.tier.enum : null, prompt })
  const row = Object.prototype.hasOwnProperty.call(cfg.responses, opts.label) ? cfg.responses[opts.label] : {}
  if (row === null) return null
  const out = { model: `claude-${opts.model}-5-5`, summary: 's', items: [], findings: [], pass: true,
           reason: 'r', verdict: 'confirmed', ...row }
  for (const k of row.$drop || []) delete out[k]  // $drop: fields the leg leaves out
  delete out.$drop
  return out
}
async function workflow(name, wargs) {
  wcalls.push({ name, args: wargs })
  const q = (cfg.workflows || {})[name] || []
  const next = q.length ? q.shift() : null
  if (next && next.$throw) throw new Error(next.$throw)
  return next
}
async function parallel(thunks) {
  return Promise.all(thunks.map((t) => Promise.resolve().then(t).catch(() => null)))
}
async function pipeline(items, ...stages) {
  return Promise.all(items.map(async (item, i) => {
    let prev = item
    try { for (const s of stages) prev = await s(prev, item, i) } catch (e) { return null }
    return prev
  }))
}
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
const body = new AsyncFunction('args', 'agent', 'parallel', 'pipeline', 'phase', 'log', 'workflow', src)
body(cfg.args, agent, parallel, pipeline, () => {}, (m) => logs.push(m), workflow)
  .then((result) => console.log(JSON.stringify({ result, calls, logs, wcalls })))
  .catch((e) => { console.log(JSON.stringify({ error: String(e), calls })); process.exit(1) })
"""


def run_template(name, args, responses=None, refused=False, workflows=None):
    return run_script(os.path.join(WORKFLOWS, name + ".js"), args, responses, refused, workflows)


def run_script(path, args, responses=None, refused=False, workflows=None):
    name = os.path.basename(path)
    env = {"PATH": os.environ.get("PATH", "")}
    if os.environ.get("SYSTEMROOT"):
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    proc = subprocess.run(
        [shutil.which("node") or "node", "-e", HARNESS, path,
         json.dumps({"args": args, "responses": responses or {}, "workflows": workflows or {}})],
        text=True, encoding="utf-8", capture_output=True, check=False, timeout=60, env=env)
    if (proc.returncode != 0) != refused:
        raise AssertionError(f"{name} exit {proc.returncode}, refused={refused}: {proc.stdout}{proc.stderr}")
    return json.loads(proc.stdout)


class SavedTemplates(unittest.TestCase):
    def test_the_saved_workflows_are_the_saved_templates(self):
        names = {f[:-3] for f in os.listdir(WORKFLOWS) if f.endswith(".js")}
        self.assertEqual(set(SAVED), names)

    def test_meta_name_equals_the_file_stem(self):
        # A template is called by name: Workflow({name: 'fan-out'}). Its meta.name is that name.
        names = {}
        for stem in SAVED:
            with open(os.path.join(WORKFLOWS, stem + ".js"), encoding="utf-8") as fh:
                found = re.search(r"export const meta = \{\s*name: '([^']*)'", fh.read())
            names[stem] = found.group(1) if found else None
        self.assertEqual({s: s for s in SAVED}, names)


# The smallest valid args per template.
ARGS = {
    "one-leg": {"goal": "g", "brief": "b"},
    "fan-out": {"goal": "g", "legs": [{"label": "a", "brief": "b"}, {"label": "c", "brief": "d"}]},
    "find-verify": {"goal": "g", "scope": "s", "finders": [{"label": "a", "brief": "b"}]},
    "chain": {"goal": "g", "steps": [{"label": "a", "brief": "b", "check": "c"}]},
    "rules-trim": {"mode": "propose"},
}
FINDING = {"file": "x.py", "line": 1, "claim": "c", "evidence": "e"}


AGENTS_DIR = os.path.join(os.path.dirname(HERE), ".claude", "agents")


def profile_tools(name):
    """The tools: line of one .claude/agents profile's front matter, as a list (empty when none)."""
    with open(os.path.join(AGENTS_DIR, name + ".md"), encoding="utf-8") as f:
        head = f.read().split("---")[1]
    m = re.search(r"^tools:\s*(.+)$", head, re.M)
    return [t.strip() for t in m.group(1).split(",")] if m else []


class AgentProfiles(unittest.TestCase):
    """A leg runs Code Mode only when its profile tools line names REPL."""

    def test_every_profile_names_repl(self):
        names = sorted(n[:-3] for n in os.listdir(AGENTS_DIR) if n.endswith(".md"))
        self.assertIn("workflow-leg", names)
        for name in names:
            with self.subTest(profile=name):
                self.assertIn("REPL", profile_tools(name))

    def test_workflow_leg_is_lean(self):
        tools = profile_tools("workflow-leg")
        self.assertEqual(["REPL", "Read", "Write", "Edit", "Glob", "Grep", "Bash"], tools)
        for banned in ("Skill", "Agent", "Workflow"):
            self.assertNotIn(banned, tools)
        self.assertFalse([t for t in tools if t.startswith("mcp__")])

    def test_implementer_is_lean_and_fenced(self):
        # The card-run worker runs REPL with no Skill, Agent, Workflow or MCP tool.
        tools = profile_tools("implementer")
        self.assertEqual(["REPL", "Read", "Write", "Edit", "Glob", "Grep", "Bash"], tools)
        for banned in ("Skill", "Agent", "Workflow"):
            self.assertNotIn(banned, tools)
        self.assertFalse([t for t in tools if t.startswith("mcp__")])

    def test_reviewer_is_read_only_code_mode(self):
        # The reviewer leg runs REPL and can write no file.
        tools = profile_tools("reviewer")
        self.assertEqual(["REPL", "Read", "Glob", "Grep", "Bash"], tools)
        for banned in ("Write", "Edit", "MultiEdit", "NotebookEdit", "Skill", "Agent", "Workflow"):
            self.assertNotIn(banned, tools)
        self.assertFalse([t for t in tools if t.startswith("mcp__")])


class TemplateRuns(unittest.TestCase):
    def test_one_leg_profile_reviewer_spawns_agent_type_reviewer(self):
        # One-leg takes profile 'reviewer'; any other profile falls back to workflow-leg.
        out = run_template("one-leg", {**ARGS["one-leg"], "profile": "reviewer"})
        self.assertEqual(["reviewer"], [c["agentType"] for c in out["calls"]])
        for profile in ("implementer", "nope", None):
            with self.subTest(profile=profile):
                out = run_template("one-leg", {**ARGS["one-leg"], "profile": profile})
                self.assertEqual(["workflow-leg"], [c["agentType"] for c in out["calls"]])
                if profile is not None:
                    self.assertTrue(any("profile" in m for m in out["logs"]), out["logs"])

    def test_each_template_pins_the_requested_model_on_workflow_leg(self):
        # Each template runs its legs on workflow-leg and pins 'opus', 'sonnet' or 'haiku' on request
        # ([gates] allowed_models); no model or any other model falls back to the leg's route row.
        # find-verify gets one finding, so its verify leg runs too (verifyModel defaults to the model arg).
        responses = {"find:a": {"findings": [FINDING]}}
        for name in TEMPLATES:
            for requested, pin in (({}, None), ({"model": "opus"}, "opus"), ({"model": "haiku"}, "haiku"),
                                   ({"model": "sonnet"}, "sonnet"), ({"model": "fable"}, None),
                                   ({"model": ["opus"]}, None)):  # a non-string arg never passes a pick
                with self.subTest(template=name, requested=requested):
                    out = run_template(name, {**ARGS[name], **requested}, responses)
                    self.assertEqual([(pin or route_of(name, c["label"])["model"], "workflow-leg") for c in out["calls"]],
                                     [(c["model"], c["agentType"]) for c in out["calls"]])
                    self.assertEqual([], out["result"]["modelMismatches"])

    def test_each_template_default_is_its_route_row(self):
        # A template with no model or effort arg runs each leg on its [routing.legs] row.
        responses = {"find:a": {"findings": [FINDING]}}
        for name in TEMPLATES:
            with self.subTest(template=name):
                out = run_template(name, ARGS[name], responses)
                self.assertTrue(out["calls"])
                self.assertEqual([(route_of(name, c["label"])["model"], route_of(name, c["label"])["effort"])
                                  for c in out["calls"]], [(c["model"], c["effort"]) for c in out["calls"]])
        labels = {c["label"] for c in run_template("find-verify", ARGS["find-verify"], responses)["calls"]}
        self.assertEqual({"find:a", "verify:a#1.1"}, labels)

    def test_rules_trim_review_fails_on_a_lost_rule_or_over_the_cap_and_needs_a_mode(self):
        # The review verdict fails when one removed line has no home or the file is over its cap,
        # whatever the leg's own verdict. One leg per file; AGENTS.md caps at 40, a SKILL.md at 500.
        row = {"lines": 39, "verdict": "pass", "items": [
            {"line": "a", "action": "move", "home": "x/SKILL.md", "ok": True, "why": "w"},
            {"line": "b", "action": "move", "home": "none", "ok": False, "why": "lost"}]}
        out = run_template("rules-trim", {"mode": "review", "model": "opus"}, {"review:AGENTS.md": row})
        self.assertEqual("fail", out["result"]["verdict"])
        self.assertEqual(["b"], [x["line"] for x in out["result"]["results"][0]["lost"]])
        self.assertIn("removal test", out["calls"][0]["prompt"])
        row["items"] = row["items"][:1]
        self.assertEqual("pass", run_template("rules-trim", {"mode": "review"}, {"review:AGENTS.md": row})["result"]["verdict"])
        row["lines"] = 41  # every rule kept, but over the cap
        self.assertEqual("fail", run_template("rules-trim", {"mode": "review"}, {"review:AGENTS.md": row})["result"]["verdict"])
        skill = ".claude/skills/x/SKILL.md"
        out = run_template("rules-trim", {"mode": "review", "files": ["AGENTS.md", skill]},
                           {"review:AGENTS.md": {**row, "lines": 30}, f"review:{skill}": {**row, "lines": 480}})
        self.assertEqual(["pass", "pass"], [r["verdict"] for r in out["result"]["results"]])
        out = run_template("rules-trim", {}, refused=True)
        self.assertIn("mode", out["error"])

    def test_rules_trim_propose_brings_agents_md_to_about_35_and_a_skill_under_its_cap(self):
        # An AGENTS.md trim lands near 35 lines (cap 40), so the TRIM DUE note does not fire on each new rule.
        skill = ".claude/skills/x/SKILL.md"
        row = {"lines": 39, "verdict": "pass", "items": []}
        out = run_template("rules-trim", {"mode": "propose", "files": ["AGENTS.md", skill]},
                           {"propose:AGENTS.md": row, f"propose:{skill}": row})
        prompts = {c["label"]: c["prompt"] for c in out["calls"]}
        self.assertIn("Bring the file to about 35 lines when it is over 35", prompts["propose:AGENTS.md"])
        self.assertIn("Bring the file to about 499 lines when it is over 499", prompts[f"propose:{skill}"])

    def test_find_verify_pins_verifyModel_opus_on_its_verify_legs_only(self):
        # verifyModel takes 'sonnet', 'opus' or 'haiku'; the finders keep the model pin.
        for model, verify in (("sonnet", "opus"), ("haiku", "sonnet"), ("opus", "haiku")):
            with self.subTest(model=model, verifyModel=verify):
                out = run_template("find-verify", {**ARGS["find-verify"], "model": model, "verifyModel": verify},
                                   {"find:a": {"findings": [FINDING]}})
                self.assertEqual({model}, {c["model"] for c in out["calls"] if c["label"] == "find:a"})
                self.assertEqual({verify}, {c["model"] for c in out["calls"] if c["label"] != "find:a"})
                self.assertEqual([], out["result"]["modelMismatches"])

    def test_items_cap_at_maxItems_with_the_dropped_count(self):
        # items holds at most maxItems (default 20); dropped counts the rest, each one logged.
        lines = [f"i{n}" for n in range(25)]
        for extra, kept in (({"maxItems": 2}, 2), ({}, 20)):
            with self.subTest(maxItems=kept):
                out = run_template("one-leg", {**ARGS["one-leg"], **extra}, {"leg:one": {"items": lines}})
                self.assertEqual(lines[:kept], out["result"]["items"])
                self.assertEqual(25 - kept, out["result"]["dropped"])
                self.assertEqual([f'capped item: "{x}"' for x in lines[kept:]], out["logs"])

    def test_find_verify_confirms_only_a_strict_vote_majority(self):
        # A finding stands when a strict majority of its votes confirm it: 1 of 2 is no majority.
        no, yes = {"verdict": "refuted"}, {"verdict": "confirmed"}
        findings = [{**FINDING, "line": n} for n in (1, 2)]
        for votes, ballots1, ballots2 in ((2, (yes, no), (yes, yes)), (3, (yes, no, no), (yes, no, yes))):
            with self.subTest(votes=votes):
                responses = {"find:a": {"findings": findings}}
                for n, ballots in ((1, ballots1), (2, ballots2)):
                    for v, b in enumerate(ballots, 1):
                        responses[f"verify:a#{n}.{v}"] = b
                out = run_template("find-verify", {**ARGS["find-verify"], "votes": votes}, responses)
                self.assertEqual([("x.py", 2)], [(x["file"], x["line"]) for x in out["result"]["items"]])
                self.assertEqual((1, 1), (out["result"]["counts"]["confirmed"],
                                          out["result"]["counts"]["refuted"]))

    def test_chain_stops_at_the_first_failed_check(self):
        steps = [{"label": s, "brief": "b", "check": "c"} for s in ("a", "b", "c")]
        out = run_template("chain", {"goal": "g", "steps": steps},
                           {"check:b": {"pass": False, "reason": "not built"}})
        self.assertEqual(["step:a", "check:a", "step:b", "check:b"], [c["label"] for c in out["calls"]])
        self.assertEqual([("a", "passed"), ("b", "failed"), ("c", "skipped")],
                         [(r["label"], r["status"]) for r in out["result"]["items"]])
        self.assertEqual({"steps": 3, "passed": 1, "failed": 1, "skipped": 1}, out["result"]["counts"])

    def test_a_leg_on_the_wrong_model_lands_in_modelMismatches(self):
        # A non-empty modelMismatches means a leg ran on the wrong model; a right leg stays out.
        route = LEGS["fan-out"]["model"]
        out = run_template("fan-out", ARGS["fan-out"],
                           {"leg:a": {"model": "claude-fable-5-1"}, "leg:c": {"model": f"claude-{route}-5-5"}})
        self.assertEqual([{"label": "leg:a", "expected": route, "got": "claude-fable-5-1"}],
                         out["result"]["modelMismatches"])



def packed(model, **fields):
    """A leg result whose later fields are packed into the model string as tool-call text."""
    tail = "".join(f'</parameter><parameter name="{k}">{v if isinstance(v, str) else json.dumps(v)}'
                   for k, v in fields.items())
    return {"model": f"claude-{model}-5-5{tail}", "$drop": list(fields)}


class LegHardening(unittest.TestCase):
    # A leg can pack its later fields into the model string; the agent schema requires only model,
    # unpack() restores the fields, and lacks() fails a leg still missing one.
    PACKED = {
        "one-leg": ({"leg:one": packed(LEGS["one-leg"]["model"], summary="s2", items=["x"])},
                    lambda r: (r["summary"], r["items"]) == ("s2", ["x"])),
        "fan-out": ({"leg:a": packed(LEGS["fan-out"]["model"], summary="s2", items=["x"])},
                    lambda r: (r["items"][0]["status"], r["items"][0]["summary"], r["items"][0]["items"]) == ("ok", "s2", ["x"])),
        "find-verify": ({"find:a": packed(LEGS["find-verify-finder"]["model"], findings=[FINDING]),
                         "verify:a#1.1": packed("sonnet", verdict="confirmed", reason="r2")},
                        lambda r: r["counts"]["confirmed"] == 1 and r["items"][0]["file"] == "x.py"),
        "chain": ({"step:a": packed("sonnet", summary="built"), "check:a": packed("sonnet", **{"pass": True, "reason": "ok"})},
                  lambda r: (r["items"][0]["status"], r["items"][0]["summary"]) == ("passed", "built")),
        "rules-trim": ({"propose:AGENTS.md": packed("sonnet", lines=70, verdict="pass", items=[], reason="r")},
                       lambda r: (r["results"][0]["verdict"], r["results"][0]["lines"]) == ("pass", 70)),
    }

    def test_a_packed_field_leg_is_unpacked_and_its_schema_requires_only_model(self):
        for name in TEMPLATES:
            responses, holds = self.PACKED[name]
            with self.subTest(template=name):
                out = run_template(name, ARGS[name], responses)
                self.assertTrue(holds(out["result"]), out["result"])
                self.assertEqual([], out["result"]["modelMismatches"])
                self.assertEqual({("model",)}, {tuple(c["required"]) for c in out["calls"]})

    def test_a_leg_that_lacks_a_required_field_fails_with_a_log_line(self):
        out = run_template("fan-out", ARGS["fan-out"], {"leg:a": {"$drop": ["items"]}})
        self.assertEqual([("a", "failed"), ("c", "ok")], [(r["label"], r["status"]) for r in out["result"]["items"]])
        self.assertTrue(any(m.startswith("leg leg:a lacks items") for m in out["logs"]), out["logs"])
        out = run_template("chain", ARGS["chain"], {"check:a": {"pass": "yes"}})  # wrong type
        self.assertEqual("failed", out["result"]["items"][0]["status"])
        self.assertTrue(any(m.startswith("leg check:a lacks pass") for m in out["logs"]), out["logs"])
        out = run_template("one-leg", ARGS["one-leg"], {"leg:one": {"$drop": ["summary"]}})
        self.assertEqual({"legs": 1, "ok": 0}, out["result"]["counts"])

    def test_each_template_passes_the_effort_arg_and_drops_a_bad_one(self):
        responses = {"find:a": {"findings": [FINDING]}}
        for name in TEMPLATES:
            with self.subTest(template=name):
                out = run_template(name, {**ARGS[name], "effort": "high"}, responses)
                self.assertEqual({"high"}, {c["effort"] for c in out["calls"]})
                out = run_template(name, {**ARGS[name], "effort": "huge"}, responses)
                self.assertEqual([route_of(name, c["label"])["effort"] for c in out["calls"]],
                                 [c["effort"] for c in out["calls"]])
                self.assertTrue(any("effort \"huge\" is not" in m for m in out["logs"]), out["logs"])
        out = run_template("find-verify", {**ARGS["find-verify"], "effort": "low", "verifyEffort": "max"}, responses)
        self.assertEqual([("find:a", "low"), ("verify:a#1.1", "max")], [(c["label"], c["effort"]) for c in out["calls"]])

SB_SPEC = ("door: a key opens one door. Spec: state s; life l; breaks b; neighbours n; data d; proof p; sees v; "
           "keeps k. Why: w. Done when: e.")
SB_BLOCK = {"case": "breaks", "question": "Does the key stay after a restart?", "size": "blocks"}
SB_SMALL = {"case": "sees", "question": "Which word does the door print?", "size": "small"}
SB_DRAFT = {"spec": SB_SPEC, "sources": ["dc569def taste: plain short words"]}


class SpecBuildRuns(unittest.TestCase):
    """spec-build: one round is a draft or revise leg, then a cold attack leg on a second model."""

    def test_round_one_drafts_then_attacks_on_the_two_route_rows(self):
        out = run_template("spec-build", {"intent": "add a key and a locked door"},
                           {"draft": SB_DRAFT, "attack": {"findings": [SB_BLOCK, SB_SMALL]}})
        draft, attack = LEGS["spec-draft"], LEGS["review-other"]
        self.assertNotEqual(draft["model"], attack["model"])  # the attack is a second model
        self.assertEqual([("draft", draft["model"], draft["effort"], "workflow-leg"),
                          ("attack", attack["model"], attack["effort"], "workflow-leg")],
                         [(c["label"], c["model"], c["effort"], c["agentType"]) for c in out["calls"]])
        self.assertEqual({("model",)}, {tuple(c["required"]) for c in out["calls"]})
        r = out["result"]
        self.assertEqual((1, SB_SPEC, False), (r["round"], r["spec"], r["done"]))
        self.assertEqual({"findings": 2, "blocks": 1, "small": 1}, r["counts"])
        self.assertEqual([SB_BLOCK, SB_SMALL], r["findings"])
        self.assertEqual(SB_DRAFT["sources"], r["sources"])
        self.assertEqual([], r["modelMismatches"])
        self.assertNotIn("failed", r)

    def test_the_attack_leg_gets_the_spec_alone(self):
        # Cold: the intent line, the old spec and the human's decisions never reach the attack leg.
        decisions = [{"finding": "Q-MARKER", "verdict": "answer", "text": "A-MARKER"}]
        out = run_template("spec-build", {"intent": "INTENT-MARKER", "spec": "OLD-SPEC-MARKER", "decisions": decisions},
                           {"revise": SB_DRAFT, "attack": {"findings": []}})
        prompts = {c["label"]: c["prompt"] for c in out["calls"]}
        self.assertIn(SB_SPEC, prompts["attack"])
        for hidden in ("INTENT-MARKER", "OLD-SPEC-MARKER", "Q-MARKER", "A-MARKER"):
            self.assertNotIn(hidden, prompts["attack"])
        for shown in ("OLD-SPEC-MARKER", "Q-MARKER", "A-MARKER"):
            self.assertIn(shown, prompts["revise"])
        out = run_template("spec-build", {"intent": "INTENT-MARKER"}, {"draft": SB_DRAFT, "attack": {"findings": []}})
        prompts = {c["label"]: c["prompt"] for c in out["calls"]}
        self.assertIn("INTENT-MARKER", prompts["draft"])
        self.assertNotIn("INTENT-MARKER", prompts["attack"])

    def test_a_later_round_revises_by_the_decisions_and_is_done_when_no_finding_blocks(self):
        decisions = [{"finding": "q1", "verdict": "answer", "text": "a1"}, {"finding": "q2", "verdict": "reject"}]
        args = {"intent": "i", "spec": "old", "decisions": decisions}
        out = run_template("spec-build", args, {"revise": SB_DRAFT, "attack": {"findings": [SB_SMALL]}})
        self.assertEqual(["revise", "attack"], [c["label"] for c in out["calls"]])
        self.assertIn("Rejected: change nothing for it.", out["calls"][0]["prompt"])
        self.assertIn("Answer: a1", out["calls"][0]["prompt"])
        r = out["result"]
        self.assertEqual((2, True, SB_SPEC), (r["round"], r["done"], r["spec"]))
        self.assertEqual({"findings": 1, "blocks": 0, "small": 1}, r["counts"])
        out = run_template("spec-build", {**args, "round": 5}, {"revise": SB_DRAFT, "attack": {"findings": [SB_BLOCK]}})
        self.assertEqual((5, False), (out["result"]["round"], out["result"]["done"]))
        self.assertIn("round 6", out["result"]["summary"])

    def test_a_spec_with_no_decisions_takes_the_attack_alone(self):
        out = run_template("spec-build", {"intent": "i", "spec": SB_SPEC}, {"attack": {"findings": [SB_BLOCK]}})
        self.assertEqual(["attack"], [c["label"] for c in out["calls"]])
        r = out["result"]
        self.assertEqual((SB_SPEC, False, 1, 1), (r["spec"], r["done"], r["counts"]["blocks"], r["round"]))
        out = run_template("spec-build", {"intent": "i", "spec": SB_SPEC}, {"attack": None})  # a dead leg on this path
        self.assertEqual(("attack", False), (out["result"]["failed"], out["result"]["done"]))

    def test_bad_args_are_refused_before_a_leg_runs(self):
        answer = {"finding": "q", "verdict": "answer"}
        for args, word in (({}, "intent"), ({"intent": ["i"]}, "intent"), ({"intent": "i", "decisions": []}, "decisions need the spec"),
                           ({"intent": "i", "spec": 123}, "spec must be the text"), ({"intent": "i", "spec": "  "}, "spec must be the text"),
                           ({"intent": "i", "spec": "s", "decisions": "q: a"}, "decisions must be a list"),
                           ({"intent": "i", "spec": "s", "decisions": {"finding": "q"}}, "decisions must be a list"),
                           ({"intent": "i", "spec": "s", "decisions": [{"finding": "q", "verdict": "maybe"}]}, "verdict"),
                           ({"intent": "i", "spec": "s", "decisions": [answer]}, "text for an answer")):
            with self.subTest(args=args):
                out = run_template("spec-build", args, refused=True)
                self.assertIn(word, out["error"])
                self.assertEqual([], out["calls"])

    def test_a_dead_leg_never_reads_as_done(self):
        out = run_template("spec-build", {"intent": "i"}, {"draft": None})
        self.assertEqual((["draft"], "draft", False), ([c["label"] for c in out["calls"]], out["result"]["failed"], out["result"]["done"]))
        out = run_template("spec-build", {"intent": "i"}, {"draft": {"sources": []}})  # the leg left the spec out
        self.assertEqual(("draft", False), (out["result"]["failed"], out["result"]["done"]))
        self.assertTrue(any(m.startswith("leg draft lacks spec") for m in out["logs"]), out["logs"])
        out = run_template("spec-build", {"intent": "i"}, {"draft": {"spec": "  ", "sources": []}})  # an empty spec is no spec
        self.assertEqual((["draft"], "draft", False), ([c["label"] for c in out["calls"]], out["result"]["failed"], out["result"]["done"]))
        decided = {"intent": "i", "spec": "old", "decisions": [{"finding": "q", "verdict": "reject"}]}
        out = run_template("spec-build", decided, {"revise": None})
        r = out["result"]
        self.assertEqual((["revise"], "revise", False, "old"), ([c["label"] for c in out["calls"]], r["failed"], r["done"], r["spec"]))
        out = run_template("spec-build", {"intent": "i"}, {"draft": SB_DRAFT, "attack": None})
        r = out["result"]
        self.assertEqual(("attack", False, SB_SPEC, []), (r["failed"], r["done"], r["spec"], r["findings"]))
        out = run_template("spec-build", {"intent": "i"}, {"draft": SB_DRAFT, "attack": {"findings": [{"case": "breaks"}], "$drop": ["findings"]}})
        self.assertEqual(("attack", False), (out["result"]["failed"], out["result"]["done"]))

    def test_done_needs_no_open_case_no_blocking_finding_and_the_right_models(self):
        open_spec = SB_SPEC.replace("life l;", "life OPEN when is the key lost?;")
        out = run_template("spec-build", {"intent": "i"}, {"draft": {"spec": open_spec, "sources": []}, "attack": {"findings": []}})
        self.assertEqual(False, out["result"]["done"])  # the attack missed it; the OPEN mark still holds the round
        self.assertIn("OPEN", out["result"]["summary"])
        out = run_template("spec-build", {"intent": "i"}, {"draft": SB_DRAFT, "attack": {"findings": [SB_SMALL]}})
        self.assertEqual(True, out["result"]["done"])
        out = run_template("spec-build", {"intent": "i"},
                           {"draft": SB_DRAFT, "attack": {"model": "claude-sonnet-5-5", "findings": []}})  # no second model read it
        self.assertEqual(False, out["result"]["done"])
        self.assertIn("wrong model", out["result"]["summary"])

    def test_findings_go_blocks_first_and_a_bad_one_never_reads_as_small(self):
        # The cap must not hide a blocking finding behind a small one; a finding with no size counts as blocks.
        out = run_template("spec-build", {"intent": "i", "maxItems": 0.5},
                           {"draft": SB_DRAFT, "attack": {"findings": [SB_SMALL, SB_BLOCK]}})
        r = out["result"]
        self.assertEqual(([SB_BLOCK], 1, False), (r["findings"], r["dropped"], r["done"]))
        sizeless = {"case": "proof", "question": "Which test?"}
        out = run_template("spec-build", {"intent": "i", "round": 0.5},
                           {"draft": SB_DRAFT, "attack": {"findings": [None, {"case": "sees"}, sizeless, SB_SMALL]}})
        r = out["result"]
        self.assertEqual([{**sizeless, "size": "blocks"}, SB_SMALL], r["findings"])
        self.assertEqual(({"findings": 2, "blocks": 1, "small": 1}, False, 1), (r["counts"], r["done"], r["round"]))
        self.assertTrue(any("dropped 2 finding(s) with no question" in m for m in out["logs"]), out["logs"])

    def test_the_revise_leg_gets_the_case_and_a_long_spec_is_named(self):
        decisions = [{"finding": "q1", "verdict": "answer", "text": "a1", "case": "breaks"}]
        long_spec = SB_SPEC + " x" * 900
        out = run_template("spec-build", {"intent": "i", "spec": "old", "decisions": decisions},
                           {"revise": {"spec": long_spec, "sources": []}, "attack": {"findings": []}})
        self.assertIn("Finding (case breaks): q1", out["calls"][0]["prompt"])
        self.assertIn("An answer replaces the OPEN mark of its case.", out["calls"][0]["prompt"])
        self.assertTrue(any("split it into two tasks" in m for m in out["logs"]), out["logs"])

    def test_a_packed_leg_is_unpacked_a_wrong_model_is_named_and_findings_cap_at_maxItems(self):
        out = run_template("spec-build", {"intent": "i", "maxItems": 1},
                           {"draft": packed(LEGS["spec-draft"]["model"], spec=SB_SPEC, sources=["x"]),
                            "attack": {"model": "claude-fable-5-1", "findings": [SB_BLOCK, SB_SMALL]}})
        r = out["result"]
        self.assertEqual((SB_SPEC, ["x"], [SB_BLOCK], 1), (r["spec"], r["sources"], r["findings"], r["dropped"]))
        self.assertEqual({"findings": 2, "blocks": 1, "small": 1}, r["counts"])
        self.assertEqual([{"label": "attack", "expected": LEGS["review-other"]["model"], "got": "claude-fable-5-1"}], r["modelMismatches"])


# risky-review: the smallest valid args (a review-diff.py line, builder and round) and a clean checklist.
GR = {"repo": "/x/hub", "head": "a" * 40, "base": "b" * 40, "hash": "c" * 64, "diffPath": "/x/s/review/a.diff",
      "files": ["Drawer/x.py", "Drawer/test_x.py"], "builder": "opus", "round": 1}
HUB_ITEMS = ("bug", "rule", "gate", "test")
WEAK = {"n": 1, "file": "Drawer/test_x.py", "line": 7, "class": "test", "what": "the refusal assertion is cut",
        "trigger": "a bad input now passes the test", "blocking": True}


def checklist(files, items=HUB_ITEMS, drop=()):
    return [{"file": f, "item": i, "result": "clear"} for f in files for i in items if (f, i) not in drop]


def review_row(findings=(), files=GR["files"], items=HUB_ITEMS, drop=(), **extra):
    return {"needs_context": "", "checklist": checklist(files, items, drop), "findings": list(findings), **extra}


def off_family(builder):
    """The [routing.legs] row a risky-review leg runs on: review unless it shares the builder's family."""
    return LEGS["review-other"] if LEGS["review"]["model"] in builder else LEGS["review"]


GR_RECORDED = json.dumps({"state": "recorded", "hash": GR["hash"], "path": "/x/s/review/pass/" + GR["hash"] + ".json"})


class RiskyReviewRuns(unittest.TestCase):
    H12 = "c" * 12

    def run_review(self, responses, **args):
        # Each Pass runs one record leg that stores it; it answers recorded unless a test says otherwise.
        return run_template("risky-review", {**GR, **args}, {f"record:{self.H12}": {"receipt": GR_RECORDED}, **responses})

    def test_a_clean_diff_passes_with_its_hash_on_one_reviewer_leg(self):
        out = self.run_review({f"review:{self.H12}": review_row()})
        r = out["result"]
        self.assertEqual(("Pass", GR["hash"], "hub", 1), (r["verdict"], r["hash"], r["kind"], r["round"]))
        self.assertEqual([(f"review:{self.H12}", "reviewer", off_family("opus")["model"], off_family("opus")["effort"])],
                         [(c["label"], c["agentType"], c["model"], c["effort"]) for c in out["calls"][:1]])
        self.assertEqual(["model"], out["calls"][0]["required"])
        self.assertTrue(r["stored"])
        record = out["calls"][1]
        self.assertEqual((2, f"record:{self.H12}", "workflow-leg", None, None, ["model", "receipt"]),
                         (len(out["calls"]), record["label"], record["agentType"], record.get("model"), record["effort"],
                          record["required"]))
        model = f"claude-{off_family('opus')['model']}-5-5"
        self.assertIn(f"Drawer/review_pass.py record --hash {GR['hash']} --head {GR['head']} --diff '{GR['diffPath']}'"
                      f" --round 1 --reviewer {model}", record["prompt"])
        prompt = out["calls"][0]["prompt"]
        for f in GR["files"]:
            self.assertIn(f"- {f}: bug, rule, gate, test", prompt)
        self.assertIn("weakened test", prompt)

    def test_the_skeptic_runs_only_on_blocking_findings_and_reads_only_those(self):
        # A Pass, with or without report-only findings, runs no refute leg.
        note = {**WEAK, "n": 2, "line": 3, "class": "report-only", "what": "a style nit 4c1e", "blocking": False}
        for findings in ((), (note,)):
            with self.subTest(findings=len(findings)):
                out = self.run_review({f"review:{self.H12}": review_row(findings)})
                self.assertEqual("Pass", out["result"]["verdict"])
                self.assertEqual([f"review:{self.H12}", f"record:{self.H12}"], [c["label"] for c in out["calls"]])
        out = self.run_review({f"review:{self.H12}": review_row([WEAK, note]),
                               f"refute:{self.H12}": {"verdicts": [{"n": 1, "refuted": False, "reason": "it is cut"}]}})
        refute = [c for c in out["calls"] if c["label"] == f"refute:{self.H12}"]
        self.assertEqual(1, len(refute))
        self.assertIn(WEAK["what"], refute[0]["prompt"])
        self.assertNotIn(note["what"], refute[0]["prompt"])

    def test_a_pass_that_is_not_stored_is_needs_context(self):
        # The risky land needs a stored Pass, so a Pass the record leg did not store is no Pass.
        refused = json.dumps({"state": "refused", "error": "the diff file does not hash"})
        other = json.dumps({"state": "recorded", "hash": "e" * 64})
        for row in (None, {"receipt": ""}, {"receipt": refused}, {"receipt": other}, {"receipt": "not json"}):
            with self.subTest(row=row):
                out = self.run_review({f"review:{self.H12}": review_row(), f"record:{self.H12}": row})
                r = out["result"]
                self.assertEqual("Needs-context", r["verdict"])
                self.assertIn("the Pass was not stored", r["reason"])
                self.assertNotIn("stored", r)
        out = self.run_review({f"review:{self.H12}": review_row([WEAK]), f"record:{self.H12}": {"receipt": refused},
                               f"refute:{self.H12}": {"verdicts": [{"n": 1, "refuted": True, "reason": "moved"}]}})
        self.assertEqual("Needs-context", out["result"]["verdict"])

    def test_an_upheld_blocking_finding_blocks(self):
        out = self.run_review({f"review:{self.H12}": review_row([WEAK]),
                               f"refute:{self.H12}": {"verdicts": [{"n": 1, "refuted": False, "reason": "it is cut"}]}})
        r = out["result"]
        self.assertEqual("Block", r["verdict"])
        self.assertEqual([("Drawer/test_x.py", 7, "test")], [(f["file"], f["line"], f["class"]) for f in r["blocking"]])
        self.assertEqual([], r["refuted"])
        self.assertIn("run round 2", r["reason"])
        self.assertEqual(["reviewer", "reviewer"], [c["agentType"] for c in out["calls"]])

    def test_a_refuted_finding_drops_to_pass(self):
        out = self.run_review({f"review:{self.H12}": review_row([WEAK]),
                               f"refute:{self.H12}": {"verdicts": [{"n": 1, "refuted": True, "reason": "moved, not cut"}]}})
        r = out["result"]
        self.assertEqual(("Pass", []), (r["verdict"], r["blocking"]))
        self.assertEqual([("Drawer/test_x.py", "moved, not cut")], [(f["file"], f["reason"]) for f in r["refuted"]])

    def test_a_failed_skeptic_or_a_missing_verdict_leaves_the_finding_standing(self):
        second = {**WEAK, "n": 2, "line": 9}
        for refute in (None, {"verdicts": []}, {"verdicts": [{"n": 2, "refuted": True, "reason": "r"}]}):
            with self.subTest(refute=refute):
                out = self.run_review({f"review:{self.H12}": review_row([WEAK, second]), f"refute:{self.H12}": refute})
                self.assertEqual("Block", out["result"]["verdict"])
                self.assertIn(7, [f["line"] for f in out["result"]["blocking"]])

    def test_a_missing_checklist_row_fails_closed(self):
        out = self.run_review({f"review:{self.H12}": review_row(drop={("Drawer/test_x.py", "test")})})
        r = out["result"]
        self.assertEqual("Needs-context", r["verdict"])
        self.assertEqual(["Drawer/test_x.py:test"], r["unchecked"])
        self.assertEqual(1, len(out["calls"]))

    def test_a_finding_row_with_no_finding_fails_closed(self):
        # A checklist row marked finding with no finding passed.
        row = review_row()
        row["checklist"][-1]["result"] = "finding"
        out = self.run_review({f"review:{self.H12}": row})
        self.assertEqual(("Needs-context", ["Drawer/test_x.py:test"]), (out["result"]["verdict"], out["result"]["orphans"]))
        row["findings"] = [{**WEAK, "class": "report-only", "blocking": False}]  # a report-only finding answers its row
        self.assertEqual("Pass", self.run_review({f"review:{self.H12}": row})["result"]["verdict"])
        row["findings"] = [{**WEAK, "class": "bug", "blocking": False}]  # another class does not
        self.assertEqual("Needs-context", self.run_review({f"review:{self.H12}": row})["result"]["verdict"])

    def test_a_blocking_finding_with_no_trigger_fails_closed(self):
        out = self.run_review({f"review:{self.H12}": review_row([{**WEAK, "trigger": " "}])})
        self.assertEqual("Needs-context", out["result"]["verdict"])
        self.assertIn("names no trigger", out["result"]["reason"])

    def test_a_failed_reviewer_or_needs_context_fails_closed(self):
        for row in (None, {"$drop": ["checklist"]}, review_row(needs_context="cannot read /x/s/review/a.diff")):
            with self.subTest(row=row):
                out = self.run_review({f"review:{self.H12}": row})
                self.assertEqual("Needs-context", out["result"]["verdict"])

    def test_the_reviewer_runs_on_the_family_the_builder_did_not_use(self):
        for builder in ("opus", "sonnet", "claude-sonnet-5-5", "haiku", "claude-opus-5-5"):
            with self.subTest(builder=builder):
                out = self.run_review({f"review:{self.H12}": review_row([WEAK])}, builder=builder)
                route = off_family(builder)
                self.assertEqual([(route["model"], route["effort"])] * 1,
                                 [(c["model"], c["effort"]) for c in out["calls"][:1]])
                for c in out["calls"]:
                    self.assertNotIn(c["model"], builder)

    def test_a_reviewer_on_the_builders_family_fails_closed(self):
        builder = off_family("opus")["model"] == "sonnet" and "opus" or "sonnet"
        out = self.run_review({f"review:{self.H12}": review_row(model=f"claude-{builder}-5-5")}, builder=builder)
        r = out["result"]
        self.assertEqual("Needs-context", r["verdict"])
        self.assertEqual([f"review:{self.H12}"], [m["label"] for m in r["modelMismatches"]])

    def test_a_product_diff_checks_bug_only_and_blocks_only_its_bar(self):
        files = ["src/combat.py", "src/items.json"]
        rows = checklist(files, ("bug",))
        weak = {**WEAK, "file": "src/combat.py"}  # a weakened test is not on the product bar: report-only
        stray = {**WEAK, "n": 2, "file": "src/other.py", "class": "bug"}  # not a review file: report-only
        out = self.run_review({f"review:{self.H12}": {"needs_context": "", "checklist": rows, "findings": [weak, stray]}},
                              files=files)
        r = out["result"]
        self.assertEqual(("product", "Pass", 2), (r["kind"], r["verdict"], len(r["reportOnly"])))
        self.assertIn("- src/combat.py: bug\n", out["calls"][0]["prompt"])
        self.assertIn("- src/items.json: bug\n", out["calls"][0]["prompt"])
        out = self.run_review({f"review:{self.H12}": {"needs_context": "", "checklist": rows[:1], "findings": []}},
                              files=files)
        self.assertEqual(["src/items.json:bug"], out["result"]["unchecked"])

    def test_bad_args_are_refused_before_any_leg(self):
        for drop, extra in (("builder", {}), ("hash", {}), (None, {"files": []}), (None, {"round": 0}),
                            (None, {"builder": "opus; rm"}), (None, {"head": "HEAD"})):
            with self.subTest(drop=drop, extra=extra):
                args = {k: v for k, v in {**GR, **extra}.items() if k != drop}
                out = run_template("risky-review", args, refused=True)
                self.assertEqual([], out["calls"])



CR_TASK = "c5d03a43-1b84-4cf3-b31b-156bbd713153"
CR_CLAIM = "fed0984a-f3c0-49a4-b9a1-1eeeb2bd1576"
CR_ARGS = {"task": CR_TASK, "claim_id": CR_CLAIM}
CR_TASK8 = CR_TASK[:8]
CR_LINE = json.dumps({"repo": "/x/hub_wt/s-c5d03a43", "head": "a" * 40, "base": "b" * 40, "hash": "c" * 64,
                      "diffPath": "/x/s/review/a.diff", "files": ["Drawer/x.py"]})
CR_PLAN_REQUIRED = ["model", "text", "earlier_work", "tags", "repo", "skills", "files", "steps", "checks", "risks", "decisions"]
CR_WORK_REQUIRED = ["model", "status", "cause", "commits", "receipt", "worktree", "rounds", "need", "extras", "review",
                    "trim", "land", "evidence"]
CR_DONE = json.dumps({"result": "COMPLETED", "id": CR_TASK, "status": "done"})
CR_CITE = f'git fetch --quiet origin main && git log --reverse --format=%H%x09%s -F --grep="task {CR_TASK8}" refs/remotes/origin/main'
CR_ROW_KEYS = {"task", "claim_id", "status", "commits", "delivered", "need", "extras", "cause"}


def cr_log(sha, subject=None):
    """One CITE stdout line: the full sha, a tab and the subject, which holds (task <task8>) by default."""
    return f"{sha}\t{subject or f'Fix a thing (task {CR_TASK8}): body'}"


def cr_plan(tags=(), **extra):
    return {"text": "card-run test card: Done when: it runs.", "earlier_work": "", "tags": list(tags), "repo": "hub", "skills": ["harness"],
            "files": [], "steps": ["s"], "checks": [], "risks": [], "decisions": [], **extra}


def cr_work(status, receipt=None, review=None, **extra):
    done = json.dumps({"result": "COMPLETED", "id": CR_TASK, "status": "done"})
    return {"model": "claude-opus-5-5", "status": status, "cause": "RESUME: none",
            "commits": [{"repo": "hub", "sha": "d" * 40}] if status == "done" else [],
            "receipt": done if receipt is None and status == "done" else (receipt or ""),
            "worktree": None, "rounds": 1, "need": None, "extras": [], "review": review, "trim": [], "land": [],
            "evidence": "", **extra}


def cr_verdict(verdict, **extra):
    return {"verdict": verdict, "reason": "r", "hash": "c" * 64, "blocking": [], **extra}


def cr_trim(mode, verdict="pass", lost=()):
    """One rules-trim.js result for one SKILL.md."""
    item = {"line": "old rule", "action": "cut", "home": "", "ok": not lost, "why": "w"}
    res = {"file": CR_SKILL, "verdict": verdict, "lines": 40, "items": [item], "lost": list(lost), "reason": "r"}
    return {"summary": f"{mode}: {verdict}", "verdict": verdict, "results": [res], "modelMismatches": []}


CR_SKILL = ".claude/skills/x/SKILL.md"
CR_WT = "/x/hub_wt/s-c5d03a43"
CR_LAND = "Drawer/hub-land.py --name"


class CardRunRuns(unittest.TestCase):
    """Card-run takes a claimed task through plan, work and a risky review loop to one row."""

    def run_card(self, plan=None, legs=None, workflows=None, **args):
        responses = {f"plan:{CR_TASK8}": plan if plan is not None else cr_plan(), **(legs or {})}
        return run_template("card-run", {**CR_ARGS, **args}, responses, workflows=workflows)

    def test_bad_args_are_refused_before_any_leg(self):
        for bad in ({}, {"task": CR_TASK}, {"task": CR_TASK, "claim_id": "c1"}, {"task": "c5d03a43", "claim_id": CR_CLAIM}):
            with self.subTest(args=bad):
                out = run_template("card-run", bad, refused=True)
                self.assertEqual([], out["calls"])
                self.assertIn("claim_id", out["error"])

    def test_the_source_has_no_clock_or_random(self):
        with open(os.path.join(WORKFLOWS, "card-run.js"), encoding="utf-8") as fh:
            src = fh.read()
        for banned in ("Date.now", "Math.random", "new Date"):
            self.assertNotIn(banned, src)

    def test_plan_and_work_schemas_routes_and_a_done_row(self):
        out = self.run_card(legs={f"work:{CR_TASK8}": cr_work("done")})
        plan, work = out["calls"]
        self.assertEqual((f"plan:{CR_TASK8}", "workflow-leg", LEGS["plan"]["model"], LEGS["plan"]["effort"], CR_PLAN_REQUIRED),
                         (plan["label"], plan["agentType"], plan["model"], plan["effort"], plan["required"]))
        tier = LEGS["tier-default"]  # the plan names no tier: the default tier
        self.assertEqual((f"work:{CR_TASK8}", "implementer", tier["model"], tier["effort"], CR_WORK_REQUIRED),
                         (work["label"], work["agentType"], work.get("model"), work["effort"], work["required"]))
        self.assertIn("Read-only", plan["prompt"])
        row = out["result"]
        self.assertTrue(CR_ROW_KEYS <= set(row), row)
        self.assertEqual(("done", True, CR_CLAIM, CR_TASK, ""), (row["status"], row["delivered"], row["claim_id"], row["task"], row["cause"]))
        self.assertEqual([], out["wcalls"])

    def test_the_plan_tier_routes_the_work_and_resume_legs(self):
        # The plan leg returns a tier; each implementer leg (work and resume) runs on that tier's [routing.legs] row.
        for tier in ("trivial", "default", "hard", "design"):
            with self.subTest(tier=tier):
                out = self.risky_two_rounds(cr_plan(["risky"], tier=tier))
                legs = [c for c in out["calls"] if c["agentType"] == "implementer"]
                self.assertEqual(3, len(legs))
                self.assertEqual([(LEGS[f"tier-{tier}"]["model"], LEGS[f"tier-{tier}"]["effort"])] * 3,
                                 [(c["model"], c["effort"]) for c in legs])
                self.assertEqual("done", out["result"]["status"])

    def test_a_missing_or_unknown_tier_takes_the_default_tier(self):
        for extra in ({}, {"tier": "huge"}, {"tier": None}, {"tier": ["design"]}, {"tier": "constructor"}, {"tier": "DESIGN"}):
            with self.subTest(extra=extra):
                out = self.risky_two_rounds(cr_plan(["risky"], **extra))
                legs = [c for c in out["calls"] if c["agentType"] == "implementer"]
                self.assertEqual([(LEGS["tier-default"]["model"], LEGS["tier-default"]["effort"])] * 3,
                                 [(c["model"], c["effort"]) for c in legs])

    def test_the_plan_leg_is_asked_for_a_tier_and_the_tier_rows_follow_the_ruling(self):
        # Operator ruling: opus only for planning and critical reasoning; design is the one opus build row.
        plan = self.run_card(legs={f"work:{CR_TASK8}": cr_work("done")})["calls"][0]
        self.assertEqual(["trivial", "default", "hard", "design"], plan["tiers"])
        self.assertIn("tier", plan["prompt"])
        self.assertNotIn("tier", plan["required"])  # a missing tier is allowed: it takes default
        tiers = {name: (row["model"], row["effort"]) for name, row in LEGS.items() if name.startswith("tier-")}
        self.assertEqual({"tier-trivial": ("haiku", "high"), "tier-default": ("sonnet", "medium"),
                          "tier-hard": ("sonnet", "high"), "tier-design": ("opus", "xhigh")}, tiers)
        self.assertEqual({"tier-design"}, {name for name, (model, _) in tiers.items() if model == "opus"})

    def test_routing_has_one_home_for_models_and_the_meeting_extract_names_no_model(self):
        # [routing] holds the effort rungs and [routing.legs]; the old per-tag rows nothing read are gone.
        self.assertEqual({"rungs", "legs"}, set(harness_config.load(os.path.dirname(HERE))["routing"]))
        with open(os.path.join(os.path.dirname(HERE), ".claude", "skills", "memory", "meeting.md"), encoding="utf-8") as fh:
            step = fh.read().split("## Extract", 1)[1].split("2. A leg returns", 1)[0]
        self.assertIn("fan-out", step)
        self.assertEqual([], re.findall(r"opus|sonnet|haiku", step))

    def test_one_leg_and_fan_out_read_on_haiku(self):
        self.assertEqual({"one-leg": ("haiku", "high"), "fan-out": ("haiku", "high")},
                         {name: (LEGS[name]["model"], LEGS[name]["effort"]) for name in ("one-leg", "fan-out")})

    def risky_two_rounds(self, plan, **args):
        return self.run_card(plan, {
            f"work:{CR_TASK8}": cr_work("review", review=CR_LINE),
            f"resume:{CR_TASK8}:1": cr_work("review", review=CR_LINE),
            f"resume:{CR_TASK8}:2": cr_work("done")}, {"risky-review": [cr_verdict("Block"), cr_verdict("Pass")]}, **args)

    def test_the_work_brief_opens_with_the_resume_block_only_on_a_rerun(self):
        # A re-run: the plan leg found earlier work, or Flight passed notes.
        for plan, args in ((cr_plan(["risky"], earlier_work=f"worktree s-{CR_TASK8}"), {}),
                           (cr_plan(["risky"]), {"notes": "the answer to the NEED"})):
            with self.subTest(earlier=plan["earlier_work"], notes=args):
                out = self.risky_two_rounds(plan, **args)
                briefs = [c for c in out["calls"] if c["agentType"] == "implementer"]
                self.assertEqual([f"work:{CR_TASK8}", f"resume:{CR_TASK8}:1", f"resume:{CR_TASK8}:2"], [c["label"] for c in briefs])
                p = briefs[0]["prompt"]
                self.assertLess(p.index("RESUME"), p.index("The task card, as written"))
                for part in (f"-{CR_TASK8}", f"product_worktree.py add --task {CR_TASK8}", "git log --oneline origin/main..HEAD",
                             f"jobs/{CR_TASK8}-*.json", "long-job.py status", f"mem.py notes {CR_TASK} --json",
                             f"--claim-id {CR_CLAIM}"):
                    self.assertIn(part, p)
                self.assertEqual("done", out["result"]["status"])
        # A first run: no RESUME; each implementer brief still reads the notes and obeys a stop row.
        out = self.risky_two_rounds(cr_plan(["risky"]))
        briefs = [c for c in out["calls"] if c["agentType"] == "implementer"]
        self.assertEqual(3, len(briefs))
        for c in briefs:
            with self.subTest(leg=c["label"]):
                self.assertNotIn("RESUME (do this first", c["prompt"])
                for part in (f"mem.py notes {CR_TASK} --json", '"stop": true',
                             f"task-lifecycle.py release {CR_TASK} --claim-id {CR_CLAIM}"):
                    self.assertIn(part, c["prompt"])
        self.assertEqual("done", out["result"]["status"])

    def test_resume_legs_after_a_child_workflow_rerun_no_resume_and_read_no_skills(self):
        out = self.risky_two_rounds(cr_plan(["risky"], earlier_work=f"worktree s-{CR_TASK8}"))
        for c in out["calls"][2:4]:
            with self.subTest(leg=c["label"]):
                self.assertTrue(c["label"].startswith(f"resume:{CR_TASK8}:"))
                self.assertNotIn("RESUME (do this first", c["prompt"])
                self.assertNotIn("Read the skills", c["prompt"])
                self.assertIn(f"-{CR_TASK8}", c["prompt"])  # the worktree finder when the return names none

    def test_each_brief_holds_the_card_text_once_and_a_one_line_env(self):
        card = "Unique card body 7f3a: Done when: the widget answers."
        out = self.risky_two_rounds(cr_plan(["risky"], text=card, earlier_work="notes: 1"))
        for c in out["calls"]:
            with self.subTest(leg=c["label"]):
                env = c["prompt"].split("\n", 1)[0]
                self.assertLessEqual(len(env.encode()), 300, env)
                self.assertIn(f"Claim {CR_CLAIM} on task {CR_TASK}", env)
                if c["agentType"] == "implementer":
                    self.assertEqual(1, c["prompt"].count(card))
                    self.assertNotIn('"text"', c["prompt"])

    def test_a_refused_complete_is_failed_and_never_delivered(self):
        refusal = json.dumps({"error": "claim_id does not match the live claim"})
        out = self.run_card(legs={f"work:{CR_TASK8}": cr_work("failed", receipt=refusal, cause="complete refused")})
        row = out["result"]
        self.assertEqual(("failed", False), (row["status"], row["delivered"]))
        self.assertIn("complete refused", row["cause"])
        self.assertIn("run complete no second time", out["calls"][1]["prompt"])
        for receipt in ("", json.dumps({"result": "COMPLETED", "id": "f" * 8 + CR_TASK[8:]}), "not json"):
            with self.subTest(receipt=receipt):
                row = self.run_card(legs={f"work:{CR_TASK8}": {**cr_work("done"), "receipt": receipt}})["result"]
                self.assertEqual(("failed", False), (row["status"], row["delivered"]))
                self.assertIn("no COMPLETED receipt", row["cause"])

    def test_a_resumed_already_landed_item_completes_with_every_landed_commit(self):
        # RESUME finds the item on origin/main. The leg returns landed with the rev-list,
        # and card-run completes under this claim with every landed commit; no brief has the leg write --commit.
        s1, s2 = "e" * 40, "d" * 40
        work = cr_work("landed", cause="RESUME: origin/main holds 2 task commits", evidence="RESUME found the delivery landed",
                       land=[{"repo": "hub", "line": "", "revlist": f"{cr_log(s1)}\n{cr_log(s2)}\n"}])
        earlier = cr_plan(earlier_work="origin/main holds 2 task commits")
        out = self.run_card(earlier, {f"work:{CR_TASK8}": work, f"complete:{CR_TASK8}": {"receipt": CR_DONE}})
        row = out["result"]
        self.assertEqual(("done", True, ""), (row["status"], row["delivered"], row["cause"]))
        self.assertEqual([{"repo": "hub", "sha": s1}, {"repo": "hub", "sha": s2}], row["commits"])
        self.assertEqual(["hub=" + s1, "hub=" + s2], re.findall(r"--commit (\S+)", out["calls"][-1]["prompt"]))
        self.assertIn(f'git log --format=%H -F --grep="task {CR_TASK8}" origin/main', out["calls"][0]["prompt"])
        # After this run's risky review the land line is never '': hub-land prints already.
        already = {**work, "land": [{**work["land"][0], "line": json.dumps({"state": "already", "tip": s2})}]}
        risky = self.run_card(cr_plan(["risky"], earlier_work="origin/main holds 2 task commits"),
                              {f"work:{CR_TASK8}": cr_work("review", review=CR_LINE),
                               f"resume:{CR_TASK8}:1": already, f"complete:{CR_TASK8}": {"receipt": CR_DONE}},
                              {"risky-review": [cr_verdict("Pass")]})
        self.assertEqual(("done", f"complete:{CR_TASK8}"), (risky["result"]["status"], risky["calls"][-1]["label"]))
        briefs = [out["calls"][1]] + [c for c in risky["calls"] if c["agentType"] == "implementer"]
        self.assertEqual([f"work:{CR_TASK8}", f"work:{CR_TASK8}", f"resume:{CR_TASK8}:1"], [c["label"] for c in briefs])
        for c in briefs:
            with self.subTest(leg=c["label"]):
                parts = (CR_CITE, "status landed", f"--claim-id {CR_CLAIM}")
                if c["label"].startswith("work:"):  # RESUME, in the work brief of a re-run only
                    parts += (f'git log --format=%H -F --grep="task {CR_TASK8}" origin/main', "build nothing more")
                for part in parts:
                    self.assertIn(part, c["prompt"])
                self.assertNotIn("--commit", c["prompt"])
                self.assertNotIn("for each pushed repo", c["prompt"])

    def test_a_rebased_land_completes_with_the_commits_card_run_computes(self):
        """A land of two rebased commits; card-run parses the raw rev-list (fetch noise and a short sha
        dropped) and one complete leg gets exactly both full shas."""
        s1, s2 = "1" * 39 + "a", "2" * 39 + "b"
        noise = "From github.com:o/r\n * branch            main       -> FETCH_HEAD\n3b74e21\n"
        land = {"repo": "hub", "line": json.dumps({"state": "landed", "tip": s2, "removed": True}),
                "revlist": f"{noise}{cr_log(s1)}\n{cr_log(s2)}\n"}
        out = self.run_card(legs={f"work:{CR_TASK8}": cr_work("landed", land=[land], evidence="built the CITE parse as planned"),
                                  f"complete:{CR_TASK8}": {"receipt": CR_DONE}})
        self.assertEqual([f"plan:{CR_TASK8}", f"work:{CR_TASK8}", f"complete:{CR_TASK8}"], [c["label"] for c in out["calls"]])
        work, complete = out["calls"][1], out["calls"][2]
        self.assertEqual(("workflow-leg", LEGS["complete"]["model"], LEGS["complete"]["effort"], ["model", "receipt"]),
                         (complete["agentType"], complete.get("model"), complete["effort"], complete["required"]))
        p = complete["prompt"]
        self.assertIn(f"task-lifecycle.py complete {CR_TASK} --claim-id {CR_CLAIM} --evidence", p)
        self.assertEqual(["hub=" + s1, "hub=" + s2], re.findall(r"--commit (\S+)", p))
        self.assertIn("built the CITE parse as planned", p)
        for junk in ("FETCH_HEAD", "From ", "3b74e21", "--path"):
            self.assertNotIn(junk, p)
        self.assertNotIn("--commit", work["prompt"])
        self.assertIn(CR_CITE, work["prompt"])
        self.assertIn("--no-file-changes", work["prompt"])
        self.assertEqual(CR_WORK_REQUIRED, work["required"])
        row = out["result"]
        self.assertEqual(("done", True, ""), (row["status"], row["delivered"], row["cause"]))
        self.assertEqual([{"repo": "hub", "sha": s1}, {"repo": "hub", "sha": s2}], row["commits"])

    # Oneshot run 2026-11-17: the land log showed smoke green, but the result row carried only the shas, so Flight's
    # report said it could not tell whether the ship smoke ran. Injection: drop land from the card-run row.
    def test_the_row_carries_each_land_state_and_its_smoke_or_gate(self):
        s1, s2 = "1" * 39 + "a", "2" * 39 + "b"
        hub = {"repo": "hub", "line": json.dumps({"state": "landed", "tip": s1, "smoke": "green", "removed": True}),
               "revlist": cr_log(s1) + "\n"}
        product = {"repo": "product", "line": json.dumps({"state": "pushed", "head": s2, "gated": True, "rounds": 1}),
                "revlist": cr_log(s2) + "\n"}
        out = self.run_card(cr_plan(repo="both"), {f"work:{CR_TASK8}": cr_work("landed", land=[hub, product], evidence="e e e e"),
                                                   f"complete:{CR_TASK8}": {"receipt": CR_DONE}})
        row = out["result"]
        self.assertEqual(("done", True), (row["status"], row["delivered"]))
        self.assertEqual([{"repo": "hub", "state": "landed", "smoke": "green"},
                          {"repo": "product", "state": "pushed", "gated": True}], row["land"])
        resumed = cr_work("landed", land=[{"repo": "hub", "line": "", "revlist": cr_log(s1) + "\n"}], evidence="e e e e")
        row = self.run_card(legs={f"work:{CR_TASK8}": resumed, f"complete:{CR_TASK8}": {"receipt": CR_DONE}})["result"]
        self.assertEqual([{"repo": "hub", "state": "already"}], row["land"])  # RESUME found it landed: no smoke this run
        self.assertEqual([], self.run_card(legs={f"work:{CR_TASK8}": cr_work("done")})["result"]["land"])

    def test_a_landed_return_card_run_cannot_cite_fails_with_no_complete(self):
        s = "f" * 40
        line = json.dumps({"state": "landed", "tip": s})
        for land, cause in (([{"repo": "hub", "line": line, "revlist": cr_log("e" * 40) + "\n"}], "lacks the landed tip " + s[:12]),
                            ([{"repo": "hub", "line": "", "revlist": "From x\n3b74e21\n"}], "no full sha"),
                            ([], "no land"),
                            ([{"repo": "both", "line": "", "revlist": cr_log(s)}], "repo both"),
                            ([{"repo": "hub", "line": json.dumps({"state": "failed", "tip": s}), "revlist": cr_log(s)}], "not a landed state")):
            with self.subTest(cause=cause):
                out = self.run_card(legs={f"work:{CR_TASK8}": cr_work("landed", land=land, evidence="e e e e")})
                self.assertEqual([f"plan:{CR_TASK8}", f"work:{CR_TASK8}"], [c["label"] for c in out["calls"]])
                row = out["result"]
                self.assertEqual(("failed", False, []), (row["status"], row["delivered"], row["commits"]))
                self.assertIn(cause, row["cause"])

    def test_a_foreign_commit_that_mentions_the_task_is_not_cited(self):
        """The -F grep also finds a commit of another task whose message
        names this task. card-run cites a commit only when the first (task <hex8> of its subject
        is this task."""
        s1, s2, f1, f2 = "1" * 39 + "a", "2" * 39 + "b", "3" * 39 + "c", "4" * 39 + "d"
        revlist = "\n".join([
            cr_log(s1),
            cr_log(f1, f"Watchdog knows a seated Flight (task 1a7bd899, incident 53005a38): a fix that task {CR_TASK8} found"),
            cr_log(f2, f"Review diff (task fe69f631): a follow-up to the work that landed in (task {CR_TASK8})"),
            cr_log(s2, f"Fix the review finding (task {CR_TASK8}; review leg fixes): body"),
        ]) + "\n"
        land = {"repo": "hub", "line": json.dumps({"state": "landed", "tip": s2}), "revlist": revlist}
        out = self.run_card(legs={f"work:{CR_TASK8}": cr_work("landed", land=[land], evidence="e e e e"),
                                  f"complete:{CR_TASK8}": {"receipt": CR_DONE}})
        p = out["calls"][-1]["prompt"]
        self.assertEqual(f"complete:{CR_TASK8}", out["calls"][-1]["label"])
        self.assertEqual(["hub=" + s1, "hub=" + s2], re.findall(r"--commit (\S+)", p))
        for foreign in (f1, f2):
            self.assertNotIn(foreign, p)
        row = out["result"]
        self.assertEqual(("done", True), (row["status"], row["delivered"]))
        self.assertEqual([{"repo": "hub", "sha": s1}, {"repo": "hub", "sha": s2}], row["commits"])
        # Only foreign commits: nothing to cite, no complete.
        land = {"repo": "hub", "line": "", "revlist": cr_log(f1, f"Other (task 1a7bd899): names task {CR_TASK8}") + "\n"}
        out = self.run_card(legs={f"work:{CR_TASK8}": cr_work("landed", land=[land], evidence="e e e e")})
        self.assertEqual([f"plan:{CR_TASK8}", f"work:{CR_TASK8}"], [c["label"] for c in out["calls"]])
        self.assertEqual(("failed", []), (out["result"]["status"], out["result"]["commits"]))
        self.assertIn("no full sha of task", out["result"]["cause"])

    def test_a_subject_that_starts_with_task_and_this_id_is_cited(self):
        """P4 E2E (incident 7f44bdcd): the worker wrote 'task <id8>: ...' in place of '(task <id8>)'; the land
        was green, but card-run cited nothing and the card stayed todo."""
        s, f = "5" * 40, "6" * 40
        revlist = "\n".join([cr_log(s, f"task {CR_TASK8}: hub-hello prints the hub name"),
                             cr_log(f, f"task 1a7bd899: a fix that names task {CR_TASK8}")]) + "\n"
        land = {"repo": "hub", "line": json.dumps({"state": "landed", "tip": s}), "revlist": revlist}
        out = self.run_card(legs={f"work:{CR_TASK8}": cr_work("landed", land=[land], evidence="e e e e"),
                                  f"complete:{CR_TASK8}": {"receipt": CR_DONE}})
        self.assertEqual(["hub=" + s], re.findall(r"--commit (\S+)", out["calls"][-1]["prompt"]))
        self.assertEqual(("done", True), (out["result"]["status"], out["result"]["delivered"]))

    def test_an_empty_land_line_after_this_runs_review_fails_with_no_complete(self):
        """After a risky review this run, the finish brief runs hub-land or product-push, which print
        a line; an empty line skipped the state, reviewed and tip checks."""
        s = "9" * 40
        out = self.run_card(cr_plan(["risky"]), {
            f"work:{CR_TASK8}": cr_work("review", review=CR_LINE),
            f"resume:{CR_TASK8}:1": cr_work("landed", evidence="e e e e", land=[{"repo": "hub", "line": "", "revlist": cr_log(s)}]),
            f"complete:{CR_TASK8}": {"receipt": CR_DONE}}, {"risky-review": [cr_verdict("Pass")]})
        self.assertNotIn(f"complete:{CR_TASK8}", [c["label"] for c in out["calls"]])
        self.assertEqual(("failed", False), (out["result"]["status"], out["result"]["delivered"]))
        self.assertIn("land line is empty", out["result"]["cause"])

    def test_a_land_that_misses_a_repo_the_plan_names_fails_with_no_complete(self):
        """Each repo plan.repo names (both: hub and product) needs a land item."""
        s = "9" * 40
        hub = {"repo": "hub", "line": json.dumps({"state": "landed", "tip": s}), "revlist": cr_log(s)}
        for repo in ("both", "product"):
            with self.subTest(repo=repo):
                out = self.run_card(cr_plan(repo=repo), {f"work:{CR_TASK8}": cr_work("landed", land=[hub], evidence="e e e e"),
                                                         f"complete:{CR_TASK8}": {"receipt": CR_DONE}})
                self.assertEqual([f"plan:{CR_TASK8}", f"work:{CR_TASK8}"], [c["label"] for c in out["calls"]])
                self.assertEqual(("failed", []), (out["result"]["status"], out["result"]["commits"]))
                self.assertIn("no product land", out["result"]["cause"])

    def test_a_product_land_and_a_refused_complete(self):
        s = "a" * 39 + "c"
        land = [{"repo": "product", "line": json.dumps({"state": "pushed", "head": s}), "revlist": cr_log(s) + "\n"}]
        out = self.run_card(cr_plan(repo="product"), {f"work:{CR_TASK8}": cr_work("landed", land=land, evidence="e e e e"),
                                                   f"complete:{CR_TASK8}": {"receipt": CR_DONE}})
        self.assertEqual(["product=" + s], re.findall(r"--commit (\S+)", out["calls"][-1]["prompt"]))
        self.assertEqual(("done", [{"repo": "product", "sha": s}]), (out["result"]["status"], out["result"]["commits"]))
        refusal = json.dumps({"error": "delivery check refused: a cited commit is not on origin/main"})
        out = self.run_card(cr_plan(repo="product"), {f"work:{CR_TASK8}": cr_work("landed", land=land, evidence="e e e e"),
                                                   f"complete:{CR_TASK8}": {"receipt": refusal}})
        row = out["result"]
        self.assertEqual(("failed", False), (row["status"], row["delivered"]))
        self.assertIn("complete refused: delivery check refused", row["cause"])
        out = self.run_card(cr_plan(repo="product"), {f"work:{CR_TASK8}": cr_work("landed", land=land, evidence="e e e e"),
                                                   f"complete:{CR_TASK8}": None})
        self.assertEqual(("failed", "the complete leg returned nothing"), (out["result"]["status"], out["result"]["cause"]))

    def test_a_refused_complete_names_its_reason_in_the_row_cause(self):
        refusal = json.dumps({"error": "delivery check refused: no cited commit changes Drawer/x.py"})
        row = self.run_card(legs={f"work:{CR_TASK8}": cr_work("failed", receipt=refusal, cause="RESUME: worktree found")})["result"]
        self.assertEqual(("failed", False), (row["status"], row["delivered"]))
        self.assertIn("complete refused: delivery check refused: no cited commit changes Drawer/x.py", row["cause"])
        self.assertIn("RESUME: worktree found", row["cause"])

    def test_a_failed_plan_or_worker_is_a_failed_row(self):
        out = self.run_card(plan=None, legs={f"plan:{CR_TASK8}": None})
        self.assertEqual(1, len(out["calls"]))
        self.assertEqual(("failed", "the plan leg returned nothing"), (out["result"]["status"], out["result"]["cause"]))
        out = self.run_card(legs={f"work:{CR_TASK8}": None})
        self.assertEqual(("failed", "the implementer returned nothing"), (out["result"]["status"], out["result"]["cause"]))
        for status in ("released", "need"):
            with self.subTest(status=status):
                row = self.run_card(legs={f"work:{CR_TASK8}": cr_work(status, need="q" if status == "need" else None)})["result"]
                self.assertEqual((status, False), (row["status"], row["delivered"]))

    def test_a_risky_pass_lands_with_the_reviewed_hash(self):
        out = self.run_card(cr_plan(["complex", "risky"]), {f"work:{CR_TASK8}": cr_work("review", review=CR_LINE),
                                                            f"resume:{CR_TASK8}:1": cr_work("done")},
                            {"risky-review": [cr_verdict("Pass")]})
        self.assertIn("The task is risky", out["calls"][1]["prompt"])
        self.assertEqual(1, len(out["wcalls"]))
        call = out["wcalls"][0]
        self.assertEqual(("risky-review", "a" * 40, "c" * 64, 1, "claude-opus-5-5"),
                         (call["name"], call["args"]["head"], call["args"]["hash"], call["args"]["round"], call["args"]["builder"]))
        self.assertIn("--reviewed " + "c" * 64, out["calls"][2]["prompt"])
        self.assertEqual(("done", True), (out["result"]["status"], out["result"]["delivered"]))

    def test_a_risky_item_whose_work_leg_returns_done_still_runs_the_review(self):
        # Card-run reviewed only a work leg that returned status review.
        s = "9" * 40
        landed = cr_work("landed", evidence="e e e e", land=[
            {"repo": "hub", "line": json.dumps({"state": "landed", "tip": s, "reviewed": "c" * 64}), "revlist": cr_log(s) + "\n"}])
        for first in (cr_work("done"), cr_work("landed", evidence="e e e e", land=[
                {"repo": "hub", "line": json.dumps({"state": "landed", "tip": s}), "revlist": cr_log(s) + "\n"}])):
            with self.subTest(status=first["status"]):
                out = self.run_card(cr_plan(["risky"]), {f"work:{CR_TASK8}": first,
                                                         f"review-first:{CR_TASK8}": cr_work("review", review=CR_LINE),
                                                         f"resume:{CR_TASK8}:1": landed,
                                                         f"complete:{CR_TASK8}": {"receipt": CR_DONE}},
                                    {"risky-review": [cr_verdict("Pass")]})
                self.assertEqual([f"plan:{CR_TASK8}", f"work:{CR_TASK8}", f"review-first:{CR_TASK8}", f"resume:{CR_TASK8}:1",
                                  f"complete:{CR_TASK8}"], [c["label"] for c in out["calls"]])
                first_leg = out["calls"][2]
                self.assertEqual("implementer", first_leg["agentType"])
                self.assertIn("Drawer/review-diff.py", first_leg["prompt"])
                self.assertEqual(["risky-review"], [w["name"] for w in out["wcalls"]])
                self.assertIn("--reviewed " + "c" * 64, out["calls"][3]["prompt"])
                self.assertEqual(("done", True), (out["result"]["status"], out["result"]["delivered"]))
                self.assertEqual("c" * 64, out["result"]["reviews"][0]["hash"])

    def test_a_risky_item_with_no_pass_is_a_failed_row(self):
        # A risky land line whose reviewed is not a Pass hash of this run, or a risky done with commits and no
        # review, faults: no complete leg runs.
        s = "9" * 40
        for reviewed in (None, "e" * 64):
            with self.subTest(reviewed=reviewed):
                line = {"state": "landed", "tip": s, **({"reviewed": reviewed} if reviewed else {})}
                out = self.run_card(cr_plan(["risky"]), {
                    f"work:{CR_TASK8}": cr_work("review", review=CR_LINE),
                    f"resume:{CR_TASK8}:1": cr_work("landed", evidence="e e e e",
                                                    land=[{"repo": "hub", "line": json.dumps(line), "revlist": cr_log(s)}]),
                    f"complete:{CR_TASK8}": {"receipt": CR_DONE}}, {"risky-review": [cr_verdict("Pass")]})
                self.assertNotIn(f"complete:{CR_TASK8}", [c["label"] for c in out["calls"]])
                self.assertEqual(("failed", False), (out["result"]["status"], out["result"]["delivered"]))
                self.assertIn("a risky land with no risky-review Pass", out["result"]["cause"])
        out = self.run_card(cr_plan(["risky"]), {f"work:{CR_TASK8}": cr_work("done"),
                                                 f"review-first:{CR_TASK8}": cr_work("done")})
        self.assertEqual([], out["wcalls"])
        self.assertEqual(("failed", False), (out["result"]["status"], out["result"]["delivered"]))
        self.assertIn("no risky-review Pass", out["result"]["cause"])
        # A risky task that changes no file, and one an earlier run landed, complete with no review.
        out = self.run_card(cr_plan(["risky"]), {f"work:{CR_TASK8}": cr_work("done", commits=[]),
                                                 f"review-first:{CR_TASK8}": cr_work("done", commits=[])})
        self.assertEqual(("done", True), (out["result"]["status"], out["result"]["delivered"]))
        out = self.run_card(cr_plan(["risky"]), {
            f"work:{CR_TASK8}": cr_work("landed", evidence="e e e e", land=[{"repo": "hub", "line": "", "revlist": cr_log(s)}]),
            f"complete:{CR_TASK8}": {"receipt": CR_DONE}})
        self.assertEqual([f"plan:{CR_TASK8}", f"work:{CR_TASK8}", f"complete:{CR_TASK8}"], [c["label"] for c in out["calls"]])
        self.assertEqual("done", out["result"]["status"])

    def test_a_resumed_risky_partial_land_is_delivered_with_no_review(self):
        # The tip was on origin/main before this hub-land call (a partial land); hub-land prints landed,
        # smoke 'pushed before' and reviewed null. That is an earlier land: no review-first leg, no Pass needed.
        s = "9" * 40
        line = {"state": "landed", "tip": s, "smoke": "pushed before", "reviewed": None, "removed": True}
        work = cr_work("landed", evidence="e e e e", land=[{"repo": "hub", "line": json.dumps(line), "revlist": cr_log(s)}])
        out = self.run_card(cr_plan(["risky"]), {f"work:{CR_TASK8}": work, f"complete:{CR_TASK8}": {"receipt": CR_DONE}})
        self.assertEqual([f"plan:{CR_TASK8}", f"work:{CR_TASK8}", f"complete:{CR_TASK8}"], [c["label"] for c in out["calls"]])
        self.assertEqual(("done", True), (out["result"]["status"], out["result"]["delivered"]))
        self.assertEqual(["hub=" + s], re.findall(r"--commit (\S+)", out["calls"][-1]["prompt"]))
        # A fresh land (smoke green) or a product push with reviewed null is no earlier land: it still faults.
        for repo, fresh in (("hub", {**line, "smoke": "green"}),
                            ("product", {"state": "pushed", "head": s, "smoke": "pushed before", "reviewed": None})):
            with self.subTest(repo=repo, line=fresh):
                out = self.run_card(cr_plan(["risky"], repo=repo), {
                    f"work:{CR_TASK8}": cr_work("review", review=CR_LINE),
                    f"resume:{CR_TASK8}:1": cr_work("landed", evidence="e e e e",
                                                    land=[{"repo": repo, "line": json.dumps(fresh), "revlist": cr_log(s)}]),
                    f"complete:{CR_TASK8}": {"receipt": CR_DONE}}, {"risky-review": [cr_verdict("Pass")]})
                self.assertNotIn(f"complete:{CR_TASK8}", [c["label"] for c in out["calls"]])
                self.assertEqual(("failed", False), (out["result"]["status"], out["result"]["delivered"]))
                self.assertIn("a risky land with no risky-review Pass", out["result"]["cause"])

    def test_no_verdict_is_needs_context_twice_then_a_release(self):
        for answers in ([None, None], [{"$throw": "boom"}, None], [cr_verdict("Needs-context"), cr_verdict("Needs-context")]):
            with self.subTest(answers=answers):
                out = self.run_card(cr_plan(["risky"]), {f"work:{CR_TASK8}": cr_work("review", review=CR_LINE),
                                                         f"resume:{CR_TASK8}:1": cr_work("released", cause="card-run: no verdict")},
                                    {"risky-review": answers})
                self.assertEqual(2, len(out["wcalls"]))
                p = out["calls"][2]["prompt"]
                self.assertIn(f"task-lifecycle.py release {CR_TASK} --claim-id {CR_CLAIM}", p)
                self.assertNotIn("--reviewed", p)
                self.assertEqual(("released", False), (out["result"]["status"], out["result"]["delivered"]))
                self.assertEqual("Needs-context", out["result"]["reviews"][0]["verdict"])

    def test_a_block_goes_back_to_fix_and_releases_after_three_rounds(self):
        legs = {f"work:{CR_TASK8}": cr_work("review", review=CR_LINE),
                f"resume:{CR_TASK8}:1": cr_work("review", review=CR_LINE),
                f"resume:{CR_TASK8}:2": cr_work("review", review=CR_LINE),
                f"resume:{CR_TASK8}:3": cr_work("released", cause="block")}
        out = self.run_card(cr_plan(["risky"]), legs, {"risky-review": [cr_verdict("Block")] * 3})
        labels = [c["label"] for c in out["calls"]]
        self.assertEqual([f"plan:{CR_TASK8}", f"work:{CR_TASK8}", f"resume:{CR_TASK8}:1", f"resume:{CR_TASK8}:2", f"resume:{CR_TASK8}:3"], labels)
        self.assertIn("Fix each blocking finding", out["calls"][2]["prompt"])
        self.assertIn("Block after 3 rounds", out["calls"][4]["prompt"])
        self.assertEqual([1, 2, 3], [w["args"]["round"] for w in out["wcalls"]])
        self.assertEqual("released", out["result"]["status"])

    def test_review_on_a_task_not_tagged_risky_or_with_a_bad_line_runs_no_review(self):
        for plan, line in ((cr_plan(), CR_LINE), (cr_plan(["risky"]), "none"), (cr_plan(["risky"]), None)):
            with self.subTest(tags=plan["tags"], line=line):
                out = self.run_card(plan, {f"work:{CR_TASK8}": cr_work("review", review=line)}, {"risky-review": [cr_verdict("Pass")]})
                self.assertEqual([], out["wcalls"])
                self.assertEqual(("failed", False), (out["result"]["status"], out["result"]["delivered"]))
        self.assertIn("never return status review", self.run_card(legs={f"work:{CR_TASK8}": cr_work("done")})["calls"][1]["prompt"])

    def test_a_skill_md_item_runs_rules_trim_propose_then_review(self):
        """OPR-17: a SKILL.md edit runs rules-trim propose, the cuts, a
        review on another model, then the pass rows and the commit, before the land."""
        legs = {f"work:{CR_TASK8}": cr_work("trim", trim=[CR_SKILL], worktree=CR_WT),
                f"trim:{CR_TASK8}:1": cr_work("trim", trim=[CR_SKILL], worktree=CR_WT),
                f"trim:{CR_TASK8}:2": cr_work("done")}
        out = self.run_card(cr_plan(), legs, {"rules-trim": [cr_trim("propose"), cr_trim("review")]})
        row = LEGS["review"] if LEGS["review"]["model"] not in "claude-opus-5-5" else LEGS["review-other"]
        self.assertEqual([{"name": "rules-trim", "args": {"mode": "propose", "files": [CR_SKILL], "root": CR_WT}},
                          {"name": "rules-trim", "args": {"mode": "review", "files": [CR_SKILL], "root": CR_WT,
                                                          "model": row["model"], "effort": row["effort"]}}],
                         out["wcalls"])
        # A builder on the review row's family takes the review-other row, model and effort.
        for label in (f"work:{CR_TASK8}", f"trim:{CR_TASK8}:1"):
            legs[label] = cr_work("trim", trim=[CR_SKILL], worktree=CR_WT, model=f"claude-{LEGS['review']['model']}-5-5")
        out = self.run_card(cr_plan(), legs, {"rules-trim": [cr_trim("propose"), cr_trim("review")]})
        other = LEGS["review-other"]
        self.assertEqual({"mode": "review", "files": [CR_SKILL], "root": CR_WT, "model": other["model"], "effort": other["effort"]},
                         out["wcalls"][1]["args"])
        self.assertEqual([f"plan:{CR_TASK8}", f"work:{CR_TASK8}", f"trim:{CR_TASK8}:1", f"trim:{CR_TASK8}:2"],
                         [c["label"] for c in out["calls"]])
        work, apply, after = (c["prompt"] for c in out["calls"][1:])
        self.assertIn("return status trim", work)
        self.assertIn("rules_audit.py log --file", apply)
        self.assertIn("old rule", apply)
        self.assertNotIn(CR_LAND, apply)
        self.assertIn(f"rules_audit.py pass --file {CR_SKILL}", after)
        self.assertLess(after.index("rules_audit.py pass"), after.index(CR_LAND))
        row = out["result"]
        self.assertEqual(("done", True, "pass"), (row["status"], row["delivered"], row["trim"]["verdict"]))
        lost = [{"line": "old rule", "action": "move", "home": "x.md", "ok": False, "why": "the clause is lost"}]
        legs[f"trim:{CR_TASK8}:2"] = cr_work("released", cause="card-run: rules-trim review fail")
        for answers, verdict in (([cr_trim("propose"), cr_trim("review", "fail", lost)], "fail"),
                                 ([cr_trim("propose"), None, {"$throw": "boom"}], "none")):
            with self.subTest(verdict=verdict):
                out = self.run_card(cr_plan(), legs, {"rules-trim": answers})
                self.assertEqual(len(answers), len(out["wcalls"]))
                p = out["calls"][3]["prompt"]
                self.assertIn(f"task-lifecycle.py release {CR_TASK} --claim-id {CR_CLAIM}", p)
                self.assertIn(f"rules-trim review {verdict}", p)
                self.assertNotIn("rules_audit.py pass", p)
                self.assertEqual(("released", verdict), (out["result"]["status"], out["result"]["trim"]["verdict"]))
        for work in (cr_work("trim", trim=["Drawer/x.py"], worktree=CR_WT), cr_work("trim", trim=[CR_SKILL])):
            with self.subTest(trim=work["trim"], worktree=work["worktree"]):
                out = self.run_card(cr_plan(), {f"work:{CR_TASK8}": work}, {"rules-trim": [cr_trim("propose")]})
                self.assertEqual(([], "failed"), (out["wcalls"], out["result"]["status"]))


if __name__ == "__main__":
    unittest.main(verbosity=1)
