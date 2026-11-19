#!/usr/bin/env python3
"""claude-usage report: what used up your Claude limits, per chat and per prompt.

Reads the transcripts Claude Code keeps on this machine (~/.claude/projects),
where every model call records its token counts, groups the calls into chats
and prompts, and says what kind of usage each one was: re-reading a long
conversation, re-caching it after a break, subagents, big tool outputs, long
answers. It writes an interactive HTML page and opens it.

Percentages come from calibration. Every time claude-usage or the top bar
reads your real usage, the reading is logged (history.jsonl). A reading says
"X% of the window used since it started"; the transcripts say which tokens
were used in the same span. Fitting one against the other gives "% per
token", and it gets better with every reading. Without readings, the report
shows API-equivalent dollars instead.

Only Claude Code on this machine is counted. claude.ai chats, the desktop
app, Claude Code on the web and other computers use the same limits but leave
no transcripts here; the report shows that part as "not in local transcripts".
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import re
import sys
import time
import webbrowser
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import claude_usage as cu

HOUR = cu.HOUR
DAY = cu.DAY
WINDOWS = {"five_hour": 5 * HOUR, "seven_day": 7 * DAY}

# $ per million tokens: (input, output, cache read), first match wins. Cache
# writes cost 1.25x input (5-minute cache) or 2x input (1-hour cache). Only
# the ratios matter here: they weigh token types and models against each
# other before calibration turns the total into percent.
PRICES = [
    ("fable-5-1", (10, 50, 0.25)),
    ("mythos-5-1", (10, 50, 0.25)),
    ("fable", (10, 50, 1.0)),
    ("mythos", (10, 50, 1.0)),
    ("opus-5-5", (4, 20, 0.2)),
    ("opus-5", (5, 25, 0.5)),
    ("opus-4-5", (5, 25, 0.5)),
    ("opus-4-6", (5, 25, 0.5)),
    ("opus-4-7", (5, 25, 0.5)),
    ("opus-4-8", (5, 25, 0.5)),
    ("opus", (15, 75, 1.5)),
    ("sonnet-5", (2, 10, 0.2)),
    ("sonnet", (3, 15, 0.3)),
    ("haiku-5", (0.1, 0.5, 0.01)),
    ("haiku-4", (1, 5, 0.1)),
    ("haiku-3-5", (0.8, 4, 0.08)),
    ("haiku", (0.25, 1.25, 0.03)),
]
DEFAULT_PRICE = (4, 20, 0.2)

AGENT_TOOLS = ("Agent", "Task")
LONG_CTX = 150_000  # past this, every step re-reads a lot more than a fresh chat would
HANDOFF_CTX = 60_000  # a new chat started from a summary: system prompt, tools, the summary
INJECTED = "skills & injected text"  # stands in for a tool name in tool_out


def price(model: str) -> tuple[float, float, float]:
    for key, p in PRICES:
        if key in model:
            return p
    return DEFAULT_PRICE


def parse_ts(value) -> float | None:
    dt = cu.parse_time(value)
    return dt.timestamp() if dt else None


# --------------------------------------------------------------------------
# reading transcripts


@dataclass
class Request:
    """One model call."""
    t: float
    session: str
    model: str
    sidechain: bool  # made by a subagent
    inp: int = 0
    cw5: int = 0  # written to the 5-minute cache
    cw1h: int = 0  # written to the 1-hour cache
    cr: int = 0  # read from cache
    out: int = 0
    think: int = 0
    fast: bool = False
    tools: list = field(default_factory=list)
    gap: float = math.inf  # seconds since the session's previous main call

    @property
    def ctx(self) -> int:
        return self.inp + self.cw5 + self.cw1h + self.cr

    def costs(self) -> dict:
        """API-equivalent dollars by kind of usage."""
        p_in, p_out, p_cr = price(self.model)
        m = 2e-6 if self.fast else 1e-6  # fast mode costs double
        return {"read": self.cr * p_cr * m,
                "write": (self.cw5 * 1.25 + self.cw1h * 2) * p_in * m,
                "input": self.inp * p_in * m,
                "output": self.out * p_out * m}


@dataclass
class Prompt:
    t: float
    text: str
    requests: list = field(default_factory=list)
    tool_out: dict = field(default_factory=lambda: defaultdict(int))  # tool -> result tokens


@dataclass
class Session:
    id: str
    project: str = ""
    title: str = ""
    prompts: list = field(default_factory=list)
    requests: list = field(default_factory=list)
    results: list = field(default_factory=list)  # (t, tool_use_id, tokens)


TAG_RE = re.compile(r"<(system-reminder|local-command-caveat)>.*?</\1>", re.S)
SKIP_PREFIXES = ("<local-command-stdout", "<local-command-stderr", "<bash-stdout", "<bash-stderr",
                 "Caveat:", "[Request interrupted")


def prompt_text(content) -> str | None:
    """The text a person typed, or None for tool results and bookkeeping."""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text") or "")
            elif isinstance(b, dict) and b.get("type") == "image":
                parts.append("[image]")
        text = " ".join(parts)
    else:
        return None
    text = TAG_RE.sub("", text).strip()
    if not text or text.startswith(SKIP_PREFIXES):
        return None
    cmd = re.search(r"<command-name>(.*?)</command-name>", text)
    if cmd:
        args = re.search(r"<command-args>(.*?)</command-args>", text, re.S)
        text = f"{cmd.group(1)} {args.group(1) if args else ''}"
    bash = re.search(r"<bash-input>(.*?)</bash-input>", text, re.S)
    if bash:
        text = f"! {bash.group(1)}"
    text = " ".join(text.split())
    return text[:400] or None


def result_tokens(content) -> int:
    """Rough token count of a tool result (4 characters per token)."""
    if isinstance(content, str):
        return len(content) // 4
    if isinstance(content, list):
        total = 0
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    total += len(b.get("text") or "") // 4
                elif b.get("type") in ("image", "document"):
                    total += 1500
        return total
    return 0


COWORK = "local-agent-mode-sessions"  # the desktop app's Cowork sessions


def cowork_dirs() -> list[Path]:
    """Where the Claude desktop app keeps Cowork sessions. Cowork runs Claude
    Code in a small VM, and each session's working folder holds its
    transcript in the same format as ~/.claude/projects."""
    home = Path.home()
    bases = [Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config"),  # Linux
             home / "Library" / "Application Support"]  # macOS
    bases += [Path(os.environ[v]) for v in ("APPDATA", "LOCALAPPDATA") if os.environ.get(v)]  # Windows
    return [b / app / COWORK for b in bases for app in ("Claude", "Claude-3p")]


def cowork_title(transcript: Path) -> str:
    """The title the desktop app shows for a Cowork session, from the session's
    state file local_<id>.json next to its folder; "" if not found."""
    for folder in transcript.parents:
        if folder.name.startswith("local_"):
            try:
                state = json.loads(folder.with_name(folder.name + ".json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return ""
            for key in ("title", "name", "sessionTitle", "displayName"):
                if isinstance(state, dict) and isinstance(state.get(key), str) and state[key].strip():
                    return state[key].strip()
            return ""
    return ""


def transcript_dirs() -> list[Path]:
    dirs = [cu.claude_config_dir() / "projects", Path.home() / ".config" / "claude" / "projects", *cowork_dirs()]
    seen, out = set(), []
    for d in dirs:
        r = d.resolve() if d.exists() else None
        if r and r not in seen:
            seen.add(r)
            out.append(d)
    return out


def iter_lines(path: Path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                # Cheap pre-filter: most lines are attachments and progress noise.
                if '"assistant"' not in line and '"user"' not in line and "title" not in line.lower():
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if isinstance(d, dict):
                    yield d
    except OSError:
        return


def load_sessions(since: float, dirs: list[Path] | None = None) -> dict[str, Session]:
    sessions: dict[str, Session] = {}
    seen: dict[tuple, Request] = {}
    tool_names: dict[str, str] = {}
    for base in dirs if dirs is not None else transcript_dirs():
        cowork = COWORK in base.parts
        for path in sorted(base.rglob("*.jsonl")):
            # In Cowork folders, skip the event log and the user's own files.
            if cowork and (path.name == "audit.jsonl" or {"uploads", "outputs"} & set(path.parts)):
                continue
            try:
                if path.stat().st_mtime < since:
                    continue
            except OSError:
                continue
            in_subagent_dir = path.parent.name == "subagents"
            parent_id = path.parent.parent.name if in_subagent_dir else None
            for d in iter_lines(path):
                sid = parent_id or d.get("sessionId") or path.stem
                s = sessions.get(sid)
                if s is None:
                    s = sessions[sid] = Session(id=sid)
                kind = d.get("type")
                if kind in ("custom-title", "ai-title"):
                    title = d.get("customTitle") or d.get("aiTitle") or d.get("title")
                    if isinstance(title, str) and (kind == "custom-title" or not s.title):
                        s.title = title.strip()
                    continue
                t = parse_ts(d.get("timestamp"))
                if t is None or t < since:
                    continue
                cwd = d.get("cwd") if isinstance(d.get("cwd"), str) else ""
                if cowork or COWORK in cwd:  # its cwd is the session's scratch folder
                    s.project = "Cowork"
                    if not s.title:
                        s.title = cowork_title(path)
                elif not s.project and cwd:
                    s.project = cwd
                msg = d.get("message") if isinstance(d.get("message"), dict) else {}
                sidechain = bool(d.get("isSidechain")) or in_subagent_dir
                if kind == "assistant":
                    usage, model = msg.get("usage"), msg.get("model") or ""
                    if not isinstance(usage, dict) or model == "<synthetic>":
                        continue
                    key = (msg.get("id"), d.get("requestId")) if msg.get("id") else (d.get("uuid"),)
                    req = seen.get(key)
                    if req is None:
                        req = seen[key] = Request(t=t, session=sid, model=model, sidechain=sidechain)
                        s.requests.append(req)
                    # Each content block is its own line with the same usage; the
                    # last one carries the final output count.
                    cc = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
                    cw = int(usage.get("cache_creation_input_tokens") or 0)
                    cw1h = int(cc.get("ephemeral_1h_input_tokens") or 0)
                    req.inp = int(usage.get("input_tokens") or 0)
                    req.cr = int(usage.get("cache_read_input_tokens") or 0)
                    req.cw1h, req.cw5 = cw1h, max(0, cw - cw1h)
                    out = int(usage.get("output_tokens") or 0)
                    if out >= req.out:
                        req.out = out
                        details = usage.get("output_tokens_details")
                        if isinstance(details, dict):
                            req.think = int(details.get("thinking_tokens") or 0)
                    req.fast = usage.get("speed") == "fast"
                    for b in msg.get("content") or []:
                        if isinstance(b, dict) and b.get("type") == "tool_use":
                            name = b.get("name") or "?"
                            req.tools.append(name)
                            if b.get("id"):
                                tool_names[b["id"]] = name
                elif kind == "user":
                    content = msg.get("content")
                    if isinstance(content, list):
                        for b in content:
                            if isinstance(b, dict) and b.get("type") == "tool_result":
                                s.results.append((t, b.get("tool_use_id"), result_tokens(b.get("content"))))
                    if d.get("isMeta") and not sidechain:  # skill bodies, command expansions
                        s.results.append((t, None, result_tokens(content)))
                    if sidechain or d.get("isMeta") or d.get("isCompactSummary"):
                        continue
                    text = prompt_text(content)
                    if text:
                        s.prompts.append(Prompt(t=t, text=text))
    for s in list(sessions.values()):
        if not s.requests:
            del sessions[s.id]
            continue
        group(s, tool_names)
    return sessions


def group(s: Session, tool_names: dict) -> None:
    """Hand every call and tool result to the prompt it ran under."""
    s.prompts.sort(key=lambda p: p.t)
    s.requests.sort(key=lambda r: r.t)
    if not s.prompts or s.requests[0].t < s.prompts[0].t:
        s.prompts.insert(0, Prompt(t=s.requests[0].t, text="(work before the first prompt)"))
    starts = [p.t for p in s.prompts]

    def owner(t: float) -> Prompt:
        lo, hi = 0, len(starts) - 1
        while lo < hi:  # last prompt that started at or before t
            mid = (lo + hi + 1) // 2
            if starts[mid] <= t:
                lo = mid
            else:
                hi = mid - 1
        return s.prompts[lo]

    prev = None
    for r in s.requests:
        if not r.sidechain:
            r.gap = r.t - prev if prev is not None else math.inf
            prev = r.t
        owner(r.t).requests.append(r)
    for t, tool_id, tokens in s.results:
        owner(t).tool_out[tool_names.get(tool_id, "?") if tool_id else INJECTED] += tokens
    s.prompts = [p for p in s.prompts if p.requests]


# --------------------------------------------------------------------------
# explaining usage


def fmt_k(tokens: float) -> str:
    if tokens >= 1e6:
        return f"{tokens / 1e6:.1f}M"
    if tokens >= 1000:
        return f"{tokens / 1000:.0f}k"
    return f"{tokens:.0f}"


def summarize(requests: list, tool_out: dict) -> dict:
    """Totals by kind plus a ranked list of reasons, for one prompt or chat."""
    cost = {"read": 0.0, "write": 0.0, "input": 0.0, "output": 0.0}
    tok = {"in": 0, "cw": 0, "cr": 0, "out": 0, "think": 0}
    sub_cost = sub_read = rebuild_cost = main_read = 0.0
    break_cost = break_saving = long_read = 0.0  # for the patterns: after a break, above LONG_CTX
    rebuilds, models, tools = [], set(), defaultdict(int)
    n_main = n_sub = agents = max_ctx = ctx_sum = 0
    for r in requests:
        c = r.costs()
        for k, v in c.items():
            cost[k] += v
        tok["in"] += r.inp
        tok["cw"] += r.cw5 + r.cw1h
        tok["cr"] += r.cr
        tok["out"] += r.out
        tok["think"] += r.think
        models.add(r.model)
        for name in r.tools:
            tools[name] += 1
        if r.sidechain:
            n_sub += 1
            sub_cost += sum(c.values())
            sub_read += c["read"]
            continue
        n_main += 1
        main_read += c["read"]
        agents += sum(1 for name in r.tools if name in AGENT_TOOLS)
        max_ctx = max(max_ctx, r.ctx)
        ctx_sum += r.ctx
        cw = r.cw5 + r.cw1h
        ttl = HOUR if r.cw1h > r.cw5 else 300
        if r.ctx > LONG_CTX:
            long_read += c["read"] * (r.ctx - LONG_CTX) / r.ctx
        if cw >= 15000 and (r.gap == math.inf or (r.gap >= ttl and cw >= 0.5 * r.ctx)):
            rebuilds.append((r.gap, cw))
            rebuild_cost += c["write"]
            if r.gap != math.inf:
                break_cost += c["write"]
                break_saving += c["write"] * max(0.0, 1 - HANDOFF_CTX / max(cw, 1))
    total = sum(cost.values()) or 1e-12
    reasons = []

    def add(amount: float, text: str) -> None:
        reasons.append([text, round(amount / total, 3)])

    if sub_cost / total >= 0.1:
        who = f"{agents} subagents" if agents > 1 else "a subagent" if agents == 1 else "subagents"
        add(sub_cost, f"{who} ({n_sub} calls): each starts its own context and re-reads it every step")
    if main_read / total >= 0.15 and n_main >= 2:
        add(main_read, f"{n_main} model calls, each re-reading the conversation "
                       f"(~{fmt_k(ctx_sum / n_main)} tokens on average, peak {fmt_k(max_ctx)})")
    if rebuilds:
        idle = [g for g, _ in rebuilds if g != math.inf]
        size = fmt_k(max(cw for _, cw in rebuilds))
        if idle:
            add(rebuild_cost, f"cache expired after {cu.fmt_duration(max(idle))} idle: "
                              f"the whole conversation ({size} tokens) was cached again")
        else:
            add(rebuild_cost, f"new chat: system prompt, tools and project files cached ({size} tokens)")
    other_write = cost["write"] - rebuild_cost
    if other_write / total >= 0.15:
        top = sorted(((v, k) for k, v in tool_out.items() if v >= 2000), reverse=True)[:3]
        detail = ", ".join(f"{k} {fmt_k(v)}" for v, k in top)
        add(other_write, f"{fmt_k(tok['cw'])} tokens of new content cached"
                         + (f" (largest sources: {detail})" if detail else ""))
    if cost["output"] / total >= 0.15:
        think = f", {fmt_k(tok['think'])} of it thinking" if tok["think"] > 0.3 * tok["out"] else ""
        add(cost["output"], f"{fmt_k(tok['out'])} tokens written{think}")
    if cost["input"] / total >= 0.15:
        add(cost["input"], f"{fmt_k(tok['in'])} uncached input tokens")
    reasons.sort(key=lambda x: -x[1])
    if any("fable" in m or "mythos" in m for m in models):
        reasons.append(["runs on Fable, which costs about 2.5x Opus per token", 0])
    return {
        "cost": {k: round(v, 6) for k, v in cost.items()},
        "sub": [round(sub_read, 6), round(sub_cost - sub_read, 6)],  # subagents: cache reads, rest
        "tok": tok, "n": n_main, "ns": n_sub, "agents": agents,
        "max_ctx": max_ctx, "models": sorted(models), "reasons": reasons[:5],
        # API dollars: re-cached after a break, of which a fresh chat would have saved; re-reading above LONG_CTX
        "pat": [round(break_cost, 6), round(break_saving, 6), round(long_read, 6)],
        "tools": sorted(([k, tools.get(k, 0), tool_out.get(k, 0)] for k in set(tools) | set(tool_out)),
                        key=lambda x: (-x[2], -x[1]))[:12],
    }


# --------------------------------------------------------------------------
# readings, windows, calibration


def load_readings(path: Path | None = None) -> dict[str, list]:
    """Logged readings: {limit: [(t, pct, resets_at), ...]}, oldest first."""
    out = {k: [] for k in WINDOWS}
    try:
        lines = (path or cu.history_path()).read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    seen = set()
    for line in lines:
        try:
            d = json.loads(line)
            t = float(d["t"])
            limits = d["limits"]
        except (ValueError, KeyError, TypeError):
            continue
        for key in WINDOWS:
            val = limits.get(key) if isinstance(limits, dict) else None
            if not isinstance(val, dict) or val.get("pct") is None:
                continue
            resets = parse_ts(val.get("resets_at"))
            if resets is None or (key, round(t)) in seen:
                continue
            seen.add((key, round(t)))
            out[key].append((t, float(val["pct"]), resets))
    for v in out.values():
        v.sort()
    return out


def windows_for(key: str, times: list[float], readings: list, now: float) -> list[list]:
    """Windows as [start, end, measured]. Measured ones come from the resets_at
    of readings; gaps between them are filled the way Claude does it: a 5h
    window starts with the first message after the last one ended (on the
    hour), and weekly windows repeat every 7 days from a known reset."""
    span = WINDOWS[key]
    known = []
    for resets in sorted({r for _, _, r in readings}):
        if not known or resets - known[-1] > 600:  # resets_at can wobble by seconds
            known.append(resets)
    wins = [[e - span, e, True] for e in known]
    if key == "seven_day":
        if not wins:
            end = math.ceil(now / HOUR) * HOUR
            wins = [[end - span, end, False]]
        lo = min(times) if times else now
        while wins[0][0] > lo:
            wins.insert(0, [wins[0][0] - span, wins[0][0], False])
        while wins[-1][1] < now:
            wins.append([wins[-1][1], wins[-1][1] + span, False])
        return [w for w in wins if w[1] > lo]
    added, end = [], -math.inf
    for t in sorted(times):
        if t < end:
            continue
        hit = next((w for w in wins if w[0] <= t < w[1]), None)
        if hit:
            end = hit[1]
            continue
        before = max((w[1] for w in wins if w[1] <= t), default=-math.inf)
        start = max(math.floor(t / HOUR) * HOUR, end, before)
        nxt = min((w[0] for w in wins if w[0] > t), default=math.inf)
        added.append([start, min(start + span, nxt), False])
        end = added[-1][1]
    return sorted(wins + added)


def fit(points: list) -> dict | None:
    """Fit pct = k_read * read_cost + k_other * other_cost (+ offset) to the
    readings. Each point is (window, read_cost, other_cost, pct): one reading
    against the local usage since its window began.

    Usage that leaves no transcript here (claude.ai, other computers) only
    adds to a reading, so each window with enough readings gets its own
    offset >= 0 and the local weights come from how the readings grow with
    local usage. Two weights only with enough varied data, else one shared
    weight. One pass then drops outliers."""
    pts = list(dict.fromkeys((w, round(a, 6), round(b, 6), y) for w, a, b, y in points if y >= 1 and a + b > 0))
    if not pts:
        return None

    def solve(ps):  # least squares through the origin on (a, b, y)
        sab = sum(a * b for a, b, _ in ps)
        saa, sbb = sum(a * a for a, _, _ in ps), sum(b * b for _, b, _ in ps)
        say, sby = sum(a * y for a, _, y in ps), sum(b * y for _, b, y in ps)
        det = saa * sbb - sab * sab
        if len(ps) >= 12 and det > 1e-9 * saa * sbb and sab / math.sqrt(saa * sbb) < 0.97:
            ka, kb = (say * sbb - sby * sab) / det, (sby * saa - say * sab) / det
            if kb > 0 and 0.2 <= ka / kb <= 5:
                return ka, kb, 2
        sxx = sum((a + b) ** 2 for a, b, _ in ps)
        k = sum((a + b) * y for a, b, y in ps) / sxx
        return k, k, 1

    def run(ps):
        by_window = defaultdict(list)
        for p in ps:
            by_window[p[0]].append(p)
        offset_windows = [w for w, wp in by_window.items()
                          if len(wp) >= 3 and max(a + b for _, a, b, _ in wp) > 1.5 * min(a + b for _, a, b, _ in wp)]
        off = defaultdict(float)
        for _ in range(20):
            ka, kb, n_params = solve([(a, b, y - off[w]) for w, a, b, y in ps])
            new = {}
            for w in offset_windows:
                res = sorted(y - ka * a - kb * b for _, a, b, y in by_window[w])
                new[w] = max(0.0, res[len(res) // 2])
            if all(abs(new[w] - off[w]) < 0.01 for w in new):
                break
            off.update(new)
        return ka, kb, n_params, off

    ka, kb, n_params, off = run(pts)
    res = [y - off[w] - ka * a - kb * b for w, a, b, y in pts]
    mad = sorted(abs(r) for r in res)[len(res) // 2]
    keep = [p for p, r in zip(pts, res) if abs(r) <= max(3 * 1.4826 * mad, 3.0)]
    if len(pts) >= 4 and len(keep) >= max(3, len(pts) // 2) and len(keep) < len(pts):
        pts = keep
        ka, kb, n_params, off = run(pts)
    pred = [ka * a + kb * b for _, a, b, _ in pts]
    err = sorted(abs(y - off[w] - p) for (w, _, _, y), p in zip(pts, pred))
    return {"k_read": ka, "k_other": kb, "params": n_params, "n": len(pts),
            "err": round(err[len(err) // 2], 2),
            "explained": round(sum(pred) / max(1e-9, sum(y for *_, y in pts)), 3)}


def calibrate(requests: list, readings: dict) -> dict:
    """{limit: fit result or None}."""
    times = [r.t for r in requests]
    costs = [r.costs() for r in requests]
    cum_read, cum_other, a, b = [], [], 0.0, 0.0
    for c in costs:
        a += c["read"]
        b += c["write"] + c["input"] + c["output"]
        cum_read.append(a)
        cum_other.append(b)

    def upto(t):  # cumulative costs of requests at or before t
        lo, hi = 0, len(times)
        while lo < hi:
            mid = (lo + hi) // 2
            if times[mid] <= t:
                lo = mid + 1
            else:
                hi = mid
        return (cum_read[lo - 1], cum_other[lo - 1]) if lo else (0.0, 0.0)

    out = {}
    for key, span in WINDOWS.items():
        points = []
        for t, pct, resets in readings.get(key, []):
            r1, o1 = upto(t)
            r0, o0 = upto(resets - span - 1e-6)
            points.append((round(resets / 600), r1 - r0, o1 - o0, pct))
        out[key] = fit(points)
    return out


# --------------------------------------------------------------------------
# report


def fit_notes(fits: dict, readings: dict) -> dict:
    """Why a limit has no calibration yet, for the page."""
    notes = {}
    for key, label in (("five_hour", "5-hour window"), ("seven_day", "week")):
        if fits.get(key):
            continue
        if not any(readings.values()):
            notes[key] = "No usage reading yet. The top bar takes one every 5 minutes; running the report takes one too."
        else:
            notes[key] = (f"No usable reading yet: each one so far was taken when no {label} was running or "
                          f"this computer's Claude Code had used under 1% of it. The next reading during a {label} you use "
                          f"Claude Code in fixes that.")
    return notes


def tool_carry(s: Session) -> list[list[float]]:
    """Per prompt, an estimate of what its tool output (files read, command
    output) cost from then on: cached once, then re-read by every later call
    until the chat is compacted. [cache-read $, other $]"""
    main = [r for r in s.requests if not r.sidechain]
    times = [r.t for r in main]
    compactions = [b.t for a, b in zip(main, main[1:]) if a.ctx > 100_000 and b.ctx < 0.5 * a.ctx]
    out = []
    for p in s.prompts:
        tokens = sum(v for k, v in p.tool_out.items() if k != INJECTED)
        if not tokens:
            out.append([0.0, 0.0])
            continue
        end = next((t for t in compactions if t > p.t), math.inf)
        first = next((r.t for r in p.requests if not r.sidechain), p.t)  # tool output arrives after the first call
        later = bisect.bisect_left(times, end) - bisect.bisect_right(times, first)
        p_in, _, p_cr = price(p.requests[0].model)
        out.append([round(tokens * p_cr * max(0, later) * 1e-6, 6), round(tokens * 1.25 * p_in * 1e-6, 6)])
    return out


def power_data(since: float, now: float) -> dict | None:
    """Samples from `claude-usage power log`, binned for the Power tab."""
    try:
        import claude_power
        return claude_power.build(since, now)
    except Exception as exc:  # a broken power log must not break the usage report
        print(f"claude-usage report: skipped power data: {exc}", file=sys.stderr)
        return None


def build(days: float, now: float, readings: dict, plan: str | None, dirs=None, warning: str | None = None) -> dict:
    since = now - days * DAY
    sessions = load_sessions(since, dirs)
    home = str(Path.home())
    rows, reqs = [], []
    all_requests = sorted((r for s in sessions.values() for r in s.requests), key=lambda r: r.t)
    order = sorted(sessions.values(), key=lambda s: s.requests[-1].t, reverse=True)
    for si, s in enumerate(order):
        prompts = []
        carry = tool_carry(s)
        for pi, p in enumerate(s.prompts):
            info = summarize(p.requests, p.tool_out)
            info.update(t=p.t, end=p.requests[-1].t, text=p.text, tool=carry[pi],
                        ctx=[r.ctx for r in p.requests if not r.sidechain][:300])
            prompts.append(info)
            for r in p.requests:
                c = r.costs()
                reqs.append([round(r.t, 1), si, pi, *(round(c[k], 7) for k in ("read", "write", "input", "output")),
                             int(r.sidechain)])
        tool_out = defaultdict(int)
        for p in s.prompts:
            for k, v in p.tool_out.items():
                tool_out[k] += v
        info = summarize(s.requests, tool_out)
        project = s.project.replace(home, "~", 1) if s.project.startswith(home) else s.project
        info.update(id=s.id, title=s.title or (s.prompts[0].text if s.prompts else s.id),
                    project=project, start=s.requests[0].t, end=s.requests[-1].t, prompts=prompts)
        rows.append(info)
    reqs.sort()
    times = [r.t for r in all_requests]
    fits = calibrate(all_requests, readings)
    return {
        "generated": now, "days": days, "plan": plan, "warning": warning,
        "fit": fits, "notes": fit_notes(fits, readings),
        "readings": {k: [[round(t, 1), p, r] for t, p, r in v if t >= since] for k, v in readings.items()},
        "windows": {k: windows_for(k, times, [x for x in readings[k] if x[2] > since], now) for k in WINDOWS},
        "sessions": rows,
        "power": power_data(since, now),
        # one row per model call: [t, session, prompt, read$, write$, input$, output$, by subagent]
        "req": reqs,
    }


def log_once(data: dict, fetched_at: float) -> None:
    """Put a cached reading into history.jsonl unless it's there already
    (versions before the report cached readings without logging them)."""
    try:
        tail = cu.history_path().read_text(encoding="utf-8").splitlines()[-20:]
    except OSError:
        tail = []
    for line in tail:
        try:
            if abs(float(json.loads(line)["t"]) - fetched_at) < 1:
                return
        except (ValueError, KeyError, TypeError):
            continue
    cu.append_history(data, fetched_at)


def current_reading(fetch: bool) -> tuple[dict | None, str | None, str | None]:
    """Latest usage reading (fetched now unless the cache is fresh), plan, and
    a warning if it couldn't be read. Either way it ends up in the history."""
    cached = cu.read_cache(300)
    if cached:
        log_once(*cached)
        return cached[0], None, None
    if not fetch:
        return None, None, None
    try:
        data, creds = cu.get_usage()
    except cu.UsageError as exc:
        return None, None, str(exc)
    cu.write_cache(data, time.time())
    return data, creds.plan, None


