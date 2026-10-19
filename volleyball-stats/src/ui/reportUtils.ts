// Shared report helpers: box score column definitions (used by the table and the CSV),
// season aggregation and the event log export.

import type { Rally } from '../domain/engine';
import { describeEvent, eventToCode } from '../domain/parser';
import { SKILL_NAMES } from '../domain/presets';
import {
  acePct,
  addCoverage,
  addLines,
  blockTotal,
  boxScore,
  coverage,
  emptyLine,
  errorsTotal,
  hittingPct,
  killPct,
  passAvg,
  perfectPct,
  points,
  recErrPct,
  serveErrPct,
  type BoxScore,
  type Coverage,
  type Line,
} from '../domain/stats';
import type { MatchSummary } from '../domain/summary';
import { SKILLS, type MatchEvent, type Player, type Skill } from '../domain/types';
import { DASH, avg, hit, num, pct } from './format';

/** What a column needs to be meaningful: every outcome of a skill, or only its errors. */
type Need = { skill: Skill; mode: 'all' | 'any' } | { anyOf: Skill[]; mode: 'all' | 'any' };

export interface BoxCol {
  key: string;
  label: string;
  title: string;
  need: Need;
  value: (l: Line) => string;
  /** For ratios: the attempts behind them, to dim small samples. */
  attempts?: (l: Line) => number;
}

export interface BoxGroup {
  name: string;
  cols: BoxCol[];
}

const count = (f: (l: Line) => number) => (l: Line) => num(f(l));
const all = (skill: Skill): Need => ({ skill, mode: 'all' });
const any = (skill: Skill): Need => ({ skill, mode: 'any' });

