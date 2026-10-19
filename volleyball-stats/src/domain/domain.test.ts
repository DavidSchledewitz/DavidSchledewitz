import { describe, expect, it } from 'vitest';
import { replay, setWinner } from './engine';
import { parseLine, eventToCode } from './parser';
import { AMAVOLLEY, FIVB, trackingForProfile } from './presets';
import { boxScore, hittingPct, passAvg, rotationRows } from './stats';
import type { EventBody, Match, MatchEvent, Player } from './types';

// Roster: setter #1 (F), OH #2 (M), MB #3 (M), OPP #4 (F), OH #5 (F), MB #6 (M),
// libero #7 (F), bench #8 (M), #9 (F).
const players: Player[] = [
  [1, 'F', 'S'],
  [2, 'M', 'OH'],
  [3, 'M', 'MB'],
  [4, 'F', 'OPP'],
  [5, 'F', 'OH'],
  [6, 'M', 'MB'],
  [7, 'F', 'L'],
  [8, 'M', 'OH'],
  [9, 'F', 'OH'],
].map(([n, g, pos]) => ({
  id: `p${n}`,
  name: `Player ${n}`,
  number: n as number,
  gender: g as 'F' | 'M',
  positions: [pos as 'S'],
  active: true,
}));

const match: Pick<Match, 'rules' | 'tracking'> = { rules: AMAVOLLEY, tracking: trackingForProfile('full') };

const setStart = (weServe: boolean, extra: Partial<Extract<EventBody, { type: 'set_start' }>> = {}): EventBody => ({
  type: 'set_start',
  set: 1,
  // zones 1..6: setter in zone 1 → rotation 1
  lineup: ['p1', 'p2', 'p3', 'p4', 'p5', 'p6'],
  setterId: 'p1',
  liberos: ['p7'],
  liberoReplaces: ['p3', 'p6'],
  weServe,
  ...extra,
});

function build(bodies: EventBody[]): MatchEvent[] {
  return bodies.map((b, i) => ({ ...b, id: `e${i}`, matchId: 'm', order: i, createdAt: 0 }) as MatchEvent);
}
function codes(...lines: string[]): EventBody[] {
  return lines.flatMap((l) => {
    const r = parseLine(l, players);
    if (r.error) throw new Error(r.error);
    return r.events;
  });
}
function run(bodies: EventBody[], m = match) {
  return replay(m, players, build(bodies));
}
const errorsOf = (res: ReturnType<typeof run>) =>
  Object.values(res.issues).flat().filter((i) => i.severity === 'error');
const warningsOf = (res: ReturnType<typeof run>) =>
  Object.values(res.issues).flat().filter((i) => i.severity === 'warning');

