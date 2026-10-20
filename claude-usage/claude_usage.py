#!/usr/bin/env python3
"""claude-usage: a live terminal view of your Claude plan usage limits.

Shows the current 5-hour session, the weekly limits and the time left until
each one resets. It reuses the OAuth login that Claude Code already stores on
your machine and asks the same endpoint the `/usage` command in Claude Code
uses. No third-party dependencies.

Modes:
    claude-usage            live dashboard (q to quit, r to refresh)
    claude-usage --compact  small live dashboard (also automatic in narrow windows)
    claude-usage --window   open the small dashboard in its own terminal window
    claude-usage --once     print the dashboard once and exit
    claude-usage --line     one compact line, for tmux / status bars
    claude-usage --json     raw JSON from the API
    claude-usage --login    log in again via Claude Code (opens your browser)

An expired login is renewed the way Claude Code renews it and written back to
Claude Code's credentials file, so a browser login is rarely needed. Turn that
off with --no-renew or by creating ~/.config/claude-usage/no-auto-renew.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

__version__ = "0.1.0"

API_URL = os.environ.get("CLAUDE_USAGE_API_URL", "https://api.anthropic.com/api/oauth/usage")
OAUTH_BETA = "oauth-2025-04-20"
LOGIN_HINT = "Log in again with `claude-usage --login`; it opens your browser."
TOKEN_URL = os.environ.get("CLAUDE_USAGE_TOKEN_URL", "https://platform.claude.com/v1/oauth/token")
# Claude Code's OAuth client. Renewing exactly as Claude Code does keeps the
# renewed login valid for Claude Code as well.
CLAUDE_CODE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
RENEW_MARGIN = 60  # renew this many seconds before the login expires

HOUR = 3600
DAY = 24 * HOUR

# key -> (label, short label for --line, window length in seconds)
KNOWN_LIMITS = {
    "five_hour": ("Current session (5h)", "5h", 5 * HOUR),
    "seven_day": ("Week · all models", "wk", 7 * DAY),
    "seven_day_opus": ("Week · Opus", "opus", 7 * DAY),
    "seven_day_sonnet": ("Week · Sonnet", "sonnet", 7 * DAY),
    "seven_day_oauth_apps": ("Week · OAuth apps", "apps", 7 * DAY),
    # Internal codename; claude.ai shows it as "Cloud session credits", a
    # one-off allowance that expires rather than a rolling window.
    "iguana_necktie": ("Cloud session credits", "cloud", None),
}
ALWAYS_SHOWN = ("five_hour", "seven_day")
EXPIRING = ("iguana_necktie",)


# --------------------------------------------------------------------------
# credentials


class UsageError(Exception):
    """An error with a message meant for the user."""

    def __init__(self, message: str, retry_after: float = 0):
        super().__init__(message)
        self.retry_after = retry_after  # seconds the server asked us to wait


class LoginNeeded(UsageError):
    """No usable login: the user has to log in again (in the browser)."""


class AuthError(LoginNeeded):
    """The usage endpoint rejected the login (HTTP 401/403)."""


@dataclass
class Credentials:
    token: str
    expires_at: float | None = None  # unix seconds
    plan: str | None = None
    source: str = ""
    refresh_token: str | None = None
    scopes: tuple = ()
    path: Path | None = None  # set when the login came from a file we can update

    def expired(self) -> bool:
        return self.expires_at is not None and time.time() >= self.expires_at - RENEW_MARGIN


def claude_config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def _from_blob(blob: dict, source: str, path: Path | None = None) -> Credentials | None:
    oauth = blob.get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    if not token:
        return None
    expires = oauth.get("expiresAt")
    scopes = oauth.get("scopes")
    return Credentials(
        token=token,
        expires_at=expires / 1000 if isinstance(expires, (int, float)) else None,
        plan=oauth.get("subscriptionType"),
        source=source,
        refresh_token=oauth.get("refreshToken") or None,
        scopes=tuple(scopes) if isinstance(scopes, list) else (),
        path=path,
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
            creds = _from_blob(json.loads(path.read_text()), str(path), path)
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

    raise LoginNeeded(
        "no Claude Code login found. The Claude desktop app's login can't be "
        "read by other programs. Install Claude Code, then run `claude-usage --login` "
        "(it opens your browser), or set CLAUDE_CODE_OAUTH_TOKEN."
    )


def find_claude() -> str | None:
    """Claude Code's command, also where the native installer puts it when
    ~/.local/bin isn't on PATH (e.g. for programs started by the desktop)."""
    found = shutil.which("claude")
    if found:
        return found
    for candidate in (Path.home() / ".local/bin/claude", Path.home() / ".claude/local/claude"):
        if os.access(candidate, os.X_OK):
            return str(candidate)
    return None


