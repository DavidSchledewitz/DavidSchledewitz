"""Run with: python -m unittest -v test_claude_usage"""

import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

import claude_usage as cu

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)

SAMPLE = {
    "five_hour": {"utilization": 40.0, "resets_at": (NOW + timedelta(hours=2)).isoformat()},
    "seven_day": {"utilization": 12.0, "resets_at": (NOW + timedelta(days=3, hours=4)).isoformat()},
    "seven_day_opus": {"utilization": None, "resets_at": None},
    "seven_day_sonnet": {"utilization": 5.0, "resets_at": (NOW + timedelta(days=3)).isoformat()},
    "some_future_limit": {"utilization": 7.0, "resets_at": None},
    "iguana_necktie": {"utilization": 11.0, "resets_at": (NOW + timedelta(days=17, hours=16)).isoformat()},
    "extra_usage": {"is_enabled": True, "monthly_limit": 4000, "used_credits": 147.0, "utilization": 3.675},
}


class ParseTests(unittest.TestCase):
    def test_parse_limits_order_and_filtering(self):
        keys = [l.key for l in cu.parse_limits(SAMPLE)]
        self.assertEqual(keys, ["five_hour", "seven_day", "seven_day_sonnet", "iguana_necktie", "some_future_limit"])

    def test_five_hour_without_session_is_still_shown(self):
        limits = cu.parse_limits({"five_hour": {"utilization": None, "resets_at": None}})
        self.assertEqual(len(limits), 1)
        self.assertIsNone(limits[0].seconds_left(NOW))

    def test_elapsed_fraction(self):
        five = cu.parse_limits(SAMPLE)[0]
        self.assertAlmostEqual(five.elapsed_fraction(NOW), 0.6)

    def test_z_suffix_timestamp(self):
        self.assertEqual(cu.parse_time("2026-10-16T12:00:00Z"), NOW)

    def test_extra(self):
        self.assertEqual(cu.parse_extra(SAMPLE), cu.Extra(pct=3.675, used=147.0, limit=4000))
        self.assertIsNone(cu.parse_extra({"extra_usage": {"is_enabled": False, "utilization": 3}}))
        # utilization can be null: derive it from the amounts
        derived = cu.parse_extra({"extra_usage": {"is_enabled": True, "monthly_limit": 100000,
                                                  "used_credits": 0.0, "utilization": None}})
        self.assertEqual(derived.pct, 0.0)

    def test_cloud_credits(self):
        cloud = [l for l in cu.parse_limits(SAMPLE) if l.key == "iguana_necktie"][0]
        self.assertEqual(cloud.label, "Cloud session credits")
        self.assertIsNone(cu.pace_note(cloud, NOW))


