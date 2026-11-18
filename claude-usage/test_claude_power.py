"""Run with: python -m unittest -v test_claude_power"""

import os
import tempfile
import time
import unittest
from pathlib import Path

import claude_power as cp


def rec(t, busy, w=None, groups=None, cap=60 * 4, dt=60):
    return {"t": t, "dt": dt, "cap": cap, "busy": busy, "w": w, "ac": w is None, "g": groups or {"Chrome": busy}}


class NamingTests(unittest.TestCase):
    def test_app_names(self):
        self.assertEqual(cp.app_name("claude", "/home/me/.local/bin/claude"), cp.CLAUDE_CODE)
        self.assertEqual(cp.app_name("claude-desktop", "/usr/lib/claude-desktop/claude-desktop --type=renderer"),
                         cp.CLAUDE_APP)
        self.assertEqual(cp.app_name("chrome", "/opt/google/chrome/chrome"), "Chrome")
        self.assertEqual(cp.app_name("python3", "python3 /x/analyse.py"), "Python: analyse.py")
        self.assertEqual(cp.app_name("someproc", ""), "someproc")


class ModelTests(unittest.TestCase):
    def test_defaults_without_battery_samples(self):
        m = cp.fit_model([rec(0, 10)])
        self.assertFalse(m["fitted"])
        self.assertEqual(m["base"], cp.DEFAULT_MODEL["base"])

    def test_fit_recovers_line(self):
        recs = [rec(i, busy=240 * f, w=4 + 20 * f) for i, f in enumerate([0.05 * (k % 10) for k in range(40)])]
        m = cp.fit_model(recs)
        self.assertTrue(m["fitted"])
        self.assertAlmostEqual(m["base"], 4, places=1)
        self.assertAlmostEqual(m["per_cpu"], 20, places=1)

    def test_battery_split_adds_up(self):
        w = cp.watts_by_group(rec(0, 60, w=12, groups={"Chrome": 45, cp.CLAUDE_CODE: 15}), dict(cp.DEFAULT_MODEL))
        self.assertAlmostEqual(sum(w.values()), 12)
        self.assertAlmostEqual(w["Chrome"], 3 * w[cp.CLAUDE_CODE])
        self.assertEqual(w[cp.BASELINE], cp.DEFAULT_MODEL["base"])

    def test_ac_uses_model(self):
        w = cp.watts_by_group(rec(0, 120, groups={"Chrome": 120}), {"base": 5, "per_cpu": 30})
        self.assertAlmostEqual(w["Chrome"], 15)  # half the CPUs busy


class BuildTests(unittest.TestCase):
    def test_bins_and_claude_always_listed(self):
        recs = [rec(900 * 10 + 60 * i, 30, groups={**{f"app{k}": 2 for k in range(20)}, cp.CLAUDE_TOOLS: 0.01})
                for i in range(30)]
        d = cp.build(0, 10 ** 6, recs)
        self.assertIn(cp.CLAUDE_TOOLS, d["groups"])
        self.assertEqual(d["groups"][0], cp.BASELINE)
        self.assertEqual(d["groups"][-1], cp.OTHER)
        self.assertEqual(len(d["bins"]), 2)
        total_wh = sum(sum(b[4]) for b in d["bins"])
        self.assertAlmostEqual(total_wh, sum(sum(cp.watts_by_group(r, d["model"]).values()) * 60 for r in recs) / 3600, places=2)
        self.assertIsNone(cp.build(0, 1, []))


class SamplerTests(unittest.TestCase):
    def test_live_sample(self):
        s = cp.Sampler()
        self.assertIsNone(s.sample())
        end = time.time() + 0.3
        while time.time() < end:  # burn some CPU so this process shows up
            pass
        r = s.sample()
        self.assertGreater(r["cap"], 0)
        self.assertGreater(sum(r["g"].values()), 0)

    def test_log_roundtrip_and_prune(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "power.jsonl"
            now = time.time()
            cp.append(rec(now - 50 * 86400, 1), path)
            cp.append(rec(now, 1), path)
            self.assertEqual(len(cp.load(0, path)), 2)
            cp.prune(now, path)
            self.assertEqual(len(cp.load(0, path)), 1)


if __name__ == "__main__":
    unittest.main()
