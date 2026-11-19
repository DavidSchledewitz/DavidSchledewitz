#!/usr/bin/env python3
"""claude-usage power: what uses your laptop's power, per app, Claude included.

`claude-usage power log` samples once a minute and appends one line to
power.jsonl next to the usage history; the report turns them into a Power
tab. A sample holds:

- the CPU time every process used since the last sample, grouped into apps
- the GPU time of every graphics client (from /proc/PID/fdinfo) and how busy
  the GPU was overall (from its idle-state counters)
- the screen's backlight level and the Wi-Fi traffic
- the battery's power draw while it's discharging

Linux doesn't measure power per part or per process, so it's modelled:

    watts = idle + screen * brightness + cpu * busy CPUs + gpu * busy GPU + wifi * MB/s

The coefficients are fitted on the samples taken on battery, where the total
is measured, pulled towards typical laptop values while there's too little
data (a ridge fit). On battery the parts are scaled to add up to the measured
draw. The CPU part is split among apps by CPU time, the GPU part by GPU time.

Claude gets three groups: Claude Code itself, the desktop app, and the
commands and tools Claude runs (shells, scripts, MCP servers), found by
walking each process's parents.

While logging it also watches the battery: if the laptop draws a lot more
than usual for a few minutes, it sends a notification naming the biggest
users and, unless you've changed the brightness yourself, dims the screen a
step. Plugging in restores it. `claude-usage power config` turns either off.
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

INTERVAL = 60  # seconds between samples
KEEP_DAYS = 40
TOP_GROUPS = 15  # per sample; the rest is merged into OTHER
DRM_RESCAN = 10  # samples between full looks for processes' GPU file handles

CLK = os.sysconf("SC_CLK_TCK")
NCPU = os.cpu_count() or 1

CLAUDE_CODE = "Claude Code"
CLAUDE_APP = "Claude desktop app"
CLAUDE_TOOLS = "Claude: commands & tools"
CLAUDE_GROUPS = (CLAUDE_CODE, CLAUDE_APP, CLAUDE_TOOLS)
KERNEL = "Kernel"
OTHER = "Other"
UNSEEN = "Short-lived processes"
IDLE = "Idle: memory, chipset, SSD"
SCREEN = "Screen"
WIFI = "Wi-Fi"
GPU_OTHER = "Graphics: other"
BASE_GROUPS = (IDLE, SCREEN, WIFI)  # the parts no app is charged for

# The model's terms, what each multiplies, and typical values for a 14"
# laptop with a 15 W CPU: (prior watts at 1.0, prior spread).
TERMS = {
    "idle": (3.0, 3.0),     # always on
    "screen": (4.0, 3.0),   # at full brightness
    "cpu": (30.0, 20.0),    # all CPUs busy
    "gpu": (8.0, 8.0),      # GPU busy all the time
    "wifi": (1.0, 2.0),     # per MB/s of traffic
}
MIN_FIT_SAMPLES = 30
RIDGE = 1.0  # how strongly the typical values hold when the data says little

# comm (or start of it) -> app name
NAMES = {
    "chrome": "Chrome", "chromium": "Chromium", "firefox": "Firefox", "Isolated Web Co": "Firefox",
    "code": "VS Code", "gnome-shell": "GNOME Shell (desktop)", "Xwayland": "GNOME Shell (desktop)",
    "pipewire": "Audio", "wireplumber": "Audio", "pulseaudio": "Audio",
    "tracker-miner-f": "File indexer", "localsearch": "File indexer",
    "nordvpnd": "NordVPN", "nordvpn": "NordVPN", "gnome-terminal-": "Terminal", "ptyxis": "Terminal",
    "nautilus": "Files", "spotify": "Spotify", "slack": "Slack", "thunderbird": "Thunderbird",
    "kicad": "KiCad", "openscad": "OpenSCAD", "root.exe": "ROOT", "evince": "Document viewer",
}


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "claude-usage"


def log_path() -> Path:
    return data_dir() / "power.jsonl"


def events_path() -> Path:
    return data_dir() / "power-events.jsonl"


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "claude-usage" / "power.json"


DEFAULT_CONFIG = {
    "alert": True,        # notify when the draw on battery is unusually high
    "dim": True,          # and dim the screen a step
    "alert_watts": None,  # fixed threshold; None = learned from your battery samples
    "dim_floor": 0.2,     # never dim below this fraction of full brightness
}


def load_config() -> dict:
    try:
        cfg = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {}
    return {**DEFAULT_CONFIG, **{k: v for k, v in cfg.items() if k in DEFAULT_CONFIG}}


# --------------------------------------------------------------------------
# reading /proc and /sys


def read_stat(pid: str) -> tuple | None:
    """(comm, ppid, own ticks, reaped-children ticks, start ticks)"""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            raw = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    lp, rp = raw.find("("), raw.rfind(")")
    comm = raw[lp + 1:rp]
    f = raw[rp + 2:].split()
    try:
        return comm, int(f[1]), int(f[11]) + int(f[12]), int(f[13]) + int(f[14]), int(f[19])
    except (IndexError, ValueError):
        return None


def read_exe(pid: str) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            args = f.read().split(b"\0")
    except OSError:
        return ""
    return " ".join(a.decode("utf-8", "replace") for a in args[:2])


def drm_fds(pid: str) -> list[str]:
    """File descriptors of a process that are open on a GPU (only readable
    for your own processes)."""
    out = []
    try:
        for fd in os.listdir(f"/proc/{pid}/fd"):
            try:
                if os.readlink(f"/proc/{pid}/fd/{fd}").startswith("/dev/dri/"):
                    out.append(fd)
            except OSError:
                continue
    except OSError:
        pass
    return out


def gpu_clients(pid: str, fds: list[str]) -> dict[str, int]:
    """drm client id -> nanoseconds of GPU engine time so far."""
    out = {}
    for fd in fds:
        try:
            with open(f"/proc/{pid}/fdinfo/{fd}") as f:
                text = f.read()
        except OSError:
            continue
        client, ns = None, 0
        for line in text.splitlines():
            if line.startswith("drm-client-id:"):
                client = line.split(":", 1)[1].strip()
            elif line.startswith("drm-engine-") and not line.startswith("drm-engine-capacity") and line.endswith(" ns"):
                try:
                    ns += int(line.split(":", 1)[1].split()[0])
                except ValueError:
                    pass
        if client is not None:
            out[client] = max(out.get(client, 0), ns)
    return out


def cpu_ticks() -> tuple[int, int]:
    """(busy, total) jiffies summed over all CPUs."""
    with open("/proc/stat") as f:
        vals = [int(v) for v in f.readline().split()[1:9]]
    total = sum(vals)
    return total - vals[3] - vals[4], total  # minus idle and iowait


def uptime_ticks() -> float:
    with open("/proc/uptime") as f:
        return float(f.read().split()[0]) * CLK


def _read(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def backlight() -> tuple[str, int, int] | None:
    """(device, brightness, max) of the built-in screen's backlight."""
    for d in sorted(glob.glob("/sys/class/backlight/*")):
        cur, mx = _read(d + "/actual_brightness") or _read(d + "/brightness"), _read(d + "/max_brightness")
        try:
            if cur is not None and mx and int(mx) > 0:
                return os.path.basename(d), int(cur), int(mx)
        except ValueError:
            continue
    return None


