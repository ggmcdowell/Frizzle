"""The MCP stdio probe against real stdio servers: paging, the acceptance rule, timeout.

Run under the prepared MCP interpreter, which carries the SDK:
    Operator\\server\\.venv\\Scripts\\python.exe -B -m unittest Drawer/runtime/test_mcp_probe.py
"""
import os
from pathlib import Path
import sys
import tempfile
import unittest

DRAWER = Path(__file__).resolve().parents[1]
if str(DRAWER) not in sys.path:
    sys.path.insert(0, str(DRAWER))
from runtime import mcp_probe  # noqa: E402

try:
    import mcp  # noqa: F401
    SDK = True
except ImportError:
    SDK = False

# A hand-written JSON-RPC stdio server: FastMCP answers one page only, and the probe's
# cursor loop is the part worth proving.
SERVER = '''\
import json, sys

SCHEMA = {"type": "object", "properties": {}}
GOOD = {"name": "first", "inputSchema": SCHEMA}
# Each rejected page carries one good tool as a decoy: one bad tool must sink the list.
PAGES = {
    "pages": [([], "page-2"),
              ([GOOD, {"name": "second", "inputSchema": SCHEMA}], None)],
    "empty": [([], None)],
    "no-schema": [([GOOD, {"name": "second"}], None)],
    "empty-name": [([GOOD, {"name": "", "inputSchema": SCHEMA}], None)],
}
mode = sys.argv[1]
page = 0


def send(message):
    sys.stdout.write(json.dumps(message) + "\\n")
    sys.stdout.flush()


for line in sys.stdin:
    if not line.strip():
        continue
    request = json.loads(line)
    if request.get("id") is None:
        continue
    if request.get("method") == "initialize":
        send({"jsonrpc": "2.0", "id": request["id"], "result": {
            "protocolVersion": request["params"]["protocolVersion"],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "fake", "version": "0.0.1"}}})
    elif request.get("method") == "tools/list":
        tools, cursor = PAGES[mode][page]
        page += 1
        result = {"tools": tools}
        if cursor:
            result["nextCursor"] = cursor
        send({"jsonrpc": "2.0", "id": request["id"], "result": result})
    else:
        send({"jsonrpc": "2.0", "id": request["id"],
              "error": {"code": -32601, "message": "unsupported"}})
'''

SLEEPER = "import time\ntime.sleep(600)\n"


@unittest.skipUnless(SDK, "the MCP SDK lives in the prepared MCP environment")
class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mcp probe ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.server = self.root / "fake_server.py"
        self.server.write_text(SERVER, encoding="utf-8")
        # All a bare interpreter needs to start: no seat variable reaches the fake server.
        self.env = {"SYSTEMROOT": os.environ.get("SYSTEMROOT", "")} if os.name == "nt" else {}

    def probe(self, *arguments, timeout=30):
        return mcp_probe.probe_stdio([sys.executable, str(self.server), *arguments],
                                     self.root, self.env, timeout=timeout)

    def test_a_paged_tool_list_passes_and_reports_what_it_listed(self):
        self.assertEqual((True, "2 tools: first, second"), self.probe("pages"))

    def test_an_empty_tool_list_is_inactive(self):
        self.assertEqual((False, "inactive"), self.probe("empty"))

    def test_a_tool_list_the_sdk_cannot_read_fails_with_its_own_diagnostic(self):
        # The SDK model requires inputSchema, so this list never reaches the probe's own
        # rule. What must not happen: a pass, or a reason a seat check reads as a verdict.
        started, reason = self.probe("no-schema")
        self.assertIs(False, started)
        self.assertNotEqual("inactive", reason)
        self.assertNotEqual("timeout", reason)
        self.assertIn("inputSchema", reason)  # the field the fake server left out

    def test_a_tool_with_an_empty_name_fails(self):
        started, reason = self.probe("empty-name")
        self.assertIs(False, started)
        # The probe's own refusal, inside the repr of the SDK task group's ExceptionGroup.
        self.assertIn("RuntimeError('MCP server exposed no valid tools')", reason)

    def test_a_server_that_never_answers_times_out(self):
        sleeper = self.root / "sleeper.py"
        sleeper.write_text(SLEEPER, encoding="utf-8")
        result = mcp_probe.probe_stdio([sys.executable, str(sleeper)], self.root, self.env, timeout=3)
        self.assertEqual((False, "timeout"), result)


class ScrubTests(unittest.TestCase):
    def test_credential_shaped_diagnostics_never_leave_the_probe(self):
        self.assertEqual("bearer [redacted]", mcp_probe._scrub("bearer abc123"))
        self.assertEqual("Authorization: Bearer [redacted]",
                         mcp_probe._scrub("Authorization: Bearer abc123"))
        self.assertEqual("token [redacted]", mcp_probe._scrub("token=" + "a" * 8))
        self.assertEqual("api_key [redacted]", mcp_probe._scrub("api_key: s3cret"))
        self.assertEqual("[redacted]", mcp_probe._scrub("b" * 40))
        # The boundary the rule keeps: a short run is a word, not a secret.
        self.assertEqual("b" * 39, mcp_probe._scrub("b" * 39))
        self.assertEqual("", mcp_probe._scrub(None))


if __name__ == "__main__":
    unittest.main()
