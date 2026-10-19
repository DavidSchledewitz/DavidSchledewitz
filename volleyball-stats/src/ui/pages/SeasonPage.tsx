import { useLiveQuery } from 'dexie-react-hooks';
import { useMemo, useState } from 'react';
import { db, setSetting } from '../../db/db';
import { SKILL_NAMES } from '../../domain/presets';
import { acePct, boxScore, hittingPct, killPct, passAvg, ratio, rotationRows, serveErrPct, sumRotationRows, type Line } from '../../domain/stats';
import { SKILLS, type Skill } from '../../domain/types';
import { BoxScoreTable } from '../components/BoxScoreTable';
import { LineChart, type ChartSeries } from '../components/LineChart';
import { RotationTable } from '../components/RotationTable';
import { avg, formatDate, hit } from '../format';
import { useAllMatches, usePlayers, useSetting } from '../hooks';
import { matchesTracking, mergeBoxScores, playerName } from '../reportUtils';
import './report.css';

interface Metric {
  key: string;
  label: string;
  skill: Skill;
  value: (l: Line) => number | null;
  attempts: (l: Line) => number;
  format: (v: number) => string;
  yMin?: number;
  yMax?: number;
}

const pctFmt = (v: number) => `${Math.round(v * 100)}%`;

const METRICS: Metric[] = [
  { key: 'hit', label: 'Hitting %', skill: 'A', value: hittingPct, attempts: (l) => l.attAtt, format: hit },
  { key: 'kill', label: 'Kill %', skill: 'A', value: killPct, attempts: (l) => l.attAtt, format: pctFmt, yMin: 0 },
  { key: 'pass', label: 'Pass average', skill: 'R', value: passAvg, attempts: (l) => l.recRated, format: (v) => avg(v, 1), yMin: 0, yMax: 3 },
  { key: 'ace', label: 'Ace %', skill: 'S', value: acePct, attempts: (l) => l.serveAtt, format: pctFmt, yMin: 0 },
  { key: 'serr', label: 'Serve error %', skill: 'S', value: serveErrPct, attempts: (l) => l.serveAtt, format: pctFmt, yMin: 0 },
];

const MAX_SERIES = 8;

