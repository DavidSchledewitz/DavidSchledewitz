import { useEffect, useMemo, useRef, useState } from 'react';
import { db, makeEvents } from '../../db/db';
import { replay, type Issue, type LiveState, type Rally } from '../../domain/engine';
import { describeEvent, eventToCode, parseLine } from '../../domain/parser';
import { PROFILE_LABELS, SKILL_NAMES, trackingForProfile } from '../../domain/presets';
import { SKILLS, type EventBody, type MatchEvent, type Player, type Skill, type Tracking, type TrackingProfile } from '../../domain/types';
import { CodeHelp } from '../components/CodeHelp';
import { Court } from '../components/Court';
import { Modal } from '../components/Modal';
import { SetStartDialog } from '../components/SetStartDialog';
import { useMatch, useSetting, type MatchData } from '../hooks';
import { TrackingGrid } from './MatchSetupPage';

type Dialog = null | 'set' | 'sub' | 'fix' | 'tracking';

export function LivePage({ matchId }: { matchId: string }) {
  const data = useMatch(matchId);
  if (data === null) return <main className="page empty">Match not found.</main>;
  if (!data) return null;
  if (data.match.forfeit) {
    return (
      <main className="page">
        <div className="banner info">
          This match is recorded as a forfeit, so there is nothing to enter. <a href={`#/match/${matchId}/report`}>See the report</a>.
        </div>
      </main>
    );
  }
  return <Live data={data} />;
}

