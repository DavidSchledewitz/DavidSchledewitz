# Volleyball Stats App: Requirements Spec (v1)

Status: draft from the requirements interview, 2026-10-19. Nothing is built yet.

## 1. Summary

A laptop/web app for statistics on **one indoor (6v6) team**. A statistician enters
actions live during a match, and anyone can correct or complete them afterwards. The
app tracks the full game state (score, serve, rotation, lineup), so the coach gets a
per-player box score, a rotation analysis and season trends. It works offline in any
gym and stores data on the device. Optional sync comes later.

## 2. Users

| User | Needs |
|---|---|
| **Statistician** (often a parent or bench player) | Fast, forgiving live entry. Undo and fix mistakes without stopping. Must be usable after 10 minutes of training. |
| **Coach** | After the match: box score, rotation analysis, trends across the season. Makes lineup and training decisions from them. |

Out of scope for v1: fans or club members as readers, players entering their own stats.

## 3. Decisions from the interview

| Topic | Decision |
|---|---|
| Discipline | Indoor 6v6 only (rotations, libero, substitutions) |
| Platform | Laptop / web browser |
| Entry timing | Live during the match, plus editing afterwards |
| Teams | Own team only (no opponent scouting) |
| Skills | Serve, reception, attack, block, dig, set |
| Detail | Simple outcomes. **Exception: reception gets a 0–3 rating** |
| Game state | Full rally tracking: score, serving team, rotation |
| Lineup | Fully tracked: starting six per set, substitutions, libero swaps |
| Input | Keyboard codes **and** clickable buttons |
| Reports | Post-match box score, rotation analysis, season trends |
| Storage | Local-first; optional sync later |
| Video | Not in v1 |
| Scope | One team, several seasons |

## 4. Core concepts and data model

The source of truth is an **append-only event log** per match. Score, rotation, who is
on court and every statistic are *derived* by replaying the events. This makes undo,
editing afterwards and recalculating stats straightforward and consistent.

```
Team ─┬─ Player (id, name, jersey #, positions: S/OH/MB/OPP/L, active flag)
      └─ Season ── Match (date, opponent name, home/away, competition, rules preset)
                     └─ Set (number, starting lineup, first server)
                          └─ Event (ordered, timestamped)
```

Event types:

- `action`: player, skill, outcome (and the 0–3 rating for reception)
- `opponent_point`: the opponent won the rally with no own-team action to blame
  (e.g. an unreturnable opponent attack), optionally with a reason
- `opponent_error`: the opponent lost the rally by their own fault
- `substitution`: player out, player in
- `libero_swap`: libero in/out for a player
- `timeout`, `set_start`, `set_end`, `manual_correction` (score or rotation override)

A rally is the sequence of events between two serves. Its winner is **inferred** from
the terminal event (see §6), so the statistician never enters "point us/them" separately
when an own-team action ended the rally.

## 5. Skills and outcomes

Simple outcomes for every skill, except reception, which gets a pass rating.

| Skill | Code | Outcomes | Ends rally? |
|---|---|---|---|
| Serve | `S` | `#` ace · `+` in play · `=` error | ace → we win; error → they win |
| Reception | `R` | `3` perfect · `2` good · `1` poor · `0` error (aced) | `0` → they win |
| Attack | `A` | `#` kill · `+` in play · `/` blocked · `=` error | kill → we win; blocked/error → they win |
| Block | `B` | `#` block point · `+` touch (keeps ball in play) · `=` error (net, out) | point → we win; error → they win |
| Dig | `D` | `+` dig · `=` error (ball dropped / not playable) | error → they win |
| Set | `E` | `+` set · `=` set error (double, lift, unhittable) | error → they win |

Pass rating scale (common 0–3 convention):
**3** setter has all options · **2** setter limited, still attackable · **1** only a high
ball or free ball over · **0** ace / reception error.

**Assists** are derived, not typed. When an attack is a kill, the player who made the
last recorded set in that rally gets the assist. If no set was recorded, the assist goes
to the designated setter on court. This fallback is a setting that can be switched off.

**Set distribution**: each attack is linked to the preceding set (or the on-court setter),
giving "who did the setter set, at which pass quality and in which rotation".

**Blocks**: if one player gets `B#`, it is a solo block (1.0). If two or three players get
`B#` in the same rally, each gets a block assist (0.5).

## 6. Game state and rules engine

- **Score and serve**: the rally winner gets the point. If the receiving team wins, it
  gets the serve (sideout) and **our team rotates** when we win the serve back.
- **Rotation numbering**: R1–R6 = the zone the setter is in (R1 = setter serving from
  zone 1). The app always shows the current rotation and the six players on court.
- **Set end**: 25 points (5th set 15), win by 2; match is best of 5 (or 3).
  Configurable through **rules presets** (e.g. FIVB, youth leagues with different set
  counts or substitution limits).
- **Substitutions**: tracked per set, with a warning (not a hard block) when a preset's
  limit is exceeded or the re-entry rules are broken.
- **Libero**: one or two liberos per match. The statistician sets which players the
  libero replaces (typically the middle blockers in the back row). Swaps are then
  **automatic** when such a player rotates to the back row or after they serve,
  with a one-key override for exceptions.
- **Rally winner inference**: when an own-team action ends the rally (table in §5), the
  rally closes automatically. Otherwise, the statistician presses
  "opponent point" or "opponent error".
- **Manual correction**: score or rotation can be forced at any time and is logged as an
  event, so it survives replay and is visible in the audit trail.

