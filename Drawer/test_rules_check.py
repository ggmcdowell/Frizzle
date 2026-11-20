"""Tests for Drawer/rules_check.py: the AGENTS.md sentinel and the CLAUDE.md shadow guard."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Drawer"))
import rules_check  # noqa: E402


class RulesCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hub-rules-check-")
        self.box = Path(os.path.realpath(self.tmp.name))
        self.home = self.box / "home"
        self.hub = self.home / "hub"
        self.product = self.home / "product_wt" / "abcd1234"
        for d in (self.hub, self.product / "app", self.home / ".claude"):
            d.mkdir(parents=True)
        (self.hub / "AGENTS.md").write_text("# Hub rules\n- Rules sentinel: test-code. Stop when unread.\n")
        (self.home / ".claude" / "CLAUDE.md").write_text("user scope\n")
        env = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.tmp.cleanup)

    def faults(self):
        return rules_check.faults(self.hub, [self.product])

    def test_the_live_agents_md_holds_the_sentinel_line_once(self):
        lines = (ROOT / "AGENTS.md").read_text(encoding="utf-8").splitlines()
        self.assertEqual(1, sum(rules_check.SENTINEL in line for line in lines))

    def test_a_clean_hub_has_no_fault_and_the_notice_asks_for_the_code_without_printing_it(self):
        self.assertEqual([], self.faults())
        with mock.patch.object(rules_check, "cli_version", return_value="2.1.314 (Claude Code)\n"):
            text = rules_check.notice(self.hub, [self.product])
        self.assertNotIn("\n", text)
        self.assertIn("send NEED rules missing", text)
        self.assertNotIn("test-code", text)

    def test_an_old_claude_fails_loudly_as_upgrade_required(self):
        with mock.patch.object(rules_check, "cli_version", return_value="2.1.280 (Claude Code)\n"):
            text = rules_check.notice(self.hub, [self.product])
        self.assertIn("UPGRADE REQUIRED: claude 2.1.280 (Claude Code) is older than 2.1.281", text)

    def test_a_missing_sentinel_fails_loudly(self):
        (self.hub / "AGENTS.md").write_text("# Hub rules\n")
        text = rules_check.notice(self.hub, [self.product])
        self.assertIn("RULES SENTINEL MISSING", text)
        self.assertIn("send NEED rules fault", text)

    def test_every_claude_file_in_the_hub_or_a_parent_shadows_and_the_user_file_does_not(self):
        for place in (self.hub / "CLAUDE.md", self.hub / "CLAUDE.local.md", self.hub / ".claude" / "CLAUDE.md",
                      self.home / "CLAUDE.md", self.home / "CLAUDE.local.md"):
            with self.subTest(place=place):
                place.parent.mkdir(parents=True, exist_ok=True)
                place.write_text("x\n")
                self.assertEqual([f"RULES SHADOWED: {place} turns AGENTS.md off for every hub session. Delete it."],
                                 self.faults())
                self.assertTrue(rules_check.shadowing(str(place), self.hub))
                place.unlink()
        self.assertFalse(rules_check.shadowing(str(self.home / ".claude" / "CLAUDE.md"), self.hub))
        self.assertFalse(rules_check.shadowing(str(self.hub / "Drawer" / "CLAUDE.md"), self.hub))

    def test_a_lower_case_shadow_file_counts_only_where_the_file_system_ignores_case(self):
        self.assertFalse(rules_check.is_shadow_name("/hub/" + "claude.md"))
        with mock.patch.object(rules_check.oscompat, "WINDOWS", True):
            self.assertTrue(rules_check.is_shadow_name("C:/hub/" + "claude.md"))
            self.assertTrue(rules_check.is_shadow_name("C:/hub/" + "Claude.Local.md"))
            self.assertFalse(rules_check.is_shadow_name("C:/hub/AGENTS.md"))

    def test_cli_version_runs_the_claude_that_which_finds(self):
        with mock.patch.object(rules_check.shutil, "which", return_value="/x/claude.cmd"), \
                mock.patch.object(rules_check.subprocess, "run", return_value=mock.Mock(stdout="2.1.314")) as run:
            self.assertEqual("2.1.314", rules_check.cli_version())
        self.assertEqual("/x/claude.cmd", run.call_args.args[0][0])

    def test_a_claude_file_in_a_product_worktree_is_reported_as_added_rules(self):
        (self.product / "app" / "CLAUDE.local.md").write_text("x\n")
        self.assertEqual([f"RULES ADDED: {self.product / 'app' / 'CLAUDE.local.md'} adds rules when a session "
                          f"reads the product repo. Delete it."], self.faults())

    def test_the_index_check_refuses_a_committed_claude_file_at_any_depth(self):
        repo = self.box / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "a.txt").write_text("a\n")
        subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True)
        self.assertEqual(0, rules_check.main(["--index", str(repo)]))
        (repo / "sub").mkdir()
        (repo / "sub" / "CLAUDE.md").write_text("x\n")
        subprocess.run(["git", "-C", str(repo), "add", "sub/CLAUDE.md"], check=True)
        self.assertEqual(1, rules_check.main(["--index", str(repo)]))

    def test_the_index_check_passes_an_agents_md_over_the_cap_with_trim_due_and_refuses_one_over_the_ceiling(self):
        import rules_audit
        self.enterContext(mock.patch.object(rules_audit, "refusals", return_value=[]))  # the audit pass: its own test
        repo = self.box / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        agents = repo / "AGENTS.md"
        agents.write_text("".join(f"- rule {n}\n" for n in range(rules_check.RULES_CAP)))
        subprocess.run(["git", "-C", str(repo), "add", "AGENTS.md"], check=True)
        self.assertEqual(0, rules_check.main(["--index", str(repo)]))
        agents.write_text(agents.read_text() + "- one rule too many\n")
        self.assertEqual(0, rules_check.main(["--index", str(repo)]))  # the index, not the work tree, is judged
        subprocess.run(["git", "-C", str(repo), "add", "AGENTS.md"], check=True)
        with mock.patch("sys.stderr", new_callable=__import__("io").StringIO) as err:
            self.assertEqual(0, rules_check.main(["--index", str(repo)]))
        self.assertIn("TRIM DUE: AGENTS.md has 41 lines, over the 40-line cap", err.getvalue())
        agents.write_text("".join(f"- rule {n}\n" for n in range(rules_check.RULES_CEILING)))
        subprocess.run(["git", "-C", str(repo), "add", "AGENTS.md"], check=True)
        with mock.patch("sys.stderr", new_callable=__import__("io").StringIO) as err:
            self.assertEqual(0, rules_check.main(["--index", str(repo)]))
        self.assertIn("TRIM DUE: AGENTS.md has 50 lines", err.getvalue())
        agents.write_text(agents.read_text() + "- one rule past the ceiling\n")
        subprocess.run(["git", "-C", str(repo), "add", "AGENTS.md"], check=True)
        with mock.patch("sys.stderr", new_callable=__import__("io").StringIO) as err:
            self.assertEqual(1, rules_check.main(["--index", str(repo)]))
        self.assertIn("51 lines, over the 50-line ceiling", err.getvalue())
        self.assertNotIn("TRIM DUE", err.getvalue())
        self.assertIn(rules_check.TRIM, err.getvalue())
        self.assertTrue((ROOT / rules_check.TRIM).is_file())

    def test_the_index_check_refuses_a_staged_rule_file_with_no_audit_pass(self):
        import rules_audit
        repo = self.box / "repo"
        (repo / ".claude" / "skills" / "a").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / ".claude" / "skills" / "a" / "SKILL.md").write_text("- a rule\n")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        state = self.box / "rules-audit"
        with mock.patch.object(rules_audit, "state_dir", return_value=state), \
                mock.patch("sys.stderr", new_callable=__import__("io").StringIO) as err:
            self.assertEqual(1, rules_check.main(["--index", str(repo)]))
            self.assertIn(".claude/skills/a/SKILL.md changed and its staged text has no rules audit pass", err.getvalue())
            blob = subprocess.run(["git", "-C", str(repo), "rev-parse", ":.claude/skills/a/SKILL.md"],
                                  capture_output=True, text=True, check=True).stdout.strip()
            state.mkdir()
            (state / "passed.jsonl").write_text('{"file": ".claude/skills/a/SKILL.md", "blob": "%s"}\n' % blob)
            self.assertEqual(0, rules_check.main(["--index", str(repo)]))

    def test_the_live_agents_md_is_within_the_ceiling(self):
        lines = (ROOT / "AGENTS.md").read_text(encoding="utf-8").splitlines()
        self.assertLessEqual(len(lines), rules_check.RULES_CEILING)

    def test_a_hung_git_reads_as_a_fault_line_within_the_timeout(self):
        import time
        import harness_config
        bin_dir = self.box / "bin"
        bin_dir.mkdir()
        stub = bin_dir / "git"
        stub.write_text("#!/bin/sh\nexec sleep 5\n")
        stub.chmod(0o755)
        self.enterContext(mock.patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}))
        self.enterContext(mock.patch.object(harness_config, "load",
                                            return_value={"product": {"path": str(self.product)}}))
        self.enterContext(mock.patch.object(rules_check, "GIT_TIMEOUT", 1))
        start = time.monotonic()
        found = rules_check.faults(self.hub)
        self.assertLess(time.monotonic() - start, 3)
        self.assertEqual([f"RULES CHECK FAULT: git worktree list in {self.product} timed out after 1 s; "
                          f"the product repo instruction files went unchecked."], found)

    def test_the_cli_floor_is_the_first_version_that_loads_agents_md_on_every_provider(self):
        self.assertTrue(rules_check.cli_too_old("2.1.280 (Claude Code)"))
        self.assertFalse(rules_check.cli_too_old("2.1.281 (Claude Code)"))
        self.assertFalse(rules_check.cli_too_old("2.1.314 (Claude Code)"))
        self.assertFalse(rules_check.cli_too_old("garbage"))


if __name__ == "__main__":
    unittest.main()
