import { leaguePoints, replay, setsToWin, type Rally, type ReplayResult } from './engine';
import type { Match, MatchEvent, Player } from './types';

export interface MatchSummary {
  match: Match;
  replay: ReplayResult;
  setsUs: number;
  setsThem: number;
  /** Set scores from our point of view, e.g. ["21-18", "19-21"]. */
  setScores: string[];
  finished: boolean;
  leaguePoints: number | null;
  /** Rallies that count for stats: forfeits and excluded sets are left out. */
  statRallies: Rally[];
}

export function summarize(match: Match, players: Player[], events: MatchEvent[]): MatchSummary {
  const r = replay(match, players, events);
  if (match.forfeit) {
    const win = match.forfeit === 'them'; // the opponent forfeited
    const need = setsToWin(match.rules);
    const score = win ? `${match.rules.setPoints}-0` : `0-${match.rules.setPoints}`;
    return {
      match,
      replay: r,
      setsUs: win ? need : 0,
      setsThem: win ? 0 : need,
      setScores: Array(need).fill(score),
      finished: true,
      leaguePoints: win ? leaguePoints(match.rules, need, 0) : leaguePoints(match.rules, 0, need),
      statRallies: [],
    };
  }
  const finished = r.state.phase === 'match_over';
  return {
    match,
    replay: r,
    setsUs: r.state.setsUs,
    setsThem: r.state.setsThem,
    setScores: r.sets.map((s) => (s.winner ? `${s.us}-${s.them}` : `${r.state.us}-${r.state.them}`)),
    finished,
    leaguePoints: finished ? leaguePoints(match.rules, r.state.setsUs, r.state.setsThem) : null,
    statRallies: r.rallies.filter((x) => !match.excludedSets.includes(x.set)),
  };
}
