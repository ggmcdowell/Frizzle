"""Token budgets of the always-loaded text. Each cap fails on any overrun.

The deny-reason cap (200 chars) lives in Drawer/test_deny_reasons.py, which drives the gates.
"""
import importlib.util
from pathlib import Path
import re
import sys
import unittest

HERE = Path(__file__).resolve().parent
HUB = HERE.parent
sys.path.insert(0, str(HERE))

AGENTS_BYTES = 3000
AGENTS_LINES = 40
SKILL_DESCRIPTION_CHARS = 180
FLIGHT_SKILL_BYTES = 6000
AGENT_PROFILE_BYTES = 1500
SESSION_START_BYTES = 1500
BEARINGS_BYTES = 4000
LOG_TAIL = 5
STATE_CAP = 40
CARD_RUN_ENV_BYTES = 300


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def description(skill_md):
    """The frontmatter description, unquoted."""
    m = re.search(r"^description:\s*(.*)$", skill_md.read_text(encoding="utf-8"), re.M)
    text = m.group(1).strip() if m else ""
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1].replace("''", "'")
    return text


class BudgetTest(unittest.TestCase):
    def test_agents_md_fits_its_cap(self):
        text = (HUB / "AGENTS.md").read_bytes()
        self.assertLessEqual(len(text), AGENTS_BYTES)
        self.assertLessEqual(len(text.splitlines()), AGENTS_LINES)

    def test_agents_md_line_cap_is_the_rules_audit_cap(self):
        import rules_audit
        self.assertEqual(AGENTS_LINES, rules_audit.CAPS["AGENTS.md"])

    def test_each_skill_description_fits_its_cap(self):
        skills = sorted((HUB / ".claude" / "skills").glob("*/SKILL.md"))
        self.assertTrue(skills)
        over = [(p.parent.name, len(description(p))) for p in skills
                if not description(p) or len(description(p)) > SKILL_DESCRIPTION_CHARS]
        self.assertEqual([], over)

    def test_the_flight_skill_fits_its_cap_with_code_mode_merged(self):
        flight = HUB / ".claude" / "skills" / "flight"
        self.assertLessEqual(len((flight / "SKILL.md").read_bytes()), FLIGHT_SKILL_BYTES)
        self.assertFalse((flight / "code-mode.md").exists())

    def test_each_agent_profile_fits_its_cap(self):
        profiles = sorted((HUB / ".claude" / "agents").glob("*.md"))
        self.assertTrue(profiles)
        over = [(p.name, len(p.read_bytes())) for p in profiles if len(p.read_bytes()) > AGENT_PROFILE_BYTES]
        self.assertEqual([], over)

    def test_session_start_output_budget(self):
        module = load("session_start_budget", HERE / "hooks" / "session-start.py")
        self.assertLessEqual(module.OUTPUT_BUDGET, SESSION_START_BYTES)

    def test_flight_bearings_budget(self):
        module = load("flight_hooks_budget", HERE / "flight-hooks.py")
        self.assertLessEqual(module.OUTPUT_BUDGET, BEARINGS_BYTES)
        self.assertLessEqual(module.LOG_TAIL, LOG_TAIL)
        self.assertLessEqual(module.STATE_CAP, STATE_CAP)

    def test_card_run_env_block_fits_its_cap(self):
        text = (HUB / ".claude" / "workflows" / "card-run.js").read_text(encoding="utf-8")
        m = re.search(r"^const ENV = (`[^`]*`|'[^']*'|\"[^\"]*\")", text, re.M)
        self.assertIsNotNone(m, "card-run.js defines no one-line const ENV")
        self.assertLessEqual(len(m.group(1).encode()) - 2, CARD_RUN_ENV_BYTES)


if __name__ == "__main__":
    unittest.main()
