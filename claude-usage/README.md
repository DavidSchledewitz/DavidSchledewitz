# claude-usage

A small terminal dashboard for your Claude (Pro/Max) plan limits: the current
**5-hour session**, the **weekly** limits, and a live countdown until each one
resets. It's one Python file with no dependencies.

```
 Claude usage · Max plan

 Current session (5h)                                               62%
 ███████████████████████████████████████████░┃░░░░░░░░░░░░░░░░░░░░░░░░░
 resets in 1h 47m 08s · at 13:13
 at this pace: ~96% at reset

 Week · all models                                                  34%
 ████████████████████████░░░┃░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░
 resets in 4d 5h · at Tue 20 Oct 17:26
 at this pace: ~87% at reset

 updated 2s ago · next in 57s · [r] refresh  [q] quit
```

The bar is colored green, yellow or red (50% / 80%). The `┃` marker shows how
much of the window has passed. If the fill is left of the marker, you're
under pace. The "at this pace" line extrapolates your current burn rate
linearly.

## Install

```bash
pipx install ./claude-usage      # or: pip install --user ./claude-usage
claude-usage
```

You can also run it without installing: `python3 claude_usage.py`.

## Usage

| Command | What it does |
|---|---|
| `claude-usage` | Live dashboard. Polls every 60 s and ticks every second. `r` refreshes, `q` quits. |
| `claude-usage --once` | Prints the dashboard once. |
| `claude-usage --line` | Prints one line, e.g. `5h 62% (1h47m) · wk 34% (4d5h)` |
| `claude-usage --json` | Prints the raw API response. |
| `-i 120` | Polls every 120 s instead (minimum 15). |
| `--no-color` / `NO_COLOR=1` | Plain output. |

### In a status bar

`--line` reuses a cached result for up to 60 s (`--cache-ttl`) and shares the
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
python3 -m unittest -v test_claude_usage
```

The tests include an end-to-end run against a local fake usage endpoint.