function Live({ data }: { data: MatchData }) {
  const { match, players, events, summary } = data;
  const state = summary.replay.state;
  const teamName = useSetting('teamName', 'My team');
  const [text, setText] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [dialog, setDialog] = useState<Dialog>(null);
  const [picked, setPicked] = useState<string | null>(null);
  const [skill, setSkill] = useState<Skill | null>(null);
  const [showHelp, setShowHelp] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const byId = useMemo(() => new Map(players.map((p) => [p.id, p])), [players]);
  const locked = match.final;

  const lastSetStart = [...events].reverse().find((e): e is Extract<MatchEvent, { type: 'set_start' }> => e.type === 'set_start') ?? null;

  // Open the lineup dialog when a set needs starting.
  useEffect(() => {
    if (!locked && (state.phase === 'no_set') && players.length >= 6) setDialog('set');
  }, [state.phase, locked, players.length]);

  const focus = () => setTimeout(() => inputRef.current?.focus(), 0);

  /** Validate by replaying with the new events; write them only if none is rejected. */
  async function commit(bodies: EventBody[]): Promise<boolean> {
    if (locked || bodies.length === 0) return false;
    const temp = makeEvents(match.id, bodies, events.at(-1)?.order ?? 0);
    const res = replay(match, players, [...events, ...temp]);
    for (const e of temp) {
      const err = (res.issues[e.id] ?? []).find((i) => i.severity === 'error');
      if (err) {
        setError(bodies.length > 1 ? `${eventToCode(e, players)}: ${err.message}` : err.message);
        return false;
      }
    }
    await db.events.bulkAdd(temp);
    setError(null);
    setWarnings(temp.flatMap((e) => (res.issues[e.id] ?? []).map((i) => i.message)));
    return true;
  }

  async function submitText() {
    if (!text.trim()) return;
    const parsed = parseLine(text, players);
    if (parsed.error) {
      setError(parsed.error);
      return;
    }
    if (await commit(parsed.events)) setText('');
  }

  async function undo() {
    const last = events.at(-1);
    if (!last || locked) return;
    await db.events.delete(last.id);
    setError(null);
    setWarnings([`Undid: ${describeEvent(last, players)}`]);
  }

  async function quick(code: string) {
    setPicked(null);
    setSkill(null);
    const parsed = parseLine(code, players);
    if (parsed.error) setError(parsed.error);
    else await commit(parsed.events);
    focus();
  }

  const pickedPlayer = picked ? byId.get(picked) : undefined;
  const codePrefix = `${pickedPlayer ? pickedPlayer.number : ''}${skill ?? ''}`;

  return (
    <main className="page wide">
      <Scoreboard data={data} teamName={teamName} onTracking={() => setDialog('tracking')} />

      {locked && (
        <div className="banner info">
          This match is marked final and locked. Unlock it on the <a href={`#/match/${match.id}/edit`}>edit page</a> to change it.
        </div>
      )}
      <PhaseBanner data={data} onStartSet={() => setDialog('set')} />

      <div className="live">
        <div>
          {state.phase === 'no_set' ? (
            <div className="card empty">No set started yet.</div>
          ) : (
            <Court
              state={state}
              players={players}
              picked={picked}
              onPick={(id) => {
                setPicked(id === picked ? null : id);
                focus();
              }}
            />
          )}
          <div className="row" style={{ marginTop: 12 }}>
            <button onClick={() => setDialog('sub')} disabled={locked || state.phase !== 'rally'}>
              Substitution…
            </button>
            <button onClick={() => quick('lib')} disabled={locked || state.phase !== 'rally' || state.liberos.length === 0} title="Turn the automatic libero on/off">
              Libero {state.liberoAuto ? 'on' : 'off'}
            </button>
            {state.liberos.length > 1 &&
              state.liberos.map((id) => (
                <button key={id} className={`small ${state.activeLibero === id ? 'selected' : ''}`} onClick={() => quick(`lib ${byId.get(id)?.number}`)} disabled={locked}>
                  L #{byId.get(id)?.number}
                </button>
              ))}
            <button onClick={() => setDialog('fix')} disabled={locked || state.phase !== 'rally'}>
              Fix score/rotation…
            </button>
          </div>
          {state.liberoOn && (
            <p className="subtle small">
              Libero #{byId.get(state.liberoOn.liberoId)?.number} is in for #{byId.get(state.liberoOn.replacedId)?.number}.
            </p>
          )}
        </div>

        <div className="card entry">
          <input
            ref={inputRef}
            className={`code${error ? ' invalid' : ''}`}
            value={text}
            disabled={locked}
            autoFocus
            spellCheck={false}
            autoComplete="off"
            placeholder={state.weServe ? 'e.g. S+ 3D+ 7A#' : 'e.g. 7R2 3A#'}
            aria-label="Code entry"
            onChange={(e) => {
              setText(e.target.value);
              setError(null);
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                submitText();
              } else if (e.key === 'Escape') {
                setText('');
                setError(null);
              } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z' && !text) {
                e.preventDefault();
                undo();
              }
            }}
          />
          <div style={{ minHeight: 24, marginTop: 6 }}>
            {error ? (
              <span className="text-bad">{error}</span>
            ) : warnings.length ? (
              warnings.map((w, i) => (
                <div key={i} className="text-warn small">
                  {w}
                </div>
              ))
            ) : (
              <span className="subtle small">
                <kbd>Enter</kbd> to record · <kbd>Ctrl</kbd>+<kbd>Z</kbd> on an empty line to undo
              </span>
            )}
          </div>

          <Pad
            state={state}
            players={players}
            picked={picked}
            skill={skill}
            disabled={locked || state.phase !== 'rally'}
            onPick={(id) => {
              setPicked(id);
              setSkill(null);
            }}
            onSkill={setSkill}
            onOutcome={(sym) => quick(`${codePrefix}${sym}`)}
          />

          <div className="pad-section">
            <div className="label">Rally</div>
            <div className="pad">
              <button className="win" disabled={locked || state.phase !== 'rally'} onClick={() => quick('p')}>
                Our point <span className="k">p</span>
              </button>
              <button className="lose" disabled={locked || state.phase !== 'rally'} onClick={() => quick('o')}>
                Opponent point <span className="k">o</span>
              </button>
              <button className="win" disabled={locked || state.phase !== 'rally'} onClick={() => quick('oe')}>
                Opponent error <span className="k">oe</span>
              </button>
              <button disabled={locked || state.phase !== 'rally'} onClick={() => quick('to')}>
                Timeout <span className="k">to</span>
              </button>
              <button disabled={locked || state.phase !== 'rally'} onClick={() => quick('rc')}>
                Red card <span className="k">rc</span>
              </button>
              <button disabled={locked || events.length === 0} onClick={undo}>
                Undo
              </button>
            </div>
          </div>

          <div className="pad-section">
            <button className="ghost small" onClick={() => setShowHelp(!showHelp)}>
              {showHelp ? 'Hide' : 'Show'} keyboard codes
            </button>
            {showHelp && <CodeHelp compact />}
          </div>
        </div>

        <RallyLog data={data} />
      </div>

      {dialog === 'set' && (
        <SetStartDialog
          setNumber={state.phase === 'no_set' ? 1 : state.set + 1}
          players={players}
          rules={match.rules}
          previous={lastSetStart}
          onClose={() => {
            setDialog(null);
            focus();
          }}
          onStart={async (body) => {
            if (await commit([body])) setDialog(null);
            focus();
          }}
        />
      )}
      {dialog === 'sub' && (
        <SubDialog
          state={state}
          players={players}
          onClose={() => {
            setDialog(null);
            focus();
          }}
          onSub={async (body) => {
            if (await commit([body])) setDialog(null);
            focus();
          }}
        />
      )}
      {dialog === 'fix' && (
        <FixDialog
          state={state}
          onClose={() => {
            setDialog(null);
            focus();
          }}
          onFix={async (body) => {
            if (await commit([body])) setDialog(null);
            focus();
          }}
        />
      )}
      {dialog === 'tracking' && (
        <TrackingDialog
          tracking={state.tracking}
          onClose={() => {
            setDialog(null);
            focus();
          }}
          onSave={async (tracking) => {
            if (await commit([{ type: 'tracking', tracking }])) setDialog(null);
            focus();
          }}
        />
      )}
    </main>
  );
}