/** Box score columns (spec §9.1), split in three tables so each fits a portrait page. */
export const BOX_TABLES: BoxGroup[][] = [
  [
    {
      name: 'Serve',
      cols: [
        { key: 'serveAtt', label: 'Att', title: 'Serves', need: all('S'), value: count((l) => l.serveAtt) },
        { key: 'aces', label: 'Ace', title: 'Aces', need: all('S'), value: count((l) => l.aces) },
        { key: 'serveErr', label: 'Err', title: 'Serve errors', need: any('S'), value: count((l) => l.serveErr) },
        { key: 'acePct', label: 'Ace%', title: 'Ace %', need: all('S'), value: (l) => pct(acePct(l)), attempts: (l) => l.serveAtt },
        { key: 'serveErrPct', label: 'Err%', title: 'Serve error %', need: all('S'), value: (l) => pct(serveErrPct(l)), attempts: (l) => l.serveAtt },
      ],
    },
    {
      name: 'Reception',
      cols: [
        { key: 'recAtt', label: 'Att', title: 'Receptions', need: all('R'), value: count((l) => l.recAtt) },
        { key: 'rec0', label: '0', title: 'Reception errors (rated 0)', need: any('R'), value: count((l) => l.rec[0]) },
        { key: 'rec1', label: '1', title: 'Rated 1', need: all('R'), value: count((l) => l.rec[1]) },
        { key: 'rec2', label: '2', title: 'Rated 2', need: all('R'), value: count((l) => l.rec[2]) },
        { key: 'rec3', label: '3', title: 'Rated 3 (perfect)', need: all('R'), value: count((l) => l.rec[3]) },
        { key: 'passAvg', label: 'Avg', title: 'Pass average (rated receptions)', need: all('R'), value: (l) => avg(passAvg(l)), attempts: (l) => l.recRated },
        { key: 'perfectPct', label: 'Perf%', title: 'Perfect passes %', need: all('R'), value: (l) => pct(perfectPct(l)), attempts: (l) => l.recRated },
        { key: 'recErrPct', label: 'Err%', title: 'Reception error %', need: all('R'), value: (l) => pct(recErrPct(l)), attempts: (l) => l.recAtt },
      ],
    },
  ],
  [
    {
      name: 'Attack',
      cols: [
        { key: 'attAtt', label: 'Att', title: 'Attacks', need: all('A'), value: count((l) => l.attAtt) },
        { key: 'kills', label: 'K', title: 'Kills', need: all('A'), value: count((l) => l.kills) },
        { key: 'attErr', label: 'E', title: 'Attack errors', need: any('A'), value: count((l) => l.attErr) },
        { key: 'blocked', label: 'Blk', title: 'Attacks blocked', need: any('A'), value: count((l) => l.blocked) },
        { key: 'hitPct', label: 'Hit%', title: 'Hitting % = (K − E − Blocked) / Att', need: all('A'), value: (l) => hit(hittingPct(l)), attempts: (l) => l.attAtt },
        { key: 'killPct', label: 'K%', title: 'Kill %', need: all('A'), value: (l) => pct(killPct(l)), attempts: (l) => l.attAtt },
      ],
    },
    {
      name: 'Block',
      cols: [
        { key: 'blockSolo', label: 'Solo', title: 'Solo blocks', need: all('B'), value: count((l) => l.blockSolo) },
        { key: 'blockAssist', label: 'Ast', title: 'Block assists', need: all('B'), value: count((l) => l.blockAssist) },
        { key: 'blockTotal', label: 'Tot', title: 'Total blocks (solo + 0.5 × assists)', need: all('B'), value: (l) => num(blockTotal(l)) },
        { key: 'blockErr', label: 'Err', title: 'Block errors', need: any('B'), value: count((l) => l.blockErr) },
      ],
    },
    {
      name: 'Dig',
      cols: [
        { key: 'digs', label: 'Digs', title: 'Digs', need: all('D'), value: count((l) => l.digs) },
        { key: 'digErr', label: 'Err', title: 'Dig errors', need: any('D'), value: count((l) => l.digErr) },
      ],
    },
  ],
  [
    {
      name: 'Set',
      cols: [
        { key: 'assists', label: 'Ast', title: 'Assists (inferred)', need: all('A'), value: count((l) => l.assists) },
        { key: 'setErr', label: 'Err', title: 'Set errors', need: any('E'), value: count((l) => l.setErr) },
      ],
    },
    {
      name: 'Other',
      cols: [{ key: 'freeErr', label: 'FB err', title: 'Free-ball errors', need: any('F'), value: count((l) => l.freeErr) }],
    },
    {
      name: 'Totals',
      cols: [
        { key: 'points', label: 'Pts', title: 'Points: kills + aces + total blocks', need: { anyOf: ['S', 'A', 'B'], mode: 'all' }, value: (l) => num(points(l)) },
        { key: 'errors', label: 'Errs', title: 'Points given away by errors', need: { anyOf: SKILLS, mode: 'any' }, value: count(errorsTotal) },
      ],
    },
  ],
];

export function covered(cov: Coverage, need: Need): boolean {
  const table = need.mode === 'all' ? cov.all : cov.any;
  return 'skill' in need ? table[need.skill] > 0 : need.anyOf.some((s) => table[s] > 0);
}

export function cellValue(col: BoxCol, line: Line, cov: Coverage): string {
  return covered(cov, col.need) ? col.value(line) : DASH;
}

/** Sentences about skills tracked in only part of the rallies, or not at all. */
export function coverageNotes(cov: Coverage): string[] {
  if (cov.total === 0) return [];
  const notes: string[] = [];
  const notTracked: string[] = [];
  for (const s of SKILLS) {
    const name = SKILL_NAMES[s];
    if (cov.any[s] === 0) notTracked.push(name.toLowerCase());
    // Set and free-ball errors are errors-only by design, so only partial tracking is worth a note.
    else if (s === 'E' || s === 'F') {
      if (cov.any[s] < cov.total) notes.push(`${name} errors tracked in ${cov.any[s]} of ${cov.total} rallies.`);
    } else if (cov.all[s] === 0) notes.push(`${name}: only errors tracked${cov.any[s] < cov.total ? ` (in ${cov.any[s]} of ${cov.total} rallies)` : ''}.`);
    else if (cov.all[s] < cov.total) notes.push(`${name} tracked in ${cov.all[s]} of ${cov.total} rallies.`);
  }
  if (notTracked.length) notes.push(`Not tracked: ${notTracked.join(', ')}.`);
  return notes;
}

