// Local-first storage in IndexedDB. Every event is written as soon as it is entered.

import Dexie, { type Table } from 'dexie';
import type { EventBody, Match, MatchEvent, Player, Season } from '../domain/types';

export interface Setting {
  key: string;
  value: unknown;
}

class StatsDB extends Dexie {
  players!: Table<Player, string>;
  seasons!: Table<Season, string>;
  matches!: Table<Match, string>;
  events!: Table<MatchEvent, string>;
  settings!: Table<Setting, string>;

  constructor() {
    super('volleyball-stats');
    this.version(1).stores({
      players: 'id, number',
      seasons: 'id, createdAt',
      matches: 'id, seasonId, date',
      events: 'id, matchId, [matchId+order]',
      settings: 'key',
    });
  }
}

export const db = new StatsDB();

export const newId = () => crypto.randomUUID();

export async function getSetting<T>(key: string, fallback: T): Promise<T> {
  const s = await db.settings.get(key);
  return s === undefined ? fallback : (s.value as T);
}

export function setSetting(key: string, value: unknown) {
  return db.settings.put({ key, value });
}

export function matchEvents(matchId: string): Promise<MatchEvent[]> {
  return db.events.where('[matchId+order]').between([matchId, Dexie.minKey], [matchId, Dexie.maxKey]).toArray();
}

/** Build events with ids and orders, appended after `afterOrder` (or between it and `beforeOrder`). */
export function makeEvents(matchId: string, bodies: EventBody[], afterOrder: number, beforeOrder?: number): MatchEvent[] {
  const step = beforeOrder === undefined ? 1 : (beforeOrder - afterOrder) / (bodies.length + 1);
  const now = Date.now();
  return bodies.map((b, i) => ({ ...b, id: newId(), matchId, order: afterOrder + step * (i + 1), createdAt: now }) as MatchEvent);
}

export async function appendEvents(matchId: string, bodies: EventBody[]): Promise<MatchEvent[]> {
  return db.transaction('rw', db.events, async () => {
    const last = await db.events.where('[matchId+order]').between([matchId, Dexie.minKey], [matchId, Dexie.maxKey]).last();
    const events = makeEvents(matchId, bodies, last?.order ?? 0);
    await db.events.bulkAdd(events);
    return events;
  });
}

/** Insert events right before `before` (used when editing after the match). */
export async function insertEventsBefore(before: MatchEvent, bodies: EventBody[]): Promise<void> {
  await db.transaction('rw', db.events, async () => {
    const prev = await db.events
      .where('[matchId+order]')
      .between([before.matchId, Dexie.minKey], [before.matchId, before.order])
      .last();
    await db.events.bulkAdd(makeEvents(before.matchId, bodies, prev?.order ?? before.order - 1, before.order));
  });
}

export async function deleteMatch(matchId: string) {
  await db.transaction('rw', db.matches, db.events, async () => {
    await db.events.where('matchId').equals(matchId).delete();
    await db.matches.delete(matchId);
  });
}

// ---- Backup -------------------------------------------------------------------

export interface Backup {
  app: 'volleyball-stats';
  version: 1;
  exportedAt: string;
  players: Player[];
  seasons: Season[];
  matches: Match[];
  events: MatchEvent[];
  settings: Setting[];
}

export async function exportBackup(): Promise<Backup> {
  const [players, seasons, matches, events, settings] = await Promise.all([
    db.players.toArray(),
    db.seasons.toArray(),
    db.matches.toArray(),
    db.events.toArray(),
    db.settings.toArray(),
  ]);
  return {
    app: 'volleyball-stats',
    version: 1,
    exportedAt: new Date().toISOString(),
    players,
    seasons,
    matches,
    events,
    settings: settings.filter((s) => s.key !== 'lastBackupAt'),
  };
}

export function parseBackup(text: string): Backup {
  const data = JSON.parse(text);
  if (data?.app !== 'volleyball-stats' || data.version !== 1) throw new Error('This is not a Volleyball Stats backup file.');
  for (const k of ['players', 'seasons', 'matches', 'events', 'settings']) {
    if (!Array.isArray(data[k])) throw new Error(`Backup is missing "${k}".`);
  }
  return data as Backup;
}

/** Replace everything with the backup's contents. */
export async function restoreBackup(b: Backup) {
  await db.transaction('rw', [db.players, db.seasons, db.matches, db.events, db.settings], async () => {
    await Promise.all([db.players.clear(), db.seasons.clear(), db.matches.clear(), db.events.clear(), db.settings.clear()]);
    await db.players.bulkAdd(b.players);
    await db.seasons.bulkAdd(b.seasons);
    await db.matches.bulkAdd(b.matches);
    await db.events.bulkAdd(b.events);
    await db.settings.bulkAdd(b.settings);
  });
}