export function SeasonPage() {
  const seasons = useLiveQuery(async () => (await db.seasons.toArray()).sort((a, b) => b.createdAt - a.createdAt), []);
  const activeSeasonId = useSetting<string | null>('activeSeasonId', null);
  const minAttempts = useSetting('minAttempts', 5);
  const all = useAllMatches();
  const players = usePlayers();

  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const [opponent, setOpponent] = useState('');
  const [venue, setVenue] = useState<'all' | 'home' | 'away'>('all');
  const [competition, setCompetition] = useState('');
  const [friendlies, setFriendlies] = useState(false);
  const [metricKey, setMetricKey] = useState('hit');
  // Player → colour slot. null = automatic (top players by attempts).
  const [picked, setPicked] = useState<Map<string, number> | null>(null);

  const seasonId = activeSeasonId ?? '';
  const inSeason = useMemo(() => (all ?? []).filter((s) => !seasonId || s.match.seasonId === seasonId), [all, seasonId]);
  const filtered = useMemo(
    () =>
      inSeason
        .filter((s) => {
          const m = s.match;
          if (!friendlies && !m.countsInSeason) return false;
          if (from && m.date < from) return false;
          if (to && m.date > to) return false;
          if (opponent && m.opponent !== opponent) return false;
          if (venue !== 'all' && m.home !== (venue === 'home')) return false;
          if (competition && m.competition !== competition) return false;
          return true;
        })
        .sort((a, b) => a.match.date.localeCompare(b.match.date) || a.match.createdAt - b.match.createdAt),
    [inSeason, friendlies, from, to, opponent, venue, competition],
  );

  const withStats = useMemo(() => filtered.filter((s) => !s.match.forfeit && s.statRallies.length > 0), [filtered]);
  const boxes = useMemo(() => withStats.map((s) => boxScore(s.statRallies)), [withStats]);
  const seasonBox = useMemo(() => mergeBoxScores(boxes), [boxes]);
  const tracked = useMemo(() => matchesTracking(withStats.map((s) => s.statRallies)), [withStats]);
  const rotations = useMemo(() => rotationRows(withStats.flatMap((s) => s.statRallies)), [withStats]);

  const metric = METRICS.find((m) => m.key === metricKey)!;
  const candidates = useMemo(() => {
    const totals = new Map<string, number>();
    for (const b of boxes) for (const [id, l] of b.players) totals.set(id, (totals.get(id) ?? 0) + metric.attempts(l));
    return [...totals.entries()].filter(([, n]) => n > 0).sort((a, b) => b[1] - a[1]);
  }, [boxes, metric]);
  const selection = useMemo(() => {
    if (picked) return new Map([...picked].filter(([id]) => candidates.some(([c]) => c === id)));
    return new Map(candidates.slice(0, 5).map(([id], i) => [id, i + 1]));
  }, [picked, candidates]);

  if (!all || !players || !seasons) return <main className="page">Loading…</main>;

  const opponents = [...new Set(inSeason.map((s) => s.match.opponent).filter(Boolean))].sort();
  const competitions = [...new Set(inSeason.map((s) => s.match.competition).filter(Boolean))].sort();
  const totalPoints = filtered.reduce((sum, s) => sum + (s.leaguePoints ?? 0), 0);
  const wins = filtered.filter((s) => s.finished && s.setsUs > s.setsThem).length;
  const losses = filtered.filter((s) => s.finished && s.setsUs < s.setsThem).length;

  const shortDate = (iso: string) => new Date(`${iso}T12:00:00`).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
  const xLabels = withStats.map((s) => shortDate(s.match.date));
  const xTitles = withStats.map((s) => `${formatDate(s.match.date)} vs ${s.match.opponent}`);

  const togglePlayer = (id: string) => {
    const next = new Map(selection);
    if (next.has(id)) next.delete(id);
    else {
      if (next.size >= MAX_SERIES) return;
      const used = new Set(next.values());
      let slot = 1;
      while (used.has(slot)) slot++;
      next.set(id, slot);
    }
    setPicked(next);
  };

  const playerSeries: ChartSeries[] = [...selection].map(([id, slot]) => {
    const points = withStats.map((_, i) => {
      const l = boxes[i].players.get(id);
      if (!l || boxes[i].coverage.all[metric.skill] === 0) return { v: null, n: 0 };
      const n = metric.attempts(l);
      return { v: n ? metric.value(l) : null, n };
    });
    const p = players.find((x) => x.id === id);
    return {
      id,
      slot,
      label: p ? `#${p.number} ${p.name}` : 'Unknown',
      values: points.map((x) => x.v),
      weak: points.map((x) => x.n < minAttempts),
      notes: points.map((x) => (x.n ? `${x.n} att` : null)),
    };
  });

  const teamRows = withStats.map((s) => sumRotationRows(rotationRows(s.statRallies)));
  const teamSeries: ChartSeries[] = [
    {
      id: 'so',
      slot: 1,
      label: 'Sideout %',
      values: teamRows.map((r) => ratio(r.sideouts, r.receive)),
      weak: teamRows.map((r) => r.receive < minAttempts),
      notes: teamRows.map((r) => `${r.sideouts}/${r.receive}`),
    },
    {
      id: 'bp',
      slot: 2,
      label: 'Break-point %',
      values: teamRows.map((r) => ratio(r.breaks, r.serve)),
      weak: teamRows.map((r) => r.serve < minAttempts),
      notes: teamRows.map((r) => `${r.breaks}/${r.serve}`),
    },
  ];

  return (
    <main className="page">
      <div className="page-head">
        <h1>Season</h1>
        <label className="field" style={{ minWidth: 200 }}>
          Season
          <select value={seasonId} onChange={(e) => setSetting('activeSeasonId', e.target.value || null)}>
            <option value="">All seasons</option>
            {seasons.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
        </label>
      </div>

      <div className="card">
        <div className="form-grid">
          <label className="field">
            From
            <input type="date" value={from} onChange={(e) => setFrom(e.target.value)} />
          </label>
          <label className="field">
            To
            <input type="date" value={to} onChange={(e) => setTo(e.target.value)} />
          </label>
          <label className="field">
            Opponent
            <select value={opponent} onChange={(e) => setOpponent(e.target.value)}>
              <option value="">All opponents</option>
              {opponents.map((o) => (
                <option key={o}>{o}</option>
              ))}
            </select>
          </label>
          <label className="field">
            Home / away
            <select value={venue} onChange={(e) => setVenue(e.target.value as typeof venue)}>
              <option value="all">All</option>
              <option value="home">Home</option>
              <option value="away">Away</option>
            </select>
          </label>
          <label className="field">
            Competition
            <select value={competition} onChange={(e) => setCompetition(e.target.value)}>
              <option value="">All competitions</option>
              {competitions.map((c) => (
                <option key={c}>{c}</option>
              ))}
            </select>
          </label>
          <label className="field">
            Min. attempts
            <input
              type="number"
              min={0}
              value={minAttempts}
              onChange={(e) => setSetting('minAttempts', Math.max(0, Number(e.target.value) || 0))}
              title="Ratios from fewer attempts are greyed out"
            />
          </label>
        </div>
        <label className="check" style={{ marginTop: 10 }}>
          <input type="checkbox" checked={friendlies} onChange={(e) => setFriendlies(e.target.checked)} /> Include friendlies / practice
        </label>
      </div>

      <section className="card">
        <div className="row spread">
          <h2>Results</h2>
          <div className="subtle">
            {wins} won · {losses} lost · <strong>{totalPoints}</strong> league points
          </div>
        </div>
        {filtered.length === 0 ? (
          <p className="empty">No matches match these filters.</p>
        ) : (
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th className="left">Date</th>
                  <th className="left">Opponent</th>
                  <th className="left">H/A</th>
                  <th>Sets</th>
                  <th className="left">Set scores</th>
                  <th>League pts</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((s) => (
                  <tr key={s.match.id}>
                    <td className="left">{formatDate(s.match.date)}</td>
                    <td className="left">
                      <a href={`#/match/${s.match.id}/report`}>{s.match.opponent || 'Unknown'}</a>
                      {!s.match.countsInSeason && <span className="badge"> friendly</span>}
                      {s.match.forfeit && <span className="badge warn"> forfeit</span>}
                    </td>
                    <td className="left">{s.match.home ? 'H' : 'A'}</td>
                    <td className={s.finished ? (s.setsUs > s.setsThem ? 'text-good' : 'text-bad') : undefined}>
                      {s.setsUs}–{s.setsThem}
                    </td>
                    <td className="left">{s.setScores.join(', ') || '–'}</td>
                    <td>{s.leaguePoints ?? (s.finished ? '–' : 'in progress')}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="card">
        <h2>Season box score</h2>
        {withStats.length > 0 && (
          <p className="small subtle">
            Tracked in{' '}
            {SKILLS.filter((s) => s !== 'E' && s !== 'F')
              .map((s) => `${SKILL_NAMES[s].toLowerCase()} ${tracked[s]} of ${withStats.length}`)
              .join(' · ')}{' '}
            matches. Season averages only use the matches where a skill was tracked.
          </p>
        )}
        <BoxScoreTable box={seasonBox} players={players} minAttempts={minAttempts} />
      </section>

      <section className="card">
        <h2>Rotation analysis</h2>
        <RotationTable rows={rotations} players={players} />
      </section>

      <section className="card">
        <div className="row spread">
          <h2>Player trends</h2>
          <label className="field">
            Metric
            <select value={metricKey} onChange={(e) => setMetricKey(e.target.value)}>
              {METRICS.map((m) => (
                <option key={m.key} value={m.key}>
                  {m.label}
                </option>
              ))}
            </select>
          </label>
        </div>
        {candidates.length === 0 ? (
          <p className="empty">No {SKILL_NAMES[metric.skill].toLowerCase()} data recorded yet.</p>
        ) : (
          <>
            <div className="row small" style={{ marginBottom: 8 }}>
              {candidates.map(([id, n]) => (
                <label className="check" key={id}>
                  <input
                    type="checkbox"
                    checked={selection.has(id)}
                    disabled={!selection.has(id) && selection.size >= MAX_SERIES}
                    onChange={() => togglePlayer(id)}
                  />
                  {playerName(players, id)} <span className="subtle">({n})</span>
                </label>
              ))}
            </div>
            <p className="small subtle">
              Hollow grey points have fewer than {minAttempts} attempts. Gaps are matches where the player had no attempts or the
              skill was not tracked. Up to {MAX_SERIES} players.
            </p>
            <LineChart
              title={`${metric.label} per match`}
              xLabels={xLabels}
              xTitles={xTitles}
              series={playerSeries}
              format={metric.format}
              yMin={metric.yMin}
              yMax={metric.yMax}
            />
          </>
        )}
      </section>

      <section className="card">
        <h2>Team trend: sideout and break-point %</h2>
        <LineChart title="Sideout % and break-point % per match" xLabels={xLabels} xTitles={xTitles} series={teamSeries} format={pctFmt} yMin={0} yMax={1} />
      </section>
    </main>
  );
}
