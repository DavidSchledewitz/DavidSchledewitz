// Keyboard code grammar (spec §7.1):
//   <jersey><skill><outcome>   e.g. 12S+  7R2  3A#  A# (team)  S= (current server)
//   p  o  oe  rc  to  lib  lib 7  sub 11>4  inj 11>4
// Several tokens per line, separated by spaces.

import type { EventBody, Outcome, Player, Skill } from './types';

export interface ParseResult {
  events: EventBody[];
  error: string | null;
}

const ACTION_RE = /^(\d{1,3})?([SRABDEFsrabdef])([#+/=]|[0-3])?$/;
const SUB_RE = /^(\d{1,3})>(\d{1,3})$/;

export function findPlayer(players: Player[], number: number): Player | undefined {
  return players.find((p) => p.number === number && p.active) ?? players.find((p) => p.number === number);
}

export function parseLine(text: string, players: Player[]): ParseResult {
  const tokens = text.trim().split(/\s+/).filter(Boolean);
  const events: EventBody[] = [];
  const fail = (error: string): ParseResult => ({ events: [], error });
  const player = (num: string): Player | string => {
    const p = findPlayer(players, Number(num));
    return p ?? `No player #${num} in the roster.`;
  };

  for (let i = 0; i < tokens.length; i++) {
    const raw = tokens[i];
    const t = raw.toLowerCase();
    if (t === 'p') {
      events.push({ type: 'our_point' });
      continue;
    }
    if (t === 'o' || t === 'op') {
      events.push({ type: 'opp_point' });
      continue;
    }
    if (t === 'oe') {
      events.push({ type: 'opp_error' });
      continue;
    }
    if (t === 'rc') {
      events.push({ type: 'penalty' });
      continue;
    }
    if (t === 'to') {
      events.push({ type: 'timeout' });
      continue;
    }
    if (t === 'lib' || /^lib\d+$/.test(t)) {
      let num = t.slice(3);
      if (!num && /^\d+$/.test(tokens[i + 1] ?? '')) num = tokens[++i];
      if (!num) {
        events.push({ type: 'libero', liberoId: null });
        continue;
      }
      const p = player(num);
      if (typeof p === 'string') return fail(p);
      events.push({ type: 'libero', liberoId: p.id });
      continue;
    }
    if (t === 'sub' || t === 'inj') {
      const m = SUB_RE.exec(tokens[i + 1] ?? '');
      if (!m) return fail(`Write substitutions as "${t} IN>OUT", e.g. "${t} 11>4".`);
      i++;
      const pin = player(m[1]);
      if (typeof pin === 'string') return fail(pin);
      const pout = player(m[2]);
      if (typeof pout === 'string') return fail(pout);
      events.push({ type: 'sub', inId: pin.id, outId: pout.id, injury: t === 'inj' });
      continue;
    }

    const m = ACTION_RE.exec(raw);
    if (!m) return fail(`Can't read "${raw}". Example: 7A# (player 7, attack, kill).`);
    const skill = m[2].toUpperCase() as Skill;
    const sym = m[3] ?? '';
    let playerId: string | null = null;
    if (m[1]) {
      const p = player(m[1]);
      if (typeof p === 'string') return fail(p);
      playerId = p.id;
    }

    if (skill === 'R') {
      if (sym === '' || /^[0-3]$/.test(sym) || sym === '=') {
        const rating = sym === '' ? null : sym === '=' ? 0 : Number(sym);
        events.push({ type: 'action', playerId, skill, outcome: rating === 0 ? '=' : '+', rating });
        continue;
      }
      return fail(`Reception takes a rating 0–3 (or nothing for unrated), e.g. ${m[1] ?? ''}R2.`);
    }
    if (sym === '' || /^[0-3]$/.test(sym)) {
      return fail(`"${raw}" needs an outcome: # point, + in play, / blocked, = error.`);
    }
    const outcome = sym as Outcome;

    // Adjacent block points become one shared block: "4B# 9B#".
    const prev = events[events.length - 1];
    if (skill === 'B' && outcome === '#' && prev?.type === 'action' && prev.skill === 'B' && prev.outcome === '#') {
      if (playerId) prev.coBlockers = [...(prev.coBlockers ?? []), playerId];
      continue;
    }
    events.push({ type: 'action', playerId, skill, outcome, rating: null });
  }
  return { events, error: null };
}

/** The keyboard code for an event, for the rally log and for editing. */
export function eventToCode(e: EventBody, players: Player[]): string {
  const num = (id: string | null) => (id ? String(players.find((p) => p.id === id)?.number ?? '?') : '');
  switch (e.type) {
    case 'action': {
      const sym = e.skill === 'R' ? (e.rating === null ? '' : String(e.rating)) : e.outcome ?? '';
      const main = `${num(e.playerId)}${e.skill}${sym}`;
      const co = (e.coBlockers ?? []).map((id) => ` ${num(id)}B#`).join('');
      return main + co;
    }
    case 'our_point':
      return 'p';
    case 'opp_point':
      return 'o';
    case 'opp_error':
      return 'oe';
    case 'penalty':
      return 'rc';
    case 'timeout':
      return 'to';
    case 'libero':
      return e.liberoId ? `lib ${num(e.liberoId)}` : 'lib';
    case 'sub':
      return `${e.injury ? 'inj' : 'sub'} ${num(e.inId)}>${num(e.outId)}`;
    case 'set_start':
      return `set ${e.set} start`;
    case 'tracking':
      return `tracking: ${e.tracking.profile}`;
    case 'correction':
      return `fix ${e.us}-${e.them}${e.rotations ? ` rot+${e.rotations}` : ''}`;
  }
}

const OUTCOME_WORDS: Record<Skill, Partial<Record<Outcome, string>>> = {
  S: { '#': 'ace', '+': 'serve in', '=': 'serve error' },
  R: {},
  A: { '#': 'kill', '+': 'attack in play', '/': 'attack blocked', '=': 'attack error' },
  B: { '#': 'block point', '+': 'block touch', '=': 'block error' },
  D: { '+': 'dig', '=': 'dig error' },
  E: { '+': 'set', '=': 'set error' },
  F: { '=': 'free-ball error' },
};

/** Plain-English description of an event, for the rally log. */
export function describeEvent(e: EventBody, players: Player[]): string {
  const who = (id: string | null) => {
    if (!id) return 'Team';
    const p = players.find((x) => x.id === id);
    return p ? `#${p.number} ${p.name}` : 'Unknown';
  };
  switch (e.type) {
    case 'action': {
      if (e.skill === 'R') {
        const r = e.rating === null ? 'reception (unrated)' : e.rating === 0 ? 'reception error' : `reception ${e.rating}`;
        return `${who(e.playerId)}: ${r}`;
      }
      const co = (e.coBlockers ?? []).length ? ` (with ${(e.coBlockers ?? []).map(who).join(', ')})` : '';
      return `${e.playerId || e.skill !== 'S' ? who(e.playerId) : 'Server'}: ${OUTCOME_WORDS[e.skill][e.outcome!] ?? e.outcome}${co}`;
    }
    case 'our_point':
      return 'Our point';
    case 'opp_point':
      return 'Opponent point';
    case 'opp_error':
      return 'Opponent error';
    case 'penalty':
      return 'Red card: point to opponent';
    case 'timeout':
      return 'Timeout';
    case 'libero':
      return e.liberoId ? `Libero ${who(e.liberoId)} in` : 'Libero auto on/off';
    case 'sub':
      return `${e.injury ? 'Injury sub' : 'Sub'}: ${who(e.inId)} in for ${who(e.outId)}`;
    case 'set_start':
      return `Set ${e.set} started`;
    case 'tracking':
      return `Tracking changed to ${e.tracking.profile}`;
    case 'correction':
      return `Score fixed to ${e.us}–${e.them}`;
  }
}