function Scoreboard({ data, teamName, onTracking }: { data: MatchData; teamName: string; onTracking: () => void }) {
  const { match, summary } = data;
  const s = summary.replay.state;
  const inSet = s.phase !== 'no_set';
  return (
    <div className="scoreboard">
      <div className="score">
        <div className="team">
          <div className="name">{teamName}</div>
          <div className="pts">
            {s.us}
            {inSet && s.weServe && s.phase === 'rally' && <span className="serve-dot" title="Serving" />}
          </div>
          <div className="sets">sets {s.setsUs}</div>
        </div>
        <div className="dash">–</div>
        <div className="team">
          <div className="name">{match.opponent || 'Opponent'}</div>
          <div className="pts">
            {s.them}
            {inSet && !s.weServe && s.phase === 'rally' && <span className="serve-dot" title="Serving" />}
          </div>
          <div className="sets">sets {s.setsThem}</div>
        </div>
      </div>
      <div className="meta-chips">
        {inSet && (
          <>
            <span className="chip">
              Set <strong>{s.set}</strong>
            </span>
            <span className="chip">
              Rotation <strong>R{s.rotation}</strong>
            </span>
            <span className="chip">
              Timeouts <strong>{s.timeouts}/{match.rules.timeoutsPerSet}</strong>
            </span>
            <span className="chip">
              Subs <strong>{s.subs}/{match.rules.subsPerSet}</strong>
            </span>
          </>
        )}
        <button className="small" onClick={onTracking} title="What is being recorded">
          Tracking: {PROFILE_LABELS[s.tracking.profile]}
        </button>
        <a className="btn small" href={`#/match/${match.id}/report`}>
          Report
        </a>
        <a className="btn small" href={`#/match/${match.id}/edit`}>
          Edit
        </a>
      </div>
    </div>
  );
}

function PhaseBanner({ data, onStartSet }: { data: MatchData; onStartSet: () => void }) {
  const { summary, players, match } = data;
  const s = summary.replay.state;
  if (players.filter((p) => p.active).length < 6) {
    return (
      <div className="banner warn">
        You need at least 6 active players. <a href="#/team">Add players</a>.
      </div>
    );
  }
  if (s.phase === 'no_set') {
    return (
      <div className="banner info row spread">
        <span>Enter the starting lineup to begin.</span>
        <button className="primary" onClick={onStartSet} disabled={match.final}>
          Start set 1
        </button>
      </div>
    );
  }
  const last = summary.replay.sets.at(-1);
  if (s.phase === 'set_over' && last) {
    return (
      <div className={`banner ${last.winner === 'us' ? 'good' : 'warn'} row spread`}>
        <span>
          Set {last.set} {last.winner === 'us' ? 'won' : 'lost'} {last.us}–{last.them}. Sets {s.setsUs}–{s.setsThem}. Made a mistake? Undo.
        </span>
        <button className="primary" onClick={onStartSet} disabled={match.final}>
          Start set {s.set + 1}
        </button>
      </div>
    );
  }
  if (s.phase === 'match_over') {
    const won = s.setsUs > s.setsThem;
    return (
      <div className={`banner ${won ? 'good' : 'warn'} row spread`}>
        <span>
          Match {won ? 'won' : 'lost'} {s.setsUs}–{s.setsThem}
          {summary.leaguePoints !== null && ` · ${summary.leaguePoints} league point${summary.leaguePoints === 1 ? '' : 's'}`}.
        </span>
        <a className="btn primary" href={`#/match/${match.id}/report`}>
          See the report
        </a>
      </div>
    );
  }
  return null;
}

