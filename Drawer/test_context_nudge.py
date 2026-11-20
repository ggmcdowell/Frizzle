"""The context nudge speaks on the crossing turn only, and never blocks a prompt."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
import unittest.mock

NUDGE = Path(__file__).resolve().parent / "context-nudge.py"
_spec = importlib.util.spec_from_file_location("context_nudge", NUDGE)
nudge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nudge)

START = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
# The two lines the hook may speak, written out from the docstring contract.
# Past 500 K the hook warns; keep working; a Flight compacts in place at 400 K, so it never sees this line.
CONTEXT_LINE = ("CONTEXT {size} K tokens - every further turn re-bills all of it. "
                "Keep working; a Flight compacts in place, no session hands off.")
IDLE_LINE = ("IDLE {lapse} since the last turn, past the {ttl} min cache TTL - "
             "this turn rewrites the whole context at the creation price.")


def turn(minute, read, *, ttl="ephemeral_1h_input_tokens", created=1000):
    return {
        "type": "assistant",
        "timestamp": (START + timedelta(minutes=minute)).isoformat().replace("+00:00", "Z"),
        "message": {"usage": {
            "input_tokens": 2,
            "cache_read_input_tokens": read,
            "cache_creation_input_tokens": created,
            "cache_creation": {ttl: created},
        }},
    }


def event(path):
    return {"hook_event_name": "UserPromptSubmit", "transcript_path": path}


class ContextNudgeTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="context-nudge-test-")
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.written = 0

    def transcript(self, rows):
        self.written += 1
        path = self.folder / ("session-%d.jsonl" % self.written)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return str(path)

    def speak(self, rows, at=START + timedelta(minutes=11)):
        return nudge.run(event(self.transcript(rows)), at)

    def test_the_turn_that_crosses_the_line_announces_the_size(self):
        text = self.speak([turn(0, 490_000), turn(10, 510_000)])
        self.assertEqual(CONTEXT_LINE.format(size=511), text)

    def test_a_context_already_over_the_line_does_not_repeat(self):
        self.assertEqual("", self.speak([turn(0, 510_000), turn(10, 540_000)]))

    def test_an_idle_lapse_past_the_live_ttl_announces(self):
        text = self.speak([turn(0, 50_000), turn(10, 60_000)],
                          at=START + timedelta(minutes=130))
        self.assertEqual(IDLE_LINE.format(lapse="2.0 h", ttl=60), text)

    def test_a_missing_transcript_never_blocks_the_prompt(self):
        printed = io.StringIO()
        missing = str(self.folder / "no-such-file.jsonl")
        with unittest.mock.patch("sys.stdin", io.StringIO(json.dumps(event(missing)))), \
                unittest.mock.patch("sys.stdout", printed):
            code = nudge.main()
        self.assertEqual(0, code)
        self.assertEqual("", printed.getvalue())


if __name__ == "__main__":
    unittest.main()