def write_html(data: dict, path: Path) -> None:
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATE.replace("/*__DATA__*/null", blob), encoding="utf-8")


def pct_of(fit_: dict | None, read: float, other: float) -> float | None:
    return None if not fit_ else fit_["k_read"] * read + fit_["k_other"] * other


def print_summary(data: dict, out: Path, warn: str | None) -> None:
    now = data["generated"]
    fits = data["fit"]
    n = len(data["sessions"])
    print(f"claude-usage report · {n} chat{'' if n == 1 else 's'} with usage in the last {data['days']:g} days")
    if warn:
        print(f"  ! couldn't read your current usage: {warn}")
    for key, label in (("five_hour", "5h window"), ("seven_day", "week")):
        f = fits.get(key)
        if f and f["n"] < 3:
            print(f"  {label}: roughly calibrated from {f['n']} reading{'' if f['n'] == 1 else 's'} so far")
        elif f:
            print(f"  {label}: calibrated from {f['n']} readings, typical error ±{f['err']:.0f} pts, "
                  f"local Claude Code explains ~{100 * min(1, f['explained']):.0f}% of it")
        else:
            print(f"  {label}: no readings yet, so amounts are in API-equivalent dollars")
    win = next((w for w in reversed(data["windows"]["five_hour"]) if w[0] <= now < w[1]), None)
    if win:
        per = defaultdict(lambda: [0.0, 0.0])
        for t, si, _, read, write, inp, output, _ in data["req"]:
            if win[0] <= t < win[1]:
                per[si][0] += read
                per[si][1] += write + inp + output
        top = sorted(per.items(), key=lambda kv: -(kv[1][0] + kv[1][1]))[:5]
        if top:
            print("  current 5h window, biggest chats:")
        for si, (a, b) in top:
            s = data["sessions"][si]
            val = pct_of(fits.get("five_hour"), a, b)
            amount = f"{val:5.1f}%" if val is not None else f"${a + b:6.2f}"
            title = s["title"] if len(s["title"]) <= 60 else s["title"][:59] + "…"
            print(f"    {amount}  {title}")
    print(f"  report: {out}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="claude-usage report",
        description="What used your Claude limits: per chat and per prompt, as an HTML page.",
    )
    p.add_argument("--days", type=float, default=35, help="how far back to read transcripts (default 35)")
    p.add_argument("-o", "--out", type=Path, help="where to write the page (default ~/.cache/claude-usage/report.html)")
    p.add_argument("--no-open", action="store_true", help="don't open the page in the browser")
    p.add_argument("--no-fetch", action="store_true", help="don't ask Claude for a fresh usage reading")
    p.add_argument("--json", action="store_true", help="print the report data as JSON instead")
    p.add_argument("--tab", choices=("usage", "power"), help="open the page on this tab")
    args = p.parse_args(argv)

    now = time.time()
    _, plan, warn = current_reading(not args.no_fetch)
    cu.prune_history(now)  # the top bar only appends; trimming happens here and in the terminal app
    try:
        plan = plan or cu.load_credentials().plan
    except cu.UsageError:
        pass
    data = build(args.days, now, load_readings(), plan, warning=warn)
    if args.json:
        print(json.dumps(data, indent=1))
        return 0
    if not data["sessions"]:
        dirs = ", ".join(str(d) for d in transcript_dirs()) or str(cu.claude_config_dir() / "projects")
        print(f"claude-usage report: no Claude Code transcripts from the last {args.days:g} days in {dirs}",
              file=sys.stderr)
        return 1
    out = args.out or cu.cache_path().with_name("report.html")
    write_html(data, out)
    print_summary(data, out, warn)
    if not args.no_open:
        webbrowser.open(out.resolve().as_uri() + (f"#{args.tab}" if args.tab else ""))
    return 0