const OUTCOMES: Record<Skill, { sym: string; label: string; kind?: 'win' | 'lose' }[]> = {
  S: [
    { sym: '#', label: 'Ace', kind: 'win' },
    { sym: '+', label: 'In' },
    { sym: '=', label: 'Error', kind: 'lose' },
  ],
  R: [
    { sym: '3', label: '3 perfect' },
    { sym: '2', label: '2 good' },
    { sym: '1', label: '1 poor' },
    { sym: '0', label: '0 error', kind: 'lose' },
    { sym: '', label: 'Unrated' },
  ],
  A: [
    { sym: '#', label: 'Kill', kind: 'win' },
    { sym: '+', label: 'In play' },
    { sym: '/', label: 'Blocked', kind: 'lose' },
    { sym: '=', label: 'Error', kind: 'lose' },
  ],
  B: [
    { sym: '#', label: 'Block point', kind: 'win' },
    { sym: '+', label: 'Touch' },
    { sym: '=', label: 'Error', kind: 'lose' },
  ],
  D: [
    { sym: '+', label: 'Dig' },
    { sym: '=', label: 'Error', kind: 'lose' },
  ],
  E: [
    { sym: '+', label: 'Set' },
    { sym: '=', label: 'Set error', kind: 'lose' },
  ],
  F: [{ sym: '=', label: 'Free-ball error', kind: 'lose' }],
};

function Pad({
  state,
  players,
  picked,
  skill,
  disabled,
  onPick,
  onSkill,
  onOutcome,
}: {
  state: LiveState;
  players: Player[];
  picked: string | null;
  skill: Skill | null;
  disabled: boolean;
  onPick: (id: string | null) => void;
  onSkill: (s: Skill) => void;
  onOutcome: (sym: string) => void;
}) {
  const byId = new Map(players.map((p) => [p.id, p]));
  const court = [...state.zones].filter(Boolean).sort((a, b) => (byId.get(a)?.number ?? 0) - (byId.get(b)?.number ?? 0));
  const tracked = SKILLS.filter((s) => state.tracking.skills[s] !== 'off');
  const outcomes = skill
    ? OUTCOMES[skill].filter((o) => {
        const mode = state.tracking.skills[skill];
        const isError = o.kind === 'lose';
        if (mode === 'errors' && !isError) return false;
        if (skill === 'E' && o.sym === '+' && !state.tracking.recordEverySet) return false;
        return true;
      })
    : [];
  if (tracked.length === 0) {
    return <p className="subtle small pad-section">Score-only tracking: use the rally buttons below, or change the tracking.</p>;
  }
  return (
    <>
      <div className="pad-section">
        <div className="label">1 · Player (or click the court)</div>
        <div className="pad">
          {court.map((id) => (
            <button key={id} disabled={disabled} className={picked === id ? 'selected' : ''} onClick={() => onPick(picked === id ? null : id)}>
              #{byId.get(id)?.number}
              {state.liberos.includes(id) && <span className="k">L</span>}
            </button>
          ))}
          <button disabled={disabled} className={picked === null ? 'selected' : ''} onClick={() => onPick(null)} title="No player credited (or the server, for a serve)">
            Team
          </button>
        </div>
      </div>
      <div className="pad-section">
        <div className="label">2 · Skill</div>
        <div className="pad">
          {tracked.map((s) => (
            <button key={s} disabled={disabled} className={skill === s ? 'selected' : ''} onClick={() => onSkill(s)}>
              {SKILL_NAMES[s]} <span className="k">{s}</span>
            </button>
          ))}
        </div>
      </div>
      <div className="pad-section">
        <div className="label">3 · Outcome {skill ? <code>{`${picked ? byId.get(picked)?.number : ''}${skill}…`}</code> : ''}</div>
        <div className="pad">
          {skill ? (
            outcomes.map((o) => (
              <button key={o.label} disabled={disabled} className={o.kind ?? ''} onClick={() => onOutcome(o.sym)}>
                {o.label} <span className="k">{o.sym || '∅'}</span>
              </button>
            ))
          ) : (
            <span className="subtle small">Pick a skill first.</span>
          )}
        </div>
      </div>
    </>
  );
}

