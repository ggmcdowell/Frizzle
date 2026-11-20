"""The project slug lives in harness.toml.

Run: .venv/bin/python -B -m unittest Drawer/test_project_slug.py.
A product hub stamped from the template must not share the template's memory DB, so every path derives from
harness.toml [project] name, state_root and config_root. Only harness.toml, pyproject.toml and uv.lock may
name the slug.
"""
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HUB = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "mem"))
import harness_config  # noqa: E402

ALLOWED = {"harness.toml", "pyproject.toml", "uv.lock"}
CFG = harness_config.load(HUB)
SLUG = CFG["project"]["name"]


class SlugTest(unittest.TestCase):
    def test_only_the_config_files_name_the_slug(self):
        out = subprocess.run(["git", "-C", str(HUB), "grep", "-l", "-I", "-F", SLUG, "--", "."],
                             capture_output=True, text=True, env=harness_config.git_env())
        self.assertIn(out.returncode, (0, 1), out.stderr)
        hits = sorted(set(out.stdout.split()) - ALLOWED)
        self.assertEqual([], hits, f"these tracked files name the slug {SLUG!r}; derive it from harness.toml")

    def test_mem_live_db_is_the_config_db_and_falls_back_under_state_root(self):
        import mem
        self.assertEqual(harness_config.path(CFG["mem"]["db"]), mem.live_db(CFG["mem"]))
        self.assertEqual(harness_config.path(CFG["project"]["state_root"]) / "hub.db",
                         mem.live_db({k: v for k, v in CFG["mem"].items() if k != "db"}))


if __name__ == "__main__":
    unittest.main()