def gpu_idle_ms() -> list[int]:
    """Time each GPU tile has spent in its deepest idle state (RC6), in ms."""
    paths = sorted(glob.glob("/sys/class/drm/card*/gt/gt*/rc6_residency_ms")) or \
        sorted(glob.glob("/sys/class/drm/card*/power/rc6_residency_ms"))
    out = []
    for p in paths:
        v = _read(p)
        if v is not None and v.isdigit():
            out.append(int(v))
    return out


def wifi_bytes() -> int | None:
    total, found = 0, False
    for d in glob.glob("/sys/class/net/*"):
        if not os.path.isdir(d + "/wireless") and not os.path.exists(d + "/phy80211"):
            continue
        for k in ("rx_bytes", "tx_bytes"):
            v = _read(f"{d}/statistics/{k}")
            if v and v.isdigit():
                total += int(v)
                found = True
    return total if found else None


def external_displays() -> int:
    n = 0
    for d in glob.glob("/sys/class/drm/card*-*"):
        if "eDP" in d or "LVDS" in d or "DSI" in d:
            continue
        if _read(d + "/status") == "connected":
            n += 1
    return n


def battery() -> tuple[float | None, bool]:
    """(watts drawn from the battery or None, on AC power)"""
    root = Path("/sys/class/power_supply")
    on_ac, watts = False, None
    try:
        supplies = list(root.iterdir())
    except OSError:
        return None, False
    for d in supplies:
        try:
            kind = (d / "type").read_text().strip()
            if kind == "Mains" and (d / "online").read_text().strip() == "1":
                on_ac = True
            if kind != "Battery" or (d / "status").read_text().strip() != "Discharging":
                continue
            if (d / "power_now").exists():
                w = int((d / "power_now").read_text()) / 1e6
            else:
                w = int((d / "current_now").read_text()) * int((d / "voltage_now").read_text()) / 1e12
            if w > 0:
                watts = (watts or 0) + w
        except (OSError, ValueError):
            continue
    return watts, on_ac


def battery_wh() -> tuple[float, float] | None:
    """(full capacity, energy left) in Wh, summed over batteries."""
    full = now = 0.0
    for d in glob.glob("/sys/class/power_supply/*"):
        if _read(d + "/type") != "Battery":
            continue
        ef, en = _read(d + "/energy_full"), _read(d + "/energy_now")
        if ef and en and ef.isdigit() and en.isdigit():
            full, now = full + int(ef) / 1e6, now + int(en) / 1e6
            continue
        cf, cn, v = _read(d + "/charge_full"), _read(d + "/charge_now"), _read(d + "/voltage_min_design")
        if cf and cn and v and cf.isdigit() and cn.isdigit() and v.isdigit():
            full, now = full + int(cf) * int(v) / 1e12, now + int(cn) * int(v) / 1e12
    return (full, now) if full > 0 else None