# The page. Self-contained (no network), the data replaces /*__DATA__*/null.
TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude usage report</title>
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,.10); --wash: rgba(11,11,11,.04);
  --s1: #2a78d6; --s2: #eb6834; --s3: #1baf7a; --s4: #eda100; --s5: #e87ba4; --s6: #008300; --s7: #6250d6; --s8: #e34948;
  --other: #c3c2b7; --warn: #fab219;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,.10); --wash: rgba(255,255,255,.05);
    --s1: #3987e5; --s2: #d95926; --s3: #199e70; --s4: #c98500; --s5: #d55181; --s6: #008300; --s7: #9085e9; --s8: #e66767;
    --other: #5f5e5a;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,.10); --wash: rgba(255,255,255,.05);
  --s1: #3987e5; --s2: #d95926; --s3: #199e70; --s4: #c98500; --s5: #d55181; --s6: #008300; --s7: #9085e9; --s8: #e66767;
  --other: #5f5e5a;
}
* { box-sizing: border-box; }
html, body { margin: 0; }
body { background: var(--page); color: var(--ink); font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1100px; margin: 0 auto; padding: 24px 16px 64px; }
h1 { font-size: 22px; margin: 0 0 2px; font-weight: 650; }
h2 { font-size: 17px; margin: 0; font-weight: 650; }
h3 { font-size: 14px; margin: 0 0 8px; font-weight: 650; }
.sub { color: var(--ink2); }
.muted { color: var(--muted); }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 16px; margin-top: 16px; }
.head { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 16px; justify-content: space-between; margin-bottom: 12px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin-top: 16px; }
.tile { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 14px 16px; }
.tile .label { color: var(--ink2); font-size: 13px; }
.tile .value { font-size: 30px; font-weight: 650; margin: 2px 0; }
.tile .note { color: var(--muted); font-size: 12.5px; }
.controls { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.controls.nav { flex-wrap: nowrap; max-width: 100%; }
.controls.nav select { min-width: 0; flex: 1 1 auto; }
select, button { font: inherit; color: var(--ink); background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 5px 9px; }
button { cursor: pointer; }
button:hover, select:hover { background: var(--wash); }
button:disabled { opacity: .4; cursor: default; }
.seg { display: inline-flex; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
.seg button { border: 0; border-radius: 0; padding: 5px 10px; }
.seg button + button { border-left: 1px solid var(--border); }
.seg button[aria-pressed="true"] { background: var(--ink); color: var(--surface); }
.seg button:disabled { opacity: .4; cursor: default; background: none; }
.chart { position: relative; width: 100%; }
.chart svg { display: block; overflow: visible; }
.legend { display: flex; flex-wrap: wrap; gap: 4px 14px; margin-top: 10px; font-size: 12.5px; color: var(--ink2); }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.sw { width: 10px; height: 10px; border-radius: 3px; flex: none; display: inline-block; }
.sw.line { height: 2px; border-radius: 1px; width: 14px; }
.caption { color: var(--ink2); font-size: 13px; margin-top: 8px; }
.rows { margin-top: 14px; display: grid; gap: 2px; }
.row { display: grid; grid-template-columns: 14px minmax(0, 1fr) minmax(80px, 34%) 64px; gap: 10px; align-items: center; padding: 6px 8px; border-radius: 8px; cursor: pointer; }
.row:hover, .row:focus-visible { background: var(--wash); outline: none; }
.row .t { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.row .num { text-align: right; font-variant-numeric: tabular-nums; }
.track { height: 10px; display: flex; gap: 2px; }
.track i { display: block; height: 100%; min-width: 0; }
.track i:last-child { border-radius: 0 4px 4px 0; }
table { width: 100%; border-collapse: collapse; }
th { text-align: left; font-weight: 600; color: var(--ink2); font-size: 12.5px; padding: 6px 8px; border-bottom: 1px solid var(--grid); }
td { padding: 8px; border-bottom: 1px solid var(--grid); vertical-align: top; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
tbody tr { cursor: pointer; }
tbody tr:hover, tbody tr:focus-visible { background: var(--wash); outline: none; }
.title { font-weight: 550; overflow-wrap: anywhere; }
.small { font-size: 12.5px; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
.chip { font-size: 12.5px; color: var(--ink2); background: var(--wash); border: 1px solid var(--border); border-radius: 999px; padding: 2px 9px; }
.chip b { color: var(--ink); font-weight: 600; }
.prompt { border-top: 1px solid var(--grid); padding: 10px 4px; }
.prompt .top { display: grid; grid-template-columns: minmax(0, 1fr) minmax(90px, 30%) 64px; gap: 12px; align-items: center; cursor: pointer; }
.prompt .text { overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow-wrap: anywhere; }
.prompt.open .text { display: block; }
.prompt .meta { color: var(--muted); font-size: 12.5px; }
.more { display: none; margin-top: 10px; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); }
.prompt.open .more { display: grid; }
.kv { display: grid; grid-template-columns: 1fr auto; gap: 2px 12px; font-size: 13px; }
.kv .num { text-align: right; font-variant-numeric: tabular-nums; }
#tip { position: fixed; pointer-events: none; z-index: 10; background: var(--surface); color: var(--ink); border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; font-size: 12.5px; box-shadow: 0 4px 16px rgba(0,0,0,.12); max-width: 320px; display: none; }
#tip .tt { font-weight: 600; margin-bottom: 4px; }
#tip .tr { display: grid; grid-template-columns: 10px minmax(0, 1fr) auto; gap: 6px; align-items: center; }
#tip .tr span:nth-child(2) { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
#tip .tr span:last-child { font-variant-numeric: tabular-nums; }
.empty { color: var(--muted); padding: 24px 0; text-align: center; }
.back { margin-bottom: 8px; }
details summary { cursor: pointer; font-weight: 600; }
.tabs { display: flex; gap: 4px; margin-top: 14px; border-bottom: 1px solid var(--border); }
.tabs button { border: 0; border-bottom: 2px solid transparent; border-radius: 0; background: none; padding: 8px 12px; color: var(--ink2); font-weight: 550; }
.tabs button[aria-selected="true"] { color: var(--ink); border-bottom-color: var(--ink); }
.tabs button:hover { background: var(--wash); }
.lead { font-size: 15px; margin: 0 0 12px; }
.pat { display: grid; grid-template-columns: minmax(0, 1fr) minmax(90px, 26%) 72px; gap: 6px 14px; align-items: center; padding: 12px 0; border-top: 1px solid var(--grid); }
.pat .name { font-weight: 600; }
.pat .why, .pat .fix, .pat .ex { grid-column: 1 / -1; font-size: 13px; color: var(--ink2); }
.pat .fix { color: var(--ink); }
.pat .fix b { font-weight: 600; }
.pat .ex { display: grid; gap: 2px; }
.pat .ex a, .linky { color: inherit; text-decoration: underline; text-decoration-color: var(--axis); cursor: pointer; }
.meter { height: 10px; border-radius: 0 4px 4px 0; background: var(--wash); position: relative; }
.meter i { position: absolute; left: 0; top: 0; bottom: 0; border-radius: 0 4px 4px 0; }
.meter i.save { background: repeating-linear-gradient(135deg, transparent 0 3px, rgba(0,0,0,.18) 3px 5px); }
tr.claude td:first-child { box-shadow: inset 3px 0 0 var(--s2); }
.tag { font-size: 11.5px; color: var(--ink2); border: 1px solid var(--border); border-radius: 999px; padding: 0 7px; margin-left: 6px; white-space: nowrap; }
pre.cmd { background: var(--wash); border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; overflow-x: auto; }
details p, details li { color: var(--ink2); }
@media (max-width: 640px) {
  .row { grid-template-columns: 12px minmax(0, 1fr) 56px; }
  .row .track { display: none; }
  .prompt .top { grid-template-columns: minmax(0, 1fr) 64px; }
  .prompt .top .track { display: none; }
  .pat { grid-template-columns: minmax(0, 1fr) 64px; }
  .pat .meter { display: none; }
  .hide-sm { display: none; }
}
</style>
</head>
<body>
<main>
  <div class="head" style="margin:0">
    <div><h1>Claude usage report</h1><div class="sub" id="gen"></div>
      <div class="caption" id="warning" hidden></div></div>
    <div class="controls" id="unit-controls"><span class="muted small">Show as</span>
      <div class="seg" id="units" role="group" aria-label="Show amounts as"></div></div>
  </div>
  <div class="tabs" role="tablist" aria-label="Report" id="tabs">
    <button role="tab" id="tabbtn-usage" aria-selected="true" aria-controls="tab-usage">Claude usage</button>
    <button role="tab" id="tabbtn-power" aria-selected="false" aria-controls="tab-power">Power</button>
  </div>
  <div id="tab-usage" role="tabpanel">
  <div class="tiles" id="tiles"></div>

  <section class="card" id="patterns">
    <div class="head">
      <h2>What drives it, and what to change</h2>
      <div class="controls"><select id="pat-period" aria-label="Period for patterns"></select></div>
    </div>
    <div id="pat-body"></div>
  </section>

  <section class="card" id="win-five_hour"></section>
  <section class="card" id="win-seven_day"></section>

  <section class="card" id="chats">
    <div class="head">
      <h2>Chats</h2>
      <div class="controls">
        <select id="period" aria-label="Period"></select>
      </div>
    </div>
    <div class="legend" id="kinds"></div>
    <div id="chat-table"></div>
  </section>

  <section class="card" id="detail" hidden></section>

  <section class="card">
    <details>
      <summary>How to read this</summary>
      <p><b>Where the numbers come from.</b> Every model call Claude Code makes is saved with its token counts in
        <code>~/.claude/projects</code>. A prompt usually triggers many calls: one per tool step. Each call sends the
        whole conversation again, so long chats get expensive per step.</p>
      <ul>
        <li><b>Re-reading context</b> (cache reads): the conversation sent again on every call. Cheap per token, but it adds up over many calls on a long chat.</li>
        <li><b>Caching context</b> (cache writes): new content (tool output, files, skills) and whole conversations re-cached after a break. A chat you come back to after the cache expired (5 minutes or 1 hour) pays this again for its full length.</li>
        <li><b>Uncached input</b>: small; the newest bit of each message.</li>
        <li><b>Output</b>: what Claude writes, including thinking. Expensive per token.</li>
      </ul>
      <p><b>Percentages</b> are calibrated: claude-usage and the top bar log every real usage reading, and the report fits
        those readings against the local token counts, weighted by API prices. With few readings the numbers are rough;
        they improve as readings accumulate. Without any readings, amounts are shown as API-equivalent dollars.</p>
      <p><b>Included</b>: Claude Code on this computer (terminal, IDE, the desktop app's Code tab) and the desktop
        app's Cowork sessions. <b>Not broken down</b>: claude.ai chats, the desktop app's chat, the phone and other
        computers. They count against the same limits but their conversations are kept on Anthropic's servers, so
        the report can't split them up. The gray "Elsewhere" band in the window charts is their total: what the
        measured readings show beyond the local usage (within the calibration error). Claude Code on the web runs on
        separate cloud credits.</p>
      <p><b>Patterns</b> sort the same amounts by cause. They overlap: a big file read in a long chat is re-read on
        every later step, so it counts under "big tool output" and under "very long chats". "Could be saved" assumes
        you'd have continued in a new chat started from a short summary (about 60k tokens with system prompt and
        tools) instead of coming back to the long one, and that chats stay under 150k tokens.</p>
    </details>
  </section>
  </div>

  <div id="tab-power" role="tabpanel" hidden></div>
</main>
<div id="tip" role="tooltip"></div>
<script>
"use strict";
const D = /*__DATA__*/null;
const NS = "http://www.w3.org/2000/svg";
const KINDS = [
  ["read", "Re-reading context", "var(--s1)"],
  ["write", "Caching context", "var(--s2)"],
  ["input", "Uncached input", "var(--s3)"],
  ["output", "Output & thinking", "var(--s4)"],
];
const UNITS = {
  h5: {label: "% of 5h", fit: D.fit.five_hour},
  wk: {label: "% of week", fit: D.fit.seven_day},
  usd: {label: "$ API", fit: null},
};
const LIMIT_UNIT = {five_hour: "h5", seven_day: "wk"};
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private window */ } },
};
// Percent by default; dollars only when there's no usage reading to calibrate with.
let unit = store.get("cu-unit");
if (!UNITS[unit] || (unit !== "usd" && !UNITS[unit].fit))
  unit = D.fit.five_hour ? "h5" : D.fit.seven_day ? "wk" : "usd";
// The unit for one limit's own chart: its own percent, else the chosen one
// (a 5h window in % of the week while only the week is calibrated).
const chartUnit = key => unit === "usd" ? "usd" : D.fit[key] ? LIMIT_UNIT[key] : unit;

// ---------------------------------------------------------------- helpers
function h(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k === "style") e.style.cssText = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) if (kid !== null && kid !== undefined && kid !== false)
    e.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  return e;
}
function sv(tag, attrs, parent) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (parent) parent.append(e);
  return e;
}
// amount in a unit, from API-equivalent dollars split into cache reads / everything else
function val(read, other, u = unit) {
  if (u === "usd") return read + other;
  const f = UNITS[u].fit;
  return f.k_read * read + f.k_other * other;
}
const reqVal = (r, u = unit) => val(r[3], r[4] + r[5] + r[6], u);
function kindVals(c, u = unit) {
  return {read: val(c.read, 0, u), write: val(0, c.write, u), input: val(0, c.input, u), output: val(0, c.output, u)};
}
function fmt(v, u = unit) {
  if (u === "usd") return v >= 100 ? "$" + v.toFixed(0) : v >= 10 ? "$" + v.toFixed(1) : "$" + v.toFixed(2);
  if (v > 0 && v < 0.1) return "<0.1%";
  return (v < 10 ? v.toFixed(1) : Math.round(v)) + "%";
}
function fmtAxis(v, u) {
  if (u === "W") return +v.toFixed(1) + " W";
  if (u === "usd") return "$" + (v >= 10 ? Math.round(v) : +v.toFixed(2));
  return +v.toFixed(1) + "%";
}
const fmtTime = t => new Date(t * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
const fmtDay = t => new Date(t * 1000).toLocaleDateString([], {weekday: "short", day: "numeric", month: "short"});
const fmtDT = t => fmtDay(t) + " " + fmtTime(t);
function fmtDur(s) {
  s = Math.max(0, Math.round(s));
  const d = Math.floor(s / 86400), hr = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  if (d) return `${d}d ${hr}h`;
  if (hr) return `${hr}h ${String(m).padStart(2, "0")}m`;
  if (m) return `${m}m`;
  return `${s}s`;
}
function fmtTok(n) {
  return n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1000 ? Math.round(n / 1000) + "k" : String(n);
}
const clip = (s, n) => s.length > n ? s.slice(0, n - 1) + "…" : s;
const sessionName = si => D.sessions[si].title;
function niceStep(max, count = 4) {
  const raw = max / count, p = 10 ** Math.floor(Math.log10(raw));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= raw) return m * p;
  return 10 * p;
}
function niceMax(max, u) {
  if (u !== "usd" && max > 75 && max <= 100) return 100;
  const st = niceStep(max);
  return Math.ceil(max / st - 1e-9) * st;
}
function timeTicks(x0, x1, width) {
  const span = x1 - x0, room = Math.max(2, Math.floor(width / 72));
  const H = 3600, steps = [900, 1800, H, 2 * H, 3 * H, 6 * H, 12 * H, 24 * H, 48 * H, 168 * H];
  const step = steps.find(s => span / s <= room) || 168 * H;
  const out = [], d = new Date(x0 * 1000);
  if (step < 24 * H) {
    d.setMinutes(step < H ? Math.ceil(d.getMinutes() / (step / 60)) * (step / 60) : 0, 0, 0);
    let t = d.getTime() / 1000;
    if (t < x0) t += step < H ? step : H;
    for (; t <= x1 && out.length < 200; t += step < H ? step : H) {
      const hr = new Date(t * 1000).getHours();
      if (step < H || hr % (step / H) === 0)
        out.push([t, hr === 0 && new Date(t * 1000).getMinutes() === 0 ? fmtDay(t) : fmtTime(t)]);
    }
  } else {
    d.setHours(0, 0, 0, 0);
    let t = d.getTime() / 1000;
    if (t < x0) { d.setDate(d.getDate() + 1); t = d.getTime() / 1000; }
    const every = step / (24 * H);
    for (let i = 0; t <= x1 && out.length < 60; i++) {
      if (i % every === 0) out.push([t, new Date(t * 1000).toLocaleDateString([], {weekday: "short", day: "numeric"})]);
      d.setDate(d.getDate() + 1);
      t = d.getTime() / 1000;
    }
  }
  return out;
}

