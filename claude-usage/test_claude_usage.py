"""Run with: python -m unittest -v test_claude_usage"""

import json
import os
import subprocess
import tempfile
import threading
import time
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

    def test_history(self):
        line = cu.history_line(SAMPLE, 1760616000.04)
        self.assertEqual(json.loads(line), {"t": 1760616000.0, "limits": {
            k: {"pct": v["utilization"], "resets_at": v["resets_at"]}
            for k, v in SAMPLE.items() if k != "extra_usage"}})
        self.assertIsNone(cu.history_line({"extra_usage": {"is_enabled": False}}, 1))
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XDG_DATA_HOME": tmp}):
            cu.append_history(SAMPLE, 1)
            cu.append_history(SAMPLE, 2)
            lines = cu.history_path().read_text().splitlines()
        self.assertEqual([json.loads(l)["t"] for l in lines], [1, 2])

    def test_history_keeps_90_days(self):
        day = 86400
        now = 200 * day
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XDG_DATA_HOME": tmp}):
            times = lambda: [json.loads(l)["t"] for l in cu.history_path().read_text().splitlines()]
            for t in (now - 120 * day, now - 95 * day, now - 89 * day):
                cu.append_history(SAMPLE, t)
            self.assertEqual(len(times()), 3)  # nothing 97+ days older than the newest yet
            cu.append_history(SAMPLE, now)  # now the first is 120 days old: cut to 90 days
            self.assertEqual(times(), [now - 89 * day, now])
            cu.append_history(SAMPLE, now + 60)  # nothing old enough: just appended
            self.assertEqual(len(times()), 3)

    def test_used_up_or_expired_credits_are_hidden(self):
        cloud = [l for l in cu.parse_limits(SAMPLE) if l.key == "iguana_necktie"][0]
        self.assertFalse(cu.finished(cloud, NOW))
        later = cloud.resets_at + timedelta(seconds=1)
        self.assertTrue(cu.finished(cloud, later))
        week = cu.parse_limits(SAMPLE)[1]
        self.assertFalse(cu.finished(cu.Limit(**{**week.__dict__, "pct": 100.0}), NOW))  # real limits stay
        used_up = {**SAMPLE, "iguana_necktie": {**SAMPLE["iguana_necktie"], "utilization": 100.0}}
        st = cu.Style(False)
        for data, shown in ((SAMPLE, True), (used_up, False)):
            state = cu.State(data=data, fetched_at=0)
            self.assertEqual("Cloud session credits" in "\n".join(cu.render(state, NOW, 80, st, live=False)), shown)
            self.assertEqual(any(l.lstrip().startswith("cloud") for l in cu.render_compact(state, NOW, st, live=False)), shown)

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

    def test_day_scale_names_the_day_each_segment_mostly_covers(self):
        # Reset Sunday 20:00 local: the first segment (Sun 20:00 -> Mon 20:00) is Monday.
        reset = datetime(2026, 10, 25, 20, 0).astimezone()
        week = cu.parse_limits({"seven_day": {"utilization": 5.0, "resets_at": reset.isoformat()}})[0]
        with mock.patch.dict(os.environ, {"LC_ALL": "C"}):
            scale = cu.day_scale(week, 70)
        self.assertEqual(scale.split(), ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])
        self.assertEqual(scale.index("Mon"), 4)  # centred in its 10-wide segment
        # A morning reset keeps the reset's own weekday first.
        reset = datetime(2026, 10, 25, 8, 0).astimezone()
        week = cu.parse_limits({"seven_day": {"utilization": 5.0, "resets_at": reset.isoformat()}})[0]
        self.assertEqual(cu.day_scale(week, 70).split()[0], "Sun")

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


class ReportKeyTests(unittest.TestCase):
    def test_report_runs_in_background_and_reports_failure(self):
        state = cu.State()
        real = subprocess.Popen
        fake = lambda *a, **kw: real(["sh", "-c", "echo 'claude-usage report: no transcripts' >&2; exit 1"], **kw)
        with mock.patch.object(cu.subprocess, "Popen", side_effect=fake) as popen:
            state.start_report()
            state.start_report()  # already running: not started twice
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(popen.call_args.args[0][-1], "report")
        self.assertIn("Building the report", state.note)
        state.report_proc.wait()
        state.check_report()
        self.assertIsNone(state.report_proc)
        self.assertEqual(state.note, "report: claude-usage report: no transcripts")