def pct_per_hour(watts: float, full_wh: float | None) -> float | None:
    """How fast a draw empties the battery, in % of a full charge per hour."""
    return None if not full_wh else watts / full_wh * 100


def fmt_pct_h(r: float) -> str:
    return f"{r:.0f}%/h" if r >= 10 else f"{r:.1f}%/h"


def fmt_rate(watts: float, full_wh: float | None) -> str:
    r = pct_per_hour(watts, full_wh)
    return f"{watts:.1f} W" if r is None else f"{watts:.1f} W · {fmt_pct_h(r)}"


def app_name(comm: str, exe: str) -> str:
    if "claude-code" in exe or comm == "claude":
        return CLAUDE_CODE
    if comm.startswith("claude-desktop") or "/claude-desktop" in exe:
        return CLAUDE_APP
    if comm in ("python3", "python") or comm.startswith("python3."):
        script = next((a for a in exe.split()[1:] if a.endswith(".py")), None)
        return f"Python: {Path(script).name}" if script else "Python"
    for key, name in NAMES.items():
        if comm == key or comm.startswith(key):
            return name
    return comm


def _top(secs: dict, n: int = TOP_GROUPS) -> dict:
    ranked = sorted(secs.items(), key=lambda kv: -kv[1])
    out = {k: round(v, 3) for k, v in ranked[:n] if v >= 0.005}
    rest = sum(v for _, v in ranked[n:])
    if rest >= 0.005:
        out[OTHER] = round(out.get(OTHER, 0) + rest, 3)
    return out


class Sampler:
    """Turns two looks at /proc and /sys into one sample."""

    def __init__(self):
        self.prev: dict[str, tuple] = {}  # pid -> (start, own ticks, children ticks, ppid, group)
        self.prev_cpu = None
        self.prev_t = None
        self.prev_up = None
        self.prev_rc6 = None
        self.prev_net = None
        self.prev_gpu: dict[tuple, int] = {}  # (pid, start, client) -> ns
        self.exe: dict[tuple, str] = {}
        self.fds: dict[tuple, list] = {}  # (pid, start) -> GPU fds
        self.n = 0

    def _groups(self, procs: dict) -> dict[str, str]:
        base, out = {}, {}
        for pid, (comm, ppid, _, _, start) in procs.items():
            if ppid == 2 or pid == "2":
                base[pid] = KERNEL
                continue
            key = (pid, start)
            if key not in self.exe:
                self.exe[key] = read_exe(pid)
            base[pid] = app_name(comm, self.exe[key])

        def claude_parent(pid: str) -> str | None:
            seen = 0
            p = str(procs[pid][1])
            while p in procs and seen < 64:
                if base[p] in (CLAUDE_CODE, CLAUDE_APP):
                    return base[p]
                p = str(procs[p][1])
                seen += 1
            return None

        for pid in procs:
            g = base[pid]
            if g not in (CLAUDE_CODE, CLAUDE_APP, KERNEL) and claude_parent(pid):
                g = CLAUDE_TOOLS
            out[pid] = g
        return out

    def _gpu(self, procs: dict, groups: dict) -> tuple[dict, dict]:
        """GPU seconds per group since last time, and the new counters."""
        rescan = self.n % DRM_RESCAN == 0
        uid = os.getuid()
        now_counts, secs = {}, defaultdict(float)
        live_fds = {}
        for pid, st in procs.items():
            key = (pid, st[4])
            fds = self.fds.get(key)
            if fds is None or rescan:
                if groups[pid] == KERNEL:
                    continue
                try:
                    if os.stat(f"/proc/{pid}").st_uid != uid:
                        continue
                except OSError:
                    continue
                fds = drm_fds(pid)
            live_fds[key] = fds
            if not fds:
                continue
            for client, ns in gpu_clients(pid, fds).items():
                ck = (pid, st[4], client)
                now_counts[ck] = ns
                old = self.prev_gpu.get(ck)
                if old is not None and ns >= old:
                    secs[groups[pid]] += (ns - old) / 1e9
        self.fds = live_fds
        return secs, now_counts

    def sample(self) -> dict | None:
        now, up = time.time(), uptime_ticks()
        busy, total = cpu_ticks()
        rc6, net = gpu_idle_ms(), wifi_bytes()
        procs = {}
        for pid in os.listdir("/proc"):
            if pid.isdigit():
                st = read_stat(pid)
                if st:
                    procs[pid] = st
        groups = self._groups(procs)
        gsecs, gcounts = self._gpu(procs, groups)
        cur = {pid: (st[4], st[2], st[3], str(st[1]), groups[pid]) for pid, st in procs.items()}
        rec = None
        if self.prev_cpu is not None:
            secs = defaultdict(float)
            gone = defaultdict(int)  # ppid -> ticks of children that ended
            for pid, (start, own, _, ppid, _) in self.prev.items():
                if pid not in cur or cur[pid][0] != start:
                    gone[ppid] += own
            for pid, (start, own, kids, ppid, g) in cur.items():
                old = self.prev.get(pid)
                if old and old[0] == start:
                    d_own, d_kids = own - old[1], kids - old[2] - gone.get(pid, 0)
                elif start >= self.prev_up:
                    d_own, d_kids = own, kids
                else:
                    continue  # first look at an old process
                if d_own > 0:
                    secs[g] += d_own / CLK
                if d_kids > 0:  # children that started and ended between samples
                    secs[CLAUDE_TOOLS if g in (CLAUDE_CODE, CLAUDE_APP) else g] += d_kids / CLK
            dt = now - self.prev_t
            busy_s = (busy - self.prev_cpu[0]) / CLK
            seen = sum(secs.values())
            if busy_s > seen + 0.05:
                secs[UNSEEN] += busy_s - seen
            gpu = None
            if rc6 and self.prev_rc6 and len(rc6) == len(self.prev_rc6) and dt > 0:
                fr = [min(1.0, max(0.0, 1 - (a - b) / (dt * 1000))) for a, b in zip(rc6, self.prev_rc6)]
                gpu = sum(fr) / len(fr)
            mb = None if net is None or self.prev_net is None else max(0, net - self.prev_net) / 1e6
            bl = backlight()
            watts, on_ac = battery()
            bat = battery_wh()
            rec = {"t": round(now, 1), "dt": round(dt, 1), "cap": round(dt * NCPU, 1),
                   "busy": round(max(0.0, busy_s), 2), "w": None if watts is None else round(watts, 2),
                   "ac": on_ac, "g": _top(secs),
                   "br": None if bl is None else round(bl[1] / bl[2], 3),
                   "gpu": None if gpu is None else round(gpu, 3),
                   "net": None if mb is None else round(mb, 2),
                   "gg": _top(gsecs, 8), "dp": external_displays(),
                   "bat": None if bat is None else round(100 * bat[1] / bat[0], 1)}
        self.prev, self.prev_cpu, self.prev_t, self.prev_up = cur, (busy, total), now, up
        self.prev_rc6, self.prev_net, self.prev_gpu = rc6, net, gcounts
        live = {(pid, st[4]) for pid, st in procs.items()}
        self.exe = {k: v for k, v in self.exe.items() if k in live}
        self.n += 1
        return rec