function RallyLog({ data }: { data: MatchData }) {
  const { events, players, summary } = data;
  const { rallies, issues, state } = summary.replay;
  const startIndex = events.map((e) => e.type).lastIndexOf('set_start');
  const setEvents = startIndex < 0 ? [] : events.slice(startIndex);
  const endOf = new Map<string, Rally>(rallies.map((r) => [r.endEventId ?? '', r]));

  return (
    <div className="card log" aria-label="Rally log">
      <h3>Set {state.set || 1} log</h3>
      {setEvents.length === 0 && <p className="subtle small">Nothing recorded yet.</p>}
      {[...setEvents].reverse().map((e) => {
        const rally = endOf.get(e.id);
        const evIssues: Issue[] = issues[e.id] ?? [];
        const rejected = evIssues.some((i) => i.severity === 'error');
        return (
          <div key={e.id} className={rally ? 'rally' : undefined}>
            {rally && (
              <div className="rally-head">
                <span>
                  Rally {rally.index} · R{rally.rotation} · {rally.weServed ? 'we served' : 'they served'}
                </span>
                <strong className={rally.winner === 'us' ? 'text-good' : 'text-bad'}>
                  {rally.winner === 'us' ? rally.scoreBefore.us + 1 : rally.scoreBefore.us}–
                  {rally.winner === 'them' ? rally.scoreBefore.them + 1 : rally.scoreBefore.them}
                </strong>
              </div>
            )}
            <div className="ev" style={rejected ? { textDecoration: 'line-through' } : undefined}>
              <code>{eventToCode(e, players)}</code>
              <span>{describeEvent(e, players)}</span>
            </div>
            {evIssues.map((i, k) => (
              <div key={k} className={`small ${i.severity === 'error' ? 'text-bad' : 'text-warn'}`}>
                {i.message}
              </div>
            ))}
          </div>
        );
      })}
    </div>
  );
}

