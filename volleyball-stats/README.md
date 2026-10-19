# Volleyball Stats

A laptop web app for live statistics of one indoor (6v6) mixed volleyball team, built
for the AmaVolley amateur league (Trento). See [SPEC.md](SPEC.md) for the full
requirements.

- **Live entry** with short keyboard codes (`12S+ 7R2 3A#`) or clickable buttons
- **Full game state**: score, serve, rotation (R1–R6), substitutions and an automatic libero
- **AmaVolley rules**: sets to 21 (cap 23), tie-break to 13 (cap 16), 3 women + 3 men on
  court, 8 same-gender substitutions per set, libero exempt from parity. Rule breaks are
  warnings, never blocks
- **Everything optional**: score only, basic, full or custom tracking, switchable mid-match;
  reports show "–" for what was not recorded
- **Reports**: box score, rotation analysis (sideout %, break-point %, sideout by pass
  rating), season trends and league points; print to PDF, CSV export
- **Local-first**: data stays in the browser (IndexedDB), works offline as an installable
  app; JSON backup and restore

## Run it

Requires Node.js 20 or newer.

```bash
cd volleyball-stats
npm install
npm run dev        # development server at http://localhost:5173
```

To use it at the court without a dev server, build it once and serve the `dist` folder
from any static host (or `npm run preview`). After the first visit it works offline and
can be installed from the browser's address bar.

```bash
npm run build      # outputs dist/
npm run preview    # serves dist/ at http://localhost:4173
```

## Develop

```bash
npm test           # unit tests for the rules engine, parser and stats
npm run typecheck
```

Code layout:

| Path | What |
|---|---|
| `src/domain/` | Pure TypeScript: types, rule presets, code parser, replay engine, stats |
| `src/db/` | IndexedDB storage (Dexie), backup and restore |
| `src/ui/` | React pages and components |

The event log per match is the source of truth. Score, rotation, lineup and every
statistic are derived by replaying it (`src/domain/engine.ts`), which makes undo and
editing after the match safe.

## Keyboard codes

| Code | Meaning |
|---|---|
| `12S+` | #12 serve in (`S#` ace, `S=` error); `S+` alone = the current server |
| `7R2` | #7 reception rated 2 (`R3` … `R0` error, `R` alone = unrated) |
| `3A#` | #3 kill (`A+` in play, `A/` blocked, `A=` error) |
| `4B# 9B#` | shared block point (`B+` touch, `B=` error) |
| `5D+` | dig (`D=` dig error) |
| `1E=` | set error |
| `2F=` | free-ball error |
| `A#` | no number: team action, no player credited |
| `p` | our point, nobody credited (for score-only tracking) |
| `o` / `oe` | opponent point / opponent error |
| `rc` | red card, point to the opponent |
| `to` | timeout |
| `sub 11>4` | #11 in for #4 (`inj 11>4` for an injury) |
| `lib` / `lib 14` | libero on/off / make #14 the active libero |
