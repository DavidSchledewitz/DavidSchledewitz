import Dexie from 'dexie';
import { useMemo, useState } from 'react';
import { appendEvents, db, insertEventsBefore, makeEvents } from '../../db/db';
import type { Rally } from '../../domain/engine';
import { describeEvent, eventToCode, parseLine } from '../../domain/parser';
import { SKILL_NAMES } from '../../domain/presets';
import { SKILLS, type EventBody, type MatchEvent, type Skill } from '../../domain/types';
import { SetStartDialog } from '../components/SetStartDialog';
import { useMatch, type MatchData } from '../hooks';
import { formatDate } from '../format';

type SetStart = Extract<EventBody, { type: 'set_start' }>;

export function EditPage({ matchId }: { matchId: string }) {
  const data = useMatch(matchId);
  if (data === null) return <main className="page empty">Match not found.</main>;
  if (!data) return null;
  return <Editor data={data} />;
}

/** Replace one event with one or more parsed events, keeping its place in the log. */
async function replaceEvent(e: MatchEvent, bodies: EventBody[]) {
  await db.transaction('rw', db.events, async () => {
    await db.events.put({ ...bodies[0], id: e.id, matchId: e.matchId, order: e.order, createdAt: e.createdAt } as MatchEvent);
    if (bodies.length > 1) {
      const next = await db.events
        .where('[matchId+order]')
        .between([e.matchId, e.order], [e.matchId, Dexie.maxKey], false, true)
        .first();
      await db.events.bulkAdd(makeEvents(e.matchId, bodies.slice(1), e.order, next?.order ?? e.order + 1));
    }
  });
}

