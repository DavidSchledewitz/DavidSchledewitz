# Volleyball Stats App: Requirements Spec (v1)

Status: draft from the requirements interview, 2026-10-19. Nothing is built yet.

## 1. Summary

A laptop/web app for statistics on **one indoor (6v6) mixed team** in the AmaVolley
amateur league (Trento). A statistician enters actions live during a match, and anyone can
correct or complete them afterwards. The app tracks the full game state (score, serve,
rotation, lineup), so the coach gets a per-player box score, a rotation analysis and
season trends. It works offline in any gym and stores data on the device. Optional sync
comes later.

**Everything beyond the score is optional.** Each match can record as much or as little
as the statistician can manage, and the reports show only what was recorded (§3.1).

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
| League | AmaVolley (Trento), **mixed**: sets to 21 (cap 23), tie-break to 13 (cap 16), best of 5, exactly 3 women + 3 men on court, libero exempt from parity |
| Language | English interface |
| Platform | Laptop / web browser |
| Entry timing | Live during the match, plus editing afterwards |
| Teams | Own team only (no opponent scouting) |
| Skills | Serve, reception, attack, block, dig, set errors, free-ball errors, each optional |
| Detail | Simple outcomes. **Exception: reception gets a 0–3 rating** (optional) |
| Sets | Not recorded by default (too much work); only set errors. Assists are inferred |
| Digs | Every successful dig counts |
| Free balls | Not a reception. Only errors on free balls are counted |
| Game state | Full rally tracking: score, serving team, rotation |
| Lineup | Fully tracked: starting six per set, substitutions, libero swaps |
| Input | Keyboard codes **and** clickable buttons |
| Reports | Post-match box score, rotation analysis, season trends |
| Sharing | PDF export is enough until sync exists |
| Storage | Local-first; optional sync later |
| Video | Not in v1 |
| Scope | One team, several seasons |

### 3.1 Optional by design

Nothing except the score is mandatory. The app must stay useful when the statistician
only records part of the game, and must never show "0" where it means "not recorded".

- **Tracking profiles** per match, switchable mid-match:
  - *Score only*: rally winner, rotation and lineup. Already gives sideout % and
    +/− per rotation.
  - *Basic*: + serve and attack outcomes, plus errors from any skill.
  - *Full*: + reception rating, blocks, digs, set errors, free-ball errors.
  - *Custom*: tick each skill on or off.
- **Per-skill toggles**: a skill that is off disappears from the buttons, its codes are
  rejected with a hint, and its columns show "–" (not tracked) in reports.
- **Optional fields**: the reception rating can be skipped (`7R` = clean reception,
  unrated); a player can be left out (`A#` = team kill, no player credited); the
  gender of a player and the libero are optional at setup.
- **Coverage-aware stats**: every report states what was tracked in which sets. Season
  averages only use matches/sets where that skill was tracked, and say so (e.g.
  "pass average: 8 of 12 matches").
- **Optional saving**: a match can be marked *friendly / practice* and excluded from
  season stats, or discarded entirely. Single sets can be excluded too (e.g. a set
  nobody tracked properly).
- **Optional rule checks**: gender, substitution and libero checks are warnings that can
  be switched off per match, never hard blocks.

## 4. Core concepts and data model

The source of truth is an **append-only event log** per match. Score, rotation, who is
on court and every statistic are *derived* by replaying the events. This makes undo,
editing afterwards and recalculating stats straightforward and consistent.

```
Team ─┬─ Player (id, name, jersey #, gender (optional), positions: S/OH/MB/OPP/L, active flag)
      └─ Season ── Match (date, opponent name, home/away, competition, rules preset,
                          tracking profile, counts-in-season flag)
                     └─ Set (number, starting lineup, liberos (0–2), first server)
                          └─ Event (ordered, timestamped)
```

Event types:

- `action`: player, skill, outcome (and the 0–3 rating for reception)
- `opponent_point`: the opponent won the rally with no own-team action to blame
  (e.g. an unreturnable opponent attack), optionally with a reason