// ---------------------------------------------------------------- tooltip
const tip = document.getElementById("tip");
function showTip(ev, title, rows, foot) {
  tip.replaceChildren(h("div", {class: "tt"}, title),
    ...rows.map(([color, name, value, line]) => h("div", {class: "tr"},
      h("span", {class: "sw" + (line ? " line" : ""), style: `background:${color}`}), h("span", {}, name), h("span", {}, value))),
    foot ? h("div", {class: "muted", style: "margin-top:4px"}, foot) : null);
  tip.style.display = "block";
  const r = tip.getBoundingClientRect();
  let x = ev.clientX + 14, y = ev.clientY + 14;
  if (x + r.width > innerWidth - 8) x = ev.clientX - r.width - 14;
  if (y + r.height > innerHeight - 8) y = ev.clientY - r.height - 14;
  tip.style.left = Math.max(8, x) + "px";
  tip.style.top = Math.max(8, y) + "px";
}
const hideTip = () => { tip.style.display = "none"; };

// ---------------------------------------------------------------- time chart
// o: {x0, x1, grid, layers:[{name,color}], values:[[...per grid]], stacked, unit, measured:[[t,v]],
//     dots:[{t,v}], marks:[{t,label}], now, height, label, tip(k, t) -> [title, rows, foot], onPick(layer),
//     pace: draw the even-pace line, 0% at x0 to 100% at x1}
function timeChart(box, o) {
  box.replaceChildren();
  const W = Math.max(280, box.clientWidth), H = o.height || 260;
  const m = {l: 48, r: 14, t: 16, b: 26};
  const svg = sv("svg", {width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": o.label});
  box.append(svg);
  const n = o.grid.length;
  const tops = o.grid.map((_, k) => o.stacked ? o.layers.reduce((a, _, L) => a + o.values[L][k], 0) : Math.max(...o.layers.map((_, L) => o.values[L][k])));
  const peak = Math.max(o.pace ? 100 : 0, ...tops, ...(o.measured || []).map(p => p[1]));
  const ymax = niceMax(peak > 0 ? peak : (o.unit === "usd" ? 1 : 10), o.unit);
  const X = t => m.l + (t - o.x0) / (o.x1 - o.x0) * (W - m.l - m.r);
  const Y = v => H - m.b - v / ymax * (H - m.t - m.b);
  const step = niceStep(ymax);
  for (let v = 0; v <= ymax + 1e-9; v += step) {
    sv("line", {x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v), style: `stroke:var(${v === 0 ? "--axis" : "--grid"});stroke-width:1`}, svg);
    sv("text", {x: m.l - 8, y: Y(v) + 4, "text-anchor": "end", style: "fill:var(--muted);font-size:11px;font-variant-numeric:tabular-nums"}, svg).textContent = fmtAxis(v, o.unit);
  }
  for (const [t, label] of timeTicks(o.x0, o.x1, W - m.l - m.r)) {
    sv("line", {x1: X(t), x2: X(t), y1: H - m.b, y2: H - m.b + 4, style: "stroke:var(--axis)"}, svg);
    sv("text", {x: X(t), y: H - 6, "text-anchor": "middle", style: "fill:var(--muted);font-size:11px"}, svg).textContent = label;
  }
  for (const mk of o.marks || []) {
    if (mk.t <= o.x0 || mk.t >= o.x1) continue;
    sv("line", {x1: X(mk.t), x2: X(mk.t), y1: m.t, y2: H - m.b, style: "stroke:var(--axis);stroke-width:1"}, svg);
    sv("text", {x: X(mk.t) + 4, y: m.t + 2, style: "fill:var(--muted);font-size:11px"}, svg).textContent = mk.label;
  }
  if (o.now && o.now > o.x0 && o.now < o.x1) {
    sv("line", {x1: X(o.now), x2: X(o.now), y1: m.t, y2: H - m.b, style: "stroke:var(--muted);stroke-width:1"}, svg);
    sv("text", {x: X(o.now) - 4, y: m.t + 2, "text-anchor": "end", style: "fill:var(--muted);font-size:11px"}, svg).textContent = "now";
  }
  const pts = (vals) => o.grid.map((t, k) => `${X(t).toFixed(1)},${Y(vals[k]).toFixed(1)}`);
  const stackTops = [];
  if (n) {
    let base = new Array(n).fill(0);
    o.layers.forEach((layer, L) => {
      const top = o.stacked ? base.map((b, k) => b + o.values[L][k]) : o.values[L];
      stackTops.push(top);
      const lower = o.stacked ? base : new Array(n).fill(0);
      sv("path", {d: `M${pts(top).join("L")}L${pts(lower).reverse().join("L")}Z`,
        style: `fill:${layer.color};fill-opacity:${o.stacked ? layer.opacity ?? 0.9 : 0.12}`}, svg);
      // stacked: a 2px surface line separates the layers; single: the 2px series line
      sv("polyline", {points: pts(top).join(" "), style: o.stacked ? "fill:none;stroke:var(--surface);stroke-width:2;stroke-linejoin:round" :
        `fill:none;stroke:${layer.color};stroke-width:2;stroke-linejoin:round;stroke-linecap:round`}, svg);
      if (o.stacked) base = top;
    });
  }
  if (o.pace)
    sv("line", {x1: X(o.x0), y1: Y(0), x2: X(o.x1), y2: Y(100),
      style: "stroke:var(--ink2);stroke-width:1.5;stroke-dasharray:5 4"}, svg);
  for (const d of o.dots || [])
    sv("circle", {cx: X(d.t), cy: Y(d.v), r: 4, style: `fill:${o.layers[0].color};stroke:var(--surface);stroke-width:2`}, svg);
  if (o.measured && o.measured.length) {
    if (o.measured.length > 1)
      sv("polyline", {points: o.measured.map(([t, v]) => `${X(t)},${Y(v)}`).join(" "),
        style: "fill:none;stroke:var(--ink);stroke-width:2;stroke-linejoin:round;stroke-linecap:round"}, svg);
    // every reading as a dot while they're sparse; past 40 just the line and the latest one
    for (const [t, v] of o.measured.length > 40 ? o.measured.slice(-1) : o.measured)
      sv("circle", {cx: X(t), cy: Y(v), r: 4, style: "fill:var(--ink);stroke:var(--surface);stroke-width:2"}, svg);
  }
  const cross = sv("line", {y1: m.t, y2: H - m.b, style: "stroke:var(--muted);stroke-width:1;display:none"}, svg);
  const hit = sv("rect", {x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b, style: "fill:transparent;cursor:crosshair"}, svg);
  const locate = ev => {
    const r = svg.getBoundingClientRect();
    const px = ev.clientX - r.left, py = ev.clientY - r.top;
    const t = o.x0 + (px - m.l) / (W - m.l - m.r) * (o.x1 - o.x0);
    let k = -1;
    if (n && t >= o.grid[0] - (o.x1 - o.x0) * 0.01) {
      let lo = 0, hi = n - 1;
      while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (o.grid[mid] <= t) lo = mid; else hi = mid - 1; }
      k = t > o.grid[n - 1] ? n - 1 : lo;
    }
    let layer = -1;
    if (k >= 0 && o.stacked) {
      const v = (H - m.b - py) / (H - m.t - m.b) * ymax;
      layer = stackTops.findIndex(top => v <= top[k]);
      if (layer >= 0 && o.values[layer][k] <= 0) layer = -1;
    } else if (k >= 0) layer = 0;
    return {t: Math.min(o.x1, Math.max(o.x0, t)), k, layer, px};
  };
  hit.addEventListener("mousemove", ev => {
    const {t, k, px} = locate(ev);
    cross.setAttribute("x1", px); cross.setAttribute("x2", px); cross.style.display = "";
    const [title, rows, foot] = o.tip(k, t);
    showTip(ev, title, rows, foot);
  });
  hit.addEventListener("mouseleave", () => { cross.style.display = "none"; hideTip(); });
  if (o.onPick) hit.addEventListener("click", ev => { const {layer} = locate(ev); if (layer >= 0) o.onPick(layer); });
}