function Editor({ data }: { data: MatchData }) {
  const { match, players, events, summary } = data;
  const { issues, rallies } = summary.replay;
  const [playerFilter, setPlayerFilter] = useState('');
  const [skillFilter, setSkillFilter] = useState<Skill | ''>('');
  const [editing, setEditing] = useState<string | null>(null);
  const [inserting, setInserting] = useState<string | null>(null);
  const [draft, setDraft] = useState('');
  const [draftError, setDraftError] = useState<string | null>(null);
  const [appendText, setAppendText] = useState('');
  const [setDialog, setSetDialog] = useState<MatchEvent | null>(null);
  const locked = match.final;

  // Which set each event belongs to, and the rally it ends.
  const setOf = useMemo(() => {
    const map = new Map<string, number>();
    let set = 0;
    for (const e of events) {
      if (e.type === 'set_start') set++;
      map.set(e.id, set);
    }
    return map;
  }, [events]);
  const endOf = useMemo(() => new Map<string, Rally>(rallies.map((r) => [r.endEventId ?? '', r])), [rallies]);
  const setCount = summary.replay.sets.length;
  const rejected = events.filter((e) => issues[e.id]?.some((i) => i.severity === 'error')).length;

  const matches = (e: MatchEvent) => {
    if (playerFilter) {
      const ids =
        e.type === 'action'
          ? [e.playerId, ...(e.coBlockers ?? [])]
          : e.type === 'sub'
            ? [e.inId, e.outId]
            : e.type === 'libero'
              ? [e.liberoId]
              : [];
      if (!ids.includes(playerFilter)) return false;
    }
    if (skillFilter && !(e.type === 'action' && e.skill === skillFilter)) return false;
    return true;
  };

  const parseDraft = (): EventBody[] | null => {
    const r = parseLine(draft, players);
    if (r.error || r.events.length === 0) {
      setDraftError(r.error ?? 'Type a code.');
      return null;
    }
    return r.events;
  };
  const startEdit = (e: MatchEvent) => {
    if (e.type === 'set_start') {
      setSetDialog(e);
      return;
    }
    setEditing(e.id);
    setInserting(null);
    setDraft(eventToCode(e, players));
    setDraftError(null);
  };
  const saveEdit = async (e: MatchEvent) => {
    const bodies = parseDraft();
    if (!bodies) return;
    await replaceEvent(e, bodies);
    setEditing(null);
  };
  const saveInsert = async (e: MatchEvent) => {
    const bodies = parseDraft();
    if (!bodies) return;
    await insertEventsBefore(e, bodies);
    setInserting(null);
  };
  const remove = async (e: MatchEvent) => {
    if (e.type === 'set_start' && !confirm('Delete the start of this set? Everything in the set will no longer count until a set start is added again.')) return;
    await db.events.delete(e.id);
  };
  const append = async () => {
    const r = parseLine(appendText, players);
    if (r.error) {
      setDraftError(r.error);
      return;
    }
    await appendEvents(match.id, r.events);
    setAppendText('');
  };
  const toggleSet = (n: number) =>
    db.matches.update(match.id, {
      excludedSets: match.excludedSets.includes(n) ? match.excludedSets.filter((x) => x !== n) : [...match.excludedSets, n].sort(),
    });

  const editor = (onSave: () => void, label: string) => (
    <div className="row" style={{ margin: '4px 0 8px 60px' }}>
      <input
        className="mono"
        value={draft}
        autoFocus
        onChange={(ev) => {
          setDraft(ev.target.value);
          setDraftError(null);
        }}
        onKeyDown={(ev) => {
          if (ev.key === 'Enter') onSave();
          if (ev.key === 'Escape') {
            setEditing(null);
            setInserting(null);
          }
        }}
        placeholder="e.g. 7A# or o o o"
      />
      <button className="primary small" onClick={onSave}>
        {label}
      </button>
      <button
        className="small"
        onClick={() => {
          setEditing(null);
          setInserting(null);
        }}
      >
        Cancel
      </button>
      {draftError && <span className="text-bad small">{draftError}</span>}
    </div>
  );

  let lastSet = -1;
  return (
    <main className="page">
      <div className="page-head">
        <div>
          <h1>Edit match vs {match.opponent || 'Opponent'}</h1>
          <div className="subtle">
            {formatDate(match.date)} · {summary.setsUs}–{summary.setsThem} ({summary.setScores.join(', ') || 'no sets'})
          </div>
        </div>
        <div className="row">
          <a className="btn" href={`#/match/${match.id}/live`}>
            Live
          </a>
          <a className="btn" href={`#/match/${match.id}/report`}>
            Report
          </a>
          <button className={locked ? 'selected' : 'primary'} onClick={() => db.matches.update(match.id, { final: !locked })}>
            {locked ? 'Final (locked) · Unlock' : 'Mark as final'}
          </button>
        </div>
      </div>

      {locked && <div className="banner info">This match is final and locked against changes. Unlock it to edit.</div>}
      {rejected > 0 && (
        <div className="banner error">
          {rejected} {rejected === 1 ? 'entry is' : 'entries are'} not accepted by the rules (struck through below) and don't count. Fix or
          delete {rejected === 1 ? 'it' : 'them'}.
        </div>
      )}

      <section className="card">
        <div className="row spread">
          <div className="row">
            <select value={playerFilter} onChange={(e) => setPlayerFilter(e.target.value)} aria-label="Filter by player">
              <option value="">All players</option>
              {players.map((p) => (
                <option key={p.id} value={p.id}>
                  #{p.number} {p.name}
                </option>
              ))}
            </select>
            <select value={skillFilter} onChange={(e) => setSkillFilter(e.target.value as Skill | '')} aria-label="Filter by skill">
              <option value="">All skills</option>
              {SKILLS.map((s) => (
                <option key={s} value={s}>
                  {SKILL_NAMES[s]}
                </option>
              ))}
            </select>
          </div>
          {setCount > 0 && (
            <div className="row small">
              Count in stats:
              {Array.from({ length: setCount }, (_, i) => i + 1).map((n) => (
                <label key={n} className="check">
                  <input type="checkbox" checked={!match.excludedSets.includes(n)} onChange={() => toggleSet(n)} disabled={locked} />
                  Set {n}
                </label>
              ))}
            </div>
          )}
        </div>
      </section>

      <section className="card">
        {events.length === 0 && <p className="empty">Nothing recorded yet.</p>}
        {events.map((e) => {
          const set = setOf.get(e.id) ?? 0;
          const header =
            set !== lastSet ? (
              <h3 key={`h${set}`} style={{ marginTop: 16 }}>
                {set === 0 ? 'Before the first set' : `Set ${set}`}{' '}
                {match.excludedSets.includes(set) && <span className="badge warn">excluded from stats</span>}
              </h3>
            ) : null;
          lastSet = set;
          if (!matches(e)) return header;
          const rally = endOf.get(e.id);
          const evIssues = issues[e.id] ?? [];
          const isRejected = evIssues.some((i) => i.severity === 'error');
          const codeEditable = e.type !== 'tracking' && e.type !== 'correction';
          return (
            <div key={e.id}>
              {header}
              {inserting === e.id && editor(() => saveInsert(e), 'Insert')}
              <div className="row" style={{ borderBottom: '1px solid var(--border)', padding: '4px 0', gap: 12 }}>
                <code style={{ minWidth: 80, textDecoration: isRejected ? 'line-through' : undefined }}>{eventToCode(e, players)}</code>
                <span style={{ flex: 1, minWidth: 180 }}>
                  {describeEvent(e, players)}
                  {evIssues.map((i, k) => (
                    <span key={k} className={`small ${i.severity === 'error' ? 'text-bad' : 'text-warn'}`}>
                      {' '}
                      · {i.message}
                    </span>
                  ))}
                </span>
                {rally && (
                  <span className={`small ${rally.winner === 'us' ? 'text-good' : 'text-bad'}`}>
                    → {rally.winner === 'us' ? rally.scoreBefore.us + 1 : rally.scoreBefore.us}–
                    {rally.winner === 'them' ? rally.scoreBefore.them + 1 : rally.scoreBefore.them}
                  </span>
                )}
                {!locked && (
                  <span className="row" style={{ gap: 4 }}>
                    {codeEditable && (
                      <button className="small" onClick={() => startEdit(e)}>
                        Edit
                      </button>
                    )}
                    <button
                      className="small"
                      title="Insert entries before this one"
                      onClick={() => {
                        setInserting(e.id);
                        setEditing(null);
                        setDraft('');
                        setDraftError(null);
                      }}
                    >
                      Insert before
                    </button>
                    <button className="small danger" onClick={() => remove(e)}>
                      Delete
                    </button>
                  </span>
                )}
              </div>
              {editing === e.id && editor(() => saveEdit(e), 'Save')}
            </div>
          );
        })}

        {!locked && (
          <div className="row" style={{ marginTop: 16 }}>
            <input
              className="mono"
              value={appendText}
              onChange={(e) => setAppendText(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && append()}
              placeholder="Add at the end, e.g. o o o (three missed opponent points)"
              style={{ flex: 1 }}
            />
            <button onClick={append} disabled={!appendText.trim()}>
              Add
            </button>
          </div>
        )}
      </section>

      {setDialog && setDialog.type === 'set_start' && (
        <SetStartDialog
          setNumber={setOf.get(setDialog.id) ?? 1}
          players={players}
          rules={match.rules}
          previous={setDialog as SetStart}
          editing
          onClose={() => setSetDialog(null)}
          onStart={async (body) => {
            await replaceEvent(setDialog, [body]);
            setSetDialog(null);
          }}
        />
      )}
    </main>
  );
}