class CompactTests(unittest.TestCase):
    def test_compact_layout_fits_and_has_rows(self):
        state = cu.State(data=SAMPLE, fetched_at=None, plan="pro")
        lines = cu.render_compact(state, NOW, cu.Style(False), live=True)
        self.assertTrue(all(len(l) <= cu.WINDOW_SIZE[0] for l in lines), lines)
        text = "\n".join(lines)
        self.assertIn("Claude · Pro", text)
        self.assertIn("2:00:00", text)      # 5h countdown with seconds
        self.assertIn("3d4h", text)         # weekly
        self.assertIn("$1.47", text)        # extra usage spent
        self.assertLessEqual(len(lines), cu.WINDOW_SIZE[1])

    def test_compact_weekly_ticks_on_whole_cells(self):
        b = cu.bar(10, None, cu.COMPACT_BAR, cu.Style(False), 7)
        self.assertEqual([i for i, c in enumerate(b) if c in "│▓"], [3, 6, 9, 12, 15, 18])

    def test_window_command(self):
        with mock.patch.object(cu.shutil, "which", lambda name: "/usr/bin/" + name if name == "gnome-terminal" else None), \
                mock.patch.object(cu.subprocess, "Popen") as popen:
            self.assertEqual(cu.main(["--window", "--zoom", "0.7"]), 0)
        cmd = popen.call_args[0][0]
        self.assertEqual(cmd[:2], ["gnome-terminal", "--window"])
        self.assertIn("--zoom=0.7", cmd)
        self.assertIn("--geometry=45x8-0+0", cmd)
        self.assertEqual(cmd[cmd.index("--") + 3], "--compact")


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

    renewals = []  # bodies POSTed to the fake token endpoint

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Handler.renewals.append(body)
        if body.get("refresh_token") != "rt-1" or body.get("client_id") != cu.CLAUDE_CODE_CLIENT_ID:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error": "invalid_grant"}')
            return
        reply = {"access_token": "good-token", "refresh_token": "rt-2", "expires_in": 28800,
                 "scope": "user:inference user:profile"}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(reply).encode())

    def log_message(self, *args):
        pass