// cumulative values per layer on an even time grid; reqs sorted by time
function cumulate(reqs, x0, x1, nLayers, layerOf, u, N = 220) {
  const grid = [], values = Array.from({length: nLayers}, () => []);
  const cum = new Array(nLayers).fill(0);
  let j = 0;
  for (let k = 0; k <= N; k++) {
    const t = x0 + (x1 - x0) * k / N;
    while (j < reqs.length && reqs[j][0] <= t) { cum[layerOf(reqs[j])] += reqVal(reqs[j], u); j++; }
    grid.push(t);
    cum.forEach((c, L) => values[L].push(c));
  }
  return {grid, values};
}
function valueAt(series, grid, t) {
  let lo = 0, hi = grid.length - 1;
  if (!grid.length || t < grid[0]) return 0;
  while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (grid[mid] <= t) lo = mid; else hi = mid - 1; }
  return series[lo];
}

// ---------------------------------------------------------------- windows
const REQ = D.req; // [t, session, prompt, read$, write$, input$, output$, subagent]
function reqsIn(s, e) {
  let lo = 0, hi = REQ.length;
  while (lo < hi) { const mid = (lo + hi) >> 1; if (REQ[mid][0] < s) lo = mid + 1; else hi = mid; }
  const out = [];
  for (let i = lo; i < REQ.length && REQ[i][0] < e; i++) out.push(REQ[i]);
  return out;
}
function readingsFor(key, w) {
  return (D.readings[key] || []).filter(([t, , r]) => Math.abs(r - w.e) < 600 && t >= w.s - 60 && t <= w.e + 60).map(([t, p]) => [t, p]);
}
const WV = {};
for (const key of ["five_hour", "seven_day"]) {
  const list = D.windows[key].map(([s, e, measured]) => ({s, e, measured}))
    .map(w => ({...w, reqs: reqsIn(w.s, w.e), readings: readingsFor(key, w)}))
    .filter(w => w.reqs.length || w.readings.length)
    .sort((a, b) => b.s - a.s);
  WV[key] = {list, i: 0};
}
const selectedWindow = key => WV[key].list[WV[key].i];

function windowLabel(key, w) {
  return key === "five_hour" ? `${fmtDT(w.s)} – ${fmtTime(w.e)}` : `${fmtDay(w.s)} – ${fmtDay(w.e)}`;
}
function renderWindow(key) {
  const box = document.getElementById("win-" + key);
  const st = WV[key];
  const name = key === "five_hour" ? "5-hour windows" : "Weekly windows";
  const u = chartUnit(key);
  box.replaceChildren();
  if (!st.list.length) {
    box.append(h("h2", {}, name), h("div", {class: "empty"}, "No usage in this period."));
    return;
  }
  const w = st.list[st.i];
  const sel = h("select", {"aria-label": "Window", onchange: e => { st.i = +e.target.value; renderWindow(key); renderChats(); }},
    st.list.map((x, i) => h("option", {value: i, selected: i === st.i}, windowLabel(key, x) + (x.e > D.generated && x.s <= D.generated ? " (current)" : ""))));
  const go = d => () => { st.i = Math.min(st.list.length - 1, Math.max(0, st.i + d)); renderWindow(key); renderChats(); };
  box.append(h("div", {class: "head"}, h("h2", {}, name),
    h("div", {class: "controls nav"},
      h("button", {onclick: go(1), disabled: st.i >= st.list.length - 1, "aria-label": "Earlier window"}, "‹"),
      sel,
      h("button", {onclick: go(-1), disabled: st.i === 0, "aria-label": "Later window"}, "›"))));

  const per = new Map();
  for (const r of w.reqs) per.set(r[1], (per.get(r[1]) || 0) + reqVal(r, u));
  const ranked = [...per.entries()].sort((a, b) => b[1] - a[1]);
  const top = ranked.slice(0, 7).map(([si]) => si);
  const layers = top.map((si, i) => ({si, name: sessionName(si), color: `var(--s${i + 1})`}));
  if (ranked.length > 7) layers.push({si: null, name: `${ranked.length - 7} other chats`, color: "var(--other)"});
  const layerOf = r => { const i = top.indexOf(r[1]); return i >= 0 ? i : layers.length - 1; };
  const tEnd = Math.max(w.s + 60, Math.min(w.e, D.generated));
  const {grid, values} = cumulate(w.reqs, w.s, tEnd, layers.length, layerOf, u);
  const measured = u === LIMIT_UNIT[key] ? w.readings : [];  // readings only on their own scale
  const nLocal = layers.length;
  const total = k => layers.slice(0, nLocal).reduce((a, _, L) => a + values[L][k], 0);
  // What the readings show beyond the local transcripts: claude.ai and the
  // desktop app's chat, the phone, other computers. Held from one reading to
  // the next; gaps within the calibration error are left out.
  let elsewhere = 0;
  if (measured.length) {
    const local = grid.map((_, k) => total(k));
    const noise = Math.max(1, D.fit[key] ? D.fit[key].err || 0 : 0);
    const gapAt = measured.map(([t, v]) => { const d = v - valueAt(local, grid, t); return d > noise ? d : 0; });
    const band = grid.map(t => { let i = -1; while (i + 1 < measured.length && measured[i + 1][0] <= t) i++; return i < 0 ? 0 : gapAt[i]; });
    elsewhere = gapAt[gapAt.length - 1];
    if (band.some(v => v > 0)) {
      layers.push({si: null, name: "Elsewhere: claude.ai, phone, other PCs", color: "var(--muted)", opacity: 0.35});
      values.push(band);
    }
  }

  // Even pace: 0% when the window starts, 100% at its reset. Only on the
  // limit's own percent scale.
  const pace = u === LIMIT_UNIT[key];
  const paceAt = t => 100 * Math.min(1, Math.max(0, (t - w.s) / (w.e - w.s)));
  const chart = h("div", {class: "chart"});
  box.append(chart);
  const draw = () => timeChart(chart, {
    x0: w.s, x1: w.e, grid, layers, values, stacked: true, unit: u, measured, now: D.generated, pace,
    label: `${name}: cumulative usage by chat`,
    tip: (k, t) => {
      const rows = [];
      const last = measured.filter(([mt]) => mt <= t).pop();
      if (last) rows.push(["var(--ink)", `measured at ${fmtTime(last[0])}`, fmt(last[1], u), true]);
      if (pace) rows.push(["var(--ink2)", "even pace", fmt(paceAt(t), u), true]);
      if (k >= 0) {
        rows.push(["transparent", "from local transcripts", fmt(total(k), u)]);
        layers.map((l, L) => [l, values[L][k]]).filter(([, v]) => v > 0).sort((a, b) => b[1] - a[1]).slice(0, 6)
          .forEach(([l, v]) => rows.push([l.color, clip(l.name, 48), fmt(v, u)]));
      }
      return [key === "five_hour" ? fmtTime(t) : fmtDT(t), rows, k >= 0 ? "click a band to open that chat" : null];
    },
    onPick: L => { if (layers[L].si !== null) openSession(layers[L].si); },
  });
  draw();
  resizers[key] = draw;

  const legend = h("div", {class: "legend"},
    layers.map(l => h("span", {}, h("i", {class: "sw", style: `background:${l.color}`}), clip(l.name, 40))),
    measured.length ? h("span", {}, h("i", {class: "sw line", style: "background:var(--ink)"}), "measured (/usage)") : null,
    pace ? h("span", {}, h("i", {class: "sw line", style: "background:repeating-linear-gradient(90deg,var(--ink2) 0 4px,transparent 4px 7px)"}),
      "even pace (100% at the reset)") : null);
  box.append(legend);

  const est = total(grid.length - 1);
  const lastM = measured[measured.length - 1];
  let cap;
  if (u === "usd" && !D.fit[key])
    cap = `${fmt(est, u)} API-equivalent in this window. Percentages appear once there is a usage reading (run claude-usage or the top bar).`;
  else if (u === "usd")
    cap = `${fmt(est, u)} API-equivalent from local Claude Code in this window.`;
  else if (u !== LIMIT_UNIT[key])
    cap = `About ${fmt(est, u)} of your week from local Claude Code in this window. ` +
      `It's shown in ${UNITS[u].label} because the ${key === "five_hour" ? "5-hour window" : "week"} isn't calibrated yet.`;
  else if (lastM) {
    const at = valueAt(grid.map((_, k) => total(k)), grid, lastM[0]);
    const elsewhere = Math.max(0, lastM[1] - at);
    cap = `Measured ${fmt(lastM[1], u)} at ${fmtTime(lastM[0])}; local Claude Code accounts for about ${fmt(at, u)}` +
      (elsewhere >= 2 ? `, so roughly ${fmt(elsewhere, u)} came from elsewhere (claude.ai, other devices) or calibration error.` : ".");
  } else
    cap = `About ${fmt(est, u)} from local Claude Code. No readings were logged in this window, so this is ` +
      `estimated from the token counts with the calibration from other windows.`;
  box.append(h("div", {class: "caption"}, cap));
  if (pace) {
    // Above the line you're using it faster than the limit allows.
    const tRef = lastM ? lastM[0] : tEnd, used = lastM ? lastM[1] : est, even = paceAt(tRef), diff = used - even;
    const when = tRef >= w.e - 60 ? "At the reset" : `At ${key === "five_hour" ? fmtTime(tRef) : fmtDT(tRef)}`;
    box.append(h("div", {class: "caption"}, h("b", {}, Math.abs(diff) < 1 ? "On pace. " :
      `${fmt(Math.abs(diff), u)} ${diff > 0 ? "over" : "under"} pace. `),
      `${when}: ${fmt(used, u)} used, where an even pace would be ${fmt(even, u)}. Above the dashed line you're using it faster than the limit allows.`));
  }

  const max = ranked.length ? ranked[0][1] : 1;
  box.append(h("div", {class: "rows"}, ranked.slice(0, 10).map(([si, v], idx) => {
    const color = idx < 7 ? `var(--s${idx + 1})` : "var(--other)";
    return h("div", {class: "row", tabindex: 0, role: "button", onclick: () => openSession(si),
      onkeydown: e => { if (e.key === "Enter") openSession(si); }},
      h("i", {class: "sw", style: `background:${color}`}),
      h("span", {class: "t"}, sessionName(si), " ", h("span", {class: "muted small"}, D.sessions[si].project)),
      h("span", {class: "track"}, h("i", {style: `width:${Math.max(1, 100 * v / max)}%;background:${color}`})),
      h("span", {class: "num"}, fmt(v, u)));
  }), elsewhere > 0 ? h("div", {class: "row", style: "cursor:default"},
    h("i", {class: "sw", style: "background:var(--muted);opacity:.5"}),
    h("span", {class: "t"}, "Elsewhere ", h("span", {class: "muted small"}, "claude.ai and desktop chat, phone, other computers (no breakdown available)")),
    h("span", {class: "track"}, h("i", {style: `width:${Math.min(100, Math.max(1, 100 * elsewhere / max))}%;background:var(--muted);opacity:.5`})),
    h("span", {class: "num"}, fmt(elsewhere, u))) : null));
}

// ---------------------------------------------------------------- tiles
function renderTiles() {
  const tiles = document.getElementById("tiles");
  const now = D.generated;
  const tile = (label, value, ...notes) => h("div", {class: "tile"}, h("div", {class: "label"}, label),
    h("div", {class: "value"}, value), ...notes.filter(Boolean).map(n => h("div", {class: "note"}, n)));
  for (const [key, label] of [["five_hour", "Current 5-hour window"], ["seven_day", "This week"]]) {
    const last = (D.readings[key] || []).slice(-1)[0];
    if (!last || last[2] <= now) {
      const w = D.windows[key].find(([s, e]) => s <= now && now < e);
      if (!D.fit[key]) tiles.append(tile(label, "—", D.notes[key] && D.notes[key].startsWith("No usable") ? "no usable reading yet" : "no usage reading logged yet"));
      else if (!w) tiles.append(tile(label, "0%", "no usage in the current window"));
      else tiles.append(tile(label, "~" + fmt(reqsIn(w[0], now).reduce((a, r) => a + reqVal(r, LIMIT_UNIT[key]), 0), LIMIT_UNIT[key]),
        "estimated from local Claude Code; no reading in this window"));
      continue;
    }
    const [t, pct, resets] = last;
    tiles.append(tile(label, Math.round(pct) + "%", `resets in ${fmtDur(resets - now)} · at ${key === "five_hour" ? fmtTime(resets) : fmtDT(resets)}`,
      `read ${fmtDur(now - t)} ago`));
  }
  const f5 = D.fit.five_hour, f7 = D.fit.seven_day;
  const missing = ["five_hour", "seven_day"].filter(k => D.notes[k]).map(k => (k === "five_hour" ? "% of 5h: " : "% of week: ") + D.notes[k]);
  if (f5 && f7) {
    const f = f5 || f7;
    if (f.n < 3)  // one or two points always fit themselves: no error estimate yet
      tiles.append(tile("Calibration", "rough", `from ${f.n} usage reading${f.n === 1 ? "" : "s"} so far`,
        "gets more accurate as the top bar or claude-usage logs readings (every 5 minutes)"));
    else
      tiles.append(tile("Calibration", `±${Math.max(1, Math.round(f.err))} pts`,
        `typical error of the ${f5 ? "5h" : "weekly"} estimate, fitted on ${f.n} readings`,
        `local Claude Code explains ~${Math.round(100 * Math.min(1, f.explained))}% of what was measured`));
  } else if (f5 || f7) {
    tiles.append(tile("Calibration", "partly", `% of ${f5 ? "5h" : "week"} works`, ...missing));
  } else {
    tiles.append(tile("Calibration", "not yet", ...missing, "until then amounts are API-equivalent dollars"));
  }
}