describe('parser', () => {
  it('reads actions, ratings and commands', () => {
    const r = parseLine('1S+ 4R2 2A# p o oe rc to lib sub 8>2 inj 9>5 A/ R', players);
    expect(r.error).toBeNull();
    expect(r.events.map((e) => eventToCode(e, players))).toEqual([
      '1S+', '4R2', '2A#', 'p', 'o', 'oe', 'rc', 'to', 'lib', 'sub 8>2', 'inj 9>5', 'A/', 'R',
    ]);
  });
  it('merges adjacent block points into a shared block', () => {
    const r = parseLine('3B# 6B#', players);
    expect(r.events).toHaveLength(1);
    expect(r.events[0]).toMatchObject({ skill: 'B', playerId: 'p3', coBlockers: ['p6'] });
  });
  it('rejects unknown players and missing outcomes', () => {
    expect(parseLine('42A#', players).error).toMatch(/#42/);
    expect(parseLine('2A', players).error).toMatch(/outcome/);
    expect(parseLine('sub 8', players).error).toMatch(/IN>OUT/);
  });
});

describe('set end with AmaVolley caps', () => {
  it('needs 21 with a 2-point lead, capped at 23', () => {
    expect(setWinner(21, 19, 1, AMAVOLLEY)).toBe('us');
    expect(setWinner(21, 20, 1, AMAVOLLEY)).toBeNull();
    expect(setWinner(22, 21, 1, AMAVOLLEY)).toBeNull();
    expect(setWinner(23, 22, 1, AMAVOLLEY)).toBe('us');
    expect(setWinner(21, 23, 1, AMAVOLLEY)).toBe('them');
  });
  it('plays the 5th set to 13, capped at 16', () => {
    expect(setWinner(13, 11, 5, AMAVOLLEY)).toBe('us');
    expect(setWinner(15, 14, 5, AMAVOLLEY)).toBeNull();
    expect(setWinner(16, 15, 5, AMAVOLLEY)).toBe('us');
  });
  it('has no cap in FIVB', () => {
    expect(setWinner(26, 25, 1, FIVB)).toBeNull();
    expect(setWinner(27, 25, 1, FIVB)).toBe('us');
  });
  it('ends sets and the match during replay', () => {
    const bodies: EventBody[] = [];
    for (let s = 0; s < 3; s++) {
      bodies.push(setStart(true));
      for (let i = 0; i < 21; i++) bodies.push({ type: 'opp_error' });
    }
    const res = run(bodies);
    expect(res.state.phase).toBe('match_over');
    expect(res.state.setsUs).toBe(3);
    expect(res.sets.map((s) => `${s.us}-${s.them}`)).toEqual(['21-0', '21-0', '21-0']);
    const extra = run([...bodies, { type: 'opp_error' }]);
    expect(errorsOf(extra)[0].message).toMatch(/over/);
  });
});

describe('serve, sideout and rotation', () => {
  it('rotates only when we win the serve back', () => {
    const res = run([setStart(false), ...codes('4R3 2A#')]);
    // Sideout: we rotate, player from zone 2 (#2) now serves.
    expect(res.state.us).toBe(1);
    expect(res.state.weServe).toBe(true);
    expect(res.state.serverId).toBe('p2');
    // Setter moved from zone 1 to zone 6.
    expect(res.state.rotation).toBe(6);
    const res2 = run([setStart(false), ...codes('4R3 2A#', '2S+ o')]);
    expect(res2.state.them).toBe(1);
    expect(res2.state.weServe).toBe(false);
    expect(res2.state.rotation).toBe(6);
  });
  it('uses the server for a bare S and rejects the wrong server', () => {
    const res = run([setStart(true), ...codes('S#')]);
    expect(res.rallies[0].actions[0].playerId).toBe('p1');
    expect(errorsOf(run([setStart(true), ...codes('2S#')]))[0].message).toMatch(/#1 is serving/);
  });
  it('validates reception context', () => {
    expect(errorsOf(run([setStart(true), ...codes('4R2')]))[0].message).toMatch(/We are serving/);
    expect(errorsOf(run([setStart(false), ...codes('4R2 5R1')]))[0].message).toMatch(/first contact/);
  });
});

describe('libero', () => {
  it('replaces the middle in the back row automatically, but not when serving', () => {
    // Receiving: #3 is in zone 3 (front), #6 in zone 6 (back) → libero for #6.
    const res = run([setStart(false)]);
    expect(res.state.liberoOn).toMatchObject({ liberoId: 'p7', replacedId: 'p6', zone: 6 });
    expect(res.state.zones[5]).toBe('p7');
    // A libero cannot serve or block.
    const bodies = [setStart(false, { lineup: ['p3', 'p1', 'p2', 'p6', 'p4', 'p5'], setterId: 'p1' })];
    const r2 = run(bodies);
    // #3 in zone 1 while receiving → libero in for #3.
    expect(r2.state.liberoOn?.replacedId).toBe('p3');
    const r3 = run([...bodies, ...codes('7R3 2A#')]);
    // After the sideout #3 has rotated to zone 6 (still back row), #1 serves.
    expect(r3.state.serverId).toBe('p1');
    expect(errorsOf(run([...bodies, ...codes('7B#')]))[0].message).toMatch(/libero cannot block/);
  });
  it('can be switched off with lib', () => {
    const res = run([setStart(false), ...codes('lib')]);
    expect(res.state.liberoOn).toBeNull();
  });
  it('does not count towards gender parity', () => {
    // Libero #7 (F) replaces #6 (M): parity still counts #6.
    const res = run([setStart(false)]);
    expect(warningsOf(res)).toHaveLength(0);
  });
});

describe('substitutions', () => {
  it('warns about gender, re-entry and the limit', () => {
    const cross = run([setStart(true), ...codes('sub 9>2')]);
    expect(warningsOf(cross).map((w) => w.message).join(' ')).toMatch(/man for man/);
    expect(warningsOf(cross).map((w) => w.message).join(' ')).toMatch(/4 women and 2 men/);

    const ok = run([setStart(true), ...codes('sub 8>2', 'sub 2>8')]);
    expect(warningsOf(ok)).toHaveLength(0);
    const reentry = run([setStart(true), ...codes('sub 8>2', 'sub 2>8', 'sub 8>2')]);
    expect(warningsOf(reentry)[0].message).toMatch(/cannot be substituted again/);

    const many: string[] = [];
    for (let i = 0; i < 9; i++) many.push(i % 2 ? 'sub 2>8' : 'sub 8>2');
    const limit = run([setStart(true), ...codes(...many)], {
      ...match,
      tracking: { ...match.tracking, ruleChecks: true },
    });
    expect(warningsOf(limit).some((w) => /More than 8/.test(w.message))).toBe(true);
  });
  it('skips rule warnings when rule checks are off', () => {
    const res = run([setStart(true), ...codes('sub 9>2')], {
      ...match,
      tracking: { ...match.tracking, ruleChecks: false },
    });
    expect(warningsOf(res)).toHaveLength(0);
  });
  it('rejects subs mid-rally and players already on court', () => {
    expect(errorsOf(run([setStart(true), ...codes('S+ sub 8>2')]))[0].message).toMatch(/between rallies/);
    expect(errorsOf(run([setStart(true), ...codes('sub 3>2')]))[0].message).toMatch(/already on court/);
  });
});

describe('score-only tracking', () => {
  it('gives sideout and break-point stats from p and o alone', () => {
    const scoreOnly = { ...match, tracking: trackingForProfile('score') };
    // They serve: we side out (p), serve and lose (o), side out again (p), then break (p).
    const res = run([setStart(false), ...codes('p o p p')], scoreOnly);
    expect(errorsOf(res)).toEqual([]);
    expect(res.state.us).toBe(3);
    const box = boxScore(res.rallies);
    expect(box.ourPoints).toBe(3);
    const rows = rotationRows(res.rallies);
    const receive = rows.reduce((n, r) => n + r.receive, 0);
    const sideouts = rows.reduce((n, r) => n + r.sideouts, 0);
    expect([receive, sideouts]).toEqual([2, 2]);
    const breaks = rows.reduce((n, r) => n + r.breaks, 0);
    expect(breaks).toBe(1);
  });
});

describe('tracking profiles', () => {
  it('rejects untracked skills and non-errors in errors mode', () => {
    const scoreOnly = { ...match, tracking: trackingForProfile('score') };
    expect(errorsOf(run([setStart(true), ...codes('S+')], scoreOnly))[0].message).toMatch(/not tracked/);
    const basic = { ...match, tracking: trackingForProfile('basic') };
    expect(errorsOf(run([setStart(true), ...codes('S+ 3D+')], basic))[0].message).toMatch(/Only dig errors/);
    expect(errorsOf(run([setStart(true), ...codes('S+ 3D=')], basic))).toHaveLength(0);
  });
  it('only allows set errors unless every set is recorded', () => {
    expect(errorsOf(run([setStart(true), ...codes('S+ 1E+')]))[0].message).toMatch(/Only set errors/);
  });
});

describe('stats', () => {
  const bodies = [
    setStart(false),
    ...codes(
      '4R3 2A#', // sideout in R1, kill → assist to setter #1
      '2S+ 3D+ 5A/', // we lose, attack blocked
      '5R1 2A=', // poor pass, attack error
      'R0', // team reception error (aced)
      'oe', // sideout: #3 rotates to zone 1 and serves; #2 and #6 are front row
      '3S+ 2B# 6B#', // shared block point
    ),
  ];
  it('computes the box score', () => {
    const res = run(bodies);
    expect(errorsOf(res)).toEqual([]);
    expect(res.rallies.at(-1)!.actions[1]).toMatchObject({ playerId: 'p2', coBlockers: ['p6'] });
    const box = boxScore(res.rallies);
    const p2 = box.players.get('p2')!;
    expect(p2.kills).toBe(1);
    expect(p2.attErr).toBe(1);
    expect(hittingPct(p2)).toBe(0);
    expect(box.players.get('p1')!.assists).toBe(1);
    expect(passAvg(box.team)).toBeCloseTo((3 + 1 + 0) / 3);
    expect(box.pointsWon + box.pointsLost).toBe(res.rallies.length);
    expect(box.pointsWon).toBe(res.state.us);
  });
  it('computes rotation rows with sideout by pass', () => {
    const res = run(bodies);
    const rows = rotationRows(res.rallies);
    const r1 = rows[0];
    expect(r1.receive).toBe(1);
    expect(r1.sideouts).toBe(1);
    expect(r1.fbso).toBe(1);
    expect(r1.byPass[3]).toEqual([1, 1]);
  });
});
