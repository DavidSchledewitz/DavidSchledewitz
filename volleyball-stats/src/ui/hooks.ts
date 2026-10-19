import { useLiveQuery } from 'dexie-react-hooks';
import { useMemo } from 'react';
import { db, matchEvents } from '../db/db';
import { summarize, type MatchSummary } from '../domain/summary';
import type { Match, MatchEvent, Player } from '../domain/types';
import { byNumber } from './format';

export function usePlayers(): Player[] | undefined {
  return useLiveQuery(async () => (await db.players.toArray()).sort(byNumber), []);
}

export function useSetting<T>(key: string, fallback: T): T {
  const v = useLiveQuery(async () => (await db.settings.get(key))?.value, [key]);
  return v === undefined ? fallback : (v as T);
}

export interface MatchData {
  match: Match;
  players: Player[];
  events: MatchEvent[];
  summary: MatchSummary;
}

/** A match with its events and replayed summary; undefined while loading, null if missing. */
export function useMatch(matchId: string): MatchData | null | undefined {
  const match = useLiveQuery(async () => (await db.matches.get(matchId)) ?? null, [matchId]);
  const events = useLiveQuery(() => matchEvents(matchId), [matchId]);
  const players = usePlayers();
  return useMemo(() => {
    if (match === null) return null;
    if (!match || !events || !players) return undefined;
    return { match, players, events, summary: summarize(match, players, events) };
  }, [match, events, players]);
}

/** Every match with its summary, newest first. */
export function useAllMatches(): MatchSummary[] | undefined {
  const matches = useLiveQuery(() => db.matches.toArray(), []);
  const events = useLiveQuery(() => db.events.toArray(), []);
  const players = usePlayers();
  return useMemo(() => {
    if (!matches || !events || !players) return undefined;
    const byMatch = new Map<string, MatchEvent[]>();
    for (const e of events) {
      const list = byMatch.get(e.matchId) ?? [];
      list.push(e);
      byMatch.set(e.matchId, list);
    }
    return matches
      .map((m) => summarize(m, players, (byMatch.get(m.id) ?? []).sort((a, b) => a.order - b.order)))
      .sort((a, b) => b.match.date.localeCompare(a.match.date) || b.match.createdAt - a.match.createdAt);
  }, [matches, events, players]);
}
