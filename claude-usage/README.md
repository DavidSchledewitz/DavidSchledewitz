# claude-usage

A small terminal dashboard for your Claude (Pro/Max) plan limits: the current
**5-hour session**, the **weekly** limits, and a live countdown until each one
resets. It comes in two forms:

- **A terminal app**: one Python file with no dependencies.
- **A GNOME top-bar extension**: an indicator next to the clock and wifi, with
  a popup. See [GNOME top bar](#gnome-top-bar-ubuntu).

```
 Claude usage · Max plan

 Current session (5h)                                               62%
 ███████████████████████████████████████████░┃░░░░░░░░░░░░░░░░░░░░░░░░░
 resets in 1h 47m 08s · at 14:29
 2% under pace · ~96% at reset

 Week · all models                                                  51%
 ██████████▓█████████▓██████┃██▓█████░░░░│░░░░░░░░░│░░░░░░░░░│░░░░░░░░░
 Thu       Fri       Sat       Sun       Mon       Tue       Wed
 resets in 4d 5h · at Thu 22 Oct 18:42
 day 3 of 7 · 12% over pace · limit in ~2d 15h at this rate

 updated 2s ago · next in 57s · [r] refresh  [q] quit
```

How to read it:

- The bar turns green, yellow or red at 50% and 80%.
- `┃` marks **now**: how much of the window has passed. If the fill stops
  left of it, you're under pace.
- On weekly bars, a tick marks each day. Each segment is one day's even share
  (about 14%) of the weekly budget. The weekday under each tick is when that
  day starts. Days begin at your reset time, not at midnight.
- "x% over/under pace" compares your usage with an even burn across the
  window. The last part extrapolates your current rate linearly. It's shown
  in yellow when you're over pace.

## Install

```bash
pipx install ./claude-usage      # or: pip install --user ./claude-usage
claude-usage
```

You can also run it without installing: `python3 claude_usage.py`.

## Usage

| Command | What it does |
|---|---|
| `claude-usage` | Live dashboard. Polls every 2 min and ticks every second. `r` refreshes, `q` quits. |
| `claude-usage --once` | Prints the dashboard once. |
| `claude-usage --line` | Prints one line, e.g. `5h 62% (1h47m) · wk 34% (4d5h)` |
| `claude-usage --json` | Prints the raw API response. |
| `-i 300` | Polls every 300 s instead (minimum 15). |
| `--no-color` / `NO_COLOR=1` | Plain output. |

### In a status bar

`--line` reuses a cached result for up to 2 min (`--cache-ttl`) and shares the
cache with the live dashboard, so it's cheap to call often.

**tmux** (`~/.tmux.conf`):
```tmux
set -g status-interval 30
set -g status-right '#(claude-usage --line)'
```

**Claude Code status line** (`~/.claude/settings.json`):
```json
{ "statusLine": { "type": "command", "command": "claude-usage --line" } }
```

## GNOME top bar (Ubuntu)

<img src="gnome-extension/screenshot.png" width="375" alt="Top-bar indicator with the usage popup open">

The top-bar icon shows two mini bars: the 5h session on top and the week
below. Next to them are both percentages. Click it for the popup: every
limit, a live countdown, day ticks on the weekly bars and the pace note.

Install it for your user:

```bash
./gnome-extension/install_claude_bar.sh
```

Then **log out and back in once**. GNOME on Wayland only picks up new
extensions at login, and the same goes after updating the code.
`./gnome-extension/install_claude_bar.sh --uninstall` removes it.

Notes:

- It's tested on GNOME 46 (Ubuntu 24.04). It declares support up to GNOME 50
  (Ubuntu 26.04), but those versions are untested. If it doesn't show up
  after an upgrade, run `journalctl -o cat -b /usr/bin/gnome-shell | grep -i claude`
  to see why.
- It needs nothing beyond GNOME itself: no Python, no extra packages. It
  reads the same login and calls the same endpoint as the terminal app, and
  it shares the cache in `~/.cache/claude-usage/`. The terminal app and the
  extension can run side by side without doubling the requests.
- It polls every 2 minutes; the endpoint rate-limits eagerly (HTTP 429), and
  then it waits as long as the server asks. While you're locked, GNOME turns
  extensions off, so it doesn't poll then.

## How it works

Claude Code stores your claude.ai login in one of these places:

- Linux/Windows: `~/.claude/.credentials.json` (or `$CLAUDE_CONFIG_DIR`)
- macOS: Keychain item `Claude Code-credentials`
- Or set `CLAUDE_CODE_OAUTH_TOKEN` yourself (e.g. from `claude setup-token`)

The tool reads that token **read-only** and calls
`GET https://api.anthropic.com/api/oauth/usage`, the same endpoint behind
`/usage` in Claude Code. The token never goes anywhere else.

Things to know:

- **This endpoint is undocumented.** Anthropic can change it without notice.
  The parser is generic, so a new limit type (anything with a `utilization`
  field) shows up automatically. If the format changes completely, run
  `claude-usage --json` to see what came back.
- **The tool never refreshes your token.** Access tokens expire after a few
  hours, and Claude Code refreshes them whenever you use it. If you see
  `HTTP 401`, open Claude Code once. The tool re-reads the credentials on
  every poll, so it recovers on its own after that. (This is deliberate:
  refreshing the token itself would rotate the refresh token and could log
  Claude Code out.)
- It works with subscription (Pro/Max) logins only. API-key usage has no
  5h/weekly limits.

## Tests

```bash
python3 -m unittest -v test_claude_usage     # terminal app, includes a fake-server end-to-end run
gjs -m gnome-extension/tests/test_usage.js   # extension's parsing/pace logic
```