class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/api/oauth/usage"
        cls.token_url = f"http://127.0.0.1:{cls.server.server_port}/v1/oauth/token"

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
        env = {"CLAUDE_CONFIG_DIR": str(cfg), "XDG_CACHE_HOME": str(Path(self.tmp.name) / "cache"),
               "XDG_DATA_HOME": str(Path(self.tmp.name) / "data")}
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        self.url_patch = mock.patch.object(cu, "API_URL", self.url)
        self.url_patch.start()
        self.token_patch = mock.patch.object(cu, "TOKEN_URL", self.token_url)
        self.token_patch.start()
        self.creds_path = cfg / ".credentials.json"
        _Handler.renewals = []

    def write_login(self, token, refresh="rt-1", expires_in_s=3600):
        expires = int((time.time() + expires_in_s) * 1000)
        self.creds_path.write_text(json.dumps({
            "claudeAiOauth": {"accessToken": token, "refreshToken": refresh, "expiresAt": expires,
                              "scopes": ["user:inference", "user:profile"], "subscriptionType": "pro",
                              "rateLimitTier": "default_claude_ai"},
            "mcpOAuth": {"some-server": {"accessToken": "keep-me"}}}))
        self.creds_path.chmod(0o600)

    def tearDown(self):
        self.token_patch.stop()
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

    def test_retry_after_is_respected(self):
        state = cu.State()
        with mock.patch.object(cu, "fetch_usage", side_effect=cu.UsageError("HTTP 429: slow down.", 900)):
            state.refresh(120)
        self.assertGreater(state.next_fetch, cu.time.time() + 890)
        self.assertIn("Retrying in 15m", state.error)

    def test_backoff_sequence_with_5_min_interval(self):
        state = cu.State()
        delays = []
        with mock.patch.object(cu, "fetch_usage", side_effect=cu.UsageError("HTTP 429", 0)):
            for _ in range(5):
                state.refresh(300)
                delays.append(round(state.next_fetch - cu.time.time()))
        self.assertEqual(delays, [300, 600, 1200, 1800, 1800])

    def test_expired_login_is_renewed_and_saved(self):
        self.write_login("old-token", expires_in_s=-3600)
        with mock.patch("builtins.print") as p:
            self.assertEqual(cu.main(["--line"]), 0)
        self.assertTrue(p.call_args[0][0].startswith("5h 40%"))
        self.assertEqual(len(_Handler.renewals), 1)
        self.assertEqual(_Handler.renewals[0]["grant_type"], "refresh_token")
        self.assertEqual(_Handler.renewals[0]["scope"], "user:inference user:profile")
        blob = json.loads(self.creds_path.read_text())
        oauth = blob["claudeAiOauth"]
        self.assertEqual((oauth["accessToken"], oauth["refreshToken"]), ("good-token", "rt-2"))
        self.assertGreater(oauth["expiresAt"] / 1000, time.time() + 28000)
        self.assertEqual(oauth["rateLimitTier"], "default_claude_ai")      # other fields kept
        self.assertEqual(blob["mcpOAuth"]["some-server"]["accessToken"], "keep-me")
        self.assertEqual(self.creds_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.creds_path.parent.glob("*.tmp")), [])

    def test_valid_login_is_not_renewed(self):
        self.write_login("good-token")
        with mock.patch("builtins.print"):
            self.assertEqual(cu.main(["--json"]), 0)
        self.assertEqual(_Handler.renewals, [])

    def test_rejected_login_is_renewed_and_retried(self):
        self.write_login("revoked-token")  # not expired, but the server says 401
        state = cu.State()
        state.refresh(300)
        self.assertIsNone(state.error)
        self.assertEqual(len(_Handler.renewals), 1)
        self.assertEqual(json.loads(self.creds_path.read_text())["claudeAiOauth"]["accessToken"], "good-token")

    def test_dead_refresh_token_asks_for_login_and_keeps_file(self):
        self.write_login("old-token", refresh="rt-dead", expires_in_s=-60)
        before = self.creds_path.read_text()
        state = cu.State()
        state.refresh(300)
        self.assertTrue(state.login_needed)
        self.assertIn("login has expired", state.error)
        self.assertEqual(self.creds_path.read_text(), before)

    def test_no_auto_renew_file_disables_renewal(self):
        self.write_login("old-token", expires_in_s=-60)
        cfg = Path(self.tmp.name) / "xdgconfig" / "claude-usage"
        cfg.mkdir(parents=True)
        (cfg / "no-auto-renew").touch()
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(cfg.parent)}):
            state = cu.State()
            state.refresh(300)
        self.assertEqual(_Handler.renewals, [])
        self.assertTrue(state.login_needed)

    def test_no_renew_flag(self):
        self.write_login("old-token", expires_in_s=-60)
        with mock.patch("sys.stderr"), mock.patch.dict(os.environ):
            self.assertEqual(cu.main(["--json", "--no-renew"]), 1)
        self.assertEqual(_Handler.renewals, [])

    def test_lock_held_by_other_program_skips_renewal(self):
        self.write_login("old-token", expires_in_s=-60)
        lock = cu.cache_path().with_name("renew.lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.touch()
        creds = cu.load_credentials()
        self.assertIs(cu.renew(creds), creds)
        self.assertEqual(_Handler.renewals, [])
        old = time.time() - 120
        os.utime(lock, (old, old))  # a stale lock from a crash is taken over
        self.assertEqual(cu.renew(cu.load_credentials()).token, "good-token")
        self.assertFalse(lock.exists())

    def test_rejected_login_asks_for_login(self):
        state = cu.State()
        with mock.patch.object(cu, "API_URL", self.url):
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "nope"
            try:
                state.refresh(300)
            finally:
                del os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
        self.assertTrue(state.login_needed)
        text = "\n".join(cu.render(state, NOW, 80, cu.Style(False), live=True))
        self.assertIn("Press l to log in", text)

    def test_login_flag_runs_claude_code_login(self):
        with mock.patch.object(cu, "find_claude", return_value="/opt/claude"), \
                mock.patch.object(cu.subprocess, "call", return_value=0) as call:
            self.assertEqual(cu.main(["--login"]), 0)
        call.assert_called_once_with(["/opt/claude", "auth", "login", "--claudeai"])

    def test_login_flag_without_claude_code(self):
        with mock.patch.object(cu, "find_claude", return_value=None), mock.patch("sys.stderr"):
            self.assertEqual(cu.main(["--login"]), 1)

    def test_background_login_triggers_refresh(self):
        fake = Path(self.tmp.name) / "fake-claude"
        fake.write_text("#!/bin/sh\n[ \"$1 $2\" = \"auth login\" ] && exit 0\nexit 3\n")
        fake.chmod(0o755)
        state = cu.State(login_needed=True, next_fetch=time.time() + 999)
        with mock.patch.object(cu, "find_claude", return_value=str(fake)):
            state.start_login()
        self.assertIsNotNone(state.login_proc)
        state.login_proc.wait(5)
        state.check_login()
        self.assertIsNone(state.login_proc)
        self.assertEqual(state.next_fetch, 0)

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
