"""Run with: python -m unittest -v test_claude_usage_report"""

import json
import math
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import claude_usage as cu
import claude_usage_report as rep

T0 = datetime(2026, 10, 16, 9, 10, tzinfo=timezone.utc).timestamp()
HOUR = 3600


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


def user(sid, t, content, **extra):
    return {"type": "user", "sessionId": sid, "timestamp": iso(t), "cwd": "/home/me/proj",
            "message": {"role": "user", "content": content}, **extra}


def assistant(sid, t, mid, usage, content=None, model="claude-opus-5-5", **extra):
    return {"type": "assistant", "sessionId": sid, "timestamp": iso(t), "requestId": "req_" + mid,
            "message": {"id": mid, "model": model, "role": "assistant",
                        "content": content or [{"type": "text", "text": "ok"}], "usage": usage}, **extra}


def usage(cr=0, cw=0, out=100, inp=3, think=0, ttl="1h"):
    return {"input_tokens": inp, "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw,
            "output_tokens": out, "output_tokens_details": {"thinking_tokens": think},
            "cache_creation": {f"ephemeral_{ttl}_input_tokens": cw}}


def write(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))


class PromptTextTests(unittest.TestCase):
    def test_plain_and_blocks(self):
        self.assertEqual(rep.prompt_text("  fix   the bug "), "fix the bug")
        self.assertEqual(rep.prompt_text([{"type": "text", "text": "look"}, {"type": "image"}]), "look [image]")

    def test_tool_results_and_bookkeeping_are_not_prompts(self):
        self.assertIsNone(rep.prompt_text([{"type": "tool_result", "tool_use_id": "x", "content": "hi"}]))
        self.assertIsNone(rep.prompt_text("<local-command-stdout>done</local-command-stdout>"))
        self.assertIsNone(rep.prompt_text("[Request interrupted by user]"))
        self.assertIsNone(rep.prompt_text("<system-reminder>only this</system-reminder>"))

    def test_slash_commands_and_shell(self):
        text = "<command-message>compact</command-message><command-name>/compact</command-name><command-args>keep tests</command-args>"
        self.assertEqual(rep.prompt_text(text), "/compact keep tests")
        self.assertEqual(rep.prompt_text("<bash-input>ls -la</bash-input>"), "! ls -la")


class LoadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "projects"
        sid = "s1"
        tool = [{"type": "tool_use", "id": "tu1", "name": "Read", "input": {}}]
        agent = [{"type": "tool_use", "id": "tu2", "name": "Agent", "input": {}}]
        write(self.root / "-home-me-proj" / "s1.jsonl", [
            {"type": "custom-title", "customTitle": "My chat", "sessionId": sid},
            user(sid, T0, "first question"),
            # one call written as two lines (thinking, then tool use): counted once
            assistant(sid, T0 + 5, "m1", usage(cr=20000, cw=30000, out=10), [{"type": "thinking", "thinking": ""}]),
            assistant(sid, T0 + 6, "m1", usage(cr=20000, cw=30000, out=400, think=300), tool),
            user(sid, T0 + 7, [{"type": "tool_result", "tool_use_id": "tu1", "content": "x" * 40000}]),
            assistant(sid, T0 + 20, "m2", usage(cr=50000, cw=10500, out=200)),
            user(sid, T0 + 30, "skill body " * 400, isMeta=True),
            # back after two hours: the 1h cache has expired
            user(sid, T0 + 2 * HOUR, "second question"),
            assistant(sid, T0 + 2 * HOUR + 5, "m3", usage(cr=0, cw=61000, out=300), agent),
            assistant(sid, T0 + 2 * HOUR + 90, "m4", usage(cr=61000, cw=500, out=800)),
            assistant(sid, T0 + 2 * HOUR + 91, "synthetic", usage(), model="<synthetic>"),
        ])
        write(self.root / "-home-me-proj" / "s1" / "subagents" / "agent-1.jsonl", [
            user(sid, T0 + 2 * HOUR + 10, "sub task", isSidechain=True),
            assistant(sid, T0 + 2 * HOUR + 20, "a1", usage(cr=10000, cw=8000, out=500), isSidechain=True),
            assistant(sid, T0 + 2 * HOUR + 30, "a2", usage(cr=18000, cw=2000, out=500), isSidechain=True),
        ])
        write(self.root / "-home-me-other" / "old.jsonl", [
            user("old", T0 - 40 * 86400, "ancient"),
            assistant("old", T0 - 40 * 86400, "o1", usage(cw=1000)),
        ])
        self.sessions = rep.load_sessions(T0 - 86400, [self.root])

    def tearDown(self):
        self.tmp.cleanup()

    def test_sessions_prompts_and_calls(self):
        self.assertEqual(list(self.sessions), ["s1"])  # the old chat is outside the period
        s = self.sessions["s1"]
        self.assertEqual(s.title, "My chat")
        self.assertEqual([p.text for p in s.prompts], ["first question", "second question"])
        first, second = s.prompts
        self.assertEqual(len(first.requests), 2)
        self.assertEqual(first.requests[0].out, 400)  # the final count of the streamed call
        self.assertEqual(first.requests[0].think, 300)
        self.assertEqual(first.requests[0].tools, ["Read"])
        self.assertEqual(len(second.requests), 4)  # 2 main + 2 subagent, synthetic dropped
        self.assertEqual(sum(r.sidechain for r in second.requests), 2)
        self.assertEqual(first.tool_out["Read"], 10000)
        self.assertGreater(first.tool_out[rep.INJECTED], 500)

    def test_reasons(self):
        s = self.sessions["s1"]
        second = rep.summarize(s.prompts[1].requests, s.prompts[1].tool_out)
        texts = " | ".join(t for t, _ in second["reasons"])
        self.assertIn("cache expired after 1h 59m idle", texts)
        self.assertIn("a subagent (2 calls)", texts)
        self.assertEqual(second["agents"], 1)
        self.assertEqual(second["n"], 2)
        self.assertEqual(second["ns"], 2)
        first = rep.summarize(s.prompts[0].requests, s.prompts[0].tool_out)
        self.assertIn("new chat", first["reasons"][0][0])
        shares = [x for _, x in first["reasons"]]
        self.assertEqual(shares, sorted(shares, reverse=True))

    def test_costs(self):
        r = rep.Request(t=0, session="s", model="claude-opus-5-5", sidechain=False,
                        inp=1_000_000, cw5=1_000_000, cw1h=1_000_000, cr=1_000_000, out=1_000_000)
        for k, v in {"read": 0.2, "write": 4 * 1.25 + 4 * 2, "input": 4.0, "output": 20.0}.items():
            self.assertAlmostEqual(r.costs()[k], v)
        r.fast = True
        self.assertEqual(r.costs()["output"], 40.0)
        self.assertEqual(rep.price("claude-sonnet-4-6"), (3, 15, 0.3))
        self.assertEqual(rep.price("claude-opus-4-1"), (15, 75, 1.5))
        self.assertEqual(rep.price("something-new"), rep.DEFAULT_PRICE)


class CoworkTests(unittest.TestCase):
    def test_cowork_sessions_are_read(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"HOME": tmp, "XDG_CONFIG_HOME": str(Path(tmp) / ".config"),
                                             "CLAUDE_CONFIG_DIR": str(Path(tmp) / ".claude")}):
            root = Path(tmp) / ".config" / "Claude" / rep.COWORK
            self.assertIn(root, rep.cowork_dirs())
            # The layout as found on Linux: the session's scratch folder is
            # its cwd, so the project folder name ends in "-outputs".
            sess = root / "96fc3670" / "0baa8912" / "local_133c973b"
            cwd = str(sess / "outputs")
            lines = [user("cw", T0, "sort my inbox", cwd=cwd),
                     assistant("cw", T0 + 5, "c1", usage(cw=20000, out=300), cwd=cwd)]
            write(sess / ".claude" / "projects" / cwd.replace("/", "-").replace(".", "-") / "cw.jsonl", lines)
            sess.with_name("local_133c973b.json").write_text(json.dumps({"title": "Inbox cleanup"}))
            # the event log and the user's own files are not transcripts
            write(sess / "audit.jsonl", [assistant("cw", T0 + 6, "dup", usage(cw=99999))])
            write(sess / "uploads" / "data.jsonl", [assistant("x", T0, "u1", usage(cw=99999))])
            root.mkdir(parents=True, exist_ok=True)
            self.assertIn(root, rep.transcript_dirs())
            sessions = rep.load_sessions(T0 - 3600)
        self.assertEqual(list(sessions), ["cw"])
        self.assertEqual(sessions["cw"].project, "Cowork")
        self.assertEqual(sessions["cw"].title, "Inbox cleanup")
        self.assertEqual(len(sessions["cw"].requests), 1)
        self.assertEqual(sessions["cw"].prompts[0].text, "sort my inbox")