class FormatTests(unittest.TestCase):
    def test_durations(self):
        self.assertEqual(cu.fmt_duration(2 * 3600 + 13 * 60 + 5), "2h 13m")
        self.assertEqual(cu.fmt_duration(2 * 3600 + 13 * 60 + 5, with_seconds=True), "2h 13m 05s")
        self.assertEqual(cu.fmt_duration(3 * 86400 + 4 * 3600 + 59), "3d 4h")
        self.assertEqual(cu.fmt_duration(42), "42s")

    def test_pace_under(self):
        # 40% used after 3h of 5h -> ~67% at reset
        note = cu.pace_note(cu.parse_limits(SAMPLE)[0], NOW)
        self.assertEqual(note, ("20% under pace · ~67% at reset", False))

    def test_pace_over(self):
        data = {"five_hour": {"utilization": 90.0, "resets_at": (NOW + timedelta(hours=2)).isoformat()}}
        note = cu.pace_note(cu.parse_limits(data)[0], NOW)
        self.assertEqual(note, ("30% over pace · limit in ~20m at this rate", True))

    def test_weekly_pace_has_day_count(self):
        # 12% used, 3d 4h left of 7d -> day 4, 55% of the week gone
        week = cu.parse_limits(SAMPLE)[1]
        self.assertEqual(cu.pace_note(week, NOW), ("day 4 of 7 · 43% under pace · ~22% at reset", False))

    def test_weekly_bar_has_day_ticks(self):
        b = cu.bar(50, None, 70, cu.Style(False), segments=7)
        self.assertEqual([i for i, c in enumerate(b) if c in "│▓"], [10, 20, 30, 40, 50, 60])
        self.assertEqual(b[10], "▓")  # inside the filled part
        self.assertEqual(b[40], "│")  # in the empty part

    def test_day_scale_labels(self):
        week = cu.parse_limits(SAMPLE)[1]
        scale = cu.day_scale(week, 70)
        self.assertEqual(len(scale), 70)
        self.assertEqual(len(scale.split()), 7)
        self.assertIsNone(cu.day_scale(week, 20))  # too narrow
        self.assertIsNone(cu.day_scale(cu.parse_limits(SAMPLE)[0], 70))  # 5h has no days

    def test_line(self):
        self.assertEqual(cu.render_line(SAMPLE, NOW), "5h 40% (2h00m) · wk 12% (3d4h)")

    def test_render_without_color_has_no_escapes(self):
        state = cu.State(data=SAMPLE, fetched_at=None)
        text = "\n".join(cu.render(state, NOW, 80, cu.Style(False), live=True))
        self.assertNotIn("\x1b", text)
        self.assertIn("Current session (5h)", text)
        self.assertIn("resets in 2h 00m 00s", text)
        self.assertIn("Usage credits (monthly)", text)
        self.assertIn("$1.47 of $40.00 spent", text)
        self.assertIn("expires in 17d 16h", text)

    def test_bar_width_and_marker(self):
        b = cu.bar(50, 0.25, 40, cu.Style(False))
        self.assertEqual(len(b), 40)
        self.assertEqual(b.index("┃"), 10)


class _Handler(BaseHTTPRequestHandler):
    seen = {}

    def do_GET(self):
        _Handler.seen = {k.lower(): v for k, v in self.headers.items()}
        if self.headers.get("Authorization") != "Bearer good-token":
            self.send_response(401)
            self.end_headers()
            return
        body = json.dumps(SAMPLE).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/api/oauth/usage"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = Path(self.tmp.name) / "claude"
        cfg.mkdir()
        expires = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp() * 1000)
        (cfg / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
            "accessToken": "good-token", "expiresAt": expires, "subscriptionType": "max"}}))
        env = {"CLAUDE_CONFIG_DIR": str(cfg), "XDG_CACHE_HOME": str(Path(self.tmp.name) / "cache")}
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        self.url_patch = mock.patch.object(cu, "API_URL", self.url)
        self.url_patch.start()

    def tearDown(self):
        self.url_patch.stop()
        self.env.stop()
        self.tmp.cleanup()

    def test_credentials_file_and_headers(self):
        creds = cu.load_credentials()
        self.assertEqual(creds.plan, "max")
        self.assertEqual(cu.fetch_usage(creds), SAMPLE)
        self.assertEqual(_Handler.seen.get("anthropic-beta"), cu.OAUTH_BETA)

    def test_bad_token_gives_friendly_error(self):
        with self.assertRaises(cu.UsageError) as ctx:
            cu.fetch_usage(cu.Credentials(token="nope"))
        self.assertIn("HTTP 401", str(ctx.exception))

    def test_line_mode_writes_and_reuses_cache(self):
        with mock.patch("builtins.print") as p:
            self.assertEqual(cu.main(["--line"]), 0)
        self.assertTrue(p.call_args[0][0].startswith("5h 40%"))
        self.assertTrue(cu.cache_path().is_file())
        with mock.patch.object(cu, "fetch_usage", side_effect=AssertionError("should use cache")):
            with mock.patch("builtins.print"):
                self.assertEqual(cu.main(["--line"]), 0)

    def test_state_backoff_on_error(self):
        state = cu.State()
        with mock.patch.object(cu, "fetch_usage", side_effect=cu.UsageError("boom")):
            state.refresh(60)
            first = state.next_fetch
            state.refresh(60)
        self.assertEqual(state.failures, 2)
        self.assertGreater(state.next_fetch, first + 50)


if __name__ == "__main__":
    unittest.main()