# --------------------------------------------------------------------------
# renewing an expired login


def config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "claude-usage"


def renewal_disabled() -> bool:
    return bool(os.environ.get("CLAUDE_USAGE_NO_RENEW")) or (config_dir() / "no-auto-renew").exists()


@contextlib.contextmanager
def renew_lock(stale: float = 60):
    """Only one program (terminal app or top bar) renews at a time, since each
    renewal replaces the refresh token. Yields False if the other one holds it."""
    path = cache_path().with_name("renew.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except FileExistsError:
            with contextlib.suppress(FileNotFoundError):
                if time.time() - path.stat().st_mtime > stale:
                    path.unlink()  # left behind by a crash
                    continue
            break
        try:
            yield True
        finally:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
        return
    yield False


def save_renewed(path: Path, reply: dict) -> None:
    """Store a renewed login the way Claude Code does, keeping every other
    field. Atomic, so Claude Code never reads a half-written file."""
    blob = json.loads(path.read_text())
    oauth = blob.setdefault("claudeAiOauth", {})
    oauth["accessToken"] = reply["access_token"]
    if reply.get("refresh_token"):
        oauth["refreshToken"] = reply["refresh_token"]
    if isinstance(reply.get("expires_in"), (int, float)):
        oauth["expiresAt"] = int((time.time() + reply["expires_in"]) * 1000)
    if isinstance(reply.get("scope"), str) and reply["scope"].strip():
        oauth["scopes"] = reply["scope"].split()
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(blob, f)
    os.replace(tmp, path)


def renew(creds: Credentials, force: bool = False) -> Credentials:
    """Renew an expired login with its refresh token, as Claude Code does.
    With force, renew although it hasn't expired (Claude rejected it)."""
    if creds.path is None or not creds.refresh_token or renewal_disabled():
        return creds
    if not force and not creds.expired():
        return creds
    with renew_lock() as mine:
        if not mine:
            return creds  # the other program is renewing; the next poll re-reads the file
        current = load_credentials()
        if current.token != creds.token or (not force and not current.expired()):
            return current  # renewed meanwhile, by Claude Code or the other program
        body = {"grant_type": "refresh_token", "refresh_token": current.refresh_token,
                "client_id": CLAUDE_CODE_CLIENT_ID}
        if current.scopes:
            body["scope"] = " ".join(current.scopes)
        req = urllib.request.Request(
            TOKEN_URL, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json", "User-Agent": f"claude-usage/{__version__}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                reply = json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 401, 403):
                raise LoginNeeded(f"your Claude login has expired. {LOGIN_HINT}") from exc
            raise UsageError(f"HTTP {exc.code} while renewing the login") from exc
        except urllib.error.URLError as exc:
            raise UsageError(f"network error while renewing the login: {exc.reason}") from exc
        except ValueError as exc:
            raise UsageError("renewing the login returned invalid JSON") from exc
        if not isinstance(reply, dict) or not reply.get("access_token"):
            raise UsageError("renewing the login returned no token")
        save_renewed(current.path, reply)
        return load_credentials()


def get_usage() -> tuple[dict, Credentials]:
    """Load the login (renewing it if expired) and fetch usage. If Claude
    rejects the login anyway, renew once and retry."""
    creds = renew(load_credentials())
    try:
        return fetch_usage(creds), creds
    except AuthError:
        renewed = renew(creds, force=True)
        if renewed.token == creds.token:
            raise
        return fetch_usage(renewed), renewed


NO_CLAUDE = "Claude Code isn't installed. Install it with: curl -fsSL https://claude.ai/install.sh | bash"
LOGIN_CMD = ("auth", "login", "--claudeai")  # Claude Code's own browser login


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
            raise AuthError(f"HTTP {exc.code}: your Claude login has expired. {LOGIN_HINT}") from exc
        if exc.code == 429:
            try:
                retry_after = float(exc.headers.get("Retry-After") or 0)
            except ValueError:
                retry_after = 0
            raise UsageError("HTTP 429: rate limited by the usage endpoint.", retry_after) from exc
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


@dataclass
class Extra:
    """Pay-as-you-go usage beyond the plan ("Usage credits" on claude.ai).
    Amounts are in cents."""
    pct: float
    used: float | None
    limit: float | None


def parse_extra(data: dict) -> Extra | None:
    extra = data.get("extra_usage")
    if not isinstance(extra, dict) or not extra.get("is_enabled"):
        return None
    num = lambda v: float(v) if isinstance(v, (int, float)) else None
    used, limit = num(extra.get("used_credits")), num(extra.get("monthly_limit"))
    pct = num(extra.get("utilization"))
    if pct is None and used is not None and limit:
        pct = 100 * used / limit
    if pct is None:
        return None
    return Extra(pct=pct, used=used, limit=limit)


def fmt_money(cents: float) -> str:
    return f"${cents / 100:,.2f}"


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


def bar(pct: float | None, elapsed: float | None, width: int, st: Style, segments: int = 0) -> str:
    """Usage bar. The ┃ marker shows how far through the window you are:
    fill to the left of it means you are on or under pace. With `segments`,
    a tick is drawn at each boundary (e.g. one per day of a weekly window),
    so each segment is one even share of the budget."""
    filled = 0 if pct is None else round(min(100.0, max(0.0, pct)) / 100 * width)
    marker = None if elapsed is None else min(width - 1, int(elapsed * width))
    ticks = {round(k * width / segments) for k in range(1, segments)} if segments > 1 else set()
    fg = st.for_pct(pct)
    bg = str(int(fg) + 10)
    out = []
    for i in range(width):
        if i == marker:
            out.append(st("┃", "1", "97" if i < filled else "37"))
        elif i in ticks:
            if i < filled:
                out.append(st("│", "30", bg) if st.enabled else "▓")
            else:
                out.append(st("│", "90"))
        elif i < filled:
            out.append(st("█", fg))
        else:
            out.append(st("░", "90"))
    return "".join(out)


def window_days(limit: Limit) -> int:
    """Number of days in a multi-day window (7 for weekly), else 0."""
    return limit.window // DAY if limit.window and limit.window >= 2 * DAY else 0


def day_scale(limit: Limit, width: int) -> str | None:
    """Weekday labels under a weekly bar, centred in each day-segment. The
    window starts exactly one week before the reset, so segments run from the
    reset's time of day to the same time next day. Each is labelled with the
    day at its midpoint, the day it mostly covers: with a Sunday 20:00 reset
    the first segment reads Mon, not Sun."""
    days = window_days(limit)
    if not days or limit.resets_at is None or width / days < 5:
        return None
    start = limit.resets_at - timedelta(seconds=limit.window)
    seg = width / days
    line = [" "] * width
    for k in range(days):
        label = (start + timedelta(days=k + 0.5)).astimezone().strftime("%a")
        pos = max(0, round(k * seg + (seg - len(label)) / 2))
        line[pos:pos + len(label)] = label
    return "".join(line[:width])


def pace_note(limit: Limit, now: datetime) -> tuple[str, bool] | None:
    """How you compare with an even burn across the window, plus a linear
    projection. Returns (text, over_pace)."""
    left = limit.seconds_left(now)
    if limit.pct is None or left is None or not limit.window:
        return None
    if limit.pct >= 100:
        return "limit reached", True
    elapsed = limit.window - left
    parts = []
    days = window_days(limit)
    if days:
        parts.append(f"day {min(days, int(elapsed // DAY) + 1)} of {days}")
    if elapsed >= limit.window * 0.05:  # earlier than this the numbers are noise
        diff = limit.pct - 100 * elapsed / limit.window
        if abs(diff) < 1:
            parts.append("on pace")
        else:
            parts.append(f"{abs(diff):.0f}% {'over' if diff > 0 else 'under'} pace")
        if limit.pct > 0:
            rate = limit.pct / elapsed  # percent per second
            to_full = (100 - limit.pct) / rate
            if to_full < left:
                parts.append(f"limit in ~{fmt_duration(to_full)} at this rate")
            else:
                parts.append(f"~{limit.pct + rate * left:.0f}% at reset")
        over = diff >= 1
    else:
        over = False
    return (" · ".join(parts), over) if parts else None


def render(state: "State", now: datetime, width: int, st: Style, live: bool) -> list[str]:
    inner = max(20, min(width - 4, 70))
    lines = []

    plan = f" · {state.plan.capitalize()} plan" if state.plan else ""
    lines.append(st(" Claude usage", "1") + st(plan, "90"))
    lines.append("")

    if state.data is None:
        if state.login_proc is not None:
            lines.append(st(" Waiting for you to log in in the browser…", "36"))
            return lines
        lines.append(st(f" {state.error or 'loading…'}", "31" if state.error else "90"))
        if state.login_needed and live:
            lines.append(st(" Press l to log in (opens your browser).", "36"))
        return lines

    for limit in parse_limits(state.data):
        pct_txt = "  —" if limit.pct is None else f"{limit.pct:3.0f}%"
        gap = max(1, inner - len(limit.label) - len(pct_txt))
        lines.append(" " + st(limit.label, "1") + " " * gap + st(pct_txt, "1", st.for_pct(limit.pct)))
        lines.append(" " + bar(limit.pct, limit.elapsed_fraction(now), inner, st, window_days(limit)))
        scale = day_scale(limit, inner)
        if scale:
            lines.append(" " + st(scale, "90"))

        left = limit.seconds_left(now)
        if left is None:
            if limit.key == "five_hour":
                detail = "no active session. The 5h window starts with your next message"
            else:
                detail = "no reset scheduled"
        elif left == 0:
            detail = "expired" if limit.key in EXPIRING else "resetting now…"
        else:
            verb = "expires" if limit.key in EXPIRING else "resets"
            detail = (f"{verb} in {st(fmt_duration(left, with_seconds=live), '1')}"
                      f" · at {fmt_clock(limit.resets_at, now)}")
        lines.append(" " + st(detail, "37"))
        note = pace_note(limit, now)
        if note:
            text, over = note
            lines.append(" " + st(text, "33" if over else "90"))
        lines.append("")

    extra = parse_extra(state.data)
    if extra is not None:
        label, pct_txt = "Usage credits (monthly)", f"{extra.pct:3.0f}%"
        gap = max(1, inner - len(label) - len(pct_txt))
        lines.append(" " + st(label, "1") + " " * gap + st(pct_txt, "1", st.for_pct(extra.pct)))
        lines.append(" " + bar(extra.pct, None, inner, st))
        if extra.used is not None and extra.limit:
            lines.append(" " + st(f"{fmt_money(extra.used)} of {fmt_money(extra.limit)} spent", "37"))
        lines.append("")

    footer = []
    if state.fetched_at:
        footer.append(f"updated {fmt_duration(max(0, time.time() - state.fetched_at))} ago")
    if live and state.next_fetch:
        footer.append(f"next in {fmt_duration(max(0, state.next_fetch - time.time()))}")
        footer.append("[r] refresh  [q] quit")
    lines.append(st(" " + " · ".join(footer), "90"))
    if state.login_proc is not None:
        lines.append(st(" Waiting for you to log in in the browser…", "36"))
    elif state.error:
        lines.append(st(f" ! {state.error}", "33"))
        if state.login_needed and live:
            lines.append(st(" Press l to log in (opens your browser).", "36"))
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


COMPACT_BAR = 21  # 7 days x 3 cells, so weekly ticks land on whole cells
COMPACT_WIDTH = 1 + 6 + COMPACT_BAR + 5 + 8  # " label  bar  pct  right"
COMPACT_BELOW = 50  # live mode switches to compact in windows narrower than this


def render_compact(state: "State", now: datetime, st: Style, live: bool) -> list[str]:
    """A small layout (about 42x6) for a little window in a screen corner."""
    head = " Claude" + (f" · {state.plan.capitalize()}" if state.plan else "")
    age = f"{fmt_compact(max(0, time.time() - state.fetched_at))} ago" if state.fetched_at else ""
    lines = [st(head, "1") + " " * max(1, COMPACT_WIDTH - len(head) - len(age)) + st(age, "90")]

    def row(label: str, pct: float | None, elapsed: float | None, days: int, right: str) -> str:
        pct_txt = "   —" if pct is None else f"{pct:4.0f}%"
        return (" " + st(f"{label[:6]:<6}", "1") + bar(pct, elapsed, COMPACT_BAR, st, days)
                + st(f"{pct_txt:>5}", "1", st.for_pct(pct)) + f" {right:>7}")

    if state.data is not None:
        day_row_done = False
        for limit in parse_limits(state.data):
            left = limit.seconds_left(now)
            if left is None:
                right = "—"
            elif left < DAY and live:
                h, rem = divmod(int(left), HOUR)
                right = f"{h}:{rem // 60:02d}:{rem % 60:02d}"
            else:
                right = fmt_compact(left)
            days = window_days(limit)
            lines.append(row(limit.short, limit.pct, limit.elapsed_fraction(now), days, right))
            if days and limit.resets_at is not None and not day_row_done:
                # Initial of the day each segment mostly covers, centred in it.
                start = limit.resets_at - timedelta(seconds=limit.window)
                seg = COMPACT_BAR / days
                cells = [" "] * COMPACT_BAR
                for k in range(days):
                    cells[int(k * seg + seg / 2)] = (start + timedelta(days=k + 0.5)).astimezone().strftime("%a")[:1]
                lines.append(" " * 7 + st("".join(cells), "90"))
                day_row_done = True
        extra = parse_extra(state.data)
        if extra is not None:
            spent = fmt_money(extra.used) if extra.used is not None else ""
            lines.append(row("extra", extra.pct, None, 0, spent))
    elif not state.error:
        lines.append(st(" loading…", "90"))

    if state.login_proc is not None:
        lines.append(st(" Waiting for the browser login…", "36"))
    elif state.login_needed and live:
        lines.append(st(" Login expired. Press l to log in.", "33"))
    elif state.error:
        for part in textwrap.wrap(state.error, COMPACT_WIDTH - 3)[:2]:
            lines.append(st(f" ! {part}", "33" if state.data is not None else "31"))
    return lines


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
    login_needed: bool = False
    login_proc: subprocess.Popen | None = None
    login_started: float = 0.0

    def start_login(self) -> None:
        """Run Claude Code's browser login in the background (live mode)."""
        if self.login_proc is not None:
            return
        claude = find_claude()
        if claude is None:
            self.error = NO_CLAUDE
            return
        self.login_proc = subprocess.Popen(
            [claude, *LOGIN_CMD], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
        self.login_started = time.time()

    def check_login(self) -> None:
        """Called every tick: refresh as soon as the browser login finished."""
        if self.login_proc is None:
            return
        code = self.login_proc.poll()
        if code is None and time.time() - self.login_started > 10 * 60:
            self.login_proc.kill()
            code = -1
        if code is None:
            return
        self.login_proc = None
        if code == 0:
            self.failures = 0
            self.next_fetch = 0
        else:
            self.error = "The login didn't complete. Press l to try again."

    def refresh(self, interval: float) -> None:
        try:
            self.data, creds = get_usage()
            self.plan = creds.plan or self.plan
            self.fetched_at = time.time()
            self.error = None
            self.failures = 0
            self.login_needed = False
            write_cache(self.data, self.fetched_at)
            self.next_fetch = time.time() + interval
        except UsageError as exc:
            self.login_needed = isinstance(exc, LoginNeeded)
            self.failures += 1
            backoff = max(min(interval * 2 ** (self.failures - 1), 30 * 60), exc.retry_after)
            self.error = str(exc)
            if exc.retry_after:
                self.error += f" Retrying in {fmt_duration(backoff)}."
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


def run_live(interval: float, st: Style, compact: bool = False) -> None:
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
            state.check_login()
            if time.time() >= state.next_fetch and state.login_proc is None:
                state.refresh(interval)
            cols = shutil.get_terminal_size((80, 24)).columns
            now = datetime.now(timezone.utc)
            if compact or cols < COMPACT_BELOW:
                term.draw(render_compact(state, now, st, live=True))
            else:
                term.draw(render(state, now, cols, st, live=True))
            key = term.read_key(1 - (time.time() % 1))  # tick on the second
            if key in ("q", "Q", "\x1b", "\x03"):
                return
            if key in ("r", "R"):
                state.next_fetch = 0
            if key in ("l", "L") and state.login_needed:
                state.start_login()


# --------------------------------------------------------------------------
# small window


WINDOW_SIZE = (COMPACT_WIDTH + 4, 8)  # columns, rows; +4 leaves room for the scrollbar


def open_window(zoom: float, passthrough: list[str]) -> int:
    """Open the compact dashboard in its own small terminal window. Font size
    can't be changed from inside a running terminal, so this starts a new
    GNOME Terminal window zoomed out and sized to fit."""
    me = [sys.executable, os.path.realpath(__file__), "--compact", *passthrough]
    cols, rows = WINDOW_SIZE
    if shutil.which("gnome-terminal"):
        # -0+0 asks for the top-right corner; GNOME on Wayland ignores positions.
        cmd = ["gnome-terminal", "--window", "--hide-menubar", "--title=Claude usage",
               f"--geometry={round(cols)}x{rows}-0+0", f"--zoom={zoom}", "--", *me]
    elif shutil.which("x-terminal-emulator"):
        cmd = ["x-terminal-emulator", "-e", *me]  # size and zoom unsupported here
    else:
        print("claude-usage: no gnome-terminal or x-terminal-emulator found. "
              "Run `claude-usage --compact` in a small terminal instead.", file=sys.stderr)
        return 1
    subprocess.Popen(cmd, start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return 0


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
    mode.add_argument("--login", action="store_true",
                      help="log in again via Claude Code; opens your browser")
    mode.add_argument("-w", "--window", action="store_true",
                      help="open the compact dashboard in its own small GNOME Terminal window")
    p.add_argument("-c", "--compact", action="store_true",
                   help=f"small layout (also automatic in windows under {COMPACT_BELOW} columns)")
    p.add_argument("--zoom", type=float, default=0.8,
                   help="font scale for --window, 1.0 = normal (default 0.8)")
    p.add_argument("-i", "--interval", type=float, default=300,
                   help="seconds between API polls in live mode (default 300, min 60)")
    p.add_argument("--cache-ttl", type=float, default=300,
                   help="--line reuses data younger than this many seconds (default 300)")
    p.add_argument("--no-color", action="store_true", help="disable colors (also: NO_COLOR env var)")
    p.add_argument("--no-renew", action="store_true",
                   help="don't renew an expired login (also: ~/.config/claude-usage/no-auto-renew)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = p.parse_args(argv)

    color = not args.no_color and "NO_COLOR" not in os.environ
    if args.no_renew:
        os.environ["CLAUDE_USAGE_NO_RENEW"] = "1"
    interval = max(60.0, args.interval)
    live = not (args.once or args.line or args.json or args.window)

    if args.login:
        claude = find_claude()
        if claude is None:
            print(f"claude-usage: {NO_CLAUDE}", file=sys.stderr)
            return 1
        return subprocess.call([claude, *LOGIN_CMD])

    if args.window:
        passthrough = ([f"--interval={interval:g}"] + (["--no-color"] if args.no_color else [])
                       + (["--no-renew"] if args.no_renew else []))
        return open_window(args.zoom, passthrough)

    if live and sys.stdout.isatty():
        try:
            run_live(interval, Style(color), compact=args.compact)
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
            data, creds = get_usage()
            fetched_at, plan = time.time(), creds.plan
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
        if args.compact:
            print("\n".join(render_compact(state, now, st, live=False)))
        else:
            print("\n".join(render(state, now, shutil.get_terminal_size((80, 24)).columns, st, live=False)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
