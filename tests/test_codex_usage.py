import json
import tempfile
import unittest
from pathlib import Path

from codex_usage import (
    all_usages,
    compact_status,
    countdown,
    latest_usage,
    project_summary,
    render_overview,
    session_state,
    usage_from_file,
)


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

    def test_includes_new_session_before_first_token_event(self):
        self.write_records([
            {
                "timestamp": "2026-10-11T09:00:00Z",
                "type": "session_meta",
                "payload": {"cwd": "/work/new-session"},
            }
        ])
        usage = usage_from_file(self.file)
        self.assertIsNotNone(usage)
        self.assertEqual(usage.project, "/work/new-session")
        self.assertEqual(usage.total_tokens, 0)
        self.assertIsNone(usage.context_used_percent)

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

    def test_lists_multiple_sessions_newest_first(self):
        self.write_records([
            {"type": "session_meta", "payload": {"cwd": "/work/old"}},
            {"timestamp": "2026-10-11T09:00:00Z", "type": "event_msg", "payload": {"type": "token_count", "info": {}}},
        ])
        second = self.file.with_name("second.jsonl")
        second.write_text(json.dumps({"timestamp": "2026-10-11T10:00:00Z", "type": "event_msg", "payload": {"type": "token_count", "info": {}}}) + "\n")
        self.assertEqual([item.session_id for item in all_usages(self.root)], ["second", "rollout"])

    def test_project_summary_aggregates_sessions(self):
        self.write_records([
            {"type": "session_meta", "payload": {"cwd": "/work/widget"}},
            {"timestamp": "2026-10-11T09:00:00Z", "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"total_tokens": 500}}}},
        ])
        items = project_summary(all_usages(self.root, limit=None))
        self.assertEqual(items[0]["sessions"], 1)
        self.assertEqual(items[0]["total_tokens"], 500)

    def test_active_state_uses_file_update_time(self):
        self.write_records([{"timestamp": "2026-10-11T09:00:00Z", "type": "event_msg", "payload": {"type": "token_count", "info": {}}}])
        usage = usage_from_file(self.file)
        self.assertEqual(session_state(usage, 15, now=usage.modified_at + 899), "active")
        self.assertEqual(session_state(usage, 15, now=usage.modified_at + 901), "idle")
        self.assertIn("Codex 1/1 active", compact_status([usage], active_within=15))
        overview = render_overview([usage], active_within=15)
        self.assertIn("1 active · 1 shown", overview)
        self.assertIn("STATE", overview)


if __name__ == "__main__":
    unittest.main()