function SubDialog({
  state,
  players,
  onSub,
  onClose,
}: {
  state: LiveState;
  players: Player[];
  onSub: (body: EventBody) => void;
  onClose: () => void;
}) {
  const [outId, setOutId] = useState('');
  const [inId, setInId] = useState('');
  const [injury, setInjury] = useState(false);
  const byId = new Map(players.map((p) => [p.id, p]));
  const onCourt = state.slotZones;
  const out = byId.get(outId);
  const bench = players.filter((p) => p.active && !onCourt.includes(p.id) && !state.liberos.includes(p.id));
  const sameGenderFirst = [...bench].sort((a, b) => Number(b.gender === out?.gender) - Number(a.gender === out?.gender) || a.number - b.number);
  const label = (p: Player) => `#${p.number} ${p.name}${p.gender ? ` (${p.gender === 'F' ? 'W' : 'M'})` : ''}`;
  return (
    <Modal title="Substitution" onClose={onClose}>
      <div className="form-grid">
        <label className="field">
          Out
          <select value={outId} onChange={(e) => setOutId(e.target.value)} autoFocus>
            <option value="">Choose…</option>
            {onCourt.map((id) => byId.get(id)).filter(Boolean).map((p) => (
              <option key={p!.id} value={p!.id}>
                {label(p!)}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          In
          <select value={inId} onChange={(e) => setInId(e.target.value)}>
            <option value="">Choose…</option>
            {sameGenderFirst.map((p) => (
              <option key={p.id} value={p.id}>
                {label(p)}
              </option>
            ))}
          </select>
        </label>
      </div>
      <label className="check" style={{ marginTop: 12 }}>
        <input type="checkbox" checked={injury} onChange={(e) => setInjury(e.target.checked)} /> Injury substitution (not counted towards the limit)
      </label>
      <p className="subtle small" style={{ marginTop: 8 }}>
        Code: <code>{`${injury ? 'inj' : 'sub'} ${byId.get(inId)?.number ?? 'IN'}>${out?.number ?? 'OUT'}`}</code>
      </p>
      <div className="modal-actions">
        <button onClick={onClose}>Cancel</button>
        <button className="primary" disabled={!outId || !inId} onClick={() => onSub({ type: 'sub', inId, outId, injury })}>
          Substitute
        </button>
      </div>
    </Modal>
  );
}

function FixDialog({ state, onFix, onClose }: { state: LiveState; onFix: (body: EventBody) => void; onClose: () => void }) {
  const [us, setUs] = useState(state.us);
  const [them, setThem] = useState(state.them);
  const [weServe, setWeServe] = useState(state.weServe);
  const [rotations, setRotations] = useState(0);
  // Rotating clockwise moves the setter's zone down by one (R1 → R6 → R5 …).
  const newRotation = ((((state.rotation - 1 - rotations) % 6) + 6) % 6) + 1;
  return (
    <Modal title="Fix score or rotation" onClose={onClose}>
      <p className="subtle small">Use this when the app is out of sync with the scoreboard. The fix is logged and can be edited later.</p>
      <div className="form-grid">
        <label className="field">
          Our score
          <input type="number" min={0} value={us} onChange={(e) => setUs(Number(e.target.value))} />
        </label>
        <label className="field">
          Their score
          <input type="number" min={0} value={them} onChange={(e) => setThem(Number(e.target.value))} />
        </label>
        <label className="field">
          Serving
          <select value={weServe ? 'us' : 'them'} onChange={(e) => setWeServe(e.target.value === 'us')}>
            <option value="us">We serve</option>
            <option value="them">They serve</option>
          </select>
        </label>
        <label className="field">
          Rotate our team
          <select value={rotations} onChange={(e) => setRotations(Number(e.target.value))}>
            {[0, 1, 2, 3, 4, 5].map((n) => (
              <option key={n} value={n}>
                {n === 0 ? 'No change' : `${n} position${n > 1 ? 's' : ''} → R${((((state.rotation - 1 - n) % 6) + 6) % 6) + 1}`}
              </option>
            ))}
          </select>
        </label>
      </div>
      <p className="small" style={{ marginTop: 8 }}>
        Result: {us}–{them}, {weServe ? 'we serve' : 'they serve'}, rotation R{newRotation}.
      </p>
      <div className="modal-actions">
        <button onClick={onClose}>Cancel</button>
        <button className="primary" onClick={() => onFix({ type: 'correction', us, them, weServe, rotations })}>
          Apply fix
        </button>
      </div>
    </Modal>
  );
}

function TrackingDialog({ tracking, onSave, onClose }: { tracking: Tracking; onSave: (t: Tracking) => void; onClose: () => void }) {
  const [t, setT] = useState(tracking);
  return (
    <Modal title="What to record from now on" onClose={onClose} wide>
      <div className="row" style={{ marginBottom: 12 }}>
        {(['score', 'basic', 'full', 'custom'] as TrackingProfile[]).map((p) => (
          <button key={p} className={t.profile === p ? 'selected' : ''} onClick={() => setT(trackingForProfile(p, t))}>
            {PROFILE_LABELS[p]}
          </button>
        ))}
      </div>
      <TrackingGrid tracking={t} onChange={setT} />
      <div className="row" style={{ marginTop: 12, gap: 20 }}>
        <label className="check">
          <input type="checkbox" checked={t.recordEverySet} onChange={(e) => setT({ ...t, recordEverySet: e.target.checked })} />
          Record every set (E+)
        </label>
        <label className="check">
          <input type="checkbox" checked={t.ruleChecks} onChange={(e) => setT({ ...t, ruleChecks: e.target.checked })} />
          Rule warnings
        </label>
      </div>
      <p className="subtle small" style={{ marginTop: 8 }}>
        Applies from the next rally. Reports show what was tracked and where.
      </p>
      <div className="modal-actions">
        <button onClick={onClose}>Cancel</button>
        <button className="primary" disabled={JSON.stringify(t) === JSON.stringify(tracking)} onClick={() => onSave(t)}>
          Apply
        </button>
      </div>
    </Modal>
  );
}
