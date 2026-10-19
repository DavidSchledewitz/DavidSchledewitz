import 'fake-indexeddb/auto';
import { beforeEach, describe, expect, it } from 'vitest';
import { appendEvents, db, exportBackup, insertEventsBefore, matchEvents, parseBackup, restoreBackup } from './db';
import { eventToCode } from '../domain/parser';

beforeEach(async () => {
  await Promise.all(db.tables.map((t) => t.clear()));
});

describe('event storage', () => {
  it('appends in order and inserts between events', async () => {
    await appendEvents('m1', [{ type: 'opp_point' }, { type: 'opp_error' }]);
    await appendEvents('m2', [{ type: 'timeout' }]);
    let events = await matchEvents('m1');
    await insertEventsBefore(events[1], [{ type: 'penalty' }, { type: 'timeout' }]);
    await insertEventsBefore(events[0], [{ type: 'timeout' }]);
    events = await matchEvents('m1');
    expect(events.map((e) => eventToCode(e, []))).toEqual(['to', 'o', 'rc', 'to', 'oe']);
    expect(await matchEvents('m2')).toHaveLength(1);
  });
});

describe('backup', () => {
  it('round-trips everything', async () => {
    await db.players.add({ id: 'p1', name: 'Giulia', number: 1, gender: 'F', positions: ['S'], active: true });
    await db.settings.put({ key: 'teamName', value: 'Volley Amici' });
    await appendEvents('m1', [{ type: 'opp_point' }]);
    const json = JSON.stringify(await exportBackup());
    await Promise.all(db.tables.map((t) => t.clear()));
    await restoreBackup(parseBackup(json));
    expect(await db.players.count()).toBe(1);
    expect((await db.settings.get('teamName'))?.value).toBe('Volley Amici');
    expect(await matchEvents('m1')).toHaveLength(1);
  });
  it('rejects files that are not backups', () => {
    expect(() => parseBackup('{"hello":1}')).toThrow(/not a Volleyball Stats backup/);
  });
});
