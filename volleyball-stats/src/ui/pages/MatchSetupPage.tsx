import { useLiveQuery } from 'dexie-react-hooks';
import { useEffect, useState } from 'react';
import { db, deleteMatch, newId, setSetting } from '../../db/db';
import { AMAVOLLEY, PRESETS, PROFILE_LABELS, SKILL_NAMES, trackingForProfile } from '../../domain/presets';
import { SKILLS, type Match, type RulesPreset, type SkillMode, type Tracking, type TrackingProfile } from '../../domain/types';
import { navigate } from '../router';
import { useSetting } from '../hooks';

const today = () => new Date().toISOString().slice(0, 10);

function seasonNameFor(date: string): string {
  const [y, m] = date.split('-').map(Number);
  const start = m >= 8 ? y : y - 1;
  return `${start}/${String((start + 1) % 100).padStart(2, '0')}`;
}

function blankMatch(seasonId: string): Match {
  return {
    id: newId(),
    seasonId,
    date: today(),
    opponent: '',
    home: true,
    competition: 'Campionato',
    rules: { ...AMAVOLLEY },
    tracking: trackingForProfile('full'),
    countsInSeason: true,
    final: false,
    forfeit: null,
    excludedSets: [],
    createdAt: Date.now(),
  };
}

export function MatchSetupPage({ matchId }: { matchId: string | null }) {
  const existing = useLiveQuery(async () => (matchId ? ((await db.matches.get(matchId)) ?? null) : null), [matchId]);
  const seasons = useLiveQuery(() => db.seasons.orderBy('createdAt').reverse().toArray(), []);
  const eventCount = useLiveQuery(() => (matchId ? db.events.where('matchId').equals(matchId).count() : 0), [matchId]);
  const activeSeasonId = useSetting<string | null>('activeSeasonId', null);
  const [m, setM] = useState<Match | null>(null);

  useEffect(() => {
    if (m || existing === undefined || !seasons) return;
    if (existing) setM(existing);
    else if (!matchId) setM(blankMatch(activeSeasonId ?? seasons[0]?.id ?? ''));
  }, [existing, seasons, activeSeasonId, matchId, m]);

  if (matchId && existing === null) return <main className="page empty">Match not found.</main>;
  if (!m || !seasons) return null;

  const set = (patch: Partial<Match>) => setM({ ...m, ...patch });
  const setRules = (patch: Partial<RulesPreset>) => set({ rules: { ...m.rules, ...patch } });
  const setTracking = (patch: Partial<Tracking>) => set({ tracking: { ...m.tracking, ...patch } });
  const numField = (label: string, key: keyof RulesPreset, nullable = false) => (
    <label className="field">
      {label}
      <input
        type="number"
        min={0}
        value={(m.rules[key] as number | null) ?? ''}
        placeholder={nullable ? 'none' : undefined}
        onChange={(e) => {
          const v = e.target.value === '' ? null : Number(e.target.value);
          if (v === null && !nullable) return;
          setRules({ [key]: v } as Partial<RulesPreset>);
        }}
      />
    </label>
  );

  const save = async () => {
    let seasonId = m.seasonId;
    if (!seasons.some((s) => s.id === seasonId)) {
      seasonId = newId();
      await db.seasons.add({ id: seasonId, name: seasonNameFor(m.date), createdAt: Date.now() });
    }
    await db.matches.put({ ...m, seasonId, opponent: m.opponent.trim() });
    await setSetting('activeSeasonId', seasonId);
    navigate(m.forfeit ? `/match/${m.id}/report` : `/match/${m.id}/live`);
  };

  const remove = async () => {
    if (!confirm('Delete this match and everything recorded in it? This cannot be undone.')) return;
    await deleteMatch(m.id);
    navigate('/');
  };

  const presetName = PRESETS.find((p) => JSON.stringify(p) === JSON.stringify(m.rules))?.name ?? 'custom';

  return (
    <main className="page">
      <div className="page-head">
        <h1>{matchId ? 'Match setup' : 'New match'}</h1>
        <div className="row">
          {matchId && (
            <button className="danger" onClick={remove}>
              Delete match
            </button>
          )}
          <button className="primary" onClick={save}>
            {matchId ? 'Save' : 'Create match'}
          </button>
        </div>
      </div>

      <section className="card">
        <h2>Match</h2>
        <div className="form-grid">
          <label className="field">
            Opponent
            <input value={m.opponent} autoFocus={!matchId} onChange={(e) => set({ opponent: e.target.value })} placeholder="Team name" />
          </label>
          <label className="field">
            Date
            <input type="date" value={m.date} onChange={(e) => set({ date: e.target.value })} />
          </label>
          <label className="field">
            Venue
            <select value={m.home ? 'home' : 'away'} onChange={(e) => set({ home: e.target.value === 'home' })}>
              <option value="home">Home</option>
              <option value="away">Away</option>
            </select>
          </label>
          <label className="field">
            Competition
            <input value={m.competition} onChange={(e) => set({ competition: e.target.value })} list="competitions" />
            <datalist id="competitions">
              <option value="Campionato" />
              <option value="Play-off" />
              <option value="Torneo" />
              <option value="Amichevole" />
            </datalist>
          </label>
          <label className="field">
            Season
            <select value={seasons.some((s) => s.id === m.seasonId) ? m.seasonId : ''} onChange={(e) => set({ seasonId: e.target.value })}>
              {!seasons.some((s) => s.id === m.seasonId) && <option value="">New season {seasonNameFor(m.date)}</option>}
              {seasons.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </select>
          </label>
        </div>
        <div className="row" style={{ marginTop: 12, gap: 20 }}>
          <label className="check">
            <input type="checkbox" checked={m.countsInSeason} onChange={(e) => set({ countsInSeason: e.target.checked })} />
            Official match (counts in season stats). Untick for friendlies and practice.
          </label>
          <label className="check">
            Forfeit:
            <select value={m.forfeit ?? ''} onChange={(e) => set({ forfeit: (e.target.value || null) as Match['forfeit'] })}>
              <option value="">No</option>
              <option value="them">Opponent did not show (we win 3–0)</option>
              <option value="us">We did not show (we lose 0–3)</option>
            </select>
          </label>
        </div>
      </section>

      <section className="card">
        <div className="row spread">
          <h2>What to record</h2>
          {!!eventCount && (
            <span className="subtle small">
              Changes here apply from the start of the match. To change from now on, use "Tracking" on the live screen.
            </span>
          )}
        </div>
        <div className="row" style={{ marginBottom: 12 }}>
          {(['score', 'basic', 'full', 'custom'] as TrackingProfile[]).map((p) => (
            <button key={p} className={m.tracking.profile === p ? 'selected' : ''} onClick={() => set({ tracking: trackingForProfile(p, m.tracking) })}>
              {PROFILE_LABELS[p]}
            </button>
          ))}
        </div>
        <TrackingGrid tracking={m.tracking} onChange={(t) => set({ tracking: t })} />
        <div className="row" style={{ marginTop: 12, gap: 20 }}>
          <label className="check">
            <input type="checkbox" checked={m.tracking.recordEverySet} onChange={(e) => setTracking({ recordEverySet: e.target.checked })} />
            Record every set (E+)
          </label>
          <label className="check">
            <input type="checkbox" checked={m.tracking.inferAssists} onChange={(e) => setTracking({ inferAssists: e.target.checked })} />
            Credit assists to the setter on court
          </label>
          <label className="check">
            <input type="checkbox" checked={m.tracking.ruleChecks} onChange={(e) => setTracking({ ruleChecks: e.target.checked })} />
            Rule warnings (gender, substitutions, libero, timeouts)
          </label>
        </div>
      </section>

      <section className="card">
        <div className="row spread">
          <h2>Rules</h2>
          <label className="check">
            Preset:
            <select
              value={presetName}
              onChange={(e) => {
                const p = PRESETS.find((x) => x.name === e.target.value);
                if (p) set({ rules: { ...p, leaguePoints: { ...p.leaguePoints } } });
              }}
            >
              {PRESETS.map((p) => (
                <option key={p.name}>{p.name}</option>
              ))}
              {presetName === 'custom' && <option value="custom">Custom</option>}
            </select>
          </label>
        </div>
        <div className="form-grid">
          {numField('Set to', 'setPoints')}
          {numField('Set cap', 'setCap', true)}
          {numField('Tie-break to', 'tiebreakPoints')}
          {numField('Tie-break cap', 'tiebreakCap', true)}
          {numField('Win by', 'winBy')}
          {numField('Best of (sets)', 'bestOf')}
          {numField('Timeouts per set', 'timeoutsPerSet')}
          {numField('Substitutions per set', 'subsPerSet')}
          {numField('Max liberos', 'maxLiberos')}
          <label className="field">
            Gender rule
            <select
              value={m.rules.genderParity ? 'on' : 'off'}
              onChange={(e) => setRules({ genderParity: e.target.value === 'on' ? { F: 3, M: 3 } : null })}
            >
              <option value="on">Women + men on court</option>
              <option value="off">None</option>
            </select>
          </label>
          {m.rules.genderParity && (
            <>
              <label className="field">
                Women on court
                <input
                  type="number"
                  min={0}
                  max={6}
                  value={m.rules.genderParity.F}
                  onChange={(e) => setRules({ genderParity: { F: Number(e.target.value), M: 6 - Number(e.target.value) } })}
                />
              </label>
              <label className="field">
                Men on court
                <input type="number" value={m.rules.genderParity.M} disabled />
              </label>
            </>
          )}
        </div>
        <h3 style={{ marginTop: 16 }}>League points</h3>
        <div className="row">
          {Object.keys(m.rules.leaguePoints).map((k) => (
            <label key={k} className="field" style={{ width: 70 }}>
              {k}
              <input
                type="number"
                value={m.rules.leaguePoints[k]}
                onChange={(e) => setRules({ leaguePoints: { ...m.rules.leaguePoints, [k]: Number(e.target.value) } })}
              />
            </label>
          ))}
        </div>
      </section>
    </main>
  );
}

const MODE_LABELS: Record<SkillMode, string> = { off: 'Off', errors: 'Errors only', all: 'Everything' };

export function TrackingGrid({ tracking, onChange }: { tracking: Tracking; onChange: (t: Tracking) => void }) {
  return (
    <div className="table-wrap">
      <table className="data" style={{ width: 'auto' }}>
        <tbody>
          {SKILLS.map((s) => {
            const modes: SkillMode[] = s === 'E' || s === 'F' ? ['off', 'errors'] : ['off', 'errors', 'all'];
            return (
              <tr key={s}>
                <td className="left">
                  <strong>{SKILL_NAMES[s]}</strong> <code className="subtle">{s}</code>
                </td>
                <td className="left">
                  <div className="row" style={{ gap: 4 }}>
                    {modes.map((mode) => (
                      <button
                        key={mode}
                        className={`small ${tracking.skills[s] === mode || (mode === 'errors' && tracking.skills[s] === 'all' && modes.length === 2) ? 'selected' : ''}`}
                        onClick={() => onChange({ ...tracking, profile: 'custom', skills: { ...tracking.skills, [s]: mode } })}
                      >
                        {MODE_LABELS[mode]}
                      </button>
                    ))}
                  </div>
                </td>
                <td className="left subtle small">
                  {s === 'R' && 'Everything = with 0–3 rating (rating can still be skipped)'}
                  {s === 'E' && 'Only set errors; normal sets via "record every set"'}
                  {s === 'F' && 'Free / down balls are never receptions'}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