# --------------------------------------------------------------------------
# log file


def append(rec: dict, path: Path | None = None) -> None:
    path = path or log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, separators=(",", ":")) + "\n")


def prune(now: float, path: Path | None = None) -> None:
    path = path or log_path()
    try:
        with open(path, encoding="utf-8") as f:
            first = json.loads(f.readline())["t"]
        if first >= now - (KEEP_DAYS + 2) * 86400:
            return
        cutoff = now - KEEP_DAYS * 86400
        keep = [ln for ln in path.read_text(encoding="utf-8").splitlines()
                if ln.strip() and json.loads(ln).get("t", 0) >= cutoff]
        tmp = path.with_suffix(".tmp")
        tmp.write_text("".join(ln + "\n" for ln in keep), encoding="utf-8")
        tmp.replace(path)
    except (OSError, ValueError, KeyError, TypeError):
        pass


def load(since: float, path: Path | None = None, key: str = "cap") -> list[dict]:
    path = path or log_path()
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("t", 0) >= since and rec.get(key) is not None:
                    out.append(rec)
    except OSError:
        pass
    return out


# --------------------------------------------------------------------------
# the model


def features(rec: dict) -> dict[str, float]:
    """What each term multiplies. Samples from before a sensor was logged
    count it as zero; the idle term absorbs it."""
    dt = rec.get("dt") or 60
    return {
        "idle": 1.0,
        "screen": rec.get("br") or 0.0,
        "cpu": rec["busy"] / rec["cap"] if rec.get("cap") else 0.0,
        "gpu": rec.get("gpu") or 0.0,
        "wifi": (rec.get("net") or 0.0) / dt,
    }


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[p] = m[p], m[c]
        if abs(m[c][c]) < 1e-12:
            continue
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                m[r] = [x - f * y for x, y in zip(m[r], m[c])]
    return [m[i][n] / m[i][i] if abs(m[i][i]) > 1e-12 else 0.0 for i in range(n)]