// ---------------------------------------------------------------- chats table
const periodSel = document.getElementById("period");
let period = "h5";
function periodRange() {
  if (period === "all") return [-Infinity, Infinity];
  const w = selectedWindow(period === "h5" ? "five_hour" : "seven_day");
  return w ? [w.s, w.e] : [-Infinity, Infinity];
}
function renderPeriods() {
  const opts = [];
  const w5 = selectedWindow("five_hour"), w7 = selectedWindow("seven_day");
  if (w5) opts.push(["h5", "Selected 5-hour window"]);
  if (w7) opts.push(["wk", "Selected week"]);
  opts.push(["all", `All ${D.days} days`]);
  if (!opts.some(([v]) => v === period)) period = opts[0][0];
  periodSel.replaceChildren(...opts.map(([v, l]) => h("option", {value: v, selected: v === period}, l)));
}
periodSel.addEventListener("change", e => { period = e.target.value; renderChats(); });
function renderUnits() {
  const box = document.getElementById("units");
  box.replaceChildren(...Object.entries(UNITS).map(([k, x]) =>
    h("button", {"aria-pressed": String(k === unit), disabled: k !== "usd" && !x.fit,
      title: k !== "usd" && !x.fit ? D.notes[k === "h5" ? "five_hour" : "seven_day"] : null,
      onclick: () => { unit = k; store.set("cu-unit", k); renderAll(); }}, x.label)));
  document.getElementById("kinds").replaceChildren(...KINDS.map(([, l, c]) => h("span", {}, h("i", {class: "sw", style: `background:${c}`}), l)));
}
function kindBar(kv, max) {
  const total = KINDS.reduce((a, [k]) => a + kv[k], 0);
  return h("span", {class: "track", style: `width:${Math.max(2, 100 * total / (max || 1))}%`,
    title: KINDS.map(([k, l]) => `${l}: ${fmt(kv[k])}`).join("\n")},
    KINDS.filter(([k]) => kv[k] > 0).map(([k, , c]) => h("i", {style: `flex:${kv[k]} 1 0;background:${c}`})));
}
let showAll = false;
function renderChats() {
  renderPeriods();
  const [s, e] = periodRange();
  const agg = new Map();
  for (const r of (s === -Infinity ? REQ : reqsIn(s, e))) {
    let a = agg.get(r[1]);
    if (!a) agg.set(r[1], a = {read: 0, write: 0, input: 0, output: 0, prompts: new Set(), calls: 0});
    a.read += r[3]; a.write += r[4]; a.input += r[5]; a.output += r[6];
    a.prompts.add(r[2]); a.calls++;
  }
  const rows = [...agg.entries()].map(([si, a]) => {
    const kv = kindVals(a);
    return {si, a, kv, v: kv.read + kv.write + kv.input + kv.output};
  }).sort((x, y) => y.v - x.v);
  const box = document.getElementById("chat-table");
  if (!rows.length) { box.replaceChildren(h("div", {class: "empty"}, "No chats in this period.")); return; }
  const max = rows[0].v, shown = showAll ? rows : rows.slice(0, 25);
  const table = h("table", {},
    h("thead", {}, h("tr", {},
      h("th", {}, "Chat"), h("th", {class: "num hide-sm"}, "Prompts"), h("th", {class: "num hide-sm"}, "Calls"),
      h("th", {class: "hide-sm", style: "width:28%"}, "Kind of usage"), h("th", {class: "num"}, UNITS[unit].label))),
    h("tbody", {}, shown.map(({si, a, kv, v}) => {
      const sess = D.sessions[si];
      const reason = sess.reasons[0];
      return h("tr", {tabindex: 0, onclick: () => openSession(si), onkeydown: ev => { if (ev.key === "Enter") openSession(si); }},
        h("td", {}, h("div", {class: "title"}, clip(sess.title, 140)),
          h("div", {class: "muted small"}, `${sess.project} · ${fmtDT(sess.start)}${sess.end - sess.start > 60 ? " – " + (fmtDay(sess.end) === fmtDay(sess.start) ? fmtTime(sess.end) : fmtDT(sess.end)) : ""}`),
          reason ? h("div", {class: "small sub"}, reason[0]) : null),
        h("td", {class: "num hide-sm"}, a.prompts.size), h("td", {class: "num hide-sm"}, a.calls),
        h("td", {class: "hide-sm"}, kindBar(kv, max)),
        h("td", {class: "num"}, fmt(v)));
    })));
  box.replaceChildren(table);
  if (rows.length > 25)
    box.append(h("div", {style: "margin-top:10px"}, h("button", {onclick: () => { showAll = !showAll; renderChats(); }},
      showAll ? "Show fewer" : `Show all ${rows.length} chats`)));
}

// ---------------------------------------------------------------- patterns
// The same usage sorted by cause, with the change that would have avoided it.
// Amounts are [cache-read $, other $] so they convert to any unit.
const patSel = document.getElementById("pat-period");
let patPeriod = store.get("cu-pat") || "7";
const PAT_PERIODS = [["1", "Last 24 hours"], ["7", "Last 7 days"], ["all", `All ${D.days} days`]];
patSel.replaceChildren(...PAT_PERIODS.map(([v, l]) => h("option", {value: v, selected: v === patPeriod}, l)));
patSel.addEventListener("change", e => { patPeriod = e.target.value; store.set("cu-pat", patPeriod); renderPatterns(); });
const PATTERNS = [
  {key: "breaks", name: "Coming back to a long chat after a break", color: "var(--s2)",
   why: "Claude keeps a conversation in its cache for up to an hour. After that, your next message caches the whole chat again, and that costs in proportion to its length: a one-line reply to a 900k-token chat costs as much as reading a book.",
   fix: "<b>Before you leave a long chat for more than an hour, ask for a short handoff summary and continue in a new chat.</b> Or run <code>/compact</code> before the break. Quick follow-ups within the hour are cheap; it's the comeback that costs."},
  {key: "long", name: "Very long chats (over 150k tokens)", color: "var(--s1)",
   why: "Every step (each tool call, each file read) sends the whole conversation again. Past 150k tokens each step costs several times what it would in a fresh chat, and a long task does many steps.",
   fix: "<b>One topic per chat.</b> When a chat passes ~150k tokens (the context bars in each prompt show it), finish the current thing and start fresh for the next one."},
  {key: "tools", name: "Big tool output kept in the chat", color: "var(--s3)",
   why: "Whatever a tool returns (a whole PDF, a long log, a big file) stays in the conversation and is re-read on every later step until the chat is compacted.",
   fix: "<b>Ask for the part you need</b>: page ranges of a PDF, <code>head</code>/<code>grep</code> of a log, one function instead of a file. Attach big documents in a separate chat that only extracts what you need."},
  {key: "agents", name: "Subagents", color: "var(--s7)",
   why: "Each subagent starts its own context and re-reads it every step. Worth it for wide searches, wasteful for small lookups.",
   fix: "<b>Ask for subagents only for broad searches or parallel work</b>, not for single-file questions."},
  {key: "output", name: "Writing (answers, files, thinking)", color: "var(--s4)",
   why: "Output costs about five times more per token than input. Long documents, rewritten files and deep thinking add up, but this is usually the part you wanted.",
   fix: "Mostly fine. If answers are longer than you need, say so; for small edits ask for an edit instead of a rewritten file."},
];
function patternAmounts(p) {
  return {
    breaks: [0, p.pat[0]], long: [p.pat[2], 0], tools: [p.tool[0], p.tool[1]],
    agents: [p.sub[0], p.sub[1]], output: [0, p.cost.output],
  };
}
function patternSaving(p) {
  return {breaks: [0, p.pat[1]], long: [p.pat[2], 0]};
}
function renderPatterns() {
  const box = document.getElementById("pat-body");
  // a week in % of a 5h window reads like 1000%; longer periods go in % of the week
  const pu = unit === "h5" && patPeriod !== "1" && D.fit.seven_day ? "wk" : unit;
  const val = (r, o) => window.val(r, o, pu), fmt = x => window.fmt(x, pu);
  const since = patPeriod === "all" ? -Infinity : D.generated - Number(patPeriod) * 86400;
  const tot = {}, save = {}, byChat = {}, examples = [];
  let total = 0;
  for (const P of PATTERNS) { tot[P.key] = [0, 0]; save[P.key] = [0, 0]; byChat[P.key] = new Map(); }
  D.sessions.forEach((sess, si) => sess.prompts.forEach(p => {
    if (p.t < since) return;
    total += val(p.cost.read, p.cost.write + p.cost.input + p.cost.output);
    const a = patternAmounts(p), sv_ = patternSaving(p);
    for (const P of PATTERNS) {
      const [r, o] = a[P.key];
      if (!(r + o)) continue;
      tot[P.key][0] += r; tot[P.key][1] += o;
      byChat[P.key].set(si, (byChat[P.key].get(si) || 0) + val(r, o));
      if (sv_[P.key]) { save[P.key][0] += sv_[P.key][0]; save[P.key][1] += sv_[P.key][1]; }
    }
    if (p.pat[0] > 0) examples.push({si, p, v: val(0, p.pat[0])});
  }));
  if (!total) { box.replaceChildren(h("div", {class: "empty"}, "No local Claude Code usage in this period.")); return; }
  const v = k => val(...tot[k]), sv2 = k => val(...save[k]);
  const avoid = sv2("breaks") + sv2("long");
  const label = PAT_PERIODS.find(([k]) => k === patPeriod)[1].toLowerCase();
  const unitWord = pu === "usd" ? " (API prices)" : pu === "h5" ? " of a 5-hour window" : " of a week";
  const top = PATTERNS.filter(P => P.key === "breaks" || P.key === "long").sort((a, b) => sv2(b.key) - sv2(a.key))[0];
  const rows = PATTERNS.filter(P => v(P.key) >= 0.005 * total).sort((a, b) => v(b.key) - v(a.key));
  box.replaceChildren(
    h("p", {class: "lead"}, `${label.charAt(0).toUpperCase() + label.slice(1)}, local Claude Code used ${fmt(total)}${unitWord}. `,
      avoid > 0.05 * total ? h("b", {}, `About ${fmt(avoid)}${pu === "usd" ? "" : unitWord} of it (${Math.round(100 * avoid / total)}%) could have been saved`) : "Little of it was avoidable",
      avoid > 0.05 * total ? `, mostly by ${top.key === "breaks" ? "starting a new chat instead of coming back to a long one" : "keeping chats shorter"}.` : "."),
    ...rows.map(P => {
      const value = v(P.key), saving = sv2(P.key);
      const chats = [...byChat[P.key].entries()].sort((a, b) => b[1] - a[1]).slice(0, 3);
      const exs = P.key === "breaks" ? examples.sort((a, b) => b.v - a.v).slice(0, 4) : [];
      return h("div", {class: "pat"},
        h("div", {}, h("div", {class: "name"}, P.name),
          h("div", {class: "muted small"}, `${Math.round(100 * value / total)}% of all local usage` +
            (saving > 0.005 * total ? ` · about ${Math.round(100 * saving / value)}% of this was avoidable` : ""))),
        h("div", {class: "meter", title: saving ? "hatched: could be saved" : null},
          h("i", {style: `width:${100 * value / total}%;background:${P.color}`}),
          saving ? h("i", {class: "save", style: `width:${100 * saving / total}%`}) : null),
        h("div", {class: "num", style: "text-align:right;font-weight:600"}, fmt(value)),
        h("div", {class: "why"}, P.why),
        exs.length ? h("div", {class: "ex"}, h("span", {class: "muted"}, "Most expensive comebacks:"),
          exs.map(({si, p, v: x}) => h("div", {},
            h("b", {}, fmt(x)), " · ", h("a", {class: "linky", tabindex: 0, onclick: () => openSession(si)}, clip(D.sessions[si].title, 40)),
            h("span", {class: "muted"}, ` · ${fmtDT(p.t)} · “${clip(p.text, 60)}”` + idleNote(p)))))
          : h("div", {class: "ex"}, h("span", {}, h("span", {class: "muted"}, "Mostly in: "),
              chats.map(([si, x], i) => [i ? ", " : "", h("a", {class: "linky", tabindex: 0, onclick: () => openSession(si)}, clip(D.sessions[si].title, 40)), ` (${fmt(x)})`]))),
        h("div", {class: "fix"}, ...htmlBits(P.fix)));
    }));
}
function idleNote(p) {
  const r = p.reasons.find(([t]) => t.startsWith("cache expired"));
  return r ? " · " + r[0].split(":")[0].replace("cache expired", "came back") + ", " + r[0].match(/\(([^)]+)\)/)?.[1] + " re-cached" : "";
}
// the fix texts carry a little markup (<b>, <code>); they're constants, not data
function htmlBits(s) {
  const t = document.createElement("template");
  t.innerHTML = s;
  return [...t.content.childNodes];
}
function chatAdvice(sess) {
  const brk = sess.prompts.reduce((a, p) => a + p.pat[0], 0), brkSave = sess.prompts.reduce((a, p) => a + p.pat[1], 0);
  const long = sess.prompts.reduce((a, p) => a + p.pat[2], 0);
  const total = sess.cost.read + sess.cost.write + sess.cost.input + sess.cost.output;
  const n = sess.prompts.filter(p => p.pat[0] > 0).length;
  const out = [], u = unit === "usd" ? "" : unit === "h5" ? " of a 5h window" : " of a week";
  if (brk > 0.15 * total)
    out.push(`${Math.round(100 * brk / total)}% of this chat (${fmt(val(0, brk))}${u}) was re-caching it after ${n} break${n === 1 ? "" : "s"}. ` +
      `Continuing in a new chat from a summary each time would have saved about ${fmt(val(0, brkSave))}${u}.`);
  if (long > 0.15 * total)
    out.push(`${Math.round(100 * long / total)}% (${fmt(val(long, 0))}${u}) was re-reading the part of the conversation beyond 150k tokens on every step. Splitting it into shorter chats would have avoided most of that.`);
  return out;
}

