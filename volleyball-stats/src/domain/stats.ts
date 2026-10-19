// Stats derived from replayed rallies (spec §9). Counts are only meaningful where a
// skill was tracked, so every aggregate carries how many rallies tracked it.

import type { Rally } from './engine';
import type { Skill } from './types';
import { SKILLS } from './types';

export interface Line {
  serveAtt: number;
  aces: number;
  serveErr: number;
  recAtt: number;
  recRated: number;
  recSum: number;
  rec: [number, number, number, number]; // count per rating 0..3
  attAtt: number;
  kills: number;
  attErr: number;
  blocked: number;
  blockSolo: number;
  blockAssist: number;
  blockTouch: number;
  blockErr: number;
  digs: number;
  digErr: number;
  sets: number;
  assists: number;
  setErr: number;
  freeErr: number;
}

export function emptyLine(): Line {
  return {
    serveAtt: 0,
    aces: 0,
    serveErr: 0,
    recAtt: 0,
    recRated: 0,
    recSum: 0,
    rec: [0, 0, 0, 0],
    attAtt: 0,
    kills: 0,
    attErr: 0,
    blocked: 0,
    blockSolo: 0,
    blockAssist: 0,
    blockTouch: 0,
    blockErr: 0,
    digs: 0,
    digErr: 0,
    sets: 0,
    assists: 0,
    setErr: 0,
    freeErr: 0,
  };
}

export function addLines(a: Line, b: Line): Line {
  const out = emptyLine();
  for (const k of Object.keys(out) as (keyof Line)[]) {
    if (k === 'rec') out.rec = [0, 1, 2, 3].map((i) => a.rec[i] + b.rec[i]) as Line['rec'];
    else (out[k] as number) = (a[k] as number) + (b[k] as number);
  }
  return out;
}

export const blockTotal = (l: Line) => l.blockSolo + 0.5 * l.blockAssist;
export const points = (l: Line) => l.kills + l.aces + blockTotal(l);
export const errorsTotal = (l: Line) =>
  l.serveErr + l.rec[0] + l.attErr + l.blocked + l.blockErr + l.digErr + l.setErr + l.freeErr;
export const passAvg = (l: Line) => (l.recRated ? l.recSum / l.recRated : null);
export const hittingPct = (l: Line) => (l.attAtt ? (l.kills - l.attErr - l.blocked) / l.attAtt : null);
export const killPct = (l: Line) => (l.attAtt ? l.kills / l.attAtt : null);
export const acePct = (l: Line) => (l.serveAtt ? l.aces / l.serveAtt : null);
export const serveErrPct = (l: Line) => (l.serveAtt ? l.serveErr / l.serveAtt : null);
export const perfectPct = (l: Line) => (l.recRated ? l.rec[3] / l.recRated : null);
export const recErrPct = (l: Line) => (l.recAtt ? l.rec[0] / l.recAtt : null);

/** Rallies in which each skill was tracked at all ('any') or with every outcome ('all'). */
export interface Coverage {
  total: number;
  any: Record<Skill, number>;
  all: Record<Skill, number>;
}

export function coverage(rallies: Rally[]): Coverage {
  const any = Object.fromEntries(SKILLS.map((s) => [s, 0])) as Record<Skill, number>;
  const all = { ...any };
  for (const r of rallies) {
    for (const s of SKILLS) {
      const m = r.tracking.skills[s];
      if (m !== 'off') any[s]++;
      if (m === 'all') all[s]++;
    }
  }
  return { total: rallies.length, any, all };
}

export function addCoverage(a: Coverage, b: Coverage): Coverage {
  const sum = (x: Record<Skill, number>, y: Record<Skill, number>) =>
    Object.fromEntries(SKILLS.map((s) => [s, x[s] + y[s]])) as Record<Skill, number>;
  return { total: a.total + b.total, any: sum(a.any, b.any), all: sum(a.all, b.all) };
}

export interface BoxScore {
  players: Map<string, Line>;
  /** Actions entered without a player. */
  unattributed: Line;
  team: Line;
  coverage: Coverage;
  pointsWon: number;
  pointsLost: number;
  /** Rallies won with nobody credited ("p"). */
  ourPoints: number;
  oppErrors: number;
  oppPoints: number;
  penalties: number;
}

/** Who gets the assist for a kill in this rally, if anyone. */
function assistFor(r: Rally, killIndex: number): string | null {
  if (!r.tracking.inferAssists) return null;
  const attacker = r.actions[killIndex].playerId;
  if (r.tracking.recordEverySet) {
    for (let i = killIndex - 1; i >= 0; i--) {
      const a = r.actions[i];
      if (a.skill === 'E' && a.outcome === '+') return a.playerId && a.playerId !== attacker ? a.playerId : null;
    }
  }
  return r.setterId && r.setterId !== attacker ? r.setterId : null;
}

