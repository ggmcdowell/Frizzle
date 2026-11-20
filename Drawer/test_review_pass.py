"""Tests for Drawer/review_pass.py: the stored risky-review Pass and the task tag read.

record writes a Pass row only for a diff file whose bytes hash to the given hash; stored reads it back; task_tags reads a
task's tags through a stub mem.py. Nothing here reads the live state_root or the live mem.
"""
import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import review_pass  # noqa: E402

HEAD = "a" * 40
STUB_MEM = """import json, sys
from pathlib import Path
reply = json.loads((Path(__file__).parent / "reply.json").read_text())
(Path(__file__).parent / "argv.json").write_text(json.dumps(sys.argv[1:]))
print(json.dumps(reply))
sys.exit(2 if "error" in reply else 0)
"""


class RecordTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "state"
        self.diff = self.state / "review" / "aaaaaaaaaaaa.diff"
        self.diff.parent.mkdir(parents=True)
        self.diff.write_bytes(b"diff --git a/x b/x\n+one\n")
        self.hash = hashlib.sha256(self.diff.read_bytes()).hexdigest()

    def test_record_writes_the_row_and_stored_reads_it(self):
        path = review_pass.record(self.state, self.hash, HEAD, self.diff, 2, "claude-sonnet-5-5")
        self.assertEqual(self.state / "review" / "pass" / f"{self.hash}.json", path)
        row = review_pass.stored(self.state, self.hash)
        self.assertEqual((self.hash, HEAD, "Pass", 2, "claude-sonnet-5-5"),
                         (row["hash"], row["head"], row["verdict"], row["round"], row["reviewer"]))
        self.assertTrue(row["recorded_at"])
        self.assertIsNone(review_pass.stored(self.state, "b" * 64))

    def test_a_diff_that_does_not_hash_to_the_hash_is_refused_and_writes_nothing(self):
        for diff, digest in ((self.diff, "b" * 64), (self.diff.parent / "absent.diff", self.hash)):
            with self.subTest(diff=diff.name), self.assertRaises(review_pass.Refused):
                review_pass.record(self.state, digest, HEAD, diff, 1, "sonnet")
        for bad in ({"digest": "xyz"}, {"head": "HEAD"}, {"round_": 0}, {"reviewer": ""}):
            args = {"digest": self.hash, "head": HEAD, "round_": 1, "reviewer": "sonnet", **bad}
            with self.subTest(bad=bad), self.assertRaises(review_pass.Refused):
                review_pass.record(self.state, args["digest"], args["head"], self.diff, args["round_"], args["reviewer"])
        self.assertFalse((self.state / "review" / "pass").exists())

    def test_a_row_that_is_not_a_pass_for_its_hash_is_not_stored(self):
        folder = self.state / "review" / "pass"
        folder.mkdir(parents=True)
        (folder / f"{self.hash}.json").write_text(json.dumps({"hash": self.hash, "verdict": "Block"}))
        self.assertIsNone(review_pass.stored(self.state, self.hash))
        (folder / f"{self.hash}.json").write_text(json.dumps({"hash": "b" * 64, "verdict": "Pass"}))
        self.assertIsNone(review_pass.stored(self.state, self.hash))
        (folder / f"{self.hash}.json").write_text("not json")
        self.assertIsNone(review_pass.stored(self.state, self.hash))
        self.assertIsNone(review_pass.stored(self.state, "../x"))

    def test_the_cli_prints_one_json_line(self):
        argv = ["record", "--hash", self.hash, "--head", HEAD, "--diff", str(self.diff), "--round", "1",
                "--reviewer", "claude-sonnet-5-5"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = review_pass.main(argv, state_root=self.state)
        self.assertEqual((0, {"state": "recorded", "hash": self.hash,
                              "path": str(self.state / "review" / "pass" / f"{self.hash}.json")}),
                         (code, json.loads(out.getvalue())))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = review_pass.main([*argv[:2], "b" * 64, *argv[3:]], state_root=self.state)
        row = json.loads(out.getvalue())
        self.assertEqual((2, "refused"), (code, row["state"]))
        self.assertIn("does not hash", row["error"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(0, review_pass.main(["--help"]))
        self.assertIn("Owner: Harness", out.getvalue())


class TaskTagsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.hub = Path(tmp.name)
        (self.hub / "Drawer" / "mem").mkdir(parents=True)
        (self.hub / "Drawer" / "mem" / "mem.py").write_text(STUB_MEM, encoding="utf-8")

    def reply(self, row):
        (self.hub / "Drawer" / "mem" / "reply.json").write_text(json.dumps(row), encoding="utf-8")

    def test_the_tags_field_and_the_tags_line_of_the_body(self):
        self.reply({"id": "x", "tags": ["Flight-Backlog"], "body": "Fix it. Why: w. Done when: d.\nTags: Risky, complex"})
        self.assertEqual(["flight-backlog", "risky", "complex"], review_pass.task_tags(self.hub, "1234abcd"))
        argv = json.loads((self.hub / "Drawer" / "mem" / "argv.json").read_text())
        self.assertEqual(["get", "1234abcd", "--json"], argv)
        self.reply({"id": "x", "body": "no tags"})
        self.assertEqual([], review_pass.task_tags(self.hub, "1234abcd"))

    def test_no_card_and_a_mem_fault_raise_apart(self):
        self.reply({"error": "no card matches '1234abcd'"})
        with self.assertRaises(review_pass.NoCard):
            review_pass.task_tags(self.hub, "1234abcd")
        self.reply({"error": "database is locked"})
        with self.assertRaises(review_pass.Refused) as caught:
            review_pass.task_tags(self.hub, "1234abcd")
        self.assertNotIsInstance(caught.exception, review_pass.NoCard)
        self.assertIn("database is locked", str(caught.exception))
        (self.hub / "Drawer" / "mem" / "mem.py").write_text("print('not json')\n", encoding="utf-8")
        with self.assertRaises(review_pass.Refused):
            review_pass.task_tags(self.hub, "1234abcd")


if __name__ == "__main__":
    unittest.main()
