"""Tests for Drawer/name-check.py. Every name here is invented; the real deny list is never read."""
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "name-check.py"
spec = importlib.util.spec_from_file_location("name_check", SCRIPT)
name_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(name_check)

RULES = "# test list\n\nzorblax corp\nsub:quuxware\nre:wib+le\n"


def run(denylist_text, file_text):
    with tempfile.TemporaryDirectory() as tmp:
        deny = Path(tmp, "deny.txt")
        target = Path(tmp, "sample.txt")
        if denylist_text is not None:
            deny.write_text(denylist_text)
        target.write_text(file_text)
        proc = subprocess.run([sys.executable, "-B", str(SCRIPT), "--denylist", str(deny), "--files", str(target)],
                              capture_output=True, text=True, cwd=HERE)
        return proc.returncode, proc.stdout + proc.stderr


class WordRule(unittest.TestCase):
    rx = name_check.compile_rule("zorblax corp")

    def test_separators_match(self):
        for text in ("Zorblax Corp", "zorblax_corp", "zorblax-corp", "ZorblaxCorp", "zorblax.corp"):
            self.assertTrue(self.rx.search(text), text)

    def test_bounded(self):
        for text in ("xzorblax corp", "zorblax corps", "zorblax corp2"):
            self.assertFalse(self.rx.search(text), text)

    def test_underscore_is_a_boundary(self):
        self.assertTrue(self.rx.search("Zorblax_Corp_Hub"))


class OtherRules(unittest.TestCase):
    def test_substring_rule_matches_inside_identifier(self):
        self.assertTrue(name_check.compile_rule("sub:quuxware").search("UQuuxwareProductMode"))

    def test_regex_rule(self):
        self.assertTrue(name_check.compile_rule("re:wib+le").search("a wibbbble here"))


class Cli(unittest.TestCase):
    def test_clean_file_passes(self):
        code, out = run(RULES, "nothing to see\n")
        self.assertEqual(code, 0, out)
        self.assertIn("clean (3 rules)", out)

    def test_denied_name_fails(self):
        code, out = run(RULES, "made by ZorblaxCorp\n")
        self.assertEqual(code, 1, out)
        self.assertIn("sample.txt:1", out)

    def test_home_path_fails(self):
        home = "Users"  # split, so this file holds no home path
        for text in (f"see /{home}/someone/x\n", f"see C:\\{home}\\someone\\x\n", "see /" + "home/someone/x\n"):
            code, out = run(RULES, text)
            self.assertEqual(code, 1, text)
            self.assertIn("absolute home path", out)

    def test_empty_list_fails(self):
        code, out = run("# only a comment\n\n", "nothing\n")
        self.assertEqual(code, 2, out)
        self.assertIn("empty", out)

    def test_missing_list_fails(self):
        code, out = run(None, "nothing\n")
        self.assertEqual(code, 2, out)
        self.assertIn("unreadable", out)


class WindowsSpellings(unittest.TestCase):
    def test_a_lowercase_drive_home_path_fails(self):
        home = "users"  # split, so this file holds no home path
        code, out = run(RULES, "see c:\\%s\\someone\\x\n" % home)
        self.assertEqual(code, 1, out)
        self.assertIn("absolute home path", out)

    def test_a_crlf_line_in_a_staged_diff_loses_its_cr(self):
        with tempfile.TemporaryDirectory() as tmp:
            for args in (["init", "-q"], ["config", "core.autocrlf", "false"]):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
            Path(tmp, "f.txt").write_bytes(b"zorblax\r\n")
            subprocess.run(["git", "-C", tmp, "add", "f.txt"], check=True, capture_output=True)
            files = name_check.staged_changes(tmp)
        self.assertEqual([("f.txt", True, [(1, "zorblax")])], files)


if __name__ == "__main__":
    unittest.main()