class WindowTests(unittest.TestCase):
    def test_five_hour_inferred_from_activity(self):
        times = [T0, T0 + HOUR, T0 + 6 * HOUR]
        wins = rep.windows_for("five_hour", times, [], T0 + 7 * HOUR)
        start = T0 - 10 * 60  # 09:00, the hour the first message fell in
        self.assertEqual(wins, [[start, start + 5 * HOUR, False], [start + 6 * HOUR, start + 11 * HOUR, False]])

    def test_five_hour_from_readings(self):
        resets = T0 + 3 * HOUR + 17
        readings = [(T0, 5.0, resets), (T0 + 60, 6.0, resets + 2)]  # the reset time wobbles a little
        wins = rep.windows_for("five_hour", [T0, T0 + 3 * HOUR + 100, T0 + 9 * HOUR], readings, T0 + 10 * HOUR)
        self.assertEqual(wins[0], [resets - 5 * HOUR, resets, True])
        # Activity right after the reset, in the same hour: the next window may
        # not overlap the measured one.
        self.assertEqual(wins[1], [resets, resets + 5 * HOUR, False])
        self.assertEqual(wins[2][0], T0 - 10 * 60 + 9 * HOUR)

    def test_weekly_repeats_from_a_known_reset(self):
        resets = T0 + 2 * 86400
        wins = rep.windows_for("seven_day", [T0 - 10 * 86400], [(T0, 3.0, resets)], T0)
        self.assertEqual([w[1] for w in wins], [resets - 7 * 86400, resets])
        self.assertEqual([w[2] for w in wins], [False, True])