def fit_model(records: list[dict]) -> dict:
    """Watts per term, a ridge fit on the samples where the battery measured
    the draw: every term is pulled towards its typical value, so a term the
    data can't pin down (brightness never changed, say) stays sensible."""
    keys = list(TERMS)
    pts = [(features(r), r["w"]) for r in records if r.get("w") and r.get("cap")]
    a = [[0.0] * len(keys) for _ in keys]
    b = [0.0] * len(keys)
    for x, y in pts:
        v = [x[k] for k in keys]
        for i in range(len(keys)):
            b[i] += v[i] * y
            for j in range(len(keys)):
                a[i][j] += v[i] * v[j]
    for i, k in enumerate(keys):
        prior, spread = TERMS[k]
        a[i][i] += RIDGE / spread ** 2
        b[i] += RIDGE * prior / spread ** 2
    coef = dict(zip(keys, _solve(a, b)))
    coef["idle"] = max(0.3, coef["idle"])
    for k in keys[1:]:
        coef[k] = max(0.0, coef[k])
    err = None
    if pts:
        err = (sum((y - sum(coef[k] * x[k] for k in keys)) ** 2 for x, y in pts) / len(pts)) ** 0.5
    return {"coef": {k: round(v, 3) for k, v in coef.items()}, "n": len(pts),
            "fitted": len(pts) >= MIN_FIT_SAMPLES, "err": None if err is None else round(err, 2)}


def term_watts(rec: dict, model: dict) -> dict[str, float]:
    """The model's watts per term for one sample; on battery scaled to add
    up to the measured draw."""
    x = features(rec)
    t = {k: model["coef"][k] * x[k] for k in TERMS}
    total = sum(t.values())
    if rec.get("w") is not None and total > 0:
        t = {k: v * rec["w"] / total for k, v in t.items()}
    return t


def watts_by_group(rec: dict, model: dict) -> dict[str, float]:
    """One sample as average watts per group, base parts included."""
    t = term_watts(rec, model)
    out = {IDLE: t["idle"], SCREEN: t["screen"], WIFI: t["wifi"]}
    cpu_s = sum(rec["g"].values())
    if cpu_s > 0:
        for g, s in rec["g"].items():
            out[g] = out.get(g, 0) + t["cpu"] * s / cpu_s
    elif t["cpu"] > 0:
        out[UNSEEN] = t["cpu"]
    gg = rec.get("gg") or {}
    gpu_s = sum(gg.values())
    if gpu_s > 0:
        for g, s in gg.items():
            out[g] = out.get(g, 0) + t["gpu"] * s / gpu_s
    elif t["gpu"] > 0:
        out[GPU_OTHER] = out.get(GPU_OTHER, 0) + t["gpu"]
    return {g: w for g, w in out.items() if w > 0 or g in BASE_GROUPS}


# --------------------------------------------------------------------------
# watching the battery


def notify(title: str, body: str) -> None:
    try:
        subprocess.run(["notify-send", "-a", "Claude usage", "-i", "battery-caution", title, body],
                       timeout=5, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        pass


def idle_ms() -> int | None:
    """How long you've not touched keyboard or mouse (GNOME)."""
    try:
        out = subprocess.run(["gdbus", "call", "--session", "--dest", "org.gnome.Mutter.IdleMonitor",
                              "--object-path", "/org/gnome/Mutter/IdleMonitor/Core",
                              "--method", "org.gnome.Mutter.IdleMonitor.GetIdletime"],
                             timeout=5, capture_output=True, text=True).stdout
        return int(out.split("uint64", 1)[1].split(",")[0].strip())
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        return None


def set_brightness(device: str, value: int) -> bool:
    """Through logind, which lets the person at the screen set it without root."""
    try:
        sid = subprocess.run(["loginctl", "show-user", str(os.getuid()), "-p", "Display", "--value"],
                             timeout=5, capture_output=True, text=True).stdout.strip()
        if not sid:
            return False
        path = subprocess.run(["gdbus", "call", "--system", "--dest", "org.freedesktop.login1",
                               "--object-path", "/org/freedesktop/login1",
                               "--method", "org.freedesktop.login1.Manager.GetSession", sid],
                              timeout=5, capture_output=True, text=True).stdout.split("'")[1]
        r = subprocess.run(["gdbus", "call", "--system", "--dest", "org.freedesktop.login1",
                            "--object-path", path, "--method", "org.freedesktop.login1.Session.SetBrightness",
                            "backlight", device, str(int(value))], timeout=5, capture_output=True)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError, IndexError):
        return False


def log_event(kind: str, now: float, **info) -> None:
    try:
        append({"t": round(now, 1), "ev": kind, **info}, events_path())
    except OSError:
        pass