export function playerName(players: Player[], id: string): string {
  const p = players.find((x) => x.id === id);
  return p ? `#${p.number} ${p.name}` : 'Unknown player';
}

/** Player ids with a line in the box score, sorted by jersey number. */
export function boxPlayerIds(box: BoxScore, players: Player[]): string[] {
  const numberOf = (id: string) => players.find((p) => p.id === id)?.number ?? 999;
  return [...box.players.keys()].sort((a, b) => numberOf(a) - numberOf(b));
}

export const hasActions = (l: Line) => (Object.keys(l) as (keyof Line)[]).some((k) => (k === 'rec' ? l.rec.some(Boolean) : l[k] !== 0));

export function boxScoreCsv(box: BoxScore, players: Player[], only?: Set<string>): unknown[][] {
  const cols = BOX_TABLES.flat().flatMap((g) => g.cols.map((c) => ({ ...c, group: g.name })));
  const rows: unknown[][] = [['Player', ...cols.map((c) => `${c.group} ${c.title}`)]];
  const line = (label: string, l: Line) => rows.push([label, ...cols.map((c) => cellValue(c, l, box.coverage))]);
  for (const id of boxPlayerIds(box, players)) {
    if (!only || only.has(id)) line(playerName(players, id), box.players.get(id)!);
  }
  if (hasActions(box.unattributed)) line('Unattributed', box.unattributed);
  line('Team', box.team);
  return rows;
}

/** Combine per-match box scores into one (season totals). */
export function mergeBoxScores(boxes: BoxScore[]): BoxScore {
  const merged: BoxScore = boxScore([]);
  for (const b of boxes) {
    for (const [id, l] of b.players) merged.players.set(id, addLines(merged.players.get(id) ?? emptyLine(), l));
    merged.unattributed = addLines(merged.unattributed, b.unattributed);
    merged.team = addLines(merged.team, b.team);
    merged.coverage = addCoverage(merged.coverage, b.coverage);
    merged.pointsWon += b.pointsWon;
    merged.pointsLost += b.pointsLost;
    merged.ourPoints += b.ourPoints;
    merged.oppErrors += b.oppErrors;
    merged.oppPoints += b.oppPoints;
    merged.penalties += b.penalties;
  }
  return merged;
}

/** In how many matches each skill was tracked with every outcome. */
export function matchesTracking(rallySets: Rally[][]): Record<Skill, number> {
  const out = Object.fromEntries(SKILLS.map((s) => [s, 0])) as Record<Skill, number>;
  for (const rallies of rallySets) {
    const cov = coverage(rallies);
    for (const s of SKILLS) if (cov.all[s] > 0) out[s]++;
  }
  return out;
}

export const LOG_HEADER = ['Set', 'Rally', 'Score before', 'Rotation', 'We served', 'Code', 'Description', 'Rally winner', 'Issues'];

/** One row per event, in order, with the rally context it belongs to. */
export function eventLogRows(summary: MatchSummary, events: MatchEvent[], players: Player[]): unknown[][] {
  const rallyOf = new Map<string, Rally>();
  for (const r of [...summary.replay.rallies, summary.replay.state.rally]) {
    for (const a of r.actions) rallyOf.set(a.eventId, r);
    if (r.endEventId) rallyOf.set(r.endEventId, r);
  }
  const rows: unknown[][] = [];
  let set = 0;
  for (const e of events) {
    const issues = summary.replay.issues[e.id] ?? [];
    if (e.type === 'set_start' && !issues.some((i) => i.severity === 'error')) set++;
    const r = rallyOf.get(e.id);
    rows.push([
      r?.set ?? (set || ''),
      r?.index ?? '',
      r ? `${r.scoreBefore.us}-${r.scoreBefore.them}` : '',
      r ? `R${r.rotation}` : '',
      r ? (r.weServed ? 'yes' : 'no') : '',
      eventToCode(e, players),
      describeEvent(e, players),
      r && r.endEventId === e.id ? (r.winner === 'us' ? 'us' : 'them') : '',
      issues.map((i) => `${i.severity}: ${i.message}`).join(' | '),
    ]);
  }
  return rows;
}
