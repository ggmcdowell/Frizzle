"""The rules audit: caps, the staged-edit gate, the cut log and the rule-ID check (harness skill OPR-17)."""
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import rules_audit  # noqa: E402


class RulesAuditTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="hub-rules-audit-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / "state" / "rules-audit"

    def test_check_counts_agents_md_against_40_and_each_skill_against_500(self):
        hub = self.root / "hub"
        (hub / ".claude" / "skills" / "a").mkdir(parents=True)
        (hub / "AGENTS.md").write_text("x\n" * 41)
        (hub / ".claude" / "skills" / "a" / "SKILL.md").write_text("y\n" * 500)
        self.assertEqual([("AGENTS.md", 41, 40), (".claude/skills/a/SKILL.md", 500, 500)], rules_audit.counts(hub))

    # The pre-commit cap (rules_check) and the audit cap once lived apart and could drift. Injection: give
    # rules_check.RULES_CAP a number of its own.
    def test_the_commit_cap_and_the_audit_cap_are_one(self):
        import rules_check
        self.assertEqual(rules_audit.CAPS["AGENTS.md"], rules_check.RULES_CAP)
        with mock.patch.dict(rules_audit.CAPS, {"AGENTS.md": 7}):
            self.assertEqual(7, importlib.reload(rules_check).RULES_CAP)
        importlib.reload(rules_check)

    def test_the_live_files_are_within_their_caps(self):
        import rules_check  # AGENTS.md may stand between the trim cap and the ceiling until the trim lands
        caps = {"AGENTS.md": rules_check.RULES_CEILING}
        over = [(p, n, c) for p, n, c in rules_audit.counts() if n > caps.get(p, c)]
        self.assertEqual([], over)
        self.assertTrue((rules_audit.HUB / rules_audit.WORKFLOW).is_file())
        self.assertTrue((rules_audit.HUB / rules_audit.GUIDE).is_file())

    def git(self, repo, *args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout

    def repo(self):
        repo = self.root / "repo"
        (repo / ".claude" / "skills" / "a").mkdir(parents=True)
        self.git(self.root, "init", "-q", str(repo))
        self.git(repo, "config", "user.name", "T")
        self.git(repo, "config", "user.email", "t@example.invalid")
        (repo / "AGENTS.md").write_text("- rule\n")
        (repo / ".claude" / "skills" / "a" / "SKILL.md").write_text("- skill rule\n")
        (repo / "notes.txt").write_text("n\n")
        return repo

    def test_a_staged_rule_file_needs_a_pass_for_its_staged_text(self):
        repo = self.repo()
        self.git(repo, "add", "notes.txt")
        self.assertEqual([], rules_audit.unaudited(str(repo), self.state))
        self.git(repo, "add", "AGENTS.md", ".claude/skills/a/SKILL.md")
        self.assertEqual([".claude/skills/a/SKILL.md", "AGENTS.md"], rules_audit.unaudited(str(repo), self.state))
        cwd = Path.cwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(repo)  # `pass` runs from the repo root, as the session does
        with mock.patch.object(rules_audit, "state_dir", return_value=self.state), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(0, rules_audit.main(["pass", "--file", "AGENTS.md"]))
        os.chdir(cwd)
        self.assertEqual([".claude/skills/a/SKILL.md"], rules_audit.unaudited(str(repo), self.state))
        (repo / "AGENTS.md").write_text("- rule\n- one more rule\n")
        self.git(repo, "add", "AGENTS.md")  # new text: the old pass no longer counts
        self.assertIn("AGENTS.md", rules_audit.unaudited(str(repo), self.state))
        with mock.patch.object(rules_audit, "state_dir", return_value=self.state), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(1, rules_audit.main(["staged", str(repo)]))
        self.assertIn("rules_audit.py pass --file AGENTS.md", err.getvalue())

    def test_a_commit_of_other_files_never_reads_the_hub_config(self):
        repo = self.repo()
        self.git(repo, "add", "notes.txt")
        with mock.patch.object(rules_audit, "state_dir", side_effect=KeyError("state_root")):
            self.assertEqual(0, rules_audit.main(["staged", str(repo)]))

    def test_pass_records_the_repo_path_from_a_subfolder_and_refuses_other_files(self):
        repo = self.repo()
        cwd = Path.cwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(repo / ".claude" / "skills" / "a")
        with mock.patch.object(rules_audit, "state_dir", return_value=self.state), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(0, rules_audit.main(["pass", "--file", "SKILL.md"]))
            with mock.patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
                rules_audit.main(["pass", "--file", "../../../notes.txt"])
        os.chdir(cwd)
        self.git(repo, "add", ".claude/skills/a/SKILL.md")
        self.assertEqual([], rules_audit.unaudited(str(repo), self.state))

    def test_a_non_ascii_skill_path_still_needs_a_pass(self):
        # git quotes a non-ASCII path in --name-only unless -z: a quoted name never matched.
        repo = self.repo()
        (repo / ".claude" / "skills" / "café").mkdir()
        (repo / ".claude" / "skills" / "café" / "SKILL.md").write_text("- a rule\n")
        self.git(repo, "add", "-A")
        self.assertIn(".claude/skills/café/SKILL.md", rules_audit.unaudited(str(repo), self.state))
        with mock.patch.object(rules_audit, "state_dir", return_value=self.state), \
                mock.patch("sys.stderr", new_callable=io.StringIO):
            import rules_check
            self.assertEqual(1, rules_check.main(["--index", str(repo)]))

    def test_a_git_fault_refuses_the_commit(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("git")):
            self.assertIn("cannot list the staged files", rules_audit.refusals(str(self.root))[0])

    def test_log_appends_each_cut_or_move_and_a_move_names_its_home(self):
        with mock.patch.object(rules_audit, "state_dir", return_value=self.state), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            rules_audit.main(["log", "--file", "AGENTS.md", "--action", "move", "--line", "lease rule",
                              "--home", "Build/MANUAL.md Ship path"])
            rules_audit.main(["log", "--file", "AGENTS.md", "--action", "cut", "--line", "old note"])
            with mock.patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
                rules_audit.main(["log", "--file", "AGENTS.md", "--action", "move", "--line", "no home"])
        rows = [json.loads(r) for r in (self.state / "log.jsonl").read_text().splitlines()]
        self.assertEqual([("move", "Build/MANUAL.md Ship path"), ("cut", "")], [(r["action"], r["home"]) for r in rows])


class RuleIdTest(unittest.TestCase):
    """The root rule-ID check (harness skill OPR-10): an unnamed root line, a duplicate ID, a reused retired ID."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="hub-rule-id-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / "state" / "rules-audit"

    def git(self, repo, *args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout

    def repo(self):
        repo = self.root / "repo"
        (repo / ".claude" / "skills" / "a").mkdir(parents=True)
        self.git(self.root, "init", "-q", str(repo))
        self.git(repo, "config", "user.name", "T")
        self.git(repo, "config", "user.email", "t@example.invalid")
        return repo

    def commit(self, repo, agents, skill, msg):
        (repo / "AGENTS.md").write_text(agents)
        (repo / ".claude" / "skills" / "a" / "SKILL.md").write_text(skill)
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "--no-verify", "-m", msg)
        return self.git(repo, "rev-parse", "HEAD").strip()

    def history(self):
        """Commit 1 holds WRK-1 and WRK-2; commit 2 cuts WRK-2; commit 3 moves WRK-1 to a skill in one commit."""
        repo = self.repo()
        base = self.commit(repo, "# R\n- WRK-1 a — x\n- WRK-2 b — y\n", "# S\n", "1")
        self.commit(repo, "# R\n- WRK-1 a — x\n", "# S\n", "2")
        self.commit(repo, "# R\n", "# S\n- WRK-1 a — x\n", "3")
        self.enterContext(mock.patch.object(rules_audit, "ID_BASE", base))
        return repo

    def test_an_unnamed_root_line_is_refused(self):
        text = ("# Hub rules\n"
                "- WRK-1 binds-all — a rule that runs on\n"
                "onto a continuation line\n"
                "  `<python> Drawer/incident.py`\n"
                "  - SKL-3 git-flow — a sub-bullet\n"
                "\n"
                "## Words\n"
                "- hub — a glossary entry takes no ID\n"
                "## Work\n"
                "- no head here\n"
                "1. numbered rule\n"
                "- OPR-1 not-a-root-prefix — x\n")
        faults = rules_audit.id_faults(text, {}, set())
        self.assertEqual(["AGENTS.md:10 has no", "AGENTS.md:11 has no", "AGENTS.md:12 has no"],
                         [f.split(" `")[0] for f in faults])
        self.assertTrue(all("OPR-10" in f for f in faults))

    def test_a_duplicate_id_is_refused(self):
        faults = rules_audit.id_faults("- WRK-2 a — x\n- WRK-3 b — y\n- WRK-2 c — z\n", {}, set())
        self.assertEqual(1, len(faults))
        self.assertIn("WRK-2 is used more than once: AGENTS.md:1, AGENTS.md:3", faults[0])
        skill = ".claude/skills/git-flow/SKILL.md"
        faults = rules_audit.id_faults("- WRK-11 a — x\n", {skill: "# S\n- WRK-11 b — y\n- OPR-11 c — z\n"}, set())
        self.assertEqual(1, len(faults))
        self.assertIn(f"WRK-11 is used more than once: AGENTS.md:1, {skill}:2", faults[0])

    def test_a_reused_retired_id_is_refused(self):
        repo = self.history()
        self.assertEqual({"WRK-2"}, rules_audit.retired_ids(str(repo)))  # the move of WRK-1 retires nothing
        self.assertEqual(["AGENTS.md:2 reuses WRK-2"],
                         [f.split(",")[0] for f in rules_audit.id_faults("# R\n- WRK-2 other — x\n", {}, {"WRK-2"})])
        self.state.mkdir(parents=True)
        self.enterContext(mock.patch.object(rules_audit, "state_dir", return_value=self.state))
        (repo / "AGENTS.md").write_text("# R\n- WRK-2 other — x\n")
        self.git(repo, "add", "AGENTS.md")
        self.assertTrue(any("reuses WRK-2, retired by an earlier commit" in r for r in rules_audit.refusals(str(repo))))
        (repo / "AGENTS.md").write_text("# R\n- WRK-3 next — x\n")
        self.git(repo, "add", "AGENTS.md")
        self.assertEqual([], [r for r in rules_audit.refusals(str(repo)) if "OPR-10" in r])

    def test_check_and_staged_refuse_each_fault(self):
        repo = self.history()
        (repo / "AGENTS.md").write_text("# R\n- no head\n- WRK-2 back — x\n- WRK-3 c — y\n- WRK-3 d — z\n")
        self.git(repo, "add", "AGENTS.md")
        blob = self.git(repo, "rev-parse", ":AGENTS.md").strip()
        self.state.mkdir(parents=True)
        (self.state / "passed.jsonl").write_text(json.dumps({"file": "AGENTS.md", "blob": blob}) + "\n")
        with mock.patch.object(rules_audit, "state_dir", return_value=self.state), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(1, rules_audit.main(["staged", str(repo)]))
        lines = err.getvalue().splitlines()
        self.assertEqual(3, len(lines), lines)  # the pass row stands: only the ID faults are left
        for kind in ("AGENTS.md:2 has no `<PFX>-<n> <slug> — ` head", "reuses WRK-2", "WRK-3 is used more than once"):
            self.assertTrue(any(kind in line for line in lines), kind)
        self.assertTrue(all(line.startswith("pre-commit: ") for line in lines))
        self.assertEqual(3, len(rules_audit.id_report(repo)))  # `check` reads the work tree the same way
        with mock.patch.object(rules_audit, "HUB", repo), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(1, rules_audit.main(["check"]))
        self.assertIn("ID AGENTS.md:2 has no", out.getvalue())
        self.assertIn("3 rule-ID faults", out.getvalue())

    def test_a_repo_without_the_id_base_refuses_an_agents_md_stage_but_checks_a_skill_stage_for_duplicates(self):
        repo = self.repo()
        self.commit(repo, "# R\n- WRK-1 a — x\n", "# S\n", "1")
        self.enterContext(mock.patch.object(rules_audit, "state_dir", return_value=self.state))
        self.enterContext(mock.patch.object(rules_audit, "ID_BASE", "0" * 40))  # a pinned base this repo lacks
        (repo / ".claude" / "skills" / "a" / "SKILL.md").write_text("# S\n- WRK-1 b — y\n")
        self.git(repo, "add", "-A")
        self.assertTrue(any("WRK-1 is used more than once" in r for r in rules_audit.refusals(str(repo))))
        self.assertFalse(any("history unreadable" in r for r in rules_audit.refusals(str(repo))))
        (repo / "AGENTS.md").write_text("# R\n- WRK-1 a — x\n- WRK-2 b — y\n")
        self.git(repo, "add", "-A")
        self.assertTrue(any("history unreadable" in r for r in rules_audit.refusals(str(repo))))
        self.assertTrue(any("history unreadable" in f for f in rules_audit.id_report(repo)))

    def test_the_live_hub_has_no_rule_id_fault(self):
        self.assertEqual([], rules_audit.id_report(rules_audit.HUB))


if __name__ == "__main__":
    unittest.main()
