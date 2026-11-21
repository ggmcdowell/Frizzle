import contextlib
import importlib.util
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name("settings-sync.py")
SPEC = importlib.util.spec_from_file_location("settings_sync", MODULE_PATH)
SETTINGS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SETTINGS)
GATE_SPEC = importlib.util.spec_from_file_location("project_gate", MODULE_PATH.with_name("project-gate.py"))
GATE = importlib.util.module_from_spec(GATE_SPEC)
GATE_SPEC.loader.exec_module(GATE)
MANIFEST_PATH = MODULE_PATH.with_name("settings-manifest.json")
sys.path.insert(0, str(MODULE_PATH.parent))
import harness_config  # noqa: E402

CFG = harness_config.load(MODULE_PATH.parents[1])
# The same config with an editor MCP server named, for the editor-server contracts.
# The shell guard each Flight hook runs behind: <env_prefix>_FLIGHT set or a Flight/seats folder.
FLIGHT_GUARD = ('{ [ -z "${' + CFG["project"]["env_prefix"] + '_FLIGHT:-}" ] && '
                '[ ! -d "$CLAUDE_PROJECT_DIR/Flight/seats" ]; } || ')
EDITOR_CFG = {**CFG, "product": {**CFG.get("product", {}), "mcp_server": "editor-server", "mcp_url": "http://127.0.0.1:50101/mcp"}}


def manifest(cfg=CFG):
    return SETTINGS.load_manifest(MANIFEST_PATH, cfg)


