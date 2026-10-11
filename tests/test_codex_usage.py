import json
import tempfile
import unittest
from pathlib import Path

from codex_usage import countdown, latest_usage, usage_from_file


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.file = self.root / "sessions" / "2026" / "10" / "11" / "rollout.jsonl"
        self.file.parent.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def write_records(self, records):
        self.file.write_text("\n".join(json.dumps(record) for record in records) + "\n")

    def test_reads_latest_usage_event(self):
        self.write_records(
            [
                {"type": "session_meta", "payload": {"cwd": "/work/widget"}},
                {
                    "timestamp": "2026-10-11T09:00:00Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 400,
                                "cached_input_tokens": 250,
                                "output_tokens": 100,
                                "total_tokens": 500,
                            },
                            "last_token_usage": {"total_tokens": 200},
                            "model_context_window": 1000,
                        },
                        "rate_limits": {
                            "plan_type": "plus",
                            "primary": {
                                "used_percent": 25,
                                "window_minutes": 300,
                                "resets_at": 2000,
                            },
                            "secondary": {
                                "used_percent": 40,
                                "window_minutes": 10080,
                                "resets_at": 3000,
                            },
                        },
                    },
                },
            ]
        )
        usage = usage_from_file(self.file)
        self.assertIsNotNone(usage)
        self.assertEqual(usage.project, "/work/widget")
        self.assertEqual(usage.context_used_percent, 20)
        self.assertEqual(usage.windows[0].remaining_percent, 75)
        self.assertEqual(usage.windows[1].label, "week")

    def test_skips_broken_json_lines(self):
        self.file.write_text("not json\n")
        self.assertIsNone(usage_from_file(self.file))

    def test_finds_latest_session(self):
        self.write_records([])
        second = self.file.with_name("newer.jsonl")
        second.write_text(
            json.dumps(
                {
                    "timestamp": "2026-10-11T10:00:00Z",
                    "type": "event_msg",
                    "payload": {"type": "token_count", "info": {}},
                }
            )
            + "\n"
        )
        usage = latest_usage(self.root)
        self.assertEqual(usage.observed_at, "2026-10-11T10:00:00Z")

    def test_countdown(self):
        self.assertEqual(countdown(10_000, now=1_000), "2h 30m")


if __name__ == "__main__":
    unittest.main()
