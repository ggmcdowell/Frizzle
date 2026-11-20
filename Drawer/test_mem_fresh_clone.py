"""A fresh clone in any folder runs mem with no OpenAI key.

The test clones the hub's committed HEAD into a temp folder that is not named after the hub, gives it a
scratch HOME (so [project] config_root and state_root, and the mem paths under them, sit in a temp folder)
and a scratch TMPDIR (the test-run scratch DB lives there), drops OPENAI_API_KEY, and runs the setup path:
settings-sync --mcp, mem doctor, mem seed, mem add and search, and the MCP server over stdio from the rendered
.mcp.json and from .mcp.json.example. The live-checkout guard finds the live hub from the clone's own main
worktree: a task worktree of the clone names the clone, not a folder under HOME.
"""
import json
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

HUB = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HUB / "Drawer"))
import harness_config  # noqa: E402

DROP = ("OPENAI_API_KEY", harness_config.env_name("MEM_DB"), harness_config.env_name("MEM_EMBED"), "MEM_LIVE_WRITE",
        harness_config.NO_LOCAL_VAR)


def run(args, cwd, env, check=True, timeout=120):
    out = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    if check and out.returncode:
        raise AssertionError(f"{args} exit {out.returncode}\n{out.stdout}\n{out.stderr}")
    return out


class Stdio:
    """A minimal MCP client: JSON-RPC lines over a server's stdin and stdout."""

    def __init__(self, argv, cwd, env):
        self.p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
        self.lines = queue.Queue()
        threading.Thread(target=lambda: [self.lines.put(x) for x in self.p.stdout], daemon=True).start()

    def send(self, msg):
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()

    def ask(self, ident, method, params=None):
        self.send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}})
        while True:
            try:
                line = self.lines.get(timeout=60)
            except queue.Empty:
                self.p.kill()
                raise AssertionError(f"no answer to {method}: {self.p.stderr.read()[-2000:]}")
            msg = json.loads(line)
            if msg.get("id") == ident:
                return msg

    def close(self):
        self.p.stdin.close()
        try:
            self.p.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.p.kill()
        self.p.stdout.close()
        self.p.stderr.close()


class FreshCloneTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="mem-fresh-")).resolve()
        cls.clone = cls.tmp / "products" / "LanternHub"  # any folder name: not the hub's own
        env = {k: v for k, v in harness_config.git_env().items() if k not in DROP}
        cls.home, scratch = cls.tmp / "home", cls.tmp / "scratch"
        cls.home.mkdir()
        scratch.mkdir()
        env.update(HOME=str(cls.home), TMPDIR=str(scratch), GIT_CONFIG_NOSYSTEM="1")
        cls.env = env
        run(["git", "clone", "-q", "--no-hardlinks", str(HUB), str(cls.clone)], cls.tmp, env)
        (cls.clone / ".venv").symlink_to(HUB / ".venv")  # the uv sync stand-in: the hub's own environment
        cls.py = str(cls.clone / ".venv" / "bin" / "python")
        cls.mem = [cls.py, "-B", "Drawer/mem/mem.py"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def mem_json(self, *args):
        out = run([*self.mem, *args, "--json"], self.clone, self.env)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_config_roots_sit_under_the_scratch_home(self):
        code = ("import sys;sys.path.insert(0,'Drawer');import harness_config as h;c=h.load();"
                "print(h.path(c['project']['config_root']));print(h.path(c['project']['state_root']));"
                "print(h.path(c['mem']['db']));print(h.path(c['mem']['env_file']))")
        paths = run([self.py, "-B", "-c", code], self.clone, self.env).stdout.split()
        self.assertEqual(4, len(paths))
        for p in paths:
            self.assertTrue(Path(p).is_relative_to(self.home), p)

    def test_setup_path_runs_with_no_key(self):
        # Setup renders .mcp.json with the clone's real path.
        run([self.py, "-B", "Drawer/settings-sync.py", "--mcp"], self.clone, self.env)
        rendered = json.loads((self.clone / ".mcp.json").read_text())["mcpServers"]["mem"]
        self.assertNotIn("/path/to", json.dumps(rendered))
        # The doctor makes the DB, loads sqlite-vec and names keyword-only mode in one OK line.
        doctor = run([*self.mem, "doctor"], self.clone, self.env, check=False)
        self.assertEqual(0, doctor.returncode, doctor.stdout + doctor.stderr)
        last = doctor.stdout.strip().splitlines()[-1]
        self.assertTrue(last.startswith("OK"), last)
        self.assertIn("keyword-only", last)
        # The starter seed loads into the empty DB once.
        seeded = self.mem_json("seed")
        self.assertEqual(5, seeded["imported"], seeded)
        self.assertEqual(0, self.mem_json("seed")["imported"])
        ready = self.mem_json("list", "--ready")
        self.assertEqual(1, len(ready), ready)
        # A card added with no key is found by keyword.
        card = self.mem_json("add", "--kind", "fact", "--body", "The lantern ferret hums teal at dusk.")
        hits = self.mem_json("search", "ferret")
        self.assertEqual(card["id"], hits[0]["id"])
        # The MCP server answers from the rendered .mcp.json and from the example as it is.
        example = json.loads((self.clone / ".mcp.json.example").read_text())["mcpServers"]["mem"]
        for name, server in (("rendered", rendered), ("example", example)):
            with self.subTest(name):
                self.assert_mcp(server, card["id"])

    def assert_mcp(self, server, card_id):
        client = Stdio([server["command"], *server["args"]], self.clone, self.env)
        try:
            init = client.ask(1, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                                "clientInfo": {"name": "fresh-clone-test", "version": "0"}})
            self.assertEqual("mem", init["result"]["serverInfo"]["name"])
            client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            tools = {t["name"] for t in client.ask(2, "tools/list")["result"]["tools"]}
            self.assertLessEqual({"search", "get", "add", "list_cards", "set_status"}, tools)
            found = client.ask(3, "tools/call", {"name": "search", "arguments": {"query": "ferret"}})
            self.assertIn(card_id, json.dumps(found["result"]))
        finally:
            client.close()

    def test_a_task_worktree_of_the_clone_names_the_clone_as_the_live_hub(self):
        wt = self.tmp / "products" / "LanternHub_wt" / "task"
        run(["git", "-C", str(self.clone), "worktree", "add", "-q", "-b", "fresh-clone-wt", str(wt)], self.tmp, self.env)
        code = ("import sys;sys.path.insert(0,'Drawer/mem');import mem;"
                "print(mem.live_hub());print(mem.foreign_live(mem.live_db()))")
        for where, foreign in ((self.clone, "False"), (wt, "True")):
            with self.subTest(str(where)):
                hub, guard = run([self.py, "-B", "-c", code], where, self.env).stdout.split()
                self.assertEqual(self.clone, Path(hub).resolve())
                self.assertEqual(foreign, guard)


if __name__ == "__main__":
    unittest.main()