## 7. Live entry (laptop)

### 7.1 Keyboard codes

Grammar: `<jersey><skill><outcome>`, several actions per line separated by spaces,
Enter to commit.

```
12S+ 7R2 3A#          # 12 serves in, 7 passes a 2, 3 kills → our point, rally closed
4S=                   # 4 serve error → opponent point
9R3 1E+ 10A+ 5D+ 10A# # long rally, ends with a kill
o                     # opponent point (no own-team action at fault)
oe                    # opponent error → our point
sub 11>4              # 11 in for 4
lib                   # manual libero swap override
to                    # our timeout
```

- **Shortcut**: the server defaults to whoever is in zone 1, so `S+` alone works.
- **Live validation**: unknown jersey, a player not on court or an impossible
  sequence → the line turns red with a short message, and nothing is committed.
- **Undo**: `Ctrl+Z` removes the last event and replays the state. Every event is editable
  later.

### 7.2 Buttons

The same model as clickable UI, for beginners and for editing:
**player (from the on-court six + libero) → skill → outcome**. Each click shows the
equivalent keyboard code, so users can learn the codes.

### 7.3 Screen layout (single laptop screen)

- Top: score, set, serving team, timeouts, current rotation
- Left: court diagram with the six players in their zones (libero marked)
- Centre: code entry line + buttons
- Right: rally log of the current set (editable, newest on top)

## 8. Editing after the match

- Full event list per set, filterable by player and skill
- Edit, insert or delete any event; score and stats recalculate by replay
- Insert missed rallies (e.g. "we lost 3 points while I wasn't looking" →
  `opponent_point` ×3)
- Mark a match as **final**, which locks it against accidental edits (can be unlocked)

## 9. Reports and stat definitions

### 9.1 Post-match box score (per player and team, per set and match)

| Area | Columns |
|---|---|
| Serve | attempts, aces, errors, ace %, error % |
| Reception | attempts, rating counts (0/1/2/3), **pass average**, perfect % (3s), error % |
| Attack | attempts, kills, errors, blocked, **hitting % = (K − E − Blocked) / attempts**, kill % |
| Block | solo, assists, total blocks (solo + 0.5 × assists), block errors |
| Dig | digs, dig errors |
| Set | sets, assists, set errors |
| Points | kills + aces + total blocks |

Team rows also include points from opponent errors, so the totals match the score.

### 9.2 Rotation analysis

For each rotation R1–R6, per set, per match and per season:

- rallies played, points won/lost, **+/−**
- **sideout %** = rallies won when the opponent served / rallies they served
- **first-ball sideout %** = sideouts won by the first attack after the reception
- **break-point %** = rallies won on our serve / rallies we served
- sideout % **by pass rating** (what does a 1-pass cost us in R4?)
- set distribution in that rotation

### 9.3 Season trends

- Per-player line charts across matches (hitting %, pass average, serve error %, …)
- Team trends: sideout %, break-point %, per-rotation +/−
- Filters: date range, opponent, home/away, competition
- Minimum-attempts threshold so small samples don't mislead (shown greyed out)

### 9.4 Exports

- Box score as **PDF** (print layout) and **CSV**
- Raw event log as **CSV** (for spreadsheets / own analysis)
- Full **JSON backup**: export and import of everything, including the roster and seasons

## 10. Data, storage and sync

- **Local-first**: all data in the browser (IndexedDB). Every event is written immediately,
  so closing the tab or a crash loses nothing.
- **Offline**: installable web app (PWA) that runs without internet once loaded.
- **Backup**: one-click JSON export, plus a reminder if no backup was made in N days.
- **Optional sync (after v1)**: the same data model synced to a server so the
  statistician's laptop and the coach's device share data. The event-log design makes
  merging simple (events have unique IDs and are append-only; edits are new events).

## 11. Non-functional requirements

- Commit of one entry line < 100 ms; no UI freeze on a 5-set match
- Learnable: a new statistician can record a set with the buttons after a 10-minute intro
- No account, no internet needed for v1
- Works in current Chrome, Firefox, Safari and Edge on a laptop screen (≥ 1280 px wide)
- Data can always be exported; no lock-in

## 12. v1 scope

**In v1:** roster and seasons; match setup with lineups; live entry (codes + buttons);
rules engine (score, rotation, subs, libero); editing after the match; box score,
rotation analysis, season trends; PDF/CSV/JSON export; offline local storage.

**Later:**

- Optional cloud sync between devices
- Live coach dashboard during the match (sideout % and struggling players at timeouts)
- Opponent scouting (their hitters, rotations, serve targets)
- Video linking (each action timestamped to a match video)
- Court zones (serve targets, attack directions, heatmaps)
- Tablet/phone entry layout
- Multiple teams / club mode
- Beach volleyball

## 13. Open questions

1. **Rules preset**: which league do you play in? This sets the set format,
   substitution limit and libero rules to ship as the default.
2. **Set recording**: should the statistician record every set (`E+`), or only set
   errors, with assists inferred from the setter on court? Recording every set is more
   accurate but adds a keypress to almost every rally.
3. **Dig definition**: count every successful dig, or only digs that lead to an attack
   attempt? Both conventions are common; pick one for consistency.
4. **Reception on free balls / down balls**: rate them like serve receptions, or skip
   them?
5. **Who sees the reports**: only the coach on the same laptop, or are PDFs enough for
   sharing with players until sync exists?