class SettingsSyncTests(unittest.TestCase):
    def test_the_tracked_project_settings_matches_the_manifest_render(self):
        # Contract: settings-sync.py with no flag exits 1 on drift; the one project file is the manifest's render.
        hub = MANIFEST_PATH.parents[1]
        self.assertEqual(SETTINGS.body_for(manifest()), (hub / ".claude/settings.json").read_text(encoding="utf-8"))
        # Every session seats at the hub root: no subfolder carries project settings.
        self.assertEqual([], sorted(str(p.relative_to(hub)) for p in hub.glob("*/.claude/settings.json")))

    def test_memory_and_the_editor_are_the_servers_and_the_root_enables_both(self):
        # Defect: the root loses the memory server or the editor server, or a third server appears.
        rendered = manifest(EDITOR_CFG)
        mem, editor = harness_config.server_name(CFG), EDITOR_CFG["product"]["mcp_server"]
        self.assertEqual([mem, editor], rendered["all_servers"])
        body = json.loads(SETTINGS.body_for(rendered))
        self.assertEqual(sorted([mem, editor]), body["enabledMcpjsonServers"])
        self.assertNotIn("disabledMcpjsonServers", body)

    def test_no_editor_url_drops_the_editor_server_everywhere(self):
        cfg = {**EDITOR_CFG, "product": {**EDITOR_CFG["product"], "mcp_url": ""}}
        rendered = SETTINGS.load_manifest(MANIFEST_PATH, cfg)
        mem = harness_config.server_name(CFG)
        self.assertEqual([mem], rendered["all_servers"])
        self.assertEqual([], rendered["capability_classes"]["engine_editor"]["servers"])
        self.assertEqual([mem], list(json.loads(SETTINGS.mcp_json_text(rendered, cfg))["mcpServers"]))
        body = json.loads(SETTINGS.body_for(rendered))
        self.assertEqual([mem], body["enabledMcpjsonServers"])
        self.assertNotIn("disabledMcpjsonServers", body)

    def test_mcp_json_points_the_editor_server_at_the_configured_url(self):
        servers = json.loads(SETTINGS.mcp_json_text(manifest(EDITOR_CFG), EDITOR_CFG))["mcpServers"]
        self.assertEqual({"type": "http", "url": EDITOR_CFG["product"]["mcp_url"]}, servers[EDITOR_CFG["product"]["mcp_server"]])

    def test_mcp_json_launches_mem_mcp_from_any_subfolder(self):
        rendered = manifest(EDITOR_CFG)
        mem = harness_config.server_name(CFG)
        servers = json.loads(SETTINGS.mcp_json_text(rendered, EDITOR_CFG))["mcpServers"]
        self.assertEqual([mem, EDITOR_CFG["product"]["mcp_server"]], list(servers))
        entry = servers[mem]
        self.assertEqual(CFG["derived"]["python"], entry["command"])
        self.assertEqual(["-B", "-c"], entry["args"][:2])
        code = entry["args"][2]
        self.assertIn(repr(CFG["project"]["marker_file"]), code)
        self.assertIn(repr(harness_config.env_name("HUB", CFG)), code)
        self.assertIn("sys.argv=[str(s),'mcp']", code)
        # The entry finds the hub from a subfolder and runs Drawer/mem/mem.py mcp.
        with tempfile.TemporaryDirectory() as temporary:
            hub = Path(temporary).resolve()
            (hub / CFG["project"]["marker_file"]).write_text("")
            script = hub / "Drawer" / "mem" / "mem.py"
            script.parent.mkdir(parents=True)
            script.write_text("import sys\nprint(sys.argv[1:], __name__)\n")
            corner = hub / "Flight"
            corner.mkdir()
            import subprocess
            out = subprocess.run([sys.executable, *entry["args"]], cwd=corner, capture_output=True, text=True)
            self.assertEqual(0, out.returncode, out.stderr)
            self.assertEqual("['mcp'] __main__", out.stdout.strip())

    def test_no_server_is_pinned_whole_past_claude_tool_search(self):
        # Defect: a server-level alwaysLoad loads every one of its tools upfront.
        servers = json.loads(SETTINGS.mcp_json_text(manifest(), CFG))["mcpServers"]
        self.assertEqual({}, {name: entry["alwaysLoad"] for name, entry in servers.items() if "alwaysLoad" in entry})

    def test_mcp_json_borrows_the_rendering_interpreter_when_the_checkout_has_no_venv(self):
        cfg = {**CFG, "derived": {**CFG["derived"], "python": "/nonexistent/.venv/bin/python"}}
        servers = json.loads(SETTINGS.mcp_json_text(manifest(), cfg))["mcpServers"]
        self.assertEqual(sys.executable, servers[harness_config.server_name(CFG)]["command"])

    def test_manifest_placeholders_render_from_config(self):
        raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        self.assertEqual(["{MEM_SERVER}", "{EDITOR_SERVER}"], raw["all_servers"])
        self.assertEqual(
            {"a": [harness_config.server_name(CFG), CFG["derived"]["tool_prefix"] + "add", EDITOR_CFG["product"]["mcp_server"]]},
            SETTINGS.render({"a": ["{MEM_SERVER}", "{MEM_TOOLS}add", "{EDITOR_SERVER}"]}, EDITOR_CFG),
        )
        self.assertNotIn("{MEM_", json.dumps(manifest()))
        self.assertNotIn("{EDITOR_", json.dumps(manifest()))

    def test_project_common_owns_hub_settings_and_the_lead_model_default(self):
        rendered = manifest()
        SETTINGS.validate_manifest(rendered)
        body = json.loads(SETTINGS.body_for(rendered))
        self.assertEqual("opus", body["model"])
        self.assertEqual("medium", body["effortLevel"])
        self.assertEqual("1", body["env"]["CLAUDE_CODE_SIMPLE_SYSTEM_PROMPT"])
        self.assertEqual("0", body["env"]["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"])
        self.assertNotIn("teammateMode", body)
        self.assertEqual(
            ["project-gate.py"],
            [re.search(r"Drawer/([\w.-]+\.py)", group["hooks"][0]["command"]).group(1)
             for group in body["hooks"]["PreToolUse"]],
        )

    # The hub answers in the STE-100 output style.
    # Injection: drop the manifest outputStyle key, or rename or delete the style file.
    def test_the_project_settings_select_the_tracked_ste100_output_style(self):
        body = json.loads(SETTINGS.body_for(manifest()))
        self.assertEqual("STE-100", body.get("outputStyle"))
        style = MODULE_PATH.parents[1] / ".claude" / "output-styles" / "ste100.md"
        self.assertTrue(style.is_file(), "the style file is missing")
        head = style.read_text(encoding="utf-8").split("---")[1]
        self.assertIn("name: STE-100", head)
        self.assertIn("keep-coding-instructions: true", head)  # the lead keeps its coding prompt

    # Subagents run at medium whatever the lead runs at (CLI 2.1.316, CLAUDE_CODE_SUBAGENT_EFFORT).
    # Injection: drop the manifest env key.
    def test_subagents_run_at_medium_effort(self):
        rendered = manifest()
        body = json.loads(SETTINGS.body_for(rendered))
        self.assertEqual("medium", body["env"]["CLAUDE_CODE_SUBAGENT_EFFORT"])
        rendered["common"]["env"].pop("CLAUDE_CODE_SUBAGENT_EFFORT")
        with self.assertRaisesRegex(ValueError, "^project common must set CLAUDE_CODE_SUBAGENT_EFFORT=medium$"):
            SETTINGS.validate_manifest(rendered)

    # Every hub Python runs in UTF-8 mode (a Windows pipe defaults to the ANSI code page): the project env, the
    # MCP server's env and the manifest check all carry it. Injection: drop the manifest env key or the .mcp.json env.
    def test_every_hub_python_runs_in_utf8_mode(self):
        rendered = manifest()
        self.assertEqual("1", json.loads(SETTINGS.body_for(rendered))["env"]["PYTHONUTF8"])
        servers = json.loads(SETTINGS.mcp_json_text(rendered, CFG))["mcpServers"]
        self.assertEqual({"PYTHONUTF8": "1"}, servers[harness_config.server_name(CFG)]["env"])
        rendered["common"]["env"].pop("PYTHONUTF8")
        with self.assertRaisesRegex(ValueError, "^project common must set PYTHONUTF8=1$"):
            SETTINGS.validate_manifest(rendered)

    def test_the_local_interpreter_rule_names_a_forward_slash_path(self):
        # A Windows checkout path (C:\\hub) must reach Git Bash's command text as C:/hub.
        from pathlib import PureWindowsPath
        with patch.object(harness_config, "main_checkout", return_value=PureWindowsPath("C:\\hub")):
            allow = json.loads(SETTINGS.local_text(manifest(), CFG))["permissions"]["allow"]
        self.assertIn("Bash(C:/hub/.venv/bin/python *)", allow)

    # A leg that names no model must not land on opus (operator ruling): the subagent model is sonnet.
    # Injection: set CLAUDE_CODE_SUBAGENT_MODEL back to opus in the manifest.
    def test_a_leg_with_no_model_runs_on_sonnet_and_the_session_pin_stays_opus(self):
        body = json.loads(SETTINGS.body_for(manifest()))
        self.assertEqual(("sonnet", "opus"), (body["env"]["CLAUDE_CODE_SUBAGENT_MODEL"], body["model"]))

    # The model pin and the subagent model are words of [gates] allowed_models, like each [routing.legs] model.
    # Injection: drop the cfg check in validate_manifest().
    def test_manifest_rejects_a_model_pin_or_subagent_model_outside_allowed_models(self):
        SETTINGS.validate_manifest(manifest(), CFG)  # the hub's own manifest passes
        for where, put in (("model", lambda m: m["common"].__setitem__("model", "fable")),
                           ("CLAUDE_CODE_SUBAGENT_MODEL", lambda m: m["common"]["env"].__setitem__("CLAUDE_CODE_SUBAGENT_MODEL", "fable"))):
            rendered = manifest()
            put(rendered)
            SETTINGS.validate_manifest(rendered)  # without cfg there is no list to check
            with self.subTest(where=where), self.assertRaisesRegex(ValueError, f"project common {where} 'fable' is not in \\[gates\\] allowed_models"):
                SETTINGS.validate_manifest(rendered, CFG)
            narrow = {**CFG, "gates": {**CFG["gates"], "allowed_models": ["sonnet"]}}  # opus is a word of the list no more
            with self.subTest(where=where, list="sonnet only"), self.assertRaises(ValueError):
                SETTINGS.validate_manifest(manifest(), narrow)

    def test_sync_fails_on_a_model_pin_outside_allowed_models(self):
        narrow = {**CFG, "gates": {**CFG["gates"], "allowed_models": ["sonnet", "haiku"]}}
        out = io.StringIO()
        with patch.object(SETTINGS, "config", lambda _hub: narrow), contextlib.redirect_stdout(out):
            code = SETTINGS.sync_projects(False, MODULE_PATH.parents[1], mcp=False)
        self.assertEqual(2, code)
        self.assertIn("is not in [gates] allowed_models", out.getvalue())

    # The local settings let Flight run headless with --permission-mode acceptEdits and no bypass: a short allow
    # list, this box's interpreter, its legs' folders and the lean leg boot. Injection: drop the allow copy, the
    # interpreter or a folder in local_text().
    def test_the_local_settings_hold_the_short_allow_list_the_folders_and_the_leg_boot(self):
        rendered = manifest()
        body = json.loads(SETTINGS.local_text(rendered, CFG))
        main = harness_config.main_checkout(MODULE_PATH.parents[1])
        self.assertEqual(["Workflow", "Bash(git *)", "Bash(.venv/bin/python *)", "Bash(uv *)", "Bash(trash *)",
                          "Bash(head *)", "Bash(sed -n *)", "Bash(printenv *)", "Bash(env)",
                          'Bash(echo "exit=$?")', 'Bash(echo "exit $?")',
                          f"mcp__{harness_config.server_name(CFG)}", f"Bash({main.as_posix()}/.venv/bin/python *)"],
                         body["permissions"]["allow"])
        self.assertEqual({"allow", "additionalDirectories"}, set(body["permissions"]))
        self.assertEqual({"permissions", "hooks"}, set(body))  # no mode, no env: nothing is bypassed
        dirs = body["permissions"]["additionalDirectories"]
        for want in (f"{main}_wt", str(harness_config.path(CFG["project"]["state_root"])),
                     *([] if sys.platform == "win32" else ["/tmp"]),
                     str(SETTINGS.user_temp_dir().resolve())):
            self.assertIn(want, dirs)
        self.assertEqual(["SubagentStart"], list(body["hooks"]))

    def test_the_local_key_holds_an_allow_list_and_hooks_only_and_never_a_blanket_allow(self):
        # Defect: a local key that sets a mode or an env, or allows every command, undoes acceptEdits.
        hooks = manifest()["local"]["hooks"]
        for bad, text in (({"hooks": hooks, "permissions": {"allow": []}, "env": {}}, "hold permissions.allow and hooks only"),
                          ({"hooks": hooks, "permissions": {"allow": [], "defaultMode": "bypassPermissions"}},
                           "hold permissions.allow and hooks only"),
                          ({"hooks": hooks}, "hold permissions.allow and hooks only"), (None, "hold permissions"),
                          ({"hooks": hooks, "permissions": {"allow": "Bash(git *)"}}, "a list of rules"),
                          ({"hooks": hooks, "permissions": {"allow": ["Bash(git *)", "Bash( * )"]}}, "every command"),
                          ({"hooks": hooks, "permissions": {"allow": ["Bash"]}}, "every command"),
                          ({"hooks": hooks, "permissions": {"allow": ["Bash(env -u OPENAI_API_KEY *)"]}}, "through a runner"),
                          ({"hooks": hooks, "permissions": {"allow": ["Bash(xargs *)"]}}, "through a runner"),
                          ({"hooks": hooks, "permissions": {"allow": ["Bash(bash -c *)"]}}, "through a runner"),
                          ({"hooks": hooks, "permissions": {"allow": ["Bash(env*)"]}}, "through a runner")):
            broken = manifest()
            broken["local"] = bad
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, f"^local .*{text}"):
                SETTINGS.validate_manifest(broken)

    def test_the_local_settings_add_product_task_trees_when_a_product_repo_is_set(self):
        with patch.object(SETTINGS.product_worktree, "product_repo", lambda _cfg: (Path("/g/product"), Path("."))):
            dirs = SETTINGS.local_dirs(CFG)
            seat = SETTINGS.leg_seat_command(CFG)
        tree = str(Path("/g/product_wt"))  # the platform's own spelling: \g\product_wt on Windows
        self.assertEqual(tree, dirs[0])
        self.assertIn("product_trees=/g/product_wt", seat)
        with patch.object(SETTINGS.product_worktree, "product_repo", lambda _cfg: None):
            self.assertFalse([d for d in SETTINGS.local_dirs(CFG) if d.startswith(tree[:3])])

    @unittest.skipUnless(sys.platform == "darwin", "getconf DARWIN_USER_TEMP_DIR is the Mac answer")
    def test_the_local_render_ignores_the_process_tmpdir(self):
        # A shell with another TMPDIR saw false drift and --mcp wrote its TMPDIR in.
        want = SETTINGS.local_text(manifest(), CFG)
        with tempfile.TemporaryDirectory() as other, patch.dict(SETTINGS.os.environ, {"TMPDIR": other}), \
                patch.object(SETTINGS.tempfile, "tempdir", None):
            self.assertEqual(want, SETTINGS.local_text(manifest(), CFG))

    def test_a_git_fault_drops_only_the_product_trees(self):
        def hung(_cfg):
            raise SETTINGS.subprocess.TimeoutExpired("git", 120)
        with patch.object(SETTINGS.product_worktree, "product_repo", hung):
            self.assertIsNone(SETTINGS.product_main(CFG))
            self.assertIn("SubagentStart", json.loads(SETTINGS.local_text(manifest(), CFG))["hooks"])

    def test_the_lean_leg_boot_answers_only_implementer_and_workflow_leg_with_a_seat_line(self):
        import subprocess
        body = json.loads(SETTINGS.local_text(manifest(), CFG))
        [entry] = body["hooks"]["SubagentStart"]
        self.assertEqual("^(implementer|workflow-leg)$", entry["matcher"])
        for agent in ("implementer", "workflow-leg"):
            self.assertTrue(re.search(entry["matcher"], agent))
        for agent in ("writer", "Explore", "fork", "implementer2"):
            self.assertFalse(re.search(entry["matcher"], agent), agent)
        command = entry["hooks"][0]["command"]
        self.assertNotIn("python", command)  # lean: no interpreter start
        run = subprocess.run(["sh", "-c", command], input="{}", capture_output=True, text=True, check=True)
        answer = json.loads(run.stdout)["hookSpecificOutput"]
        self.assertEqual("SubagentStart", answer["hookEventName"])
        main = harness_config.main_checkout(MODULE_PATH.parents[1])
        self.assertTrue(answer["additionalContext"].startswith(f"SEAT hub={main.as_posix()} hub_trees={main.as_posix()}_wt"))

    def test_a_hand_edit_of_the_local_settings_is_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            hub = Path(temporary)
            (hub / ".claude").mkdir()
            path = hub / SETTINGS.LOCAL_JSON
            out = io.StringIO()
            with patch.object(SETTINGS, "config", lambda _hub: CFG), \
                    patch.object(SETTINGS, "gates_matcher", lambda: "^Bash$"), \
                    contextlib.redirect_stdout(out):
                self.assertEqual(0, SETTINGS.sync_projects(True, hub))
                self.assertEqual(SETTINGS.local_text(manifest(), CFG, hub), path.read_text(encoding="utf-8"))
                self.assertEqual(0, SETTINGS.sync_projects(False, hub))
                edited = json.loads(path.read_text(encoding="utf-8"))
                edited["permissions"]["allow"].append("Bash(*)")
                path.write_text(json.dumps(edited, indent=2) + "\n", encoding="utf-8")
                self.assertEqual(1, SETTINGS.sync_projects(False, hub))
                self.assertIn(f"DRIFT: {SETTINGS.LOCAL_JSON}", out.getvalue())
                self.assertEqual(0, SETTINGS.sync_projects(True, hub))
                self.assertNotIn("Bash(*)", json.loads(path.read_text(encoding="utf-8"))["permissions"]["allow"])

    def test_the_local_settings_stay_out_of_git(self):
        ignored = __import__("subprocess").run(
            ["git", "-C", str(MODULE_PATH.parents[1]), "check-ignore", "-q", SETTINGS.LOCAL_JSON])
        self.assertEqual(0, ignored.returncode)

    def test_manifest_rejects_missing_cross_session_setting(self):
        rendered = manifest()
        rendered["common"].pop("crossSessionInbound")
        with self.assertRaisesRegex(ValueError, "^project common must accept cross-session messages$"):
            SETTINGS.validate_manifest(rendered)

    # Flight.command turns Code Mode on (Claude Code ignores this project env key for it); Code Mode needs agent teams off.
    def test_the_project_settings_turn_code_mode_on_and_agent_teams_off(self):
        env = manifest()["common"]["env"]
        self.assertEqual("1", env["CLAUDE_CODE_EXPERIMENTAL_CODE_MODE"])
        self.assertEqual("0", env["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"])

    # Injection: drop the teams check in validate_manifest().
    def test_manifest_rejects_agent_teams(self):
        for key, scope in (("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS", "env"), ("teammateMode", "common")):
            rendered = manifest()
            target = rendered["common"]["env"] if scope == "env" else rendered["common"]
            target[key] = "1" if scope == "env" else "auto"
            with self.assertRaisesRegex(ValueError, "^project common must not set agent teams"):
                SETTINGS.validate_manifest(rendered)

    def test_every_task_skill_is_visible_at_the_hub_root(self):
        # Defect: a task names a craft skill that the root settings hide from the model.
        rendered = manifest()
        SETTINGS.validate_task_skills(rendered, CFG)
        self.assertTrue(CFG["project"]["task_skills"])
        unrooted = {**rendered, "root": [c for c in rendered["root"] if c != "crafts"]}
        writing = {**CFG, "project": {**CFG["project"], "task_skills": ["writing"]}}
        with self.assertRaisesRegex(ValueError, r"^task_skills not visible at the hub root: \['writing'\]$"):
            SETTINGS.validate_task_skills(unrooted, writing)

    def test_apply_writes_only_the_root_settings_and_names_a_subfolder_settings_an_orphan(self):
        with tempfile.TemporaryDirectory() as temporary:
            hub = Path(temporary)
            (hub / ".claude").mkdir()
            (hub / "Flight/.claude").mkdir(parents=True)
            (hub / "Flight/.claude/settings.json").write_text("{}", encoding="utf-8")
            out = io.StringIO()
            with patch.object(SETTINGS, "config", lambda _hub: CFG), \
                    patch.object(SETTINGS, "gates_matcher", lambda: "^Bash$"), \
                    contextlib.redirect_stdout(out):
                self.assertEqual(0, SETTINGS.sync_projects(True, hub))
                self.assertEqual(0, SETTINGS.sync_projects(False, hub))
            self.assertTrue((hub / ".claude/settings.json").is_file())
            self.assertEqual("{}", (hub / "Flight/.claude/settings.json").read_text(encoding="utf-8"))
            self.assertTrue((hub / ".mcp.json").is_file())
            self.assertFalse((hub / ".claude/orchestrator-settings.json").exists())
            self.assertIn("ORPHAN: Flight/.claude/settings.json - every session seats at the hub root; delete it",
                          out.getvalue())

    def test_mcp_flag_writes_only_the_box_mcp_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            hub = Path(temporary)
            (hub / ".claude").mkdir()
            with patch.object(SETTINGS, "config", lambda _hub: CFG), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, SETTINGS.write_mcp_json(hub))
                first = (hub / ".mcp.json").read_bytes()
                self.assertEqual(0, SETTINGS.write_mcp_json(hub))
            self.assertEqual(first, (hub / ".mcp.json").read_bytes())
            self.assertEqual(SETTINGS.mcp_json_text(manifest(), CFG).encode(), first)
            # The other box-local render follows each land too.
            self.assertEqual(SETTINGS.local_text(manifest(), CFG, hub),
                             (hub / SETTINGS.LOCAL_JSON).read_text(encoding="utf-8"))
            self.assertFalse((hub / ".claude/settings.json").exists())
        with patch.object(SETTINGS.sys, "argv", ["settings-sync.py", "--mcp", "--apply"]), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(2, SETTINGS.main())

    def test_tracked_flag_checks_the_settings_without_the_box_mcp_json(self):
        # The commit tree is an export of the index, which never holds the git-ignored .mcp.json.
        with tempfile.TemporaryDirectory() as temporary:
            hub = Path(temporary)
            (hub / ".claude").mkdir()
            with patch.object(SETTINGS, "config", lambda _hub: CFG), \
                    patch.object(SETTINGS, "gates_matcher", lambda: "^Bash$"), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(1, SETTINGS.sync_projects(False, hub, mcp=False))
                self.assertEqual(0, SETTINGS.sync_projects(True, hub, mcp=False))
                self.assertFalse((hub / ".mcp.json").exists())
                self.assertFalse((hub / SETTINGS.LOCAL_JSON).exists())
                self.assertEqual(0, SETTINGS.sync_projects(False, hub, mcp=False))
                self.assertEqual(1, SETTINGS.sync_projects(False, hub))

    def test_an_editor_server_in_the_local_toml_leaves_the_tracked_settings_alone_and_is_enabled_locally(self):
        # Defect: the pack's mcp_server line made the tracked settings.json drift, and conform failed.
        with tempfile.TemporaryDirectory() as temporary:
            hub = Path(temporary)
            (hub / ".claude").mkdir()
            with patch.object(SETTINGS, "config", lambda _hub: EDITOR_CFG), \
                    patch.object(SETTINGS, "gates_matcher", lambda: "^Bash$"), \
                    contextlib.redirect_stdout(io.StringIO()):
                tracked = SETTINGS.body_for(manifest())  # the render with no editor named
                self.assertEqual(0, SETTINGS.sync_projects(True, hub))
                self.assertEqual(tracked, (hub / ".claude/settings.json").read_text(encoding="utf-8"))
                self.assertEqual(0, SETTINGS.sync_projects(False, hub))
                self.assertEqual(0, SETTINGS.sync_projects(False, hub, mcp=False))
            self.assertNotIn("editor-server", tracked)
            local = json.loads((hub / SETTINGS.LOCAL_JSON).read_text(encoding="utf-8"))
            self.assertEqual(["editor-server"], local["enabledMcpjsonServers"])
            self.assertIn("editor-server", json.loads((hub / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"])

    def test_skill_listing_is_scoped_by_root_capabilities(self):
        rendered = manifest()
        configured = {
            skill
            for capability in rendered["capability_classes"].values()
            for skill in capability.get("skills", [])
        }
        on_disk = set(SETTINGS.project_skills())
        if on_disk:
            self.assertEqual(on_disk, configured)
        all_skills = on_disk or configured
        with patch.object(SETTINGS, "project_skills", lambda: sorted(all_skills)):
            visible = set(SETTINGS.skills_for(rendered))
            overrides = json.loads(SETTINGS.body_for(rendered))["skillOverrides"]
            for skill in all_skills:
                expected = None if skill in visible else "user-invocable-only"
                self.assertEqual(expected, overrides.get(skill), skill)
        # The craft skills stay model-invocable.
        self.assertEqual([], [s for s in CFG["project"]["task_skills"] if s in overrides])

    def test_generated_settings_deny_unused_tools_and_set_name_only_overrides(self):
        # Defect: every session and leg loads the Agent tool, or the project-skill scoping drops the common
        # name-only overrides for bundled skills; no account-specific skill is named.
        body = json.loads(SETTINGS.body_for(manifest()))
        self.assertEqual(["Agent", "mcp__ccd_turn"], body["permissions"]["deny"])
        self.assertNotIn("deniedMcpServers", body)  # no account connector is denied by name
        for skill in ("simplify", "security-review", "claude-api", "code-review", "plugin-authoring"):
            self.assertEqual("name-only", body["skillOverrides"].get(skill), skill)
        self.assertEqual([], [s for s in body["skillOverrides"] if ":" in s])  # a namespaced skill is account-specific

    def test_the_root_starts_with_the_one_session_start_hook_and_no_child_boot(self):
        # Defect: the root loses the memory digest, keeps a removed boot, or its budget drops
        # under freshness (rebase settle 100 + read 6), the sweep (60) and the digest (45).
        # The only other start hook is the Flight-guarded one.
        prefix = "$CLAUDE_PROJECT_DIR"
        hooks = SETTINGS.hooks_for(manifest())
        self.assertEqual({"matcher": "^(startup|resume|clear|compact)$", "hooks": [{
            "type": "command",
            "command": f'sh "{prefix}/Drawer/hooks/run-hook.sh" boot "{prefix}/Drawer/hooks/session-start.py"',
            "timeout": 240}]}, hooks["SessionStart"][0])
        self.assertEqual(2, len(hooks["SessionStart"]))
        self.assertIn("flight-hooks.py", hooks["SessionStart"][1]["hooks"][0]["command"])
        self.assertGreaterEqual(240, 100 + 6 + 60 + 45 + 10)
        self.assertNotIn("SubagentStart", hooks)

    def test_session_end_runs_own_cleanup_for_every_session(self):
        # Defect: a closed session leaves its landed task trees, branches and temp folders.
        # No Flight guard: every session closes. The rendered file carries it (the tracked-render test).
        prefix = "$CLAUDE_PROJECT_DIR"
        self.assertEqual([{"hooks": [{
            "type": "command",
            "command": f'sh "{prefix}/Drawer/hooks/run-hook.sh" pass "{prefix}/Drawer/git-branch-policy.py" '
                       "own-cleanup --session-end",
            "timeout": 120}]}], SETTINGS.hooks_for(manifest())["SessionEnd"])
        tracked = json.loads(MODULE_PATH.parents[1].joinpath(".claude", "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(SETTINGS.hooks_for(manifest())["SessionEnd"], tracked["hooks"]["SessionEnd"])

    def test_every_flight_hook_runs_behind_the_flight_guard(self):
        # Defect: a session that is no Flight starts a Flight hook (Flight identity loads only for a Flight).
        hooks = SETTINGS.hooks_for(manifest())
        flight = [g["hooks"][0]["command"] for groups in hooks.values() for g in groups
                  if "flight-hooks.py" in g["hooks"][0]["command"]]
        self.assertEqual(4, len(flight))
        for command in flight:
            self.assertTrue(command.startswith(FLIGHT_GUARD), command)
        self.assertEqual({"SessionStart", "PreCompact", "PostCompact", "Stop"},
                         {event for event, groups in hooks.items() for g in groups
                          if "flight-hooks.py" in g["hooks"][0]["command"]})
        # The events watchdog runs under the Flight's Monitor, never as a hook.
        self.assertEqual([], [g for groups in hooks.values() for g in groups
                              if "flight-watchdog.py" in g["hooks"][0]["command"]])

    def test_gates_matcher_renders_from_the_table(self):
        manifest = {"hooks": {"PreToolUse": [{"matcher": "{GATES}", "command": "fixture"}]}}
        rendered = SETTINGS.hooks_for(manifest)["PreToolUse"][0]["matcher"]
        # Every Claude row of the GATES table in Drawer/project-gate.py matches, each anchored.
        names = [row.claude for row in GATE.GATES if row.claude]
        self.assertEqual([], [name for name in names if not re.search(rendered, name)])
        kg = [row.claude for row in GATE.GATES if row.claude and row.kg]
        prefix = CFG["derived"]["tool_prefix"]
        self.assertEqual([], [n for n in kg if not re.search(rendered, f"{prefix}{n}")])
        self.assertEqual([], [n for n in ["Read", "Bashful", "xWrite"] if re.search(rendered, n)])

    def test_hook_timeout_must_be_a_positive_integer(self):
        manifest = {"hooks": {"SessionStart": [{"command": "fixture", "timeout": 0}]}}
        with self.assertRaisesRegex(ValueError, "^SessionStart hook timeout must be a positive integer$"):
            SETTINGS.hooks_for(manifest)
        manifest["hooks"]["SessionStart"][0]["timeout"] = 1
        self.assertEqual(
            {"SessionStart": [{"hooks": [{"type": "command", "command": "fixture", "timeout": 1}]}]},
            SETTINGS.hooks_for(manifest),
        )

    def test_async_rewake_renders(self):
        # An async hook never holds the turn.
        entry = {"command": "fixture", "async": True, "asyncRewake": True}
        manifest = {"hooks": {"PostToolUse": [entry]}}
        self.assertEqual(
            {"PostToolUse": [{"hooks": [
                {"type": "command", "command": "fixture", "async": True, "asyncRewake": True}]}]},
            SETTINGS.hooks_for(manifest),
        )

    def route_hub(self, temporary, files):
        """A temporary hub holding the given .claude files, for the model routing check."""
        hub = Path(temporary)
        (hub / ".claude").mkdir()
        self.sync(hub, apply_=True)  # the settings files first, so only routing can drift
        for rel, text in files.items():
            (hub / rel).parent.mkdir(parents=True, exist_ok=True)
            (hub / rel).write_text(text, encoding="utf-8")
        return hub

    def sync(self, hub, apply_=False):
        out = io.StringIO()
        with patch.object(SETTINGS, "config", lambda _hub: CFG), \
                patch.object(SETTINGS, "gates_matcher", lambda: "^Bash$"), contextlib.redirect_stdout(out):
            return SETTINGS.sync_projects(apply_, hub, mcp=False), out.getvalue()

    def test_a_routed_line_that_differs_from_its_row_is_drift_and_apply_rewrites_it(self):
        legs = CFG["routing"]["legs"]
        files = {".claude/workflows/x.js": "const ROUTE = { model: 'opus', effort: 'low' }  // route:one-leg\n",
                 ".claude/workflows/y.js": "const ROUTE = {model:'opus',effort:'low'} // route:one-leg\n",
                 ".claude/agents/p.md": "---\nname: p\nroute: refute\nmodel: haiku\neffort: max\n---\nbody\n"}
        self.assertNotEqual(("opus", "low"), (legs["one-leg"]["model"], legs["one-leg"]["effort"]))
        with tempfile.TemporaryDirectory() as temporary:
            hub = self.route_hub(temporary, files)
            code, out = self.sync(hub)
            self.assertEqual(1, code, out)
            self.assertIn("DRIFT: .claude/workflows/x.js", out)
            self.assertIn("DRIFT: .claude/agents/p.md", out)
            self.assertIn("DRIFT: .claude/workflows/y.js", out)  # no space after the colon
            self.assertEqual(0, self.sync(hub, apply_=True)[0])
            self.assertEqual(f"const ROUTE = {{ model: '{legs['one-leg']['model']}', effort: '{legs['one-leg']['effort']}' }}"
                             "  // route:one-leg\n", (hub / ".claude/workflows/x.js").read_text(encoding="utf-8"))
            self.assertEqual(f"---\nname: p\nroute: refute\nmodel: {legs['refute']['model']}\n"
                             f"effort: {legs['refute']['effort']}\n---\nbody\n",
                             (hub / ".claude/agents/p.md").read_text(encoding="utf-8"))
            self.assertEqual(0, self.sync(hub)[0])

    def test_an_unrouted_or_unknown_model_pick_fails(self):
        cases = {
            "const MODEL = a.model || 'opus'\n": ".claude/workflows/x.js:1: unrouted model pick",
            "agent(p, { model: MODEL, effort: 'high' })\n": ".claude/workflows/x.js:1: unrouted model pick",
            "// a comment may say 'opus'\nconst M = 'claude-sonnet-5-5'\n": ".claude/workflows/x.js:2: unrouted model pick",
            "const ROUTE = { model: 'opus' }  // route:no-such-role\n": "unknown role 'no-such-role'",
            "const ROUTE = {}  // route:one-leg\n": "route tag 'one-leg' picks nothing",
            "const ROUTE = { model: 'opus' }  // route:implementer\n": "route role 'implementer' has no model",
        }
        for text, error in cases.items():
            with self.subTest(text=text), tempfile.TemporaryDirectory() as temporary:
                hub = self.route_hub(temporary, {".claude/workflows/x.js": text})
                code, out = self.sync(hub)
                self.assertEqual(2, code, out)
                self.assertIn(error, out)
                self.assertEqual(text, (hub / ".claude/workflows/x.js").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temporary:  # an agent frontmatter pick with no route: key
            hub = self.route_hub(temporary, {".claude/agents/p.md": "---\nname: p\neffort: high\n---\n"})
            code, out = self.sync(hub)
            self.assertEqual((2, True), (code, ".claude/agents/p.md:3: unrouted model pick" in out), out)
        bad = {**CFG, "routing": {**CFG["routing"], "legs": {**CFG["routing"]["legs"], "x": {"model": "fable"}}}}
        self.assertEqual(["[routing.legs] x: model must be in [gates] allowed_models, effort one of "
                          f"{SETTINGS.LEVEL}"], SETTINGS.route_renders(bad, Path(tempfile.gettempdir()) / "none")[1])

    def test_every_agent_and_workflow_is_routed_and_in_sync_with_the_table(self):
        hub = MODULE_PATH.parents[1]
        renders, errors = SETTINGS.route_renders(CFG, hub)
        self.assertEqual([], errors)
        self.assertEqual([], [p.name for p, want in renders if p.read_text(encoding="utf-8") != want])
        tags = {m.group(1) for p, _ in renders for m in SETTINGS.ROUTE_TAG.finditer(p.read_text(encoding="utf-8"))}
        keys = {m.group(1) for p, _ in renders for m in SETTINGS.ROUTE_KEY.finditer(p.read_text(encoding="utf-8"))}
        self.assertEqual({"one-leg", "fan-out", "find-verify-finder", "find-verify-verifier", "chain",
                          "spec-draft", "rules-trim", "review", "refute", "review-other", "plan", "complete", "tier-trivial", "tier-default",
                          "tier-hard", "tier-design"}, tags)
        self.assertEqual({"implementer", "reviewer", "workflow-leg"}, keys)

    def test_cli_takes_only_apply(self):
        with patch.object(SETTINGS.sys, "argv", ["settings-sync.py", "--user"]), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(2, SETTINGS.main())


if __name__ == "__main__":
    unittest.main()
