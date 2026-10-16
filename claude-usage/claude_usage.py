#!/usr/bin/env python3
"""claude-usage: a live terminal view of your Claude plan usage limits.

Shows the current 5-hour session, the weekly limits and the time left until
each one resets. It reuses the OAuth login that Claude Code already stores on
your machine and asks the same endpoint the `/usage` command in Claude Code
uses. No third-party dependencies.

Modes:
    claude-usage            live dashboard (q to quit, r to refresh)
    claude-usage --once     print the dashboard once and exit
    claude-usage --line     one compact line, for tmux / status bars
    claude-usage --json     raw JSON from the API
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

__version__ = "0.1.0"

API_URL = os.environ.get("CLAUDE_USAGE_API_URL", "https://api.anthropic.com/api/oauth/usage")
OAUTH_BETA = "oauth-2025-04-20"

HOUR = 3600
DAY = 24 * HOUR

# key -> (label, short label for --line, window length in seconds)
KNOWN_LIMITS = {
    "five_hour": ("Current session (5h)", "5h", 5 * HOUR),
    "seven_day": ("Week · all models", "wk", 7 * DAY),
    "seven_day_opus": ("Week · Opus", "opus", 7 * DAY),
    "seven_day_sonnet": ("Week · Sonnet", "sonnet", 7 * DAY),
    "seven_day_oauth_apps": ("Week · OAuth apps", "apps", 7 * DAY),
}
ALWAYS_SHOWN = ("five_hour", "seven_day")


# --------------------------------------------------------------------------
# credentials


class UsageError(Exception):
    """An error with a message meant for the user."""


@dataclass
class Credentials:
    token: str
    expires_at: float | None = None  # unix seconds
    plan: str | None = None
    source: str = ""


def claude_config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def _from_blob(blob: dict, source: str) -> Credentials | None:
    oauth = blob.get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    if not token:
        return None
    expires = oauth.get("expiresAt")
    return Credentials(
        token=token,
        expires_at=expires / 1000 if isinstance(expires, (int, float)) else None,
        plan=oauth.get("subscriptionType"),
        source=source,
    )


def load_credentials() -> Credentials:
    """Find the Claude Code OAuth token. Re-read on every fetch, so a token
    refreshed by Claude Code in the meantime is picked up automatically."""
    env_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if env_token:
        return Credentials(token=env_token, source="$CLAUDE_CODE_OAUTH_TOKEN")

    path = claude_config_dir() / ".credentials.json"
    if path.is_file():
        try:
            creds = _from_blob(json.loads(path.read_text()), str(path))
        except (OSError, ValueError) as exc:
            raise UsageError(f"could not read {path}: {exc}") from exc
        if creds:
            return creds

    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                capture_output=True, text=True, timeout=10,
            )
            if out.returncode == 0 and out.stdout.strip():
                creds = _from_blob(json.loads(out.stdout), "macOS Keychain")
                if creds:
                    return creds
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass

    raise UsageError(
        "no Claude login found. Log in with Claude Code (`claude`, then /login) "
        "using your Pro/Max account, or set CLAUDE_CODE_OAUTH_TOKEN."
    )


# --------------------------------------------------------------------------
# API


def fetch_usage(creds: Credentials, timeout: float = 15) -> dict:
    req = urllib.request.Request(
        API_URL,
        headers={
            "Authorization": f"Bearer {creds.token}",
            "anthropic-beta": OAUTH_BETA,
            "Accept": "application/json",
            "User-Agent": f"claude-usage/{__version__}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            hint = "token expired or rejected. Open Claude Code once so it refreshes your login."
            raise UsageError(f"HTTP {exc.code}: {hint}") from exc
        if exc.code == 429:
            raise UsageError("HTTP 429: rate limited by the usage endpoint, backing off.") from exc
        raise UsageError(f"HTTP {exc.code} from usage endpoint") from exc
    except urllib.error.URLError as exc:
        raise UsageError(f"network error: {exc.reason}") from exc
    except ValueError as exc:
        raise UsageError("usage endpoint returned invalid JSON") from exc


# --------------------------------------------------------------------------
# parsing


@dataclass
class Limit:
    key: str
    label: str
    short: str
    pct: float | None
    resets_at: datetime | None
    window: int | None

    def seconds_left(self, now: datetime) -> float | None:
        if self.resets_at is None:
            return None
        return max(0.0, (self.resets_at - now).total_seconds())

    def elapsed_fraction(self, now: datetime) -> float | None:
        left = self.seconds_left(now)
        if left is None or not self.window:
            return None
        return min(1.0, max(0.0, 1 - left / self.window))


def parse_time(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _guess(key: str) -> tuple[str, str, int | None]:
    if key in KNOWN_LIMITS:
        return KNOWN_LIMITS[key]
    label = key.replace("_", " ").capitalize()
    if key.startswith("five_hour"):
        return label, key[:6], 5 * HOUR
    if key.startswith("seven_day"):
        return label, key[:6], 7 * DAY
    return label, key[:6], None


def parse_limits(data: dict) -> list[Limit]:
    """Turn the API response into a list of limits. Any object that carries a
    `utilization` field is treated as a limit, so new limit types show up
    without a code change."""
    limits = []
    for key, val in data.items():
        if not isinstance(val, dict) or "utilization" not in val or key == "extra_usage":
            continue
        pct = val.get("utilization")
        if pct is None and key not in ALWAYS_SHOWN:
            continue
        label, short, window = _guess(key)
        limits.append(Limit(
            key=key, label=label, short=short,
            pct=float(pct) if pct is not None else None,
            resets_at=parse_time(val.get("resets_at")),
            window=window,
        ))
    order = list(KNOWN_LIMITS)
    limits.sort(key=lambda l: order.index(l.key) if l.key in order else len(order))
    return limits


def parse_extra(data: dict) -> float | None:
    extra = data.get("extra_usage")
    if isinstance(extra, dict) and extra.get("is_enabled") and extra.get("utilization") is not None:
        return float(extra["utilization"])
    return None


# --------------------------------------------------------------------------
# formatting


def fmt_duration(seconds: float, with_seconds: bool = False) -> str:
    s = int(seconds)
    d, s = divmod(s, DAY)
    h, s = divmod(s, HOUR)
    m, s = divmod(s, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m:02d}m {s:02d}s" if with_seconds else f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s" if with_seconds else f"{m}m"
    return f"{s}s"


def fmt_compact(seconds: float) -> str:
    return fmt_duration(seconds).replace(" ", "")


def fmt_clock(dt: datetime, now: datetime) -> str:
    local, today = dt.astimezone(), now.astimezone()
    if local.date() == today.date():
        return local.strftime("%H:%M")
    return local.strftime("%a %d %b %H:%M")


class Style:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def __call__(self, text: str, *codes: str) -> str:
        if not self.enabled or not codes:
            return text
        return f"\x1b[{';'.join(codes)}m{text}\x1b[0m"

    def for_pct(self, pct: float | None) -> str:
        if pct is None:
            return "90"
        return "31" if pct >= 80 else "33" if pct >= 50 else "32"


def bar(pct: float | None, elapsed: float | None, width: int, st: Style) -> str:
    """Usage bar. The ┃ marker shows how far through the window you are:
    fill to its left of it means you are on or under pace."""
    filled = 0 if pct is None else round(min(100.0, max(0.0, pct)) / 100 * width)
    marker = None if elapsed is None else min(width - 1, int(elapsed * width))
    out = []
    for i in range(width):
        if i == marker:
            out.append(st("┃", "1", "97" if i < filled else "37"))
        elif i < filled:
            out.append(st("█", st.for_pct(pct)))
        else:
            out.append(st("░", "90"))
    return "".join(out)


def pace_note(limit: Limit, now: datetime) -> str | None:
    """Linear projection: when do you hit 100% at the current burn rate?"""
    left = limit.seconds_left(now)
    if limit.pct is None or left is None or not limit.window:
        return None
    if limit.pct >= 100:
        return "limit reached"
    elapsed = limit.window - left
    if elapsed < limit.window * 0.05 or limit.pct <= 0:
        return None  # too early to say anything useful
    rate = limit.pct / elapsed  # percent per second
    to_full = (100 - limit.pct) / rate
    if to_full < left:
        return f"at this pace: limit in ~{fmt_duration(to_full)}"
    return f"at this pace: ~{min(100.0, limit.pct + rate * left):.0f}% at reset"


def render(state: "State", now: datetime, width: int, st: Style, live: bool) -> list[str]:
    inner = max(20, min(width - 4, 70))
    lines = []

    plan = f" · {state.plan.capitalize()} plan" if state.plan else ""
    lines.append(st(" Claude usage", "1") + st(plan, "90"))
    lines.append("")

    if state.data is None:
        lines.append(st(f" {state.error or 'loading…'}", "31" if state.error else "90"))
        return lines

    for limit in parse_limits(state.data):
        pct_txt = "  —" if limit.pct is None else f"{limit.pct:3.0f}%"
        gap = max(1, inner - len(limit.label) - len(pct_txt))
        lines.append(" " + st(limit.label, "1") + " " * gap + st(pct_txt, "1", st.for_pct(limit.pct)))
        lines.append(" " + bar(limit.pct, limit.elapsed_fraction(now), inner, st))

        left = limit.seconds_left(now)
        if left is None:
            if limit.key == "five_hour":
                detail = "no active session. The 5h window starts with your next message"
            else:
                detail = "no reset scheduled"
        elif left == 0:
            detail = "resetting now…"
        else:
            detail = (f"resets in {st(fmt_duration(left, with_seconds=live), '1')}"
                      f" · at {fmt_clock(limit.resets_at, now)}")
        lines.append(" " + st(detail, "37"))
        note = pace_note(limit, now)
        if note:
            lines.append(" " + st(note, "90"))
        lines.append("")

    extra = parse_extra(state.data)
    if extra is not None:
        lines.append(" " + st("Extra usage (monthly)", "1") + f"  {extra:.0f}%")
        lines.append(" " + bar(extra, None, inner, st))
        lines.append("")

    footer = []
    if state.fetched_at:
        footer.append(f"updated {fmt_duration(max(0, time.time() - state.fetched_at))} ago")
    if live and state.next_fetch:
        footer.append(f"next in {fmt_duration(max(0, state.next_fetch - time.time()))}")
        footer.append("[r] refresh  [q] quit")
    lines.append(st(" " + " · ".join(footer), "90"))
    if state.error:
        lines.append(st(f" ! {state.error}", "33"))
    return lines


def render_line(data: dict, now: datetime) -> str:
    parts = []
    for limit in parse_limits(data):
        if limit.key not in ALWAYS_SHOWN:
            continue
        pct = "—" if limit.pct is None else f"{limit.pct:.0f}%"
        left = limit.seconds_left(now)
        parts.append(f"{limit.short} {pct}" + (f" ({fmt_compact(left)})" if left else ""))
    return " · ".join(parts)


# --------------------------------------------------------------------------
# state, cache


def cache_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "claude-usage" / "usage.json"


def read_cache(max_age: float) -> tuple[dict, float] | None:
    try:
        blob = json.loads(cache_path().read_text())
        if time.time() - blob["fetched_at"] <= max_age:
            return blob["data"], blob["fetched_at"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def write_cache(data: dict, fetched_at: float) -> None:
    path = cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"fetched_at": fetched_at, "data": data}))
        tmp.replace(path)
    except OSError:
        pass


@dataclass
class State:
    data: dict | None = None
    fetched_at: float | None = None
    next_fetch: float = 0.0
    error: str | None = None
    plan: str | None = None
    failures: int = 0

    def refresh(self, interval: float) -> None:
        try:
            creds = load_credentials()
            self.plan = creds.plan or self.plan
            self.data = fetch_usage(creds)
            self.fetched_at = time.time()
            self.error = None
            self.failures = 0
            write_cache(self.data, self.fetched_at)
            self.next_fetch = time.time() + interval
        except UsageError as exc:
            self.error = str(exc)
            self.failures += 1
            backoff = min(interval * 2 ** (self.failures - 1), 15 * 60)
            self.next_fetch = time.time() + backoff


# --------------------------------------------------------------------------
# terminal


class Terminal:
    """Alternate screen, hidden cursor and non-blocking single-key reads."""

    def __enter__(self):
        self._saved = None
        if os.name == "nt":
            os.system("")  # turns on ANSI escape handling in the Windows console
        else:
            import termios
            import tty
            fd = sys.stdin.fileno()
            if sys.stdin.isatty():
                self._saved = termios.tcgetattr(fd)
                tty.setcbreak(fd)
        sys.stdout.write("\x1b[?1049h\x1b[?25l")
        sys.stdout.flush()
        return self

    def __exit__(self, *exc):
        sys.stdout.write("\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
        if self._saved is not None:
            import termios
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._saved)

    def draw(self, lines: list[str]) -> None:
        sys.stdout.write("\x1b[H" + "".join(f"{l}\x1b[K\n" for l in lines) + "\x1b[J")
        sys.stdout.flush()

    def read_key(self, timeout: float) -> str | None:
        if os.name == "nt":
            import msvcrt
            end = time.time() + timeout
            while time.time() < end:
                if msvcrt.kbhit():
                    return msvcrt.getwch()
                time.sleep(0.05)
            return None
        import select
        if not sys.stdin.isatty():
            time.sleep(timeout)
            return None
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        return sys.stdin.read(1) if ready else None


def run_live(interval: float, st: Style) -> None:
    state = State()
    try:
        state.plan = load_credentials().plan
    except UsageError:
        pass  # reported by the first refresh
    cached = read_cache(interval)
    if cached:
        state.data, state.fetched_at = cached
        state.next_fetch = state.fetched_at + interval
    with Terminal() as term:
        while True:
            if time.time() >= state.next_fetch:
                state.refresh(interval)
            cols = shutil.get_terminal_size((80, 24)).columns
            term.draw(render(state, datetime.now(timezone.utc), cols, st, live=True))
            key = term.read_key(1 - (time.time() % 1))  # tick on the second
            if key in ("q", "Q", "\x1b", "\x03"):
                return
            if key in ("r", "R"):
                state.next_fetch = 0


# --------------------------------------------------------------------------
# main


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="claude-usage",
        description="Live view of your Claude plan usage: 5h session, weekly limits, time to reset.",
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="print the dashboard once and exit")
    mode.add_argument("--line", action="store_true", help="print one compact line (for tmux/status bars)")
    mode.add_argument("--json", action="store_true", help="print the raw API response")
    p.add_argument("-i", "--interval", type=float, default=60,
                   help="seconds between API polls in live mode (default 60, min 15)")
    p.add_argument("--cache-ttl", type=float, default=60,
                   help="--line reuses data younger than this many seconds (default 60)")
    p.add_argument("--no-color", action="store_true", help="disable colors (also: NO_COLOR env var)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = p.parse_args(argv)

    color = not args.no_color and "NO_COLOR" not in os.environ
    interval = max(15.0, args.interval)
    live = not (args.once or args.line or args.json)

    if live and sys.stdout.isatty():
        try:
            run_live(interval, Style(color))
        except KeyboardInterrupt:
            pass
        return 0

    now = datetime.now(timezone.utc)
    cached = read_cache(args.cache_ttl) if args.line else None
    try:
        if cached:
            data, fetched_at = cached
            plan = None
        else:
            creds = load_credentials()
            data, fetched_at, plan = fetch_usage(creds), time.time(), creds.plan
            write_cache(data, fetched_at)
    except UsageError as exc:
        print(f"claude-usage: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(data, indent=2))
    elif args.line:
        print(render_line(data, now))
    else:
        state = State(data=data, fetched_at=fetched_at, plan=plan)
        st = Style(color and sys.stdout.isatty())
        print("\n".join(render(state, now, shutil.get_terminal_size((80, 24)).columns, st, live=False)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
