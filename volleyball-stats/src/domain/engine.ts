// Replays a match's event log into game state and a list of rallies.
// The engine never throws on bad input: an invalid event gets an 'error' issue and
// is skipped, a rule break gets a 'warning' issue and is applied anyway.

import { SKILL_NAMES } from './presets';
import type { Match, MatchEvent, Outcome, Player, RulesPreset, Skill, Tracking } from './types';

export type Side = 'us' | 'them';

export interface Issue {
  severity: 'error' | 'warning';
  message: string;
}

export interface RallyAction {
  eventId: string;
  playerId: string | null;
  skill: Skill;
  outcome: Outcome | null;
  rating: number | null;
  coBlockers: string[];
}

export interface Rally {
  set: number;
  /** 1-based rally number within the set. */
  index: number;
  rotation: number;
  weServed: boolean;
  serverId: string | null;
  /** Effective players on court (libero included). */
  onCourt: string[];
  /** Player in the setter's slot, for assists (null if none designated or libero there). */
  setterId: string | null;
  actions: RallyAction[];
  winner: Side | null;
  endedBy: 'action' | 'our_point' | 'opp_point' | 'opp_error' | 'penalty' | null;
  endEventId: string | null;
  scoreBefore: { us: number; them: number };
  tracking: Tracking;
}

export interface SetSummary {
  set: number;
  us: number;
  them: number;
  winner: Side | null;
  timeouts: number;
  subs: number;
}

export type Phase = 'no_set' | 'rally' | 'set_over' | 'match_over';

export interface LiberoOn {
  liberoId: string;
  replacedId: string;
  zone: number;
}

export interface LiveState {
  phase: Phase;
  set: number;
  us: number;
  them: number;
  setsUs: number;
  setsThem: number;
  weServe: boolean;
  rotation: number;
  /** Base (non-libero) player per zone, index 0 = zone 1. */
  slotZones: string[];
  /** Effective player per zone with the libero applied. */
  zones: string[];
  serverId: string | null;
  liberos: string[];
  activeLibero: string | null;
  liberoAuto: boolean;
  liberoOn: LiberoOn | null;
  setterId: string | null;
  timeouts: number;
  subs: number;
  tracking: Tracking;
  rally: Rally;
}

export interface ReplayResult {
  rallies: Rally[];
  sets: SetSummary[];
  issues: Record<string, Issue[]>;
  state: LiveState;
}

export function isTiebreak(set: number, rules: RulesPreset): boolean {
  return set === rules.bestOf;
}

export function setTarget(set: number, rules: RulesPreset): { points: number; cap: number | null } {
  return isTiebreak(set, rules)
    ? { points: rules.tiebreakPoints, cap: rules.tiebreakCap }
    : { points: rules.setPoints, cap: rules.setCap };
}

export function setWinner(us: number, them: number, set: number, rules: RulesPreset): Side | null {
  const { points, cap } = setTarget(set, rules);
  const check = (a: number, b: number) =>
    (a >= points && a - b >= rules.winBy) || (cap !== null && a >= cap && a > b);
  if (check(us, them)) return 'us';
  if (check(them, us)) return 'them';
  return null;
}

export function setsToWin(rules: RulesPreset): number {
  return Math.floor(rules.bestOf / 2) + 1;
}

/** Which side wins the rally when this action ends it, or null if play continues. */
export function terminalWinner(skill: Skill, outcome: Outcome | null, rating: number | null): Side | null {
  if (skill === 'R') return rating === 0 || outcome === '=' ? 'them' : null;
  if (outcome === '=') return 'them';
  if (outcome === '#' && (skill === 'S' || skill === 'A' || skill === 'B')) return 'us';
  if (outcome === '/' && skill === 'A') return 'them';
  return null;
}

/** True when the action records an error (the only thing tracked in 'errors' mode). */
export function isErrorAction(skill: Skill, outcome: Outcome | null, rating: number | null): boolean {
  return terminalWinner(skill, outcome, rating) === 'them';
}

