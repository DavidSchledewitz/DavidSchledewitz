#!/usr/bin/env python3
"""claude-usage power: what uses your laptop's power, per app, Claude included.

`claude-usage power log` samples once a minute: the CPU time every process
used since the last sample, grouped into apps, and the battery's power draw
while it's discharging. The samples go to power.jsonl next to the usage
history; the report turns them into a Power tab.

Linux doesn't measure power per process, so it's split up like this:

    watts = baseline + share of the rest by CPU time

The baseline (screen, memory, Wi-Fi, an idle CPU) and how many watts a busy
CPU adds are fitted from the samples taken on battery, where the total draw
is measured. Until there are enough of those, a typical value for a 15 W
laptop CPU stands in. GPU work (video, browser compositing) shows up only
through the CPU time around it.

Claude gets three groups: Claude Code itself, the desktop app, and the
commands and tools Claude runs (shells, scripts, MCP servers), found by
walking each process's parents.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

INTERVAL = 60  # seconds between samples
KEEP_DAYS = 40
TOP_GROUPS = 15  # per sample; the rest is merged into OTHER

CLK = os.sysconf("SC_CLK_TCK")
NCPU = os.cpu_count() or 1

CLAUDE_CODE = "Claude Code"
CLAUDE_APP = "Claude desktop app"
CLAUDE_TOOLS = "Claude: commands & tools"
CLAUDE_GROUPS = (CLAUDE_CODE, CLAUDE_APP, CLAUDE_TOOLS)
KERNEL = "Kernel"
OTHER = "Other"
UNSEEN = "Short-lived processes"
BASELINE = "Baseline: screen, memory, Wi-Fi, idle"

# A 15 W laptop CPU (Core Ultra U, Ryzen U) with its screen on, until the
# battery samples say otherwise.
DEFAULT_MODEL = {"base": 5.0, "per_cpu": 30.0}
MIN_FIT_SAMPLES = 30

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


# --------------------------------------------------------------------------
# reading /proc and the battery


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


def cpu_ticks() -> tuple[int, int]:
    """(busy, total) jiffies summed over all CPUs."""
    with open("/proc/stat") as f:
        vals = [int(v) for v in f.readline().split()[1:9]]
    total = sum(vals)
    return total - vals[3] - vals[4], total  # minus idle and iowait


def uptime_ticks() -> float:
    with open("/proc/uptime") as f:
        return float(f.read().split()[0]) * CLK


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


class Sampler:
    """Turns two looks at /proc into CPU seconds per app."""

    def __init__(self):
        self.prev: dict[str, tuple] = {}  # pid -> (start, own ticks, children ticks, ppid, group)
        self.prev_cpu = None
        self.prev_t = None
        self.prev_up = None
        self.exe: dict[tuple, str] = {}

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

    def sample(self) -> dict | None:
        now, up = time.time(), uptime_ticks()
        busy, total = cpu_ticks()
        procs = {}
        for pid in os.listdir("/proc"):
            if pid.isdigit():
                st = read_stat(pid)
                if st:
                    procs[pid] = st
        groups = self._groups(procs)
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
            ranked = sorted(secs.items(), key=lambda kv: -kv[1])
            g_out = {k: round(v, 2) for k, v in ranked[:TOP_GROUPS] if v >= 0.01}
            rest = sum(v for _, v in ranked[TOP_GROUPS:])
            if rest >= 0.01:
                g_out[OTHER] = round(g_out.get(OTHER, 0) + rest, 2)
            watts, on_ac = battery()
            rec = {"t": round(now, 1), "dt": round(dt, 1), "cap": round(dt * NCPU, 1),
                   "busy": round(max(0.0, busy_s), 2), "w": None if watts is None else round(watts, 2),
                   "ac": on_ac, "g": g_out}
        self.prev, self.prev_cpu, self.prev_t, self.prev_up = cur, (busy, total), now, up
        live = {(pid, st[4]) for pid, st in procs.items()}
        self.exe = {k: v for k, v in self.exe.items() if k in live}
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


def load(since: float, path: Path | None = None) -> list[dict]:
    path = path or log_path()
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("t", 0) >= since and rec.get("cap"):
                    out.append(rec)
    except OSError:
        pass
    return out


def run_log(interval: float) -> int:
    sampler = Sampler()
    sampler.sample()
    last_prune = 0.0
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
        if rec["t"] - last_prune > 86400:
            prune(rec["t"])
            last_prune = rec["t"]


# --------------------------------------------------------------------------
# turning samples into watts


def fit_model(records: list[dict]) -> dict:
    """watts = base + per_cpu * (busy CPU fraction), least squares on the
    samples where the battery measured the draw."""
    pts = [(r["busy"] / r["cap"], r["w"]) for r in records if r.get("w") and r["cap"] > 0]
    model = dict(DEFAULT_MODEL, n=len(pts), fitted=False, err=None)
    if len(pts) < MIN_FIT_SAMPLES:
        return model
    n = len(pts)
    mx = sum(x for x, _ in pts) / n
    my = sum(y for _, y in pts) / n
    sxx = sum((x - mx) ** 2 for x, _ in pts)
    if sxx < n * 0.02 ** 2:  # CPU load barely varied: slope is meaningless
        model.update(base=round(max(0.5, my - DEFAULT_MODEL["per_cpu"] * mx), 2))
        return model
    slope = sum((x - mx) * (y - my) for x, y in pts) / sxx
    slope = min(120.0, max(3.0, slope))
    base = max(0.5, my - slope * mx)
    err = (sum((y - base - slope * x) ** 2 for x, y in pts) / n) ** 0.5
    model.update(base=round(base, 2), per_cpu=round(slope, 2), fitted=True, err=round(err, 2))
    return model


def watts_by_group(rec: dict, model: dict) -> dict[str, float]:
    """One sample as average watts per group, baseline included. On battery
    the parts add up to the measured draw."""
    frac = rec["busy"] / rec["cap"] if rec["cap"] else 0.0
    base = model["base"]
    if rec.get("w") is not None:
        base = min(base, rec["w"])
        dyn = max(0.0, rec["w"] - base)
    else:
        dyn = model["per_cpu"] * frac
    out = {BASELINE: base}
    total_s = sum(rec["g"].values())
    if total_s > 0 and dyn > 0:
        for g, s in rec["g"].items():
            out[g] = out.get(g, 0) + dyn * s / total_s
    elif dyn > 0:
        out[UNSEEN] = dyn
    return out


def build(since: float, now: float, records: list[dict] | None = None, bin_s: int = 900,
          top: int = 11) -> dict | None:
    """Binned watts per group for the report page, or None without samples."""
    records = load(since) if records is None else records
    if not records:
        return None
    model = fit_model(records)
    energy = defaultdict(float)  # group -> watt-seconds over everything
    cpu = defaultdict(float)
    per_bin: dict[int, dict] = {}
    for r in records:
        b = int(r["t"] // bin_s * bin_s)
        slot = per_bin.setdefault(b, {"ws": defaultdict(float), "secs": 0.0, "meas": [0.0, 0.0], "ac": 0.0})
        dt = r["dt"]
        for g, w in watts_by_group(r, model).items():
            slot["ws"][g] += w * dt
            energy[g] += w * dt
        for g, s in r["g"].items():
            cpu[g] += s
        slot["secs"] += dt
        if r.get("w") is not None:
            slot["meas"][0] += r["w"] * dt
            slot["meas"][1] += dt
        if r.get("ac"):
            slot["ac"] += dt
    ranked = [g for g, _ in sorted(energy.items(), key=lambda kv: -kv[1]) if g != BASELINE]
    named = ranked[:top]
    for g in CLAUDE_GROUPS:  # always split out Claude, however small
        if g in energy and g not in named:
            named.append(g)
    groups = [BASELINE] + named + ([OTHER] if len(ranked) > len(named) else [])
    index = {g: i for i, g in enumerate(groups)}
    bins = []
    for b in sorted(per_bin):
        slot = per_bin[b]
        vals = [0.0] * len(groups)
        for g, ws in slot["ws"].items():
            vals[index.get(g, index.get(OTHER, 0))] += ws
        secs = slot["secs"]
        meas = slot["meas"][0] / slot["meas"][1] if slot["meas"][1] >= 0.5 * secs else None
        # [start, seconds sampled, avg measured W or null, share on AC, Wh per group...]
        bins.append([b, round(secs), None if meas is None else round(meas, 2), round(slot["ac"] / secs, 2) if secs else 0,
                     [round(v / 3600, 4) for v in vals]])
    first, last = records[0]["t"], records[-1]["t"]
    return {
        "model": model, "ncpu": NCPU, "bin": bin_s, "groups": groups, "claude": list(CLAUDE_GROUPS),
        "baseline": BASELINE, "bins": bins, "first": first, "last": last, "n": len(records),
        "cpu": {g: round(s, 1) for g, s in sorted(cpu.items(), key=lambda kv: -kv[1])[:30]},
    }


# --------------------------------------------------------------------------
# command line

SERVICE = """[Unit]
Description=claude-usage power sampler (CPU time per app, battery draw)

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
    import subprocess
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
    import subprocess
    subprocess.run(["systemctl", "--user", "disable", "--now", "claude-usage-power.service"])
    try:
        service_path().unlink()
    except FileNotFoundError:
        pass
    subprocess.run(["systemctl", "--user", "daemon-reload"])
    print(f"Removed the sampler. The samples stay in {log_path()}")
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
    print(f"{sum(w.values()):.1f} W {src}, CPU {100 * rec['busy'] / rec['cap']:.0f}% busy over {rec['dt']:.0f}s")
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
    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
