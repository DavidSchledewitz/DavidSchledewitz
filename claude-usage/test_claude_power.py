"""Run with: python -m unittest -v test_claude_power"""

import os
import tempfile
import time
import unittest
from unittest import mock
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
    def test_typical_values_without_battery_samples(self):
        m = cp.fit_model([rec(0, 10)])
        self.assertFalse(m["fitted"])
        self.assertAlmostEqual(m["coef"]["screen"], cp.TERMS["screen"][0], places=3)

    def test_fit_recovers_terms(self):
        recs = []
        for k in range(200):
            cpu, br, gpu = 0.05 * (k % 10), 0.2 + 0.1 * (k % 7), 0.1 * (k % 5)
            r = rec(k, busy=240 * cpu, w=None)
            r.update(br=br, gpu=gpu, net=0.0, w=2.5 + 6 * br + 20 * cpu + 5 * gpu)
            recs.append(r)
        c = cp.fit_model(recs)["coef"]
        self.assertAlmostEqual(c["idle"], 2.5, delta=0.2)
        self.assertAlmostEqual(c["screen"], 6, delta=0.3)
        self.assertAlmostEqual(c["cpu"], 20, delta=0.5)
        self.assertAlmostEqual(c["gpu"], 5, delta=0.3)

    def test_term_the_data_cant_see_stays_typical(self):
        recs = [dict(rec(k, busy=240 * 0.05 * (k % 10), w=4 + 20 * 0.05 * (k % 10)), br=0.5) for k in range(100)]
        c = cp.fit_model(recs)["coef"]
        self.assertEqual(c["gpu"], round(cp.TERMS["gpu"][0], 3))  # GPU never logged: prior

    def test_battery_split_adds_up(self):
        r = dict(rec(0, 60, w=12, groups={"Chrome": 45, cp.CLAUDE_CODE: 15}), br=0.5, gpu=0.2, net=6,
                 gg={"Chrome": 0.5, cp.GPU_OTHER: 0.0})
        model = cp.fit_model([])
        w = cp.watts_by_group(r, model)
        self.assertAlmostEqual(sum(w.values()), 12)
        self.assertGreater(w[cp.SCREEN], 0)
        self.assertGreater(w[cp.WIFI], 0)
        t = cp.term_watts(r, model)
        self.assertAlmostEqual(w[cp.CLAUDE_CODE], t["cpu"] / 4)
        self.assertAlmostEqual(w["Chrome"], 3 * t["cpu"] / 4 + t["gpu"])

    def test_old_samples_without_new_sensors(self):
        w = cp.watts_by_group(rec(0, 120, groups={"Chrome": 120}), cp.fit_model([]))
        self.assertAlmostEqual(w["Chrome"], cp.TERMS["cpu"][0] / 2, places=1)  # half the CPUs busy
        self.assertEqual(w[cp.SCREEN], 0)


class BuildTests(unittest.TestCase):
    def test_bins_and_claude_always_listed(self):
        recs = [rec(900 * 10 + 60 * i, 30, groups={**{f"app{k}": 2 for k in range(20)}, cp.CLAUDE_TOOLS: 0.01})
                for i in range(30)]
        d = cp.build(0, 10 ** 6, recs, events=[])
        self.assertIn(cp.CLAUDE_TOOLS, d["groups"])
        self.assertEqual(d["groups"][:3], list(cp.BASE_GROUPS))
        self.assertEqual(d["groups"][-1], cp.OTHER)
        self.assertEqual(len(d["bins"]), 2)
        total_wh = sum(sum(b[4]) for b in d["bins"])
        self.assertAlmostEqual(total_wh, sum(sum(cp.watts_by_group(r, d["model"]).values()) * 60 for r in recs) / 3600, places=2)
        self.assertIsNone(cp.build(0, 1, []))


