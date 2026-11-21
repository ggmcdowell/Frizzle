from __future__ import annotations

import contextlib
import datetime
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


PATH = Path(__file__).with_name("transcript.py")
SPEC = importlib.util.spec_from_file_location("transcript_under_test", PATH)
TRANSCRIPT = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(TRANSCRIPT)


class ExtractHubGuardTests(unittest.TestCase):
    """Contract (transcript.py docstring): extract writes to the device-local temp dir by
    default and REFUSES to write into the hub unless --out explicitly points there."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.hub = root / "stand-in-hub"
        self.hub.mkdir()
        sessions = root / "sessions"
        sessions.mkdir()
        self.session = sessions / "197ef344-2442-4574-9880-a76933a478cc.jsonl"
        rows = [
            {"type": "assistant", "message": {"id": "m1", "content": [
                {"type": "tool_use", "id": "w1", "name": "Write", "input": {
                    "file_path": "/work/Sample/Source/Wand.cpp",
                    "content": "int wand = 1;\n"}},
                {"type": "tool_use", "id": "e1", "name": "Edit", "input": {
                    "file_path": "/work/Sample/Source/Wand.cpp",
                    "old_string": "int wand = 0;", "new_string": "int wand = 1;"}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "w1"},
                {"type": "tool_result", "tool_use_id": "e1"}]}},
        ]
        self.session.write_bytes(
            "".join(json.dumps(r) + chr(10) for r in rows).encode("utf-8"))

    def run_extract(self, argv):
        """The CLI in one call: returns (exit code or None, stdout). TEMP points inside the
        stand-in hub, so the default output dir lands in the hub."""
        code, out = None, io.StringIO()
        with patch.object(TRANSCRIPT, "HUB", str(self.hub)), \
                patch.dict(os.environ, {"TEMP": str(self.hub)}, clear=True), \
                patch.object(sys, "argv", ["transcript.py", "extract"] + argv), \
                contextlib.redirect_stdout(out):
            try:
                TRANSCRIPT.main()
            except SystemExit as stop:
                code = stop.code
        return code, out.getvalue()

    def test_extract_refuses_the_hub_by_default_and_takes_it_from_an_explicit_out(self):
        refused, printed = self.run_extract([str(self.session)])
        self.assertEqual(
            "transcript.py: refusing to extract into the hub by default "
            "(transcripts can contain project content). Pass --out explicitly.", refused)
        self.assertEqual("", printed)
        # Nothing of the transcript reached the hub: the default dir was never made.
        self.assertEqual([], sorted(p.name for p in self.hub.iterdir()))

        target = self.hub / "reports" / "extract"
        accepted, _ = self.run_extract([str(self.session), "--out", str(target)])
        self.assertIsNone(accepted)
        self.assertEqual(["_edits.txt", "_work_Sample_Source_Wand.cpp"],
                         sorted(p.name for p in target.iterdir()))
        self.assertEqual("int wand = 1;\n",
                         (target / "_work_Sample_Source_Wand.cpp").read_text(encoding="utf-8"))
        self.assertEqual("EDIT /work/Sample/Source/Wand.cpp\n"
                         "  old: int wand = 0;\n  new: int wand = 1;\n\n",
                         (target / "_edits.txt").read_text(encoding="utf-8"))


class TurnContextTests(unittest.TestCase):
    """Contract (transcript.py docstring, turn_context): a turn's context is its LAST executor
    iteration; advisor_message iterations are other models' calls, kept out of the context."""

    def setUp(self):
        # Top-level usage sums the two executor iterations (1110 + 2220) and the advisor call.
        self.turn = {
            "input_tokens": 35, "cache_creation_input_tokens": 300, "cache_read_input_tokens": 3050,
            "output_tokens": 40, "iterations": [
                {"type": "message", "input_tokens": 10, "cache_creation_input_tokens": 100,
                 "cache_read_input_tokens": 1000, "output_tokens": 20},
                {"type": "advisor_message", "model": "claude-opus-5-5", "input_tokens": 5,
                 "cache_read_input_tokens": 50, "output_tokens": 7},
                {"type": "message", "input_tokens": 20, "cache_creation_input_tokens": 200,
                 "cache_read_input_tokens": 2000, "output_tokens": 13}]}

    def test_turn_context_takes_the_last_executor_iteration(self):
        advisor = {"model": "claude-opus-5-5", "input": 55, "output": 7}
        self.assertEqual((2220, [advisor]), TRANSCRIPT.turn_context(self.turn))
        # Without iterations the top-level counts stand.
        plain = {"input_tokens": 1, "cache_read_input_tokens": 2000, "output_tokens": 3}
        self.assertEqual((2001, []), TRANSCRIPT.turn_context(plain))

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        session = Path(folder.name) / "s.jsonl"
        rows = [{"type": "assistant", "message": {"id": mid, "usage": usage}}
                for mid, usage in (("m1", self.turn), ("m2", plain), ("m3", self.turn))]
        session.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        stats = TRANSCRIPT.classify_stats(str(session))
        self.assertEqual(2220, stats["peak_context"])
        self.assertEqual({"claude-opus-5-5": {"calls": 2, "input": 110, "output": 14}}, stats["advisor"])