export function boxScore(rallies: Rally[]): BoxScore {
  const players = new Map<string, Line>();
  const unattributed = emptyLine();
  const lineFor = (id: string | null) => {
    if (!id) return unattributed;
    let l = players.get(id);
    if (!l) players.set(id, (l = emptyLine()));
    return l;
  };
  let pointsWon = 0;
  let pointsLost = 0;
  let ourPoints = 0;
  let oppErrors = 0;
  let oppPoints = 0;
  let penalties = 0;

  for (const r of rallies) {
    if (r.winner === 'us') pointsWon++;
    else if (r.winner === 'them') pointsLost++;
    if (r.endedBy === 'our_point') ourPoints++;
    if (r.endedBy === 'opp_error') oppErrors++;
    if (r.endedBy === 'opp_point') oppPoints++;
    if (r.endedBy === 'penalty') penalties++;

    r.actions.forEach((a, i) => {
      const l = lineFor(a.playerId);
      switch (a.skill) {
        case 'S':
          l.serveAtt++;
          if (a.outcome === '#') l.aces++;
          if (a.outcome === '=') l.serveErr++;
          break;
        case 'R':
          l.recAtt++;
          if (a.rating !== null) {
            l.recRated++;
            l.recSum += a.rating;
            l.rec[a.rating as 0 | 1 | 2 | 3]++;
          }
          break;
        case 'A':
          l.attAtt++;
          if (a.outcome === '#') {
            l.kills++;
            const setter = assistFor(r, i);
            if (setter) lineFor(setter).assists++;
          }
          if (a.outcome === '=') l.attErr++;
          if (a.outcome === '/') l.blocked++;
          break;
        case 'B':
          if (a.outcome === '#') {
            if (a.coBlockers.length === 0) l.blockSolo++;
            else for (const id of [a.playerId, ...a.coBlockers]) lineFor(id).blockAssist++;
          }
          if (a.outcome === '+') l.blockTouch++;
          if (a.outcome === '=') l.blockErr++;
          break;
        case 'D':
          if (a.outcome === '+') l.digs++;
          if (a.outcome === '=') l.digErr++;
          break;
        case 'E':
          if (a.outcome === '+') l.sets++;
          if (a.outcome === '=') l.setErr++;
          break;
        case 'F':
          l.freeErr++;
          break;
      }
    });
  }

  let team = unattributed;
  for (const l of players.values()) team = addLines(team, l);
  return {
    players,
    unattributed,
    team,
    coverage: coverage(rallies),
    pointsWon,
    pointsLost,
    ourPoints,
    oppErrors,
    oppPoints,
    penalties,
  };
}

export interface RotationRow {
  rotation: number;
  rallies: number;
  won: number;
  lost: number;
  receive: number;
  sideouts: number;
  serve: number;
  breaks: number;
  /** Receive rallies where attacks were tracked, and those won by the first attack. */
  fbsoBase: number;
  fbso: number;
  /** Receive rallies by pass rating 0..3: [rallies, won]. */
  byPass: [number, number][];
  /** Attacks per player in this rotation (attack-tracked rallies only). */
  attacks: Map<string | null, { att: number; kills: number }>;
}

export function rotationRows(rallies: Rally[]): RotationRow[] {
  const rows: RotationRow[] = [1, 2, 3, 4, 5, 6].map((rotation) => ({
    rotation,
    rallies: 0,
    won: 0,
    lost: 0,
    receive: 0,
    sideouts: 0,
    serve: 0,
    breaks: 0,
    fbsoBase: 0,
    fbso: 0,
    byPass: [
      [0, 0],
      [0, 0],
      [0, 0],
      [0, 0],
    ],
    attacks: new Map(),
  }));
  for (const r of rallies) {
    const row = rows[r.rotation - 1];
    const won = r.winner === 'us';
    row.rallies++;
    if (won) row.won++;
    else row.lost++;
    if (r.weServed) {
      row.serve++;
      if (won) row.breaks++;
    } else {
      row.receive++;
      if (won) row.sideouts++;
      if (r.tracking.skills.A === 'all') {
        row.fbsoBase++;
        const firstAttack = r.actions.find((a) => a.skill === 'A');
        if (won && firstAttack?.outcome === '#') row.fbso++;
      }
      const rec = r.actions.find((a) => a.skill === 'R');
      if (rec && rec.rating !== null) {
        row.byPass[rec.rating][0]++;
        if (won) row.byPass[rec.rating][1]++;
      }
    }
    if (r.tracking.skills.A === 'all') {
      for (const a of r.actions) {
        if (a.skill !== 'A') continue;
        const s = row.attacks.get(a.playerId) ?? { att: 0, kills: 0 };
        s.att++;
        if (a.outcome === '#') s.kills++;
        row.attacks.set(a.playerId, s);
      }
    }
  }
  return rows;
}

export function sumRotationRows(rows: RotationRow[]): RotationRow {
  const total: RotationRow = { ...rotationRows([])[0], rotation: 0 };
  for (const r of rows) {
    total.rallies += r.rallies;
    total.won += r.won;
    total.lost += r.lost;
    total.receive += r.receive;
    total.sideouts += r.sideouts;
    total.serve += r.serve;
    total.breaks += r.breaks;
    total.fbsoBase += r.fbsoBase;
    total.fbso += r.fbso;
    r.byPass.forEach(([n, w], i) => {
      total.byPass[i][0] += n;
      total.byPass[i][1] += w;
    });
    for (const [id, s] of r.attacks) {
      const t = total.attacks.get(id) ?? { att: 0, kills: 0 };
      total.attacks.set(id, { att: t.att + s.att, kills: t.kills + s.kills });
    }
  }
  return total;
}

export const ratio = (n: number, d: number) => (d ? n / d : null);