class OutlookTests(unittest.TestCase):
    def test_scenarios(self):
        model = cp.fit_model([])
        recs = [rec(1000 + 60 * i, 120, w=10.0) for i in range(10)]
        o = cp.battery_outlook(recs, model, 1600, bat=(50.0, 25.0), bl=("b", 500, 1000), status="Discharging")
        self.assertEqual(o["pct"], 50.0)
        by = {s["key"]: s for s in o["scenarios"]}
        self.assertAlmostEqual(by["now"]["h"], 2.5)  # 25 Wh at the measured 10 W
        self.assertNotIn("typical", by)  # fewer than 30 minutes on battery
        idle = model["coef"]["idle"] + model["coef"]["screen"] * 0.5
        self.assertAlmostEqual(by["idle"]["w"], idle, places=2)
        self.assertGreater(by["idle_dim"]["h"], by["idle"]["h"])
        self.assertIsNone(cp.battery_outlook(recs, model, 1600, bat=False))


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.bl = ["intel_backlight", 800, 1000]
        self.sent, self.idle = [], 1000
        self.events = mock.patch.object(cp, "log_event")
        self.events.start()

        def set_bl(dev, v):
            self.bl[1] = v
            return True
        self.dog = cp.Watchdog(notify_fn=lambda t, b: self.sent.append(t + " | " + b),
                               brightness_fn=lambda: tuple(self.bl), set_fn=set_bl, idle_fn=lambda: self.idle)
        self.cfg = dict(cp.DEFAULT_CONFIG)
        self.model = cp.fit_model([])

    def tearDown(self):
        self.events.stop()

    def run_samples(self, watts, start=0, step=60, **extra):
        for i, w in enumerate(watts):
            r = dict(rec(start + i * step, 60, w=w), br=self.bl[1] / self.bl[2], **extra)
            self.dog.check(r, self.model, self.cfg)

    def test_sustained_high_draw_alerts_and_dims(self):
        self.run_samples([20, 20])
        self.assertEqual(self.sent, [])  # not sustained yet
        self.run_samples([20], start=120)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("20 W", self.sent[0])
        self.assertEqual(self.bl[1], 400)  # halved
        self.run_samples([20] * 5, start=180)
        self.assertEqual(self.bl[1], 400)  # next step only after DIM_EVERY

    def test_spike_is_ignored(self):
        self.run_samples([20, 8, 20, 8])
        self.assertEqual(self.sent, [])

    def test_manual_change_stops_dimming_and_restore(self):
        self.run_samples([20, 20, 20])
        self.bl[1] = 900  # you turned it back up
        self.run_samples([20] * 3, start=2000)
        self.assertTrue(self.dog.manual)
        self.assertEqual(self.bl[1], 900)
        self.dog.check(dict(rec(5000, 10), ac=True), self.model, self.cfg)  # plugged in
        self.assertEqual(self.bl[1], 900)  # yours, not restored

    def test_idle_dim_by_gnome_is_not_a_manual_change(self):
        self.run_samples([20, 20, 20])
        self.idle, self.bl[1] = 60_000, 300  # GNOME dims an idle screen
        self.run_samples([8], start=300)
        self.assertFalse(self.dog.manual)

    def test_plugging_in_restores(self):
        self.run_samples([20, 20, 20])
        self.dog.check(dict(rec(400, 10), ac=True), self.model, self.cfg)
        self.assertEqual(self.bl[1], 800)

    def test_floor_and_off_switch(self):
        self.bl[1] = 210
        self.run_samples([20, 20, 20])
        self.assertEqual(self.bl[1], 200)  # dim_floor 20%
        self.cfg["dim"] = False
        self.dog.last_dim = -10 ** 9
        self.bl[1] = 800
        self.run_samples([20, 20, 20], start=5000)
        self.assertEqual(self.bl[1], 800)

    def test_threshold(self):
        self.assertEqual(self.dog.threshold(self.cfg), 15)
        self.dog.learn([{"w": 8}] * 200)
        self.assertAlmostEqual(self.dog.threshold(self.cfg), 12.8)
        self.assertEqual(self.dog.threshold({**self.cfg, "alert_watts": 10}), 10)


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