- `opponent_error`: the opponent lost the rally by their own fault
- `our_point`: we won the rally with nobody credited (needed for score-only tracking)
- `substitution`: player out, player in
- `libero_swap`: libero in/out for a player
- `penalty`: red card, point to the opponent (art. 31)
- `timeout`, `set_start`, `set_end`, `forfeit`, `manual_correction` (score or rotation
  override)

A rally is the sequence of events between two serves. Its winner is **inferred** from
the terminal event (see §6), so the statistician never enters "point us/them" separately
when an own-team action ended the rally.

## 5. Skills and outcomes

Simple outcomes for every skill, except reception, which gets a pass rating. Every skill
can be switched off (§3.1).

| Skill | Code | Outcomes | Ends rally? |
|---|---|---|---|
| Serve | `S` | `#` ace · `+` in play · `=` error | ace → we win; error → they win |
| Reception | `R` | `3` perfect · `2` good · `1` poor · `0` error (aced) · no digit = unrated | `0` → they win |
| Attack | `A` | `#` kill · `+` in play · `/` blocked · `=` error | kill → we win; blocked/error → they win |
| Block | `B` | `#` block point · `+` touch (keeps ball in play) · `=` error (net, out) | point → we win; error → they win |
| Dig | `D` | `+` dig · `=` error (ball dropped / not playable) | error → they win |
| Set error | `E` | `=` set error (double, lift, unhittable) | → they win |
| Free-ball error | `F` | `=` error on a free ball or down ball | → they win |

- **Reception** is only the first contact on the opponent's **serve**. Free balls and
  down balls are not receptions and never affect the pass average.
- **Free balls**: only errors are recorded (`F=`). Clean free-ball passes are not
  entered at all.
- **Digs**: every successful dig counts (`D+`), whether or not it leads to an attack.
- **Sets**: normal sets are **not** entered; only set errors (`E=`). An optional
  *record every set* setting (`E+`) exists for statisticians who want more detail.

Pass rating scale (common 0–3 convention):
**3** setter has all options · **2** setter limited, still attackable · **1** only a high
ball or free ball over · **0** ace / reception error.

**Assists** are derived, not typed. When an attack is a kill, the assist goes to the
designated setter on court (or, if *record every set* is on, to whoever made the last
set). Assist inference can be switched off.

**Attack distribution**: each attack is stored with the current rotation and the
rally's pass rating, giving "who attacked, after which pass, in which rotation" without
recording sets.

**Blocks**: if one player gets `B#`, it is a solo block (1.0). If two or three players get
`B#` in the same rally, each gets a block assist (0.5).

## 6. Game state and rules engine

- **Score and serve**: the rally winner gets the point. If the receiving team wins, it
  gets the serve (sideout) and **our team rotates** when we win the serve back.
- **Rotation numbering**: R1–R6 = the zone the setter is in (R1 = setter serving from
  zone 1). The app always shows the current rotation and the six players on court.
