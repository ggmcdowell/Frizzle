"""Pure logic of Drawer/mcp-live-check.py; the live probe is proven by running the gate."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Drawer"))
import harness_config  # noqa: E402

SPEC = importlib.util.spec_from_file_location("mcp_live_check_test", ROOT / "Drawer" / "mcp-live-check.py")
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)
MEM = harness_config.server_name(harness_config.load(ROOT))


class ExpectedTests(unittest.TestCase):
    def test_the_hub_root_expects_the_one_memory_server_from_config(self):
        self.assertEqual({MEM: []}, GATE.expected())
        with mock.patch.object(GATE, "server_name", return_value="other-kg"):
            self.assertEqual({"other-kg": []}, GATE.expected())

    def test_run_probes_each_client_once_at_the_hub_root(self):
        seen = []

        def probe(want):
            seen.append(want)
            return {MEM: ("connected", ["search"])}

        with mock.patch.dict(GATE.PROBES, {"claude": probe}):
            report = GATE.run()
        self.assertEqual([{MEM: []}], seen)
        self.assertEqual("PASS", report["state"])
        self.assertEqual([("claude", ".", "PASS", [])],
                         [(p["client"], p["target"], p["state"], p["findings"]) for p in report["probes"]])

    def test_run_reports_refused_as_fail_and_unavailable_as_unavailable(self):
        for error, state in ((GATE.Refused("client exited with 1"), "FAIL"),
                             (GATE.Unavailable("claude CLI not found"), "UNAVAILABLE")):
            with self.subTest(state=state):
                def probe(want, error=error):
                    raise error
                with mock.patch.dict(GATE.PROBES, {"claude": probe}):
                    report = GATE.run()
                self.assertEqual(state, report["state"])
                self.assertEqual([f"hub claude: {error}"], report["probes"][0]["findings"])


class DiffTests(unittest.TestCase):
    def test_clean_probe_ignores_foreign_servers(self):
        want = {MEM: []}
        observed = {MEM: ("connected", ["search"]), "cloud mail": ("needs-auth", []),
                    "other": ("connected", [])}
        self.assertEqual([], GATE.diff("hub claude", want, observed))

    def test_zero_tools_failed_missing_and_lacking_are_findings(self):
        self.assertEqual([f"hub claude: {MEM} connected, 0 tools"],
                         GATE.diff("hub claude", {MEM: []}, {MEM: ("connected", [])}))
        self.assertEqual([f"hub claude: {MEM} failed"],
                         GATE.diff("hub claude", {MEM: []}, {MEM: ("failed", [])}))
        self.assertEqual([f"hub claude: {MEM} missing"],
                         GATE.diff("hub claude", {MEM: []}, {"other": ("connected", ["x"])}))
        self.assertEqual([f"hub claude: {MEM} lacks search"],
                         GATE.diff("hub claude", {MEM: ["search"]}, {MEM: ("connected", ["add"])}))


class TriggerTests(unittest.TestCase):
    def test_root_and_nested_launch_paths_trigger(self):
        for path in (".claude/settings.json", "UI/.claude/settings.json", "Drawer/mem/mem.py",
                     "Drawer/settings-sync.py", "Drawer/settings-manifest.json",
                     "Drawer\\mcp-live-check.py"):
            self.assertTrue(GATE.is_trigger(path), path)

    def test_other_paths_do_not_trigger(self):
        for path in ("Drawer/hooks/session-start.py", "a/b/.claude/settings.json", "CLAUDE.md",
                     "Drawer/mem/sub/x.py", "UI/.claude/settings.local.json", ".mcp.json"):
            self.assertFalse(GATE.is_trigger(path), path)

    def test_unstaged_trigger_refuses_before_any_probe(self):
        probed = []
        with mock.patch.object(GATE, "unstaged_triggers", return_value=["Drawer/mem/mem.py"]), \
                mock.patch.object(GATE, "run", side_effect=lambda *a: probed.append(a) or {"state": "PASS", "probes": [], "seconds": 0}):
            dirty = GATE.gate(["Drawer/mem/mem.py"])
            clean = GATE.gate(["CLAUDE.md"])
        self.assertEqual(("FAIL", "MCP launch paths differ from the index; stage or stash them",
                          ["Drawer/mem/mem.py"]), dirty)
        self.assertEqual(("PASS", "no MCP launch path staged; live probe skipped", []), clean)
        self.assertEqual([], probed)
        # Positive control: a staged trigger whose tree matches the index probes once.
        report = {"state": "PASS", "probes": [{"findings": []}], "seconds": 1.5}
        with mock.patch.object(GATE, "unstaged_triggers", return_value=[]), \
                mock.patch.object(GATE, "run", side_effect=lambda *a: probed.append(a) or report):
            staged = GATE.gate(["Drawer/mem/mem.py"])
        self.assertEqual(("PASS", "1 live client probes in 1.5 s", []), staged)
        self.assertEqual([()], probed)


BASE = {"skillOverrides": {"a": "on"}, "skipAutoPermissionPrompt": True, "env": {"K": "1"},
        "enabledPlugins": {"p@m": True}, "extraKnownMarketplaces": {"m": {"source": "x"}},
        "enabledMcpjsonServers": [MEM], "disabledMcpjsonServers": ["other-mcp"], "hooks": {}}
SETTINGS = (".claude/settings.json", "UI/.claude/settings.json")
MANIFEST = "Drawer/settings-manifest.json"
MANIFEST_BASE = {"common": {"env": {"K": "1"}, "skillOverrides": {}},
                 "hooks": {"PreToolUse": [{"matcher": "Bash", "command": "x"}]},
                 "all_servers": [MEM],
                 "capability_classes": {"graph": {"servers": [MEM], "skills": ["memory"], "access": "Graph"}},
                 "targets": {".": ["graph"], "UI": ["graph"]}}
REPORT = {"state": "PASS", "probes": [{"findings": []}], "seconds": 1.5}


class LaunchChangeTests(unittest.TestCase):
    """A settings file runs the probe only when an MCP launch key changes from HEAD to the index."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.hub = Path(temporary.name)
        for patcher in (mock.patch.dict(os.environ), mock.patch.object(GATE, "HUB", self.hub)):
            patcher.start()
            self.addCleanup(patcher.stop)
        for key in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE"):
            os.environ.pop(key, None)
        self.git("init", "-b", "main")

    def git(self, *args, stdin=None):
        return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=self.hub,
                              capture_output=True, text=True, check=True, input=stdin).stdout

    def write(self, path, data):
        target = self.hub / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(data if isinstance(data, str) else json.dumps(data, indent=2), encoding="utf-8")

    def stage(self, path, data):
        self.write(path, data)
        self.git("add", path)

    def commit_base(self):
        for path in SETTINGS:
            self.stage(path, BASE)
        self.stage(MANIFEST, {"skills": ["a"]})
        self.git("commit", "-m", "base")

    def edited(self, **changes):
        return {**BASE, **changes}

    def gate(self, staged):
        probed = []
        with mock.patch.object(GATE, "unstaged_triggers", return_value=[]), \
                mock.patch.object(GATE, "run", side_effect=lambda *a: probed.append(a) or REPORT):
            return GATE.gate(staged), probed

    def assert_skipped(self, staged):
        (state, summary, findings), probed = self.gate(staged)
        self.assertEqual(("PASS", []), (state, findings))
        self.assertIn("skipped", summary)
        self.assertEqual([], probed)

    def assert_probed(self, staged):
        result, probed = self.gate(staged)
        self.assertEqual(("PASS", "1 live client probes in 1.5 s", []), result)
        self.assertEqual([()], probed)

    def test_skill_edit_to_settings_files_skips_the_probe(self):
        self.commit_base()
        for path in SETTINGS:
            self.stage(path, self.edited(skillOverrides={"a": "off", "b": "on"}))
        self.stage(MANIFEST, {"skills": ["a", "b"]})
        staged = [*SETTINGS, MANIFEST]
        self.assertEqual([False, False, False], [GATE.launch_changed(path) for path in staged])
        self.assert_skipped(staged)

    def test_permission_edit_skipAutoPermissionPrompt_to_settings_files_skips_the_probe(self):
        self.commit_base()
        for path in SETTINGS:
            self.stage(path, self.edited(skipAutoPermissionPrompt=False))
        self.assertFalse(GATE.launch_changed(SETTINGS[1]))
        self.assert_skipped(list(SETTINGS))

    def test_each_server_entry_change_runs_the_probe(self):
        self.commit_base()
        changes = {"env": {"K": "2"}, "enabledPlugins": {"p@m": False}, "extraKnownMarketplaces": {},
                   "enabledMcpjsonServers": [MEM, "other-mcp"], "disabledMcpjsonServers": [],
                   "enableAllProjectMcpServers": True}
        for key, value in changes.items():
            with self.subTest(key=key):
                for path in SETTINGS:
                    self.stage(path, BASE)
                self.stage(SETTINGS[1], self.edited(**{key: value}))
                self.assertTrue(GATE.launch_changed(SETTINGS[1]))
                self.assert_probed(list(SETTINGS))

    def test_mixed_skill_edit_and_one_launch_change_runs_the_probe(self):
        self.commit_base()
        for path in SETTINGS:
            self.stage(path, self.edited(skillOverrides={"a": "off"}))
        self.stage(MANIFEST, {"skills": []})
        self.assert_probed([*SETTINGS, MANIFEST, "Drawer/mem/mem.py"])
        self.stage(SETTINGS[0], self.edited(skillOverrides={"a": "off"}, env={"K": "2"}))
        self.assert_probed([*SETTINGS, MANIFEST])

    def commit_manifest(self):
        self.stage(MANIFEST, MANIFEST_BASE)
        self.stage(SETTINGS[0], BASE)
        self.git("commit", "-m", "manifest")

    def test_each_manifest_launch_key_change_runs_the_probe(self):
        """.mcp.json is rendered locally and never stages, so the manifest edit itself runs the probe."""
        self.commit_manifest()
        row = MANIFEST_BASE["capability_classes"]["graph"]
        edits = {
            "targets": {**MANIFEST_BASE, "targets": {".": ["graph"], "UI": []}},
            "all_servers": {**MANIFEST_BASE, "all_servers": [MEM, "other-mcp"]},
            "servers": {**MANIFEST_BASE, "capability_classes": {"graph": {**row, "servers": []}}},
        }
        for key, data in edits.items():
            with self.subTest(key=key):
                self.stage(MANIFEST, data)
                self.assertIs(True, GATE.launch_changed(MANIFEST))
                self.assert_probed([MANIFEST])

    def test_a_manifest_common_env_edit_runs_the_probe_through_its_settings_render(self):
        self.commit_manifest()
        self.stage(MANIFEST, {**MANIFEST_BASE, "common": {**MANIFEST_BASE["common"], "env": {"K": "2"}}})
        self.assertIs(False, GATE.launch_changed(MANIFEST))
        self.assert_skipped([MANIFEST])  # the common env edit without its settings.json render
        self.stage(SETTINGS[0], self.edited(env={"K": "2"}))
        self.assertIs(True, GATE.launch_changed(SETTINGS[0]))
        self.assert_probed([MANIFEST, SETTINGS[0]])

    def test_a_manifest_skill_or_hooks_or_access_edit_alone_skips_the_probe(self):
        self.commit_manifest()
        row = MANIFEST_BASE["capability_classes"]["graph"]
        changes = {"skills": {"graph": {**row, "skills": ["memory", "kg-fact"]}},
                   "access": {"graph": {**row, "access": "Changed prose"}}}
        edits = {key: {**MANIFEST_BASE, "capability_classes": value} for key, value in changes.items()}
        edits["hooks"] = {**MANIFEST_BASE, "hooks": {"PreToolUse": [{"matcher": "Bash", "command": "y"}]}}
        self.assertTrue(all(data != MANIFEST_BASE for data in edits.values()))
        for key, data in edits.items():
            with self.subTest(key=key):
                self.stage(MANIFEST, data)
                self.assertIs(False, GATE.launch_changed(MANIFEST))
                self.assert_skipped([MANIFEST])

    def test_unjudgeable_added_deleted_malformed_or_unborn_settings_run_the_probe(self):
        path = SETTINGS[0]
        self.stage(path, BASE)  # unborn HEAD
        self.assertTrue(GATE.launch_changed(path))
        self.assert_probed([path])
        self.git("commit", "-m", "one")
        self.stage(SETTINGS[1], BASE)  # added
        self.assertTrue(GATE.launch_changed(SETTINGS[1]))
        self.git("commit", "-m", "two")
        self.git("rm", "--cached", "-q", path)  # deleted
        self.assertTrue(GATE.launch_changed(path))
        self.assert_probed([path])
        for broken in ('{"skillOverrides": ', "[1, 2]", "null"):  # malformed or not an object
            with self.subTest(broken=broken):
                self.stage(path, broken)
                self.assertTrue(GATE.launch_changed(path))
                self.assert_probed([path])

    def test_reads_head_and_the_named_index_not_the_working_tree(self):
        self.commit_base()
        path = SETTINGS[0]
        # real index: a skill edit; working tree: a launch edit; private index: a launch edit
        self.stage(path, self.edited(skillOverrides={"a": "off"}))
        self.write(path, self.edited(env={"K": "2"}))
        self.assertFalse(GATE.launch_changed(path))
        os.environ["GIT_INDEX_FILE"] = str(self.hub / ".git" / "private-index")  # setUp patched os.environ
        self.git("read-tree", "HEAD")
        self.index_blob(path, self.edited(env={"K": "3"}))
        self.assertTrue(GATE.launch_changed(path))
        # reverse: the private index holds a skill edit while the working tree holds a launch edit
        self.index_blob(path, self.edited(skillOverrides={}))
        self.assertFalse(GATE.launch_changed(path))

    def index_blob(self, path, data):
        blob = self.git("hash-object", "-w", "--stdin", stdin=json.dumps(data)).strip()
        self.git("update-index", "--cacheinfo", f"100644,{blob},{path}")


if __name__ == "__main__":
    unittest.main()