class Watchdog:
    """Flags unusually high draw on battery and dims the screen a step.

    High: above the threshold for SUSTAIN samples in a row. The threshold is
    the configured watts, else 1.6x your median on battery (at least 12 W),
    else 15 W. Dimming halves the brightness at most every DIM_EVERY seconds, never
    below dim_floor, and only while you're at the computer (GNOME's own idle
    dimming would otherwise look like a change you made). If the brightness
    differs from what was set last, you changed it: dimming stops until the
    laptop is next plugged in. Plugging in restores the brightness from
    before, unless you changed it."""

    SUSTAIN = 3
    ALERT_EVERY = 30 * 60
    DIM_EVERY = 10 * 60
    DIM_STEP = 0.5

    def __init__(self, notify_fn=notify, brightness_fn=backlight, set_fn=set_brightness, idle_fn=idle_ms,
                 battery_fn=battery_wh):
        self.notify, self.get_bl, self.set_bl, self.idle, self.get_bat = notify_fn, brightness_fn, set_fn, idle_fn, battery_fn
        self.high = 0
        self.last_alert = -math.inf
        self.last_dim = -math.inf
        self.original = None  # brightness before the first dim since unplugging
        self.last_set = None
        self.manual = False
        self.typical = None

    def learn(self, records: list[dict]) -> None:
        ws = sorted(r["w"] for r in records if r.get("w"))
        self.typical = ws[len(ws) // 2] if len(ws) >= 120 else None

    def threshold(self, cfg: dict) -> float:
        if cfg.get("alert_watts"):
            return float(cfg["alert_watts"])
        return max(12.0, 1.6 * self.typical) if self.typical else 15.0

    def check(self, rec: dict, model: dict, cfg: dict) -> None:
        now = rec["t"]
        bl = self.get_bl()
        if self.last_set is not None and bl and abs(bl[1] - self.last_set) > max(2, 0.01 * bl[2]):
            idle = self.idle()
            if idle is not None and idle < 20_000:  # changed while you were at it
                if not self.manual:
                    log_event("manual", now, br=round(bl[1] / bl[2], 3))
                self.manual = True
        if rec.get("w") is None:  # charging or full: reset for the next time on battery
            if rec.get("ac") and self.original is not None and not self.manual and bl:
                if self.set_bl(bl[0], self.original):
                    log_event("restore", now, br=round(self.original / bl[2], 3))
            self.high, self.original, self.last_set, self.manual = 0, None, None, False
            return
        limit = self.threshold(cfg)
        self.high = self.high + 1 if rec["w"] > limit else 0
        if self.high < self.SUSTAIN:
            return
        parts = sorted(((w, g) for g, w in watts_by_group(rec, model).items() if g not in (IDLE,)), reverse=True)[:3]
        bat = self.get_bat()
        full = bat[0] if bat else None
        top = ", ".join(f"{g} {fmt_rate(w, full)}" for w, g in parts)
        usual = f" (usually ~{self.typical:.0f} W)" if self.typical else ""
        rate = pct_per_hour(rec["w"], full)
        mins = bat[1] / rec["w"] * 60 if bat and rec["w"] > 0 else None
        left = "" if mins is None else f", ~{int(mins // 60)}h {int(mins % 60):02d}m left at this rate" if mins >= 60 \
            else f", ~{int(mins)} min left at this rate"
        dimmed = ""
        if cfg.get("dim") and not self.manual and bl and now - self.last_dim >= self.DIM_EVERY:
            idle = self.idle()
            floor = int(cfg.get("dim_floor", 0.2) * bl[2])
            new = max(floor, int(bl[1] * self.DIM_STEP))
            if idle is not None and idle < 20_000 and new < bl[1] and self.set_bl(bl[0], new):
                if self.original is None:
                    self.original = bl[1]
                self.last_set, self.last_dim = new, now
                dimmed = f" Dimmed the screen {round(100 * bl[1] / bl[2])}% → {round(100 * new / bl[2])}%."
                log_event("dim", now, w=rec["w"], frm=round(bl[1] / bl[2], 3), to=round(new / bl[2], 3))
        if cfg.get("alert") and (now - self.last_alert >= self.ALERT_EVERY or dimmed):
            head = f"{rec['w']:.0f} W" + (f" · {fmt_pct_h(rate)}" if rate is not None else "")
            self.notify(f"High power draw on battery: {head}{usual}", f"Biggest: {top}{left}.{dimmed}")
            if now - self.last_alert >= self.ALERT_EVERY:
                log_event("alert", now, w=rec["w"], limit=round(limit, 1), top=[[g, round(w, 2)] for w, g in parts],
                          pct_h=None if rate is None else round(rate, 2))
            self.last_alert = now


def run_log(interval: float) -> int:
    sampler = Sampler()
    sampler.sample()
    dog = Watchdog()
    model, last_fit, last_prune = None, 0.0, 0.0
    while True:
        time.sleep(interval)
        rec = sampler.sample()
        if rec is None:
            continue
        if rec["dt"] > 3 * interval:
            continue  # woke from suspend: the CPU deltas span the sleep
        try:
            append(rec)
        except OSError as exc:
            print(f"claude-usage power: can't write {log_path()}: {exc}", file=sys.stderr)
        if rec["t"] - last_fit > 3600 or model is None:
            history = load(rec["t"] - 14 * 86400)
            model, last_fit = fit_model(history), rec["t"]
            dog.learn(history)
        try:
            dog.check(rec, model, load_config())
        except Exception as exc:  # the watchdog must never stop the logging
            print(f"claude-usage power: watchdog: {exc}", file=sys.stderr)
        if rec["t"] - last_prune > 86400:
            prune(rec["t"])
            prune(rec["t"], events_path())
            last_prune = rec["t"]


# --------------------------------------------------------------------------
# battery life


def battery_outlook(records: list[dict], model: dict, now: float, bat=None, bl=None,
                    status: str | None = None) -> dict | None:
    """How long the charge left lasts under a few kinds of use, as watts and
    hours. Works plugged in too: then it's "if you unplugged now"."""
    bat = battery_wh() if bat is None else bat
    if not bat:
        return None
    full, left = bat
    if status is None:
        status = next((_read(d + "/status") for d in glob.glob("/sys/class/power_supply/*")
                       if _read(d + "/type") == "Battery"), None)
    # your usual brightness while using it: right now it may be dimmed for idleness
    lit = sorted(r["br"] for r in records if (r.get("br") or 0) >= 0.05 and r["t"] >= now - 7 * 86400)
    if lit:
        br = lit[len(lit) // 2]
    else:
        bl = backlight() if bl is None else bl
        br = bl[1] / bl[2] if bl and bl[1] / bl[2] >= 0.05 else 0.4
    c = model["coef"]
    scen = []

    def add(key, label, watts, note):
        if watts and watts > 0.3:
            scen.append({"key": key, "label": label, "w": round(watts, 2), "h": round(left / watts, 2), "note": note})

    recent = [r for r in records if r["t"] >= now - 600]
    if recent:
        cur = sum(sum(watts_by_group(r, model).values()) * r["dt"] for r in recent) / sum(r["dt"] for r in recent)
        measured = all(r.get("w") is not None for r in recent)
        add("now", "Current use", cur, "last 10 minutes, " + ("measured" if measured else "estimated while plugged in"))
    on_bat = sorted(r["w"] for r in records if r.get("w") and r["t"] >= now - 14 * 86400)
    if len(on_bat) >= 30:
        add("typical", "Your typical use", on_bat[len(on_bat) // 2], f"median of {len(on_bat)} minutes on battery")
    add("idle", "Idle, screen on", c["idle"] + c["screen"] * br, f"nothing running, screen at your usual {round(100 * br)}%")
    if br > 0.25:
        add("idle_dim", "Idle, screen at 20%", c["idle"] + c["screen"] * 0.2, "nothing running, dimmed")
    return {"full_wh": round(full, 1), "left_wh": round(left, 1), "pct": round(100 * left / full, 1),
            "status": status, "scenarios": scen}


# --------------------------------------------------------------------------
# for the report page


def build(since: float, now: float, records: list[dict] | None = None, bin_s: int = 900,
          top: int = 11, events: list[dict] | None = None) -> dict | None:
    """Binned watts per group for the report page, or None without samples."""
    records = load(since) if records is None else records
    if not records:
        return None
    model = fit_model(records)
    energy = defaultdict(float)  # group -> watt-seconds over everything
    per_bin: dict[int, dict] = {}
    for r in records:
        b = int(r["t"] // bin_s * bin_s)
        slot = per_bin.setdefault(b, {"ws": defaultdict(float), "secs": 0.0, "meas": [0.0, 0.0], "ac": 0.0, "br": [0.0, 0.0],
                                      "bat": None})
        dt = r["dt"]
        for g, w in watts_by_group(r, model).items():
            slot["ws"][g] += w * dt
            energy[g] += w * dt
        slot["secs"] += dt
        if r.get("w") is not None:
            slot["meas"][0] += r["w"] * dt
            slot["meas"][1] += dt
        if r.get("ac"):
            slot["ac"] += dt
        if r.get("br") is not None:
            slot["br"][0] += r["br"] * dt
            slot["br"][1] += dt
        if r.get("bat") is not None:
            slot["bat"] = r["bat"]  # the last reading in the slot
    ranked = [g for g, _ in sorted(energy.items(), key=lambda kv: -kv[1]) if g not in BASE_GROUPS]
    named = ranked[:top]
    for g in CLAUDE_GROUPS:  # always split out Claude, however small
        if g in energy and g not in named:
            named.append(g)
    groups = list(BASE_GROUPS) + named + ([OTHER] if len(ranked) > len(named) else [])
    index = {g: i for i, g in enumerate(groups)}
    bins = []
    for b in sorted(per_bin):
        slot = per_bin[b]
        vals = [0.0] * len(groups)
        for g, ws in slot["ws"].items():
            vals[index.get(g, index.get(OTHER, 0))] += ws
        secs = slot["secs"]
        meas = slot["meas"][0] / slot["meas"][1] if slot["meas"][1] >= 0.5 * secs else None
        br = slot["br"][0] / slot["br"][1] if slot["br"][1] else None
        # [start, seconds sampled, avg measured W or null, share on AC, Wh per group, avg brightness or null,
        #  battery % at the end of the slot or null]
        bins.append([b, round(secs), None if meas is None else round(meas, 2), round(slot["ac"] / secs, 2) if secs else 0,
                     [round(v / 3600, 4) for v in vals], None if br is None else round(br, 3), slot["bat"]])
    events = [e for e in (load(since, events_path(), key="ev") if events is None else events) if e.get("t", 0) >= since]
    return {
        "model": model, "terms": {k: v[0] for k, v in TERMS.items()}, "ncpu": NCPU, "bin": bin_s,
        "groups": groups, "claude": list(CLAUDE_GROUPS), "base": list(BASE_GROUPS),
        "bins": bins, "first": records[0]["t"], "last": records[-1]["t"], "n": len(records),
        "events": events[-200:], "config": load_config(),
        "battery_wh": (battery_wh() or (None,))[0],
        "battery": battery_outlook(records, model, now),
    }


# --------------------------------------------------------------------------
# command line

SERVICE = """[Unit]
Description=claude-usage power sampler (CPU/GPU time per app, battery draw)

[Service]
ExecStart={python} {script} power log
Restart=on-failure
RestartSec=30
Nice=10

[Install]
WantedBy=default.target
"""


def service_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "systemd" / "user" / "claude-usage-power.service"


def install_service() -> int:
    script = Path(__file__).resolve().with_name("claude_usage.py")
    path = service_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SERVICE.format(python=sys.executable, script=script), encoding="utf-8")
    for cmd in (["systemctl", "--user", "daemon-reload"],
                ["systemctl", "--user", "enable", "--now", "claude-usage-power.service"]):
        if subprocess.run(cmd).returncode:
            return 1
    print(f"Installed {path}\nSampling every {INTERVAL}s into {log_path()}")
    return 0


def uninstall_service() -> int:
    subprocess.run(["systemctl", "--user", "disable", "--now", "claude-usage-power.service"])
    try:
        service_path().unlink()
    except FileNotFoundError:
        pass
    subprocess.run(["systemctl", "--user", "daemon-reload"])
    print(f"Removed the sampler. The samples stay in {log_path()}")
    return 0


def configure(args) -> int:
    cfg = load_config()
    if args.alert:
        cfg["alert"] = args.alert == "on"
    if args.dim:
        cfg["dim"] = args.dim == "on"
    if args.watts is not None:
        cfg["alert_watts"] = args.watts or None
    if args.floor is not None:
        cfg["dim_floor"] = min(1.0, max(0.05, args.floor / 100))
    if any(x is not None for x in (args.alert, args.dim, args.watts, args.floor)):
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    dog = Watchdog()
    dog.learn(load(time.time() - 14 * 86400))
    print(f"alert on high draw: {'on' if cfg['alert'] else 'off'}"
          f" (threshold {dog.threshold(cfg):.0f} W{'' if cfg['alert_watts'] else ', learned' if dog.typical else ', default'})")
    print(f"dim the screen:     {'on' if cfg['dim'] else 'off'} (not below {round(100 * cfg['dim_floor'])}%)")
    return 0


def print_now(seconds: float) -> int:
    """Sample twice, `seconds` apart, and print the result."""
    s = Sampler()
    s.sample()
    time.sleep(seconds)
    rec = s.sample()
    model = fit_model(load(time.time() - KEEP_DAYS * 86400))
    w = watts_by_group(rec, model)
    src = "measured on battery" if rec["w"] is not None else "estimated (on AC power)"
    gpu = "" if rec["gpu"] is None else f", GPU {100 * rec['gpu']:.0f}% busy"
    br = "" if rec["br"] is None else f", screen at {100 * rec['br']:.0f}%"
    print(f"{sum(w.values()):.1f} W {src}, CPU {100 * rec['busy'] / rec['cap']:.0f}% busy{gpu}{br} over {rec['dt']:.0f}s")
    for g, v in sorted(w.items(), key=lambda kv: -kv[1])[:15]:
        print(f"  {v:5.2f} W  {g}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="claude-usage power",
                                description="What uses your laptop's power, per app, Claude included.")
    sub = p.add_subparsers(dest="cmd")
    lg = sub.add_parser("log", help="sample forever (what the background service runs)")
    lg.add_argument("-i", "--interval", type=float, default=INTERVAL)
    sub.add_parser("install", help="run the sampler in the background (systemd user service)")
    sub.add_parser("uninstall", help="stop and remove the background sampler")
    nw = sub.add_parser("now", help="sample for a few seconds and print the split")
    nw.add_argument("seconds", nargs="?", type=float, default=5)
    cf = sub.add_parser("config", help="show or change the battery alert and auto-dimming")
    cf.add_argument("--alert", choices=("on", "off"))
    cf.add_argument("--dim", choices=("on", "off"))
    cf.add_argument("--watts", type=float, help="fixed alert threshold in W (0 = learn it)")
    cf.add_argument("--floor", type=float, help="never dim below this brightness, in %%")
    args = p.parse_args(argv)
    if args.cmd == "log":
        try:
            return run_log(max(5.0, args.interval))
        except KeyboardInterrupt:
            return 0
    if args.cmd == "install":
        return install_service()
    if args.cmd == "uninstall":
        return uninstall_service()
    if args.cmd == "now":
        return print_now(args.seconds)
    if args.cmd == "config":
        return configure(args)
    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