class CodeModeTests(unittest.TestCase):
    """codemode counts the failed REPL calls by category, ranks by sessions hit and names the root fix."""

    def test_codemode_counts_failed_repl_calls_and_names_the_top_category(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        sid = "197ef344-2442-4574-9880-a76933a478cc"

        def use(uid):
            return {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": uid, "name": "REPL", "input": {"code": "x"}}]}}

        def result(uid, text, error=False):
            return {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": uid, "is_error": error, "content": text}]}}

        old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)).isoformat()
        # A failed call stamped before the --hours cut sits in a file written inside it: it must not count.
        rows = [dict(use("r0"), timestamp=old), dict(result("r0", "ReferenceError: stale", True), timestamp=old),
                use("r1"), result("r1", "ReferenceError: tools.mcp__mem__search is not a function", True),
                use("r2"), result("r2", "ok <error>tools.Bash and then failed: ShellError exit 1</error>"),
                use("r3"), result("r3", "ok")]
        (root / f"{sid}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        out = io.StringIO()
        with patch.object(sys, "argv", ["transcript.py", "--dir", str(root), "codemode", "--hours", "1"]), \
                contextlib.redirect_stdout(out):
            TRANSCRIPT.main()
        line = json.loads(out.getvalue())
        self.assertEqual((1, 3, 2), (line["code_mode_sessions"], line["repl_calls"], line["errors"]))
        self.assertEqual({"tool search": {"count": 1, "sessions": 1}, "shell exit": {"count": 1, "sessions": 1}},
                         line["by_category"])
        self.assertEqual("tool search", line["top"])
        self.assertIn("Drawer/mem/mem.py --json", line["root_fix"])

    def test_codemode_legs_counts_top_level_calls_per_agent_type(self):
        """--legs: REPL share of each agentType's top-level tool calls; a Bash nested in REPL
        (isVirtual) is not top-level."""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        runs = Path(folder.name) / "197ef344-2442-4574-9880-a76933a478cc" / "subagents" / "workflows" / "wf_1"
        runs.mkdir(parents=True)

        def call(uid, name, virtual=False):
            row = {"type": "assistant", "isSidechain": True, "message": {"content": [
                {"type": "tool_use", "id": uid, "name": name, "input": {}}]}}
            if virtual:
                row["isVirtual"] = True
            return row

        def leg(agent, agent_type, rows):
            (runs / f"agent-{agent}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
            (runs / f"agent-{agent}.meta.json").write_text(json.dumps({"agentType": agent_type}), encoding="utf-8")

        # Leg a: 3 REPL calls, 4 Bash nested in them, 1 top-level Read -> 3 of 4.
        leg("a", "workflow-leg", [call("r1", "REPL"), call("b1", "Bash", True), call("b2", "Bash", True),
                                  call("r2", "REPL"), call("b3", "Bash", True), call("b4", "Bash", True),
                                  call("r3", "REPL"), call("x1", "Read")])
        # Leg b: plain tools only.
        leg("b", "workflow-leg", [call("y1", "Bash"), call("y2", "Bash")])
        leg("c", "implementer", [call("w1", "Read"), call("w2", "Write")])
        # The journal is no agent transcript.
        (runs / "journal.jsonl").write_text(json.dumps(call("j1", "Bash")) + "\n", encoding="utf-8")
        out = io.StringIO()
        with patch.object(sys, "argv", ["transcript.py", "--dir", folder.name, "codemode", "--legs", "--hours", "1"]), \
                contextlib.redirect_stdout(out):
            TRANSCRIPT.main()
        line = json.loads(out.getvalue())
        self.assertEqual(0.9, line["bar"])
        self.assertEqual({"legs": 2, "calls": 6, "repl": 3, "repl_share": 0.5}, line["by_agent_type"]["workflow-leg"])
        self.assertEqual({"legs": 1, "calls": 2, "repl": 0, "repl_share": 0.0}, line["by_agent_type"]["implementer"])
        self.assertEqual(["implementer", "workflow-leg"], line["below_bar"])  # no type is exempt from the bar



class WindowsProjectDirTests(unittest.TestCase):
    def test_the_project_folder_is_found_whatever_the_drive_letter_case(self):
        # A fake case-sensitive projects folder: a real Windows folder is case-insensitive and cannot hold both spellings.
        projects = os.path.join(os.sep, "fake", "projects")
        present = {"C--Users-x-Hub"}

        def isdir(path):
            return path == projects or (os.path.dirname(path) == projects and os.path.basename(path) in present)

        args = type("Args", (), {"dir": None, "project": "C:\\Users\\x\\Hub"})()
        with patch.object(TRANSCRIPT, "CLAUDE_PROJECTS", projects), \
                patch.object(TRANSCRIPT, "mangle", return_value="c--Users-x-Hub"), \
                patch.object(TRANSCRIPT.os.path, "isdir", side_effect=isdir), \
                patch.object(TRANSCRIPT.os, "listdir", side_effect=lambda path: sorted(present)):
            with patch.object(TRANSCRIPT.oscompat, "WINDOWS", False):
                self.assertEqual(os.path.join(projects, "c--Users-x-Hub"), TRANSCRIPT.resolve_dir(args))  # POSIX: exact only
            with patch.object(TRANSCRIPT.oscompat, "WINDOWS", True):
                self.assertEqual(os.path.join(projects, "C--Users-x-Hub"), TRANSCRIPT.resolve_dir(args))
            present.add("c--Users-x-Hub")
            with patch.object(TRANSCRIPT.oscompat, "WINDOWS", True):
                self.assertEqual(os.path.join(projects, "c--Users-x-Hub"), TRANSCRIPT.resolve_dir(args))

    def test_the_projects_folder_follows_claude_config_dir(self):
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/cfg/claude"}):
            spec = importlib.util.spec_from_file_location("transcript_cfg", PATH)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        self.assertEqual(os.path.join(str(Path("/cfg/claude")), "projects"), mod.CLAUDE_PROJECTS)


if __name__ == "__main__":
    unittest.main()