// ---------------------------------------------------------------- session detail
let openSi = null;
function openSession(si, scroll = true) {
  openSi = si;
  const sess = D.sessions[si];
  const box = document.getElementById("detail");
  box.hidden = false;
  const reqs = REQ.filter(r => r[1] === si);
  const totalKv = kindVals(sess.cost);
  const total = KINDS.reduce((a, [k]) => a + totalKv[k], 0);
  const tile = (label, value, note) => h("div", {class: "tile"}, h("div", {class: "label"}, label), h("div", {class: "value"}, value), note ? h("div", {class: "note"}, note) : null);
  box.replaceChildren(
    h("div", {class: "back"}, h("button", {onclick: () => { box.hidden = true; openSi = null; delete resizers.detail; document.getElementById("chats").scrollIntoView({behavior: "smooth"}); }}, "↑ Back to chats")),
    h("h2", {}, sess.title),
    h("div", {class: "sub small"}, `${sess.project} · ${fmtDT(sess.start)} – ${fmtDT(sess.end)} · ${sess.models.join(", ")}`),
    h("div", {class: "tiles"},
      tile("Total", fmt(total), unit === "usd" ? "API-equivalent" : UNITS[unit].label.replace("% of", "of a")),
      tile("Prompts", sess.prompts.length, `${sess.n} model calls` + (sess.ns ? ` + ${sess.ns} by subagents` : "")),
      tile("Peak context", fmtTok(sess.max_ctx), "tokens re-sent on the largest call"),
      sess.sub[0] + sess.sub[1] > 0 ? tile("Subagents", fmt(val(sess.sub[0], sess.sub[1])), `${Math.round(100 * val(sess.sub[0], sess.sub[1]) / (total || 1))}% of this chat`) : null),
    sess.reasons.length ? h("div", {class: "chips"}, sess.reasons.map(([t, s]) => h("span", {class: "chip"}, s > 0 ? h("b", {}, Math.round(100 * s) + "% ") : null, t))) : null,
    ...chatAdvice(sess).map(t => h("p", {class: "caption", style: "color:var(--ink)"}, h("b", {}, "What would have helped: "), t)),
  );

  // burn-up over time
  const x0 = sess.start, x1 = Math.max(sess.end, sess.start + 60);
  const {grid, values} = cumulate(reqs, x0, x1, 1, () => 0, unit, 300);
  const promptAt = t => { let p = null; for (const q of sess.prompts) if (q.t <= t) p = q; return p; };
  const dots = sess.prompts.map(p => ({t: p.t, v: valueAt(values[0], grid, p.t - 1e-6)}));
  const marks = D.windows.five_hour.filter(([s, e]) => e > x0 && e < x1).map(([, e]) => ({t: e, label: "5h reset"}));
  const chart = h("div", {class: "chart"});
  box.append(h("h3", {style: "margin-top:18px"}, "How fast it was used"), chart,
    h("div", {class: "caption"}, "Cumulative usage of this chat; dots mark where each prompt started. Hover for the prompt that was running."));
  const draw = () => timeChart(chart, {
    x0, x1, grid, values, layers: [{name: sess.title, color: "var(--s1)"}], unit, dots, marks, height: 220,
    label: "Cumulative usage of this chat",
    tip: (k, t) => {
      const p = promptAt(t);
      const rows = [["var(--s1)", "used so far", fmt(valueAt(values[0], grid, t))]];
      if (p) rows.push(["transparent", "this prompt in total", fmt(val(p.cost.read, p.cost.write + p.cost.input + p.cost.output))]);
      return [fmtDT(t), rows, p ? clip(p.text, 120) : null];
    },
  });
  draw();
  resizers.detail = draw;

  // prompts
  const pv = sess.prompts.map(p => { const kv = kindVals(p.cost); return {p, kv, v: KINDS.reduce((a, [k]) => a + kv[k], 0)}; });
  const maxV = Math.max(...pv.map(x => x.v), 1e-12);
  let order = "time";
  const list = h("div", {});
  const sortBtns = h("div", {class: "seg", role: "group", "aria-label": "Order"});
  const renderList = () => {
    sortBtns.replaceChildren(...[["time", "In order"], ["size", "Biggest first"]].map(([k, l]) =>
      h("button", {"aria-pressed": String(order === k), onclick: () => { order = k; renderList(); }}, l)));
    const items = order === "time" ? pv : [...pv].sort((a, b) => b.v - a.v);
    list.replaceChildren(...items.map(x => promptRow(x, maxV, sess.prompts.indexOf(x.p) + 1)));
  };
  box.append(h("div", {class: "head", style: "margin-top:18px"}, h("h3", {style: "margin:0"}, `Prompts (${sess.prompts.length})`), sortBtns),
    h("div", {class: "legend", style: "margin-bottom:6px"}, KINDS.map(([, l, c]) => h("span", {}, h("i", {class: "sw", style: `background:${c}`}), l))),
    list);
  renderList();
  if (scroll) box.scrollIntoView({behavior: "smooth", block: "start"});
}

function promptRow({p, kv, v}, maxV, num) {
  const dur = p.end - p.t;
  const tok = p.tok;
  const more = h("div", {class: "more"},
    h("div", {}, h("h3", {}, "Tokens"), h("div", {class: "kv"},
      h("span", {}, "re-read from cache"), h("span", {class: "num"}, fmtTok(tok.cr)),
      h("span", {}, "written to cache"), h("span", {class: "num"}, fmtTok(tok.cw)),
      h("span", {}, "uncached input"), h("span", {class: "num"}, fmtTok(tok.in)),
      h("span", {}, "output"), h("span", {class: "num"}, fmtTok(tok.out)),
      tok.think ? [h("span", {class: "muted"}, "  of it thinking"), h("span", {class: "num muted"}, fmtTok(tok.think))] : null,
      h("span", {}, "model calls"), h("span", {class: "num"}, p.n + (p.ns ? ` + ${p.ns} subagent` : "")),
      h("span", {}, "peak context"), h("span", {class: "num"}, fmtTok(p.max_ctx)))),
    p.tools.length ? h("div", {}, h("h3", {}, "Tools"), h("div", {class: "kv"},
      p.tools.map(([name, calls, out]) => [h("span", {}, name, h("span", {class: "muted"}, calls ? ` ×${calls}` : "")),
        h("span", {class: "num"}, out ? fmtTok(out) + " out" : "")]))) : null,
    p.ctx.length > 1 ? h("div", {}, h("h3", {}, "Context size per call"), ctxBars(p.ctx)) : null);
  const row = h("div", {class: "prompt"});
  const toggle = () => row.classList.toggle("open");
  row.append(
    h("div", {class: "top", tabindex: 0, role: "button", "aria-label": "Show details", onclick: toggle, onkeydown: e => { if (e.key === "Enter") toggle(); }},
      h("div", {}, h("div", {class: "meta"}, `#${num} · ${fmtDT(p.t)}` + (dur >= 60 ? ` · ran ${fmtDur(dur)}` : "") + ` · ${p.n + p.ns} calls`),
        h("div", {class: "text"}, p.text)),
      kindBar(kv, maxV),
      h("div", {class: "num", style: "text-align:right;font-weight:600"}, fmt(v))),
    p.reasons.length ? h("div", {class: "chips"}, p.reasons.map(([t, s]) => h("span", {class: "chip"}, s > 0 ? h("b", {}, Math.round(100 * s) + "% ") : null, t))) : null,
    more);
  return row;
}

function ctxBars(ctx) {
  const W = 260, H = 70, max = Math.max(...ctx), n = ctx.length;
  const bw = Math.max(1, Math.min(24, (W - 2 * (n - 1)) / n));
  const svg = sv("svg", {width: W, height: H + 14, viewBox: `0 0 ${W} ${H + 14}`, role: "img", "aria-label": "Context size per model call"});
  sv("line", {x1: 0, x2: W, y1: H, y2: H, style: "stroke:var(--axis)"}, svg);
  ctx.forEach((c, i) => {
    const bh = Math.max(1, c / max * (H - 4));
    sv("rect", {x: i * (bw + (bw > 2 ? 2 : 0.5)), y: H - bh, width: bw, height: bh, rx: Math.min(2, bw / 2), style: "fill:var(--s1)"}, svg)
      .append(Object.assign(document.createElementNS(NS, "title"), {textContent: `call ${i + 1}: ${fmtTok(c)} tokens`}));
  });
  sv("text", {x: 0, y: H + 12, style: "fill:var(--muted);font-size:11px"}, svg).textContent = `peak ${fmtTok(max)} tokens`;
  return h("div", {class: "chart"}, svg);
}

// ---------------------------------------------------------------- power tab
const PW = D.power;
let pwRange = store.get("cu-pw") || "1";
const PW_RANGES = [["1", "24 hours"], ["7", "7 days"], ["all", "All"]];
const CLAUDE_COLORS = ["var(--s2)", "var(--s5)", "var(--s8)"];
const OTHER_COLORS = ["var(--s1)", "var(--s3)", "var(--s7)", "var(--s6)", "var(--s4)"];
const fmtW = w => (w >= 10 ? w.toFixed(0) : w.toFixed(1)) + " W";
// how fast a draw empties the battery: % of a full charge per minute
const fmtRate = w => PW && PW.battery_wh ? (w / PW.battery_wh * 100 / 60).toFixed(2) + "%/min" : "";
const fmtWh = wh => wh >= 1000 ? (wh / 1000).toFixed(1) + " kWh" : wh >= 10 ? wh.toFixed(0) + " Wh" : wh.toFixed(1) + " Wh";
function renderPower() {
  const box = document.getElementById("tab-power");
  const now = D.generated;
  const since = pwRange === "all" ? -Infinity : now - Number(pwRange) * 86400;
  const bins = PW.bins.filter(b => b[0] + PW.bin > since);
  const G = PW.groups, isClaude = g => PW.claude.includes(g);
  const wh = new Array(G.length).fill(0);
  let secs = 0, measSecs = 0, acSecs = 0;
  for (const [, s, meas, ac, vals] of bins) {
    vals.forEach((v, i) => { wh[i] += v; });
    secs += s; if (meas !== null) measSecs += s; acSecs += ac * s;
  }
  const total = wh.reduce((a, b) => a + b, 0), hours = secs / 3600;
  const isBase = g => PW.base.includes(g);
  const claudeWh = G.reduce((a, g, i) => a + (isClaude(g) ? wh[i] : 0), 0);
  const baseWh = G.reduce((a, g, i) => a + (isBase(g) ? wh[i] : 0), 0);
  const active = total - baseWh;
  const m = PW.model, c = m.coef;
  const tile = (label, value, ...notes) => h("div", {class: "tile"}, h("div", {class: "label"}, label), h("div", {class: "value"}, value), ...notes.map(n => h("div", {class: "note"}, n)));
  const seg = h("div", {class: "seg", role: "group", "aria-label": "Time range"},
    PW_RANGES.map(([k, l]) => h("button", {"aria-pressed": String(k === pwRange), onclick: () => { pwRange = k; store.set("cu-pw", k); renderPower(); }}, l)));
  const chart = h("div", {class: "chart"});
  box.replaceChildren(
    h("div", {class: "tiles"},
      tile("Average draw", hours ? fmtW(total / hours) : "—",
        hours && PW.battery_wh ? `${fmtRate(total / hours)} of your ${PW.battery_wh.toFixed(0)} Wh battery` : "",
        `over ${fmtDur(secs)} sampled`,
        measSecs ? `${Math.round(100 * measSecs / secs)}% of it measured on battery` : "all on AC power: estimated"),
      tile("Energy", fmtWh(total), `${fmtWh(active)} of it from apps working (CPU and GPU), ` +
        `${fmtWh(baseWh)} screen, Wi-Fi and idle`),
      (() => {
        const B = PW.battery, sc = k => B && B.scenarios.find(s => s.key === k);
        const cur = sc("now") || sc("typical"), idle = sc("idle");
        return B ? tile("Battery life", cur ? fmtDur(cur.h * 3600) : "—",
          `at ${cur && cur.key === "now" ? "current use" : "your typical use"}, from ${Math.round(B.pct)}%` +
            (B.status && B.status !== "Discharging" ? " if unplugged now" : ""),
          idle ? `idle: ${fmtDur(idle.h * 3600)}` + (sc("typical") && cur.key === "now" ? ` · typical: ${fmtDur(sc("typical").h * 3600)}` : "") : "")
          : null;
      })(),
      tile("Claude", total ? `${Math.round(100 * claudeWh / total)}%` : "—", `${fmtWh(claudeWh)} of all power`,
        active ? `${Math.round(100 * claudeWh / active)}% of what apps added on top of the baseline` : ""),
      tile("Power model", m.fitted ? `±${m.err.toFixed(1)} W` : "typical",
        m.fitted ? `fitted on ${m.n} battery samples` : `needs ${Math.max(0, 30 - m.n)} more minutes on battery to fit your laptop`,
        `idle ${c.idle.toFixed(1)} W · screen up to ${c.screen.toFixed(1)} W · ` +
        `CPU up to ${c.cpu.toFixed(0)} W · GPU up to ${c.gpu.toFixed(1)} W`)),
    h("section", {class: "card", id: "pw-outlook"}),
    h("section", {class: "card"},
      h("div", {class: "head"}, h("h2", {}, "Power over time"), seg),
      chart,
      h("div", {class: "legend", id: "pw-legend"}),
      h("div", {class: "caption"}, "Average watts per app in each slot. The black line is the battery's own measurement, " +
        "only available while unplugged. Gaps are times the laptop was off, asleep or the sampler wasn't running.")),
    h("section", {class: "card"}, h("h2", {}, "Apps"), h("div", {id: "pw-table", style: "margin-top:10px"}),
      h("details", {style: "margin-top:12px"}, h("summary", {}, "How this is measured"),
        h("p", {}, "Linux doesn't measure power per part or per process. Once a minute the sampler reads how much CPU time " +
          "every process used, how much GPU time every graphics client used, how busy the GPU was, the screen's brightness " +
          "and the Wi-Fi traffic. Claude counts as three groups: Claude Code sessions, the desktop app, and everything " +
          "Claude starts (shells, scripts, MCP servers), found by following each process's parents."),
        h("p", {}, "The watts come from a model: idle + screen × brightness + CPU × busy CPUs + GPU × busy GPU + Wi-Fi × traffic. " +
          "It's fitted on the samples taken on battery, where the total is measured, and held near typical laptop values " +
          "for anything the data can't show yet (if you never change the brightness, it can't learn what the screen costs). " +
          "On battery the parts are scaled to add up to the measured draw. The CPU part goes to apps by CPU time, the GPU " +
          "part by GPU time; \"Graphics: other\" is GPU work no process of yours accounts for."),
        h("p", {}, "\"Idle\" is what's left: memory, chipset, SSD, an idle CPU, USB devices. Short commands that start and end " +
          "within a minute are caught through their parent process; whatever can't be placed is listed as \"Short-lived processes\"."))),
    h("section", {class: "card", id: "pw-watch"}));

  // chart layers: baseline, Claude groups, the biggest other apps, the rest as Other
  const order = G.map((g, i) => i).filter(i => !isBase(G[i])).sort((a, b) => wh[b] - wh[a]);
  const claudeIdx = order.filter(i => isClaude(G[i]));
  const otherIdx = order.filter(i => !isClaude(G[i]) && G[i] !== "Other").slice(0, OTHER_COLORS.length);
  const BASE_STYLE = {[PW.base[0]]: ["Idle", "var(--other)", 0.55], [PW.base[1]]: ["Screen", "var(--ink2)", 0.45], [PW.base[2]]: ["Wi-Fi", "var(--axis)", 0.9]};
  const baseIdx = G.map((g, i) => i).filter(i => isBase(G[i]) && wh[i] > 0);
  const shown = new Set([...baseIdx, ...claudeIdx, ...otherIdx]);
  const layers = [], idxs = [];
  for (const i of baseIdx) { const [n, col, op] = BASE_STYLE[G[i]]; layers.push({name: n, color: col, opacity: op}); idxs.push([i]); }
  otherIdx.forEach((i, k) => { layers.push({name: G[i], color: OTHER_COLORS[k]}); idxs.push([i]); });
  const rest = G.map((g, i) => i).filter(i => !shown.has(i));
  if (rest.length) { layers.push({name: "Other apps", color: "var(--muted)", opacity: 0.6}); idxs.push(rest); }
  claudeIdx.forEach((i, k) => { layers.push({name: G[i], color: CLAUDE_COLORS[k % 3]}); idxs.push([i]); });
  const step = (pwRange === "1" ? 1 : 4) * PW.bin;
  const x1 = now, x0 = pwRange === "all" ? (bins.length ? bins[0][0] : now - 86400) : since;
  const slots = new Map();
  for (const b of bins) {
    const k = Math.floor(b[0] / step) * step;
    let s = slots.get(k);
    if (!s) slots.set(k, s = {secs: 0, wh: new Array(G.length).fill(0), meas: [0, 0], br: [0, 0]});
    s.secs += b[1]; b[4].forEach((v, i) => { s.wh[i] += v; });
    if (b[5] !== null && b[5] !== undefined) { s.br[0] += b[5] * b[1]; s.br[1] += b[1]; }
    if (b[2] !== null) { s.meas[0] += b[2] * b[1]; s.meas[1] += b[1]; }
  }
  const grid = [], values = layers.map(() => []), measured = [];
  for (let t = Math.floor(x0 / step) * step; t <= x1; t += step) {
    const s = slots.get(t);
    grid.push(t + step / 2);
    idxs.forEach((ii, L) => values[L].push(s && s.secs ? ii.reduce((a, i) => a + s.wh[i], 0) / (s.secs / 3600) : 0));
    if (s && s.meas[1] >= 0.5 * s.secs) measured.push([t + step / 2, s.meas[0] / s.meas[1]]);
  }
  document.getElementById("pw-legend").replaceChildren(
    ...layers.map(l => h("span", {}, h("i", {class: "sw", style: `background:${l.color}`}), l.name)),
    ...(measured.length ? [h("span", {}, h("i", {class: "sw line", style: "background:var(--ink)"}), "measured on battery")] : []));
  const draw = () => timeChart(chart, {
    x0, x1, grid, values, layers, stacked: true, unit: "W", measured, now, height: 280, label: "Watts per app over time",
    tip: (k) => {
      if (k < 0) return ["", []];
      const s = slots.get(grid[k] - step / 2);
      const rows = layers.map((l, L) => [l.color, l.name, fmtW(values[L][k])]).filter(r => parseFloat(r[2]) > 0).reverse();
      const tot = values.reduce((a, v) => a + v[k], 0);
      return [`${fmtDT(grid[k] - step / 2)} – ${fmtTime(grid[k] + step / 2)}`, rows,
        s && s.secs ? `total ${fmtW(tot)}` + (s.meas[1] ? ` · measured ${fmtW(s.meas[0] / s.meas[1])}` : " · on AC, estimated") +
          (s.br[1] ? ` · screen at ${Math.round(100 * s.br[0] / s.br[1])}%` : "") : "not sampled"];
    },
  });
  draw();
  resizers.power = draw;

  // table
  const rows = G.map((g, i) => ({g, wh: wh[i]})).filter(r => r.wh > 0).sort((a, b) => b.wh - a.wh);
  const maxWh = Math.max(...rows.map(r => r.wh), claudeWh, 1e-9);
  const bar = (x, color) => h("span", {class: "track", style: `width:${Math.max(1, 100 * x / maxWh)}%`}, h("i", {style: `flex:1 1 0;background:${color}`}));
  const colorOf = g => { const L = layers.findIndex((l, k) => idxs[k].includes(G.indexOf(g))); return L >= 0 ? layers[L].color : "var(--muted)"; };
  const tr = (name, x, color, cls, tag) => h("tr", {class: cls || null, style: "cursor:default"},
    h("td", {}, h("span", {class: "title"}, name), tag ? h("span", {class: "tag"}, tag) : null),
    h("td", {class: "num"}, fmtWh(x)), h("td", {class: "num hide-sm"}, hours ? fmtW(x / hours) : "—"),
    h("td", {class: "num hide-sm muted"}, hours ? fmtRate(x / hours) : ""),
    h("td", {class: "hide-sm", style: "width:30%"}, bar(x, color)),
    h("td", {class: "num"}, total ? (100 * x / total).toFixed(x / total < 0.1 ? 1 : 0) + "%" : "—"));
  document.getElementById("pw-table").replaceChildren(rows.length ? h("table", {},
    h("thead", {}, h("tr", {}, h("th", {}, "App"), h("th", {class: "num"}, "Energy"), h("th", {class: "num hide-sm"}, "Average"), h("th", {class: "num hide-sm"}, "Battery"),
      h("th", {class: "hide-sm"}, ""), h("th", {class: "num"}, "Share"))),
    h("tbody", {},
      claudeWh ? tr("All of Claude", claudeWh, "var(--s2)", "claude", "3 groups below") : null,
      rows.map(r => tr(r.g, r.wh, colorOf(r.g),
        isClaude(r.g) ? "claude" : null, isClaude(r.g) ? "Claude" : isBase(r.g) ? "not an app" : null)))) : h("div", {class: "empty"}, "No samples in this range."));
  renderWatch(since);
  renderOutlook(document.getElementById("pw-outlook"));
}