const VALID_OUTCOMES: Record<Skill, Outcome[]> = {
  S: ['#', '+', '='],
  R: ['+', '='],
  A: ['#', '+', '/', '='],
  B: ['#', '+', '='],
  D: ['+', '='],
  E: ['+', '='],
  F: ['='],
};

interface SetCtx {
  number: number;
  /** Base players by starting slot; slot i started in zone i+1. */
  slots: string[];
  rotations: number;
  setterSlot: number;
  hasSetter: boolean;
  liberos: string[];
  liberoSlots: number[];
  partner: Map<string, string>;
  locked: Set<string>;
}

export function replay(match: Pick<Match, 'rules' | 'tracking'>, players: Player[], events: MatchEvent[]): ReplayResult {
  const rules = match.rules;
  const byId = new Map(players.map((p) => [p.id, p]));
  const label = (id: string | null) => {
    if (!id) return 'team';
    const p = byId.get(id);
    return p ? `#${p.number}` : 'unknown player';
  };

  const issues: Record<string, Issue[]> = {};
  const addIssue = (eventId: string, severity: Issue['severity'], message: string) => {
    (issues[eventId] ??= []).push({ severity, message });
  };

  const rallies: Rally[] = [];
  const sets: SetSummary[] = [];
  let tracking: Tracking = match.tracking;
  let phase: Phase = 'no_set';
  let ctx: SetCtx | null = null;
  let us = 0;
  let them = 0;
  let setsUs = 0;
  let setsThem = 0;
  let weServe = false;
  let liberoAuto = true;
  let activeLibero: string | null = null;
  let timeouts = 0;
  let subs = 0;
  let rally: Rally = emptyRally();

  function zoneOfSlot(slot: number): number {
    return ((((slot - ctx!.rotations) % 6) + 6) % 6) + 1;
  }
  function slotInZone(zone: number): number {
    return (zone - 1 + ctx!.rotations) % 6;
  }
  function slotZones(): string[] {
    if (!ctx) return [];
    return [1, 2, 3, 4, 5, 6].map((z) => ctx!.slots[slotInZone(z)]);
  }
  function liberoOn(): LiberoOn | null {
    if (!ctx || !liberoAuto || !activeLibero) return null;
    for (const slot of ctx.liberoSlots) {
      const zone = zoneOfSlot(slot);
      if (zone === 5 || zone === 6 || (zone === 1 && !weServe)) {
        return { liberoId: activeLibero, replacedId: ctx.slots[slot], zone };
      }
    }
    return null;
  }
  function zones(): string[] {
    const base = slotZones();
    const lib = liberoOn();
    if (lib) base[lib.zone - 1] = lib.liberoId;
    return base;
  }
  function rotation(): number {
    return ctx ? zoneOfSlot(ctx.setterSlot) : 1;
  }
  function serverId(): string | null {
    return ctx && weServe ? ctx.slots[slotInZone(1)] : null;
  }
  function setterOnCourt(): string | null {
    if (!ctx || !ctx.hasSetter) return null;
    const id = ctx.slots[ctx.setterSlot];
    const lib = liberoOn();
    return lib && lib.replacedId === id ? null : id;
  }
  function emptyRally(): Rally {
    return {
      set: ctx?.number ?? 0,
      index: rallies.filter((r) => r.set === (ctx?.number ?? 0)).length + 1,
      rotation: rotation(),
      weServed: weServe,
      serverId: serverId(),
      onCourt: ctx ? zones() : [],
      setterId: setterOnCourt(),
      actions: [],
      winner: null,
      endedBy: null,
      endEventId: null,
      scoreBefore: { us, them },
      tracking,
    };
  }
  function checkParity(eventId: string) {
    if (!ctx || !rules.genderParity || !tracking.ruleChecks) return;
    const genders = ctx.slots.map((id) => byId.get(id)?.gender);
    if (genders.some((g) => !g)) return;
    const f = genders.filter((g) => g === 'F').length;
    const m = genders.length - f;
    if (f !== rules.genderParity.F || m !== rules.genderParity.M) {
      addIssue(eventId, 'warning', `Gender parity: ${f} women and ${m} men on court (rule: ${rules.genderParity.F} + ${rules.genderParity.M}).`);
    }
  }
  function closeRally(winner: Side, endedBy: Rally['endedBy'], eventId: string) {
    rally.winner = winner;
    rally.endedBy = endedBy;
    rally.endEventId = eventId;
    rallies.push(rally);
    if (winner === 'us') {
      us++;
      if (!weServe) {
        ctx!.rotations++;
        weServe = true;
      }
    } else {
      them++;
      weServe = false;
    }
    checkSetEnd();
    rally = emptyRally();
  }
  function checkSetEnd() {
    const w = setWinner(us, them, ctx!.number, rules);
    if (!w) return;
    const summary = sets[sets.length - 1];
    summary.us = us;
    summary.them = them;
    summary.winner = w;
    if (w === 'us') setsUs++;
    else setsThem++;
    const need = setsToWin(rules);
    phase = setsUs >= need || setsThem >= need ? 'match_over' : 'set_over';
  }
  function needRally(e: MatchEvent): boolean {
    if (phase === 'rally') return true;
    const msg =
      phase === 'no_set'
        ? 'No set in progress: start a set first.'
        : phase === 'set_over'
          ? `Set ${ctx!.number} is over: start the next set.`
          : 'The match is over.';
    addIssue(e.id, 'error', msg);
    return false;
  }
  function betweenRallies(e: MatchEvent, what: string): boolean {
    if (rally.actions.length > 0) {
      addIssue(e.id, 'error', `${what} only between rallies. Finish the rally first (e.g. "o" or "oe").`);
      return false;
    }
    return true;
  }
  function onCourt(id: string): boolean {
    return zones().includes(id);
  }

  for (const e of events) {
    switch (e.type) {
      case 'set_start': {
        if (phase === 'rally') {
          addIssue(e.id, 'error', `Set ${ctx!.number} is still in progress.`);
          break;
        }
        if ((phase as Phase) === 'match_over') {
          addIssue(e.id, 'error', 'The match is over.');
          break;
        }
        const ids = e.lineup;
        if (ids.length !== 6 || new Set(ids).size !== 6 || ids.some((id) => !byId.has(id))) {
          addIssue(e.id, 'error', 'The lineup needs 6 different players.');
          break;
        }
        const liberos = e.liberos.filter((id) => byId.has(id) && !ids.includes(id));
        if (liberos.length > rules.maxLiberos && tracking.ruleChecks) {
          addIssue(e.id, 'warning', `More than ${rules.maxLiberos} liberos.`);
        }
        const setterSlot = e.setterId ? ids.indexOf(e.setterId) : -1;
        ctx = {
          number: sets.length + 1,
          slots: [...ids],
          rotations: 0,
          setterSlot: setterSlot >= 0 ? setterSlot : 0,
          hasSetter: setterSlot >= 0,
          liberos,
          liberoSlots: e.liberoReplaces.map((id) => ids.indexOf(id)).filter((i) => i >= 0),
          partner: new Map(),
          locked: new Set(),
        };
        sets.push({ set: ctx.number, us: 0, them: 0, winner: null, timeouts: 0, subs: 0 });
        us = 0;
        them = 0;
        weServe = e.weServe;
        liberoAuto = true;
        activeLibero = liberos[0] ?? null;
        timeouts = 0;
        subs = 0;
        phase = 'rally';
        checkParity(e.id);
        rally = emptyRally();
        break;
      }

      case 'action': {
        if (!needRally(e)) break;
        const mode = tracking.skills[e.skill];
        const name = SKILL_NAMES[e.skill];
        if (mode === 'off') {
          addIssue(e.id, 'error', `${name} is not tracked in this match. Change the tracking to record it.`);
          break;
        }
        const outcome = e.outcome;
        if (e.skill !== 'R' && (outcome === null || !VALID_OUTCOMES[e.skill].includes(outcome))) {
          addIssue(e.id, 'error', `${name}: outcome must be one of ${VALID_OUTCOMES[e.skill].join(' ')}.`);
          break;
        }
        if (e.skill === 'E' && outcome === '+' && !tracking.recordEverySet) {
          addIssue(e.id, 'error', 'Normal sets are not recorded. Only set errors (E=), unless "record every set" is on.');
          break;
        }
        if (mode === 'errors' && !isErrorAction(e.skill, outcome, e.rating)) {
          addIssue(e.id, 'error', `Only ${name.toLowerCase()} errors are tracked in this match.`);
          break;
        }
        let playerId = e.playerId;
        if (e.skill === 'S') {
          if (!weServe) {
            addIssue(e.id, 'error', 'The opponent is serving.');
            break;
          }
          if (rally.actions.length > 0) {
            addIssue(e.id, 'error', 'The serve must be the first action of the rally.');
            break;
          }
          const server = serverId();
          if (playerId === null) playerId = server;
          else if (playerId !== server) {
            addIssue(e.id, 'error', `${label(server)} is serving (zone 1), not ${label(playerId)}. Fix the lineup or rotation if that is wrong.`);
            break;
          }
        }
        if (e.skill === 'R') {
          if (weServe) {
            addIssue(e.id, 'error', 'We are serving: receptions only happen on the opponent\'s serve.');
            break;
          }
          if (rally.actions.length > 0) {
            addIssue(e.id, 'error', 'Only the first contact on the opponent\'s serve is a reception. Use D for digs and F= for free-ball errors.');
            break;
          }
        }
        if (playerId !== null && !onCourt(playerId)) {
          addIssue(e.id, 'error', `${label(playerId)} is not on court.`);
          break;
        }
        const isLibero = playerId !== null && ctx!.liberos.includes(playerId);
        if (isLibero && (e.skill === 'S' || e.skill === 'B')) {
          addIssue(e.id, 'error', `The libero cannot ${e.skill === 'S' ? 'serve' : 'block'}.`);
          break;
        }
        const coBlockers = (e.coBlockers ?? []).filter((id) => id !== playerId);
        const badCo = coBlockers.find((id) => !onCourt(id) || ctx!.liberos.includes(id));
        if (badCo) {
          addIssue(e.id, 'error', `${label(badCo)} cannot be in the block.`);
          break;
        }
        const rating = e.skill === 'R' ? (outcome === '=' ? 0 : e.rating) : null;
        rally.actions.push({
          eventId: e.id,
          playerId,
          skill: e.skill,
          outcome: e.skill === 'R' ? (rating === 0 ? '=' : '+') : outcome,
          rating,
          coBlockers,
        });
        const w = terminalWinner(e.skill, outcome, rating);
        if (w) closeRally(w, 'action', e.id);
        break;
      }

      case 'our_point':
      case 'opp_point':
      case 'opp_error':
      case 'penalty': {
        if (!needRally(e)) break;
        closeRally(e.type === 'opp_error' || e.type === 'our_point' ? 'us' : 'them', e.type, e.id);
        break;
      }

      case 'timeout': {
        if (!needRally(e)) break;
        timeouts++;
        sets[sets.length - 1].timeouts = timeouts;
        if (timeouts > rules.timeoutsPerSet && tracking.ruleChecks) {
          addIssue(e.id, 'warning', `More than ${rules.timeoutsPerSet} timeouts in this set.`);
        }
        break;
      }

      case 'sub': {
        if (!needRally(e) || !betweenRallies(e, 'Substitutions')) break;
        const c = ctx!;
        const slot = c.slots.indexOf(e.outId);
        if (slot < 0) {
          addIssue(e.id, 'error', `${label(e.outId)} is not on court.`);
          break;
        }
        if (!byId.has(e.inId) || c.slots.includes(e.inId)) {
          addIssue(e.id, 'error', `${label(e.inId)} is already on court.`);
          break;
        }
        if (c.liberos.includes(e.inId)) {
          addIssue(e.id, 'error', `${label(e.inId)} is a libero in this set; use "lib" for libero swaps.`);
          break;
        }
        const out = e.outId;
        const inn = e.inId;
        if (tracking.ruleChecks) {
          const gi = byId.get(inn)?.gender;
          const go = byId.get(out)?.gender;
          if (rules.genderParity && gi && go && gi !== go) {
            addIssue(e.id, 'warning', 'Substitutions must be man for man, woman for woman.');
          }
          if (!e.injury) {
            if (subs + 1 > rules.subsPerSet) {
              addIssue(e.id, 'warning', `More than ${rules.subsPerSet} substitutions in this set.`);
            }
            if (c.locked.has(inn) || c.locked.has(out)) {
              addIssue(e.id, 'warning', `${label(c.locked.has(inn) ? inn : out)} cannot be substituted again in this set.`);
            } else if (c.partner.has(inn) && c.partner.get(inn) !== out) {
              addIssue(e.id, 'warning', `${label(inn)} can only come back on for ${label(c.partner.get(inn)!)}.`);
            } else if (c.partner.has(out) && c.partner.get(out) !== inn) {
              addIssue(e.id, 'warning', `${label(out)} can only be replaced by ${label(c.partner.get(out)!)}.`);
            }
          }
        }
        if (!e.injury) {
          subs++;
          if (c.partner.get(out) === inn) {
            c.locked.add(out);
            c.locked.add(inn);
          } else if (!c.partner.has(out) && !c.partner.has(inn)) {
            c.partner.set(out, inn);
            c.partner.set(inn, out);
          }
        }
        sets[sets.length - 1].subs = subs;
        c.slots[slot] = inn;
        checkParity(e.id);
        rally = emptyRally();
        break;
      }

      case 'libero': {
        if (!needRally(e) || !betweenRallies(e, 'Libero swaps')) break;
        if (e.liberoId === null) {
          if (!activeLibero) {
            addIssue(e.id, 'error', 'No libero in this set.');
            break;
          }
          liberoAuto = !liberoAuto;
        } else {
          if (!ctx!.liberos.includes(e.liberoId)) {
            addIssue(e.id, 'error', `${label(e.liberoId)} is not a libero in this set.`);
            break;
          }
          activeLibero = e.liberoId;
          liberoAuto = true;
        }
        rally = emptyRally();
        break;
      }

      case 'tracking': {
        tracking = e.tracking;
        if (rally.actions.length === 0) rally = emptyRally();
        break;
      }

      case 'correction': {
        if (!needRally(e) || !betweenRallies(e, 'Corrections')) break;
        us = e.us;
        them = e.them;
        weServe = e.weServe;
        ctx!.rotations += e.rotations;
        checkSetEnd();
        rally = emptyRally();
        break;
      }
    }
  }

  const lib = ctx ? liberoOn() : null;
  const state: LiveState = {
    phase,
    set: ctx?.number ?? 0,
    us,
    them,
    setsUs,
    setsThem,
    weServe,
    rotation: rotation(),
    slotZones: slotZones(),
    zones: ctx ? zones() : [],
    serverId: serverId(),
    liberos: ctx?.liberos ?? [],
    activeLibero,
    liberoAuto,
    liberoOn: lib,
    setterId: ctx && ctx.hasSetter ? ctx.slots[ctx.setterSlot] : null,
    timeouts,
    subs,
    tracking,
    rally,
  };
  return { rallies, sets, issues, state };
}

/** League points for a finished match, or null if it is not over. */
export function leaguePoints(rules: RulesPreset, setsUs: number, setsThem: number): number | null {
  const need = setsToWin(rules);
  if (setsUs < need && setsThem < need) return null;
  return rules.leaguePoints[`${setsUs}-${setsThem}`] ?? null;
}