- **Rules preset**: all rule values come from an editable preset. The default,
  *AmaVolley misto 2025/26*, follows the [AmaVolley 2025/26 rulebook](https://www.amavolley.it/file/regolamento-amavolley-2025-2026.pdf)
  (article numbers in brackets). A *FIVB* preset (25 / tie-break 15, best of 5, no point
  cap, no gender rule) is included for other leagues.
- **Set end** (art. 27): sets to **21**, win by 2, **capped at 23** (23–22 wins the set).
  Tie-break (5th set) to **13**, win by 2, **capped at 16**. The app ends the set
  automatically and asks for confirmation.
- **Match format** (art. 29): best of 5. A forfeit is recorded as 3–0 with every set 21–0
  (art. 10.5) and is excluded from player stats.
- **League points** (art. 29): 3–0 → 3 points, 3–2 → 2, 2–3 → 1, 0–3 → 0. 3–1 and 1–3
  are not listed in the rulebook; the preset assumes 3 and 0 as in FIPAV (editable).
- **Timeouts** (art. 28): 2 per team per set.
- **Gender parity** (art. 19): **exactly 3 women and 3 men** on court. When player
  genders are set, the app checks after every lineup change and warns if parity is broken.
  The **libero is exempt** (art. 22): a libero may replace a player of the other gender,
  and the court still counts as the replaced player's gender.
- **Substitutions** (art. 23): max **8 per set** (raised from the rulebook's 6, per the league; editable in the preset). A player who leaves can only come back
  for the player who replaced them, and that replacement cannot re-enter in the same
  set. Substitutions are **man for man, woman for woman**. Injury substitutions (art. 24)
  also keep parity and are flagged separately. Rule breaks are warnings, never blocks.
- **Libero** (art. 22): up to **2 liberos per set**, and the libero can change every set,
  so it is chosen at each set start, not once per match. The libero replaces back-row
  players only and does not count as a substitution; two liberos can swap freely with
  each other and the replaced player. The statistician sets which players the libero
  replaces (typically the middle blockers). Swaps are then **automatic** when such a player
  rotates to the back row or after they serve, with a one-key override. Libero actions
  that the rules forbid (serve, block, attack above the net) are rejected with a hint.
- **Penalties** (art. 31): a red card gives the rally and the point to the opponent and
  is recorded as its own event (`rc`), so it is not counted as anyone's error.
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
9R3 10A+ 5D+ 10A#     # long rally, ends with a kill (sets are not entered)
2F=                   # 2 misplays a free ball → opponent point
p                     # our point, nobody credited (score-only tracking)
o                     # opponent point (no own-team action at fault)
oe                    # opponent error → our point
sub 11>4              # 11 in for 4
lib                   # manual libero swap override
to                    # our timeout
rc                    # red card → opponent point
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
| Set | assists (inferred), set errors |
| Other errors | free-ball errors |
| Points | kills + aces + total blocks |
| Errors | total points given away (serve, reception, attack, block, dig, set, free-ball errors) |

Team rows also include points from opponent errors and unattributed rallies, so the
totals match the score. Columns for skills that were not tracked show "–" (§3.1).

### 9.2 Rotation analysis

For each rotation R1–R6, per set, per match and per season:

- rallies played, points won/lost, **+/−**
- **sideout %** = rallies won when the opponent served / rallies they served
- **first-ball sideout %** = sideouts won by the first attack after the reception
- **break-point %** = rallies won on our serve / rallies we served
- sideout % **by pass rating** (what does a 1-pass cost us in R4?)
- attack distribution in that rotation (who attacked, with kill rate)

Score-only tracking already gives +/−, sideout % and break-point % per rotation; the
pass-rating and attack rows appear only when those skills were tracked.

### 9.3 Season trends

- Per-player line charts across matches (hitting %, pass average, serve error %, …)
- Team trends: sideout %, break-point %, per-rotation +/−
- Results list with set scores and league points per match (3/2/1/0)
- Filters: date range, opponent, home/away, competition
- Minimum-attempts threshold so small samples don't mislead (shown greyed out)

### 9.4 Exports

- Box score as **PDF** (print layout) and **CSV**. PDF is the way to share with players
  in v1, so it must read well on a phone.
- Choose what goes into each export: sections (box score, rotations, trends), players,
  sets
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

**In v1:** roster and seasons; match setup with lineups; tracking profiles and per-skill
toggles; live entry (codes + buttons); rules engine (score, rotation, subs, libero,
mixed-gender check) with the *AmaVolley misto* preset; editing after the match;
box score, rotation analysis, season trends; PDF/CSV/JSON export; offline local storage.

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

1. **3–1 results**: the rulebook only lists league points for 3–0, 3–2, 2–3 and 0–3.
   The preset assumes 3–1 → 3 points and 1–3 → 0, as in FIPAV. Worth confirming with
   the league.

### Resolved

- Language: English interface
- League: AmaVolley mixed, rules from the [AmaVolley 2025/26 rulebook](https://www.amavolley.it/file/regolamento-amavolley-2025-2026.pdf):
  sets to 21 (cap 23), tie-break to 13 (cap 16), best of 5, 2 timeouts per set,
  exactly 3 women + 3 men on court, 8 same-gender substitutions per set, up to 2 liberos
  per set, libero exempt from parity (§6)
- Sets: not entered; set errors only, assists inferred (§5)
- Digs: every successful dig counts (§5)
- Free balls: not receptions; only errors are counted (§5)
- Sharing: PDF export is enough until sync (§9.4)
- Everything beyond the score is optional to record and save (§3.1)
