"""README.md and docs/REFERENCE.md: the setup path a fresh clone follows matches the setup and flight skills.

Each check comes from a fresh-clone run: a README step that was missing or out of order stopped it.
"""
import importlib.util
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
REFERENCE = (ROOT / "docs/REFERENCE.md").read_text(encoding="utf-8")
SETUP_SKILL = (ROOT / ".claude/skills/setup/SKILL.md").read_text(encoding="utf-8")


def section(title):
    for text in (README, REFERENCE):
        found = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", text, re.S | re.M)
        if found:
            return found[1]
    raise AssertionError(f"README and docs/REFERENCE.md have no section {title}")


class ReadmeTest(unittest.TestCase):
    def test_the_quick_start_is_the_first_section_and_has_three_commands(self):
        first = re.search(r"^## (.+)$", README, re.M)
        self.assertEqual("Quick start", first[1])
        block = re.search(r"```sh\n(.*?)```", section("Quick start"), re.S)[1]
        commands = [line for line in block.splitlines() if line.strip()]
        self.assertEqual(3, len(commands))
        self.assertTrue(commands[0].startswith("git clone"))
        self.assertIn("Drawer/e2e.sh", commands[1])
        self.assertIn("/flight", commands[2])

    def test_the_class_course_stays_out_of_git(self):
        # Operator ruling: the class content is private; docs/CLASS.md and Class/ are git-ignored.
        lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("/Class/", lines)
        self.assertIn("/docs/CLASS.md", lines)
        self.assertNotIn("CLASS.md", README)

    def test_start_flight_says_to_type_flight(self):
        text = section("Start Flight")
        self.assertIn("claude", text)
        self.assertIn("/flight", text)

    def test_the_scripted_run_uses_accept_edits_and_nothing_bypasses_permissions(self):
        self.assertIn("--permission-mode acceptEdits", section("Scripted run"))
        self.assertIn("Drawer/e2e.sh", section("Scripted run"))
        self.assertNotIn("--dangerously-skip-permissions", README)

    def test_setup_names_the_smoke_step_the_setup_hand_off_asks_for(self):
        self.assertIn("/smoke", section("Setup"))
        self.assertIn("type /smoke", (ROOT / ".claude/skills/setup/SKILL.md").read_text(encoding="utf-8"))

    def test_the_roots_override_comes_before_setup_opens_memory(self):
        text = section("Setup")
        roots, setup = text.find("state_root"), text.find("type `/setup`")
        self.assertNotEqual(-1, roots, "Setup names no state_root override")
        self.assertNotEqual(-1, setup)
        self.assertLess(roots, setup, "the roots override must come before /setup runs mem doctor and seed")
        self.assertIn("harness.local.toml", text[:setup])

    def test_setup_does_not_repeat_the_setup_skill_commands(self):
        text = section("Setup")
        for command in ("seat.py", "settings-sync.py", "mem.py doctor", "mem.py seed", "core.hooksPath"):
            self.assertNotIn(command, text)

    def test_setup_tells_the_user_to_own_a_writable_origin_and_requirements_name_the_cli_floor(self):
        setup = section("Setup")
        self.assertIn("origin not writable", setup)
        self.assertIn("new-hub.py --origin", setup)
        spec = importlib.util.spec_from_file_location("seat_doctor", ROOT / "Drawer" / "seat-doctor.py")
        doctor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(doctor)
        self.assertIn(".".join(map(str, doctor.MIN_CLI)), section("Requirements"))

    def test_setup_names_the_global_claude_config_write(self):
        self.assertIn("~/.claude.json", section("Setup"))

    def test_quick_start_gives_one_first_run_order(self):
        order = ["Drawer/e2e.sh", "/setup", "/smoke", "/flight"]
        text = section("Quick start")
        positions = [text.find(step) for step in order]
        self.assertNotIn(-1, positions, "Quick start misses a first-run step")
        self.assertEqual(sorted(positions), positions, "Quick start gives the steps in another order")
        self.assertIn("/smoke", section("Setup"))

    def test_quick_start_gives_the_e2e_cost_and_the_origin_you_push_to(self):
        text = section("Quick start") + section("Setup")
        self.assertIn("about 2 USD", text)
        self.assertIn("8 minutes", text)
        self.assertIn("git remote set-url origin", section("Setup"))
        self.assertIn("--origin", section("Setup"))

    def test_the_godot_path_runs_through_setup_and_setup_makes_the_game(self):
        text = section("Start a Godot game")
        order = [text.find(step) for step in ("/setup", "Godot game", "/smoke", "/flight")]
        self.assertNotIn(-1, order)
        self.assertEqual(sorted(order), order)
        product = re.search(r"^## (\d)\. Product\n(.*?)(?=^## )", SETUP_SKILL, re.S | re.M)
        self.assertIsNotNone(product, "the setup skill has no Product step")
        self.assertIn("product-init.py --path", product[2])
        self.assertIn("--pack godot", product[2])
        self.assertIn("brew install --cask godot", product[2])
        hand_off = re.search(r"^## (\d)\. Hand off", SETUP_SKILL, re.M)
        self.assertLess(int(product[1]), int(hand_off[1]))

    def test_the_readme_names_what_is_not_included(self):
        self.assertIn("Not included: art or asset tools", README)

    def test_no_pointer_to_the_local_godot_tag(self):
        self.assertNotIn("godot-v0", README)
        self.assertNotIn("godot-v0", (ROOT / "examples/godot-pack/README.md").read_text(encoding="utf-8"))

    def test_memory_says_it_is_per_machine_and_how_to_back_up_and_restore(self):
        text = section("Memory")
        for words in ("Each machine has its own memory", "mem nightly --if-stale", "export_remote", "mem.py import",
                      "mem doctor"):
            self.assertIn(words, text)
        self.assertNotIn("from cron", text)

    def test_the_env_table_names_each_variable_that_changes_behavior(self):
        text = section("Environment variables")
        for name in ("<PREFIX>_MEM_DB", "<PREFIX>_MEM_EMBED", "MEM_LIVE_WRITE", "DOC_GATE", "FORK_GATE", "HARNESS_CONFORM",
                     "HARNESS_NO_LOCAL_TOML", "OPENAI_API_KEY", "E2E_TIMEOUT", "CLAUDE_CONFIG_DIR"):
            self.assertIn(f"`{name}`", text)


if __name__ == "__main__":
    unittest.main()