// battery life: the charge so far and how long what's left lasts under each kind of use
const SCEN_COLORS = {now: "var(--s2)", typical: "var(--s1)", idle: "var(--s3)", idle_dim: "var(--s7)"};
function renderOutlook(card) {
  const B = PW.battery;
  if (!B || !B.scenarios.length) { card.hidden = true; return; }
  const now = D.generated;
  const plugged = B.status && B.status !== "Discharging";
  const hist = PW.bins.filter(b => b[6] !== null && b[6] !== undefined && b[0] >= now - 12 * 3600).map(b => [b[0] + PW.bin, b[6]]);
  const maxH = Math.min(36, Math.max(...B.scenarios.map(s => s.h)));
  const x0 = now - 12 * 3600, x1 = now + Math.max(2, maxH * 1.04) * 3600;
  const chart = h("div", {class: "chart"});
  const legend = h("div", {class: "legend"},
    hist.length ? h("span", {}, h("i", {class: "sw line", style: "background:var(--ink)"}), "charge so far") : null,
    B.scenarios.map(s => h("span", {}, h("i", {class: "sw line", style: `background:${SCEN_COLORS[s.key]}`}),
      `${s.label}: ${fmtDur(s.h * 3600)} (${fmtW(s.w)})`)));
  card.replaceChildren(
    h("div", {class: "head"}, h("h2", {}, "Battery life"),
      h("span", {class: "muted small"}, `${Math.round(B.pct)}% of ${B.full_wh.toFixed(0)} Wh` + (plugged ? ` · plugged in (${(B.status || "").toLowerCase()}): times are if you unplugged now` : ""))),
    chart, legend,
    h("div", {class: "caption"}, "How long the charge lasts at each kind of use, as straight lines from now to empty. " +
      B.scenarios.map(s => `${s.label}: ${s.note}.`).join(" ") +
      " Idle and screen figures come from the power model, so they firm up as it learns your laptop on battery."));
  const draw = () => {
    chart.replaceChildren();
    const W = Math.max(280, chart.clientWidth), H = 240, m = {l: 44, r: 14, t: 14, b: 26};
    const svg = sv("svg", {width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Battery charge and projections"});
    chart.append(svg);
    const X = t => m.l + (t - x0) / (x1 - x0) * (W - m.l - m.r), Y = v => H - m.b - v / 100 * (H - m.t - m.b);
    for (let v = 0; v <= 100; v += 25) {
      sv("line", {x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v), style: `stroke:var(${v ? "--grid" : "--axis"})`}, svg);
      sv("text", {x: m.l - 8, y: Y(v) + 4, "text-anchor": "end", style: "fill:var(--muted);font-size:11px"}, svg).textContent = v + "%";
    }
    for (const [t, label] of timeTicks(x0, x1, W - m.l - m.r)) {
      sv("line", {x1: X(t), x2: X(t), y1: H - m.b, y2: H - m.b + 4, style: "stroke:var(--axis)"}, svg);
      sv("text", {x: X(t), y: H - 6, "text-anchor": "middle", style: "fill:var(--muted);font-size:11px"}, svg).textContent = label;
    }
    sv("line", {x1: X(now), x2: X(now), y1: m.t, y2: H - m.b, style: "stroke:var(--muted)"}, svg);
    sv("text", {x: X(now) + 4, y: m.t + 2, style: "fill:var(--muted);font-size:11px"}, svg).textContent = "now";
    if (hist.length > 1)
      sv("polyline", {points: hist.map(([t, v]) => `${X(t)},${Y(v)}`).join(" "),
        style: "fill:none;stroke:var(--ink);stroke-width:2;stroke-linejoin:round"}, svg);
    for (const s of B.scenarios) {
      const tEnd = now + s.h * 3600, tCut = Math.min(tEnd, x1);
      const vCut = B.pct * (1 - (tCut - now) / (tEnd - now));
      sv("line", {x1: X(now), y1: Y(B.pct), x2: X(tCut), y2: Y(vCut),
        style: `stroke:${SCEN_COLORS[s.key]};stroke-width:2;stroke-linecap:round;${s.key === "now" ? "" : "stroke-dasharray:6 4"}`}, svg)
        .append(Object.assign(document.createElementNS(NS, "title"), {textContent: `${s.label}: empty in ${fmtDur(s.h * 3600)}, at ${fmtDT(tEnd)}`}));
      if (tEnd <= x1) sv("circle", {cx: X(tEnd), cy: Y(0), r: 3.5, style: `fill:${SCEN_COLORS[s.key]};stroke:var(--surface);stroke-width:2`}, svg);
    }
    sv("circle", {cx: X(now), cy: Y(B.pct), r: 4, style: "fill:var(--ink);stroke:var(--surface);stroke-width:2"}, svg);
  };
  draw();
  resizers.outlook = draw;
}

// the battery watch: settings and what it did
function renderWatch(since) {
  const cfg = PW.config, ev = PW.events.filter(e => e.t >= since).reverse();
  const pct = x => Math.round(100 * x) + "%";
  const rate = w => { const r = fmtRate(w); return r ? ` · ${r}` : ""; };
  const text = e => e.ev === "alert" ? `High draw: ${e.w.toFixed(0)} W${rate(e.w)} on battery (alert above ${e.limit.toFixed(0)} W). ` +
      `Biggest: ${(e.top || []).map(([g, w]) => `${g} ${w.toFixed(1)} W${rate(w)}`).join(", ")}`
    : e.ev === "dim" ? `Dimmed the screen ${pct(e.frm)} → ${pct(e.to)} at ${e.w.toFixed(0)} W`
    : e.ev === "restore" ? `Plugged in: brightness back to ${pct(e.br)}`
    : e.ev === "manual" ? `You changed the brightness (${pct(e.br)}): no more dimming until next unplugged` : e.ev;
  document.getElementById("pw-watch").replaceChildren(
    h("h2", {}, "Battery watch"),
    h("p", {class: "caption"}, `On battery, if the laptop draws much more than usual for 3 minutes, ` +
      (cfg.alert ? "you get a notification naming the biggest users" : "nothing is shown (alerts are off)") +
      (cfg.dim ? ` and the screen brightness is halved, at most every 10 minutes and never below ${pct(cfg.dim_floor)}. ` +
        "Change the brightness yourself and it stops until you next unplug; plugging in restores it." : ". Dimming is off.") +
      ` Threshold: ${cfg.alert_watts ? cfg.alert_watts + " W" : "1.6× your usual draw on battery once it's known, 15 W until then"}.`),
    h("p", {class: "caption"}, h("b", {}, "To change it: "), "in the top bar, open Claude usage → Power and flip the switches, " +
      "or in a terminal:"),
    h("pre", {class: "cmd"}, "claude-usage power config --alert on|off --dim on|off --watts N --floor PERCENT"),
    h("p", {class: "caption"}, `Now: alerts ${cfg.alert ? "on" : "off"}, dimming ${cfg.dim ? "on" : "off"}` +
      ` (as of when this page was built).`),
    ev.length ? h("table", {style: "margin-top:8px"}, h("tbody", {}, ev.slice(0, 50).map(e =>
      h("tr", {style: "cursor:default"}, h("td", {class: "num muted", style: "width:1%"}, fmtDT(e.t)), h("td", {}, text(e))))))
      : h("div", {class: "muted small", style: "margin-top:8px"}, "Nothing flagged in this range."));
}

// ---------------------------------------------------------------- tabs
// The Power tab is optional: it exists only once `claude-usage power install` has collected samples.
if (!PW) document.getElementById("tabs").hidden = true;
let tab = !PW ? "usage" : location.hash === "#power" ? "power" : location.hash === "#usage" ? "usage" :
  store.get("cu-tab") === "power" ? "power" : "usage";
let powerDrawn = false;
function showTab(t) {
  tab = t; store.set("cu-tab", t);
  try { history.replaceState(null, "", "#" + t); } catch { /* file: URLs in some browsers */ }
  for (const k of ["usage", "power"]) {
    document.getElementById("tab-" + k).hidden = k !== t;
    document.getElementById("tabbtn-" + k).setAttribute("aria-selected", String(k === t));
  }
  document.getElementById("unit-controls").style.visibility = t === "usage" ? "" : "hidden";
  if (t === "power" && !powerDrawn) { powerDrawn = true; renderPower(); }
  // charts in a hidden tab were drawn at the minimum width: redraw the shown tab's once laid out
  const powerCharts = ["power", "outlook"];
  requestAnimationFrame(() => Object.entries(resizers).forEach(([k, f]) => {
    if (powerCharts.includes(k) === (t === "power")) f();
  }));
}
document.getElementById("tabbtn-usage").addEventListener("click", () => showTab("usage"));
document.getElementById("tabbtn-power").addEventListener("click", () => showTab("power"));

// ---------------------------------------------------------------- boot
const resizers = {};
function renderAll() {
  renderWindow("five_hour");
  renderWindow("seven_day");
  renderUnits();
  renderChats();
  renderPatterns();
  if (openSi !== null) openSession(openSi, false);
}
document.getElementById("gen").textContent =
  `Generated ${fmtDT(D.generated)}${D.plan ? " · " + D.plan.charAt(0).toUpperCase() + D.plan.slice(1) + " plan" : ""} · ` +
  `${D.sessions.length} Claude Code and Cowork chat${D.sessions.length === 1 ? "" : "s"} from the last ${D.days} days on this computer`;
if (D.warning) {
  const w = document.getElementById("warning");
  w.textContent = "⚠ Couldn't read your current usage: " + D.warning;
  w.hidden = false;
}
renderTiles();
renderAll();
showTab(tab);
let rt = 0;
addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => Object.values(resizers).forEach(f => f()), 150); });
</script>
</body>
</html>
"""

if __name__ == "__main__":
    sys.exit(main())