class FitTests(unittest.TestCase):
    def test_recovers_rate(self):
        pts = [("w", 0.1 * i, 0.3 * i, 2.0 * 0.4 * i) for i in range(1, 30)]
        f = rep.fit(pts)
        self.assertAlmostEqual(f["k_read"], 2.0, places=6)
        self.assertEqual(f["err"], 0)
        self.assertAlmostEqual(f["explained"], 1.0)

    def test_two_weights_with_varied_data(self):
        pts = [("w", a, b, 1.0 * a + 3.0 * b) for a, b in
               [(i % 7 + 1, (i * 3) % 5 + 1) for i in range(30)]]
        f = rep.fit(pts)
        self.assertEqual(f["params"], 2)
        self.assertAlmostEqual(f["k_read"], 1.0, places=6)
        self.assertAlmostEqual(f["k_other"], 3.0, places=6)

    def test_usage_elsewhere_becomes_an_offset(self):
        # Each week 8 points came from claude.ai before local use started.
        pts = [(w, 0, c, 8.0 + 0.5 * c) for w in ("a", "b") for c in (2, 6, 10, 20, 30)]
        f = rep.fit(pts)
        self.assertAlmostEqual(f["k_other"], 0.5, places=2)
        self.assertLess(f["explained"], 0.8)

    def test_single_reading_still_calibrates(self):
        f = rep.fit([("w", 1.0, 3.0, 20.0)])
        self.assertAlmostEqual(f["k_read"], 5.0)

    def test_nothing_to_fit(self):
        self.assertIsNone(rep.fit([]))
        self.assertIsNone(rep.fit([("w", 0, 0, 30.0), ("w", 1, 1, 0.0)]))


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {
            "CLAUDE_CONFIG_DIR": str(base / "claude"), "XDG_CACHE_HOME": str(base / "cache"),
            "XDG_DATA_HOME": str(base / "data"), "HOME": str(base / "home")})
        self.env.start()
        os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        now = datetime.now(timezone.utc).timestamp()
        self.now = now
        lines = [user("s", now - 3000, "do the thing")]
        for i in range(20):
            lines.append(assistant("s", now - 2900 + 60 * i, f"m{i}", usage(cr=10000 * i, cw=5000, out=300)))
        write(base / "claude" / "projects" / "-x" / "s.jsonl", lines)
        resets = iso(int(now // HOUR) * HOUR + 2 * HOUR)
        week = iso(now + 3 * 86400)
        for i, t in enumerate(range(int(now - 2800), int(now), 600)):
            cu.append_history({"five_hour": {"utilization": 2.0 * (i + 1), "resets_at": resets},
                               "seven_day": {"utilization": 1.0 + 0.2 * i, "resets_at": week},
                               "extra_usage": {"is_enabled": False}}, t)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_report(self):
        with mock.patch("sys.stdout") as out:
            code = rep.main(["--no-fetch", "--no-open"])
        self.assertEqual(code, 0)
        printed = "".join(c.args[0] for c in out.write.call_args_list)
        self.assertIn("calibrated from", printed)
        self.assertIn("do the thing", printed)
        page = (cu.cache_path().with_name("report.html")).read_text()
        self.assertNotIn("/*__DATA__*/", page)
        blob = page.split("const D = ", 1)[1].split(";\nconst NS", 1)[0]
        data = json.loads(blob)
        self.assertEqual(len(data["sessions"]), 1)
        self.assertEqual(len(data["req"]), 20)
        self.assertEqual(len(data["req"][0]), 8)
        self.assertIsNotNone(data["fit"]["five_hour"])
        self.assertTrue(any(w[2] for w in data["windows"]["five_hour"]))

    def test_reading_cached_by_the_top_bar_is_used(self):
        # An older top bar cached a fresh reading without logging it.
        cu.history_path().unlink()
        week = iso(self.now + 3 * 86400)
        cache = cu.cache_path()
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"fetched_at": self.now - 60, "data": {
            "five_hour": {"utilization": 0.0, "resets_at": None},
            "seven_day": {"utilization": 12.0, "resets_at": week}}}))
        with mock.patch("sys.stdout"), mock.patch.object(cu, "get_usage") as fetch:
            self.assertEqual(rep.main(["--no-open"]), 0)
            self.assertEqual(rep.main(["--no-open"]), 0)  # logged once, not twice
        fetch.assert_not_called()
        self.assertEqual(len(cu.history_path().read_text().splitlines()), 1)
        data = json.loads(cu.cache_path().with_name("report.html").read_text()
                          .split("const D = ", 1)[1].split(";\nconst NS", 1)[0])
        self.assertIsNotNone(data["fit"]["seven_day"])
        self.assertIsNone(data["fit"]["five_hour"])
        self.assertIn("No usable reading yet", data["notes"]["five_hour"])

    def test_fetch_problem_is_shown_on_the_page(self):
        with mock.patch("sys.stdout"), \
                mock.patch.object(cu, "get_usage", side_effect=cu.UsageError("HTTP 429: rate limited")):
            self.assertEqual(rep.main(["--no-open"]), 0)
        page = cu.cache_path().with_name("report.html").read_text()
        self.assertIn('"warning":"HTTP 429: rate limited"', page)

    def test_dispatch_from_claude_usage(self):
        with mock.patch.object(rep, "main", return_value=0) as m:
            self.assertEqual(cu.main(["report", "--days", "3"]), 0)
        m.assert_called_once_with(["--days", "3"])

    def test_no_transcripts(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(Path(self.tmp.name) / "none")}), \
                mock.patch("sys.stderr") as err:
            self.assertEqual(rep.main(["--no-fetch", "--no-open"]), 1)
        self.assertIn("no Claude Code transcripts", "".join(c.args[0] for c in err.write.call_args_list))


if __name__ == "__main__":
    unittest.main()
