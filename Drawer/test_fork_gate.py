"""Selftest for Drawer/fork-gate.py (run: .venv/bin/python -B -m pytest -q Drawer/test_fork_gate.py).
Judges only; FORK_GATE=dry, so it writes nothing in the checkout.

Every expected value is the rule written out: a leg names haiku or sonnet; opus needs a route tag of an
opus row of [routing.legs]; no leg inherits the session model.
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
HUB = os.path.dirname(HERE)
os.environ.setdefault("FORK_GATE", "dry")
sys.path.insert(0, HERE)
import harness_config  # noqa: E402


def load():
    spec = importlib.util.spec_from_file_location("fork_gate_under_test", os.path.join(HERE, "fork-gate.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GATE = load()
ALLOWED = ("opus", "sonnet", "haiku")
OPUS_ROLES = ("plan", "review-other", "tier-design")


def judge(script):
    return GATE.script_reason(script, ALLOWED, OPUS_ROLES)


class Scripts(unittest.TestCase):
    def test_a_leg_with_no_options_is_refused(self):
        self.assertIn("line 2 has no model", judge("phase('x')\nconst r = await agent('do it')\n"))

    def test_a_leg_whose_options_lack_a_model_is_refused(self):
        self.assertIn("line 1 has no model", judge("agent(p, { label: 'a', schema: S })"))

    def test_a_non_literal_options_value_is_refused(self):
        self.assertIn("has no model", judge("agent(p, opts)"))
        self.assertIn("has no model", judge("agent(p, { ...opts })"))

    def test_haiku_and_sonnet_legs_pass(self):
        self.assertIsNone(judge("agent(p, { model: 'haiku', schema: S })\nagent(q, {label: 'v', 'model': \"sonnet\"})"))

    def test_a_routed_constant_passes(self):
        self.assertIsNone(judge("const FIND = 'haiku'\nconst model = 'sonnet'\nagent(p, { model: FIND })\nagent(q, { model })"))

    def test_every_call_is_checked_not_only_the_first(self):
        self.assertIn("line 2 has no model", judge("agent(p, { model: 'haiku' })\nagent(q, { label: 'x' })"))

    def test_a_nested_call_in_a_pipeline_is_checked(self):
        src = "await pipeline(L,\n  l => agent(l.p, { model: 'haiku' }),\n  (r, l) => parallel(r.f.map(f => () => agent(f.p, { label: 'v' }))))"
        self.assertIn("line 3 has no model", judge(src))

    def test_prompt_text_and_comments_that_mention_agent_do_not_count(self):
        self.assertIsNone(judge("// agent('x')\nconst P = 'call agent(now) please'\nagent(P, { model: 'sonnet' })"))
        self.assertIsNone(judge("agent(`read ${x} then agent(y)`, { model: 'sonnet' })"))

    def test_a_model_word_inside_a_prompt_is_not_a_pick(self):
        self.assertIsNone(judge("agent('compare opus with fable', { model: 'haiku' })"))

    def test_the_lean_model_comes_from_harness_toml(self):
        gates = harness_config.load(HUB)["gates"]
        self.assertEqual(gates["lean_model"], GATE.LEAN)
        self.assertIn(GATE.LEAN, gates["allowed_models"])

    def test_opus_with_no_route_is_refused(self):
        self.assertIn("line 1 names opus with no route", judge("agent(p, { model: 'opus' })"))
        self.assertIn("line 1 names opus", judge("const M = 'opus'\nagent(p, { model: M })"))

    def test_opus_on_a_line_tagged_with_an_opus_row_passes(self):
        self.assertIsNone(judge("const PLAN = 'opus'  // route:plan\nagent(p, { model: PLAN })"))
        self.assertIsNone(judge("agent(p, { model: 'opus' })  // route:review-other"))

    def test_opus_tagged_with_a_cheap_or_unknown_row_is_refused(self):
        self.assertIn("names opus", judge("agent(p, { model: 'opus' })  // route:fan-out"))
        self.assertIn("names opus", judge("agent(p, { model: 'opus' })  // route:nothing"))

    def test_a_model_outside_the_allowed_list_is_refused(self):
        for name in ("fable", "claude-fable-5-2", "claude-opus-5-5", "Mythos", "sonnet-5"):
            with self.subTest(name=name):
                self.assertIn("names the model " + name, judge("agent(p, { model: '%s' })" % name))

    def test_each_reason_names_the_gate_and_fits_the_cap(self):
        for src in ("agent(p)", "agent(p, { model: 'opus' })", "agent(p, { model: 'claude-fable-5-2-with-a-very-long-suffix-0000000000000' })"):
            reason = judge(src)
            self.assertTrue(reason.startswith("fork-gate: "), reason)
            self.assertLessEqual(len(reason), 200, reason)
            self.assertIn("Fix: ", reason)


class Docs(unittest.TestCase):
    def test_no_skill_orders_workflow_args_that_name_opus(self):
        root = os.path.join(HUB, ".claude", "skills")
        bad = []
        for folder, _, names in os.walk(root):
            for name in names:
                if name.endswith(".md"):
                    path = os.path.join(folder, name)
                    with open(path, encoding="utf-8") as handle:
                        for number, line in enumerate(handle, 1):
                            if re.search(r"model:\s*['\"]opus['\"]", line):
                                bad.append("%s:%d" % (os.path.relpath(path, HUB), number))
        self.assertEqual([], bad)


class Events(unittest.TestCase):
    def reason(self, tool, tool_input):
        return GATE.reason({"tool_name": tool, "tool_input": tool_input}, HUB)

    def test_an_agent_call_needs_a_cheap_model(self):
        self.assertIn("model missing", self.reason("Agent", {"prompt": "x"}))
        self.assertIn("model opus", self.reason("Agent", {"prompt": "x", "model": "opus"}))
        self.assertIsNone(self.reason("Agent", {"prompt": "x", "model": "haiku"}))
        self.assertIsNone(self.reason("Agent", {"prompt": "x", "model": "sonnet"}))
        self.assertLessEqual(len(self.reason("Agent", {"model": "x" * 300})), 200)

    def test_an_inline_script_is_judged(self):
        self.assertIn("has no model", self.reason("Workflow", {"script": "agent('x')"}))
        self.assertIsNone(self.reason("Workflow", {"script": "agent('x', { model: 'haiku' })"}))

    def test_script_path_wins_over_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "w.js")
            with open(path, "w", encoding="utf-8") as f:
                f.write("agent('x')")
            self.assertIn("has no model", self.reason("Workflow", {"scriptPath": path, "script": "agent('x', { model: 'haiku' })"}))

    def test_a_saved_hub_template_called_by_name_passes(self):
        for name in sorted(os.listdir(os.path.join(HUB, ".claude", "workflows"))):
            with self.subTest(name=name):
                self.assertIsNone(self.reason("Workflow", {"name": name[:-3]}))

    def test_an_unreadable_script_passes(self):
        self.assertIsNone(self.reason("Workflow", {"scriptPath": os.path.join(HERE, "no-such-file.js")}))
        self.assertIsNone(self.reason("Workflow", {"name": "../x"}))

    def test_args_that_name_a_model_outside_the_list_are_refused(self):
        self.assertIn("names the model fable", self.reason("Workflow", {"name": "fan-out", "args": {"legs": [{"model": "fable"}]}}))
        self.assertIsNone(self.reason("Workflow", {"name": "fan-out", "args": {"model": "sonnet", "brief": "about opus"}}))

    def test_args_that_name_opus_are_refused_at_any_depth(self):
        # Operator ruling: opus only on a route row, never a pick in Workflow args.
        for args in ({"model": "opus"}, {"legs": [{"label": "a", "model": "Opus"}]}, ["x", {"deep": {"verifyModel": " opus "}}],
                     {"model": "claude-opus-5-5"}):
            with self.subTest(args=args):
                reason = self.reason("Workflow", {"name": "fan-out", "args": args})
                self.assertIn("args name opus", reason)
                self.assertTrue(reason.startswith("fork-gate: "), reason)
                self.assertIn("Fix: ", reason)
                self.assertLessEqual(len(reason), 200, reason)
        for args in ({"model": "haiku", "verifyModel": "sonnet"}, {"brief": "compare opus with sonnet"}, {"notes": "the opus row"}):
            with self.subTest(args=args):
                self.assertIsNone(self.reason("Workflow", {"name": "fan-out", "args": args}))

    def test_the_opus_rows_come_from_routing_legs(self):
        allowed, roles = GATE.routing(HUB)
        self.assertEqual(ALLOWED, allowed)
        self.assertEqual(OPUS_ROLES, roles)


class Dispatch(unittest.TestCase):
    """The live wiring: Drawer/project-gate.py routes Agent and Workflow to the gate."""

    def decide(self, event):
        env = dict(os.environ, FORK_GATE="dry", CLAUDE_PROJECT_DIR=HUB, PYTHONDONTWRITEBYTECODE="1")
        done = subprocess.run([sys.executable, "-B", os.path.join(HERE, "project-gate.py")], input=json.dumps(event),
                              capture_output=True, text=True, env=env, cwd=HUB, timeout=60)
        self.assertEqual(0, done.returncode, done.stderr)
        return json.loads(done.stdout)["hookSpecificOutput"] if done.stdout.strip() else None

    def test_an_unmodelled_workflow_is_denied_through_the_dispatcher(self):
        out = self.decide({"tool_name": "Workflow", "tool_input": {"script": "agent('review it', { label: 'find' })"}})
        self.assertEqual("deny", out["permissionDecision"])
        self.assertIn("has no model", out["permissionDecisionReason"])

    def test_a_routed_workflow_passes_through_the_dispatcher(self):
        self.assertIsNone(self.decide({"tool_name": "Workflow", "tool_input": {"script": "agent('x', { model: 'haiku' })"}}))

    def test_opus_in_workflow_args_is_denied_through_the_dispatcher(self):
        out = self.decide({"tool_name": "Workflow", "tool_input": {"name": "fan-out", "args": {"model": "opus"}}})
        self.assertEqual("deny", out["permissionDecision"])
        self.assertIn("args name opus", out["permissionDecisionReason"])

    def test_an_agent_call_is_denied_through_the_dispatcher(self):
        self.assertEqual("deny", self.decide({"tool_name": "Agent", "tool_input": {"prompt": "x"}})["permissionDecision"])

    def test_off_switch(self):
        env = dict(os.environ, FORK_GATE="off", CLAUDE_PROJECT_DIR=HUB)
        done = subprocess.run([sys.executable, "-B", os.path.join(HERE, "fork-gate.py")], capture_output=True, text=True,
                              input=json.dumps({"tool_name": "Agent", "tool_input": {}}), env=env, timeout=60)
        self.assertEqual("", done.stdout)


if __name__ == "__main__":
    unittest.main()
