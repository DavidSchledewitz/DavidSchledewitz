import { useMemo, useState } from 'react';
import { boxScore, rotationRows } from '../../domain/stats';
import { BoxScoreTable } from '../components/BoxScoreTable';
import { RotationTable } from '../components/RotationTable';
import { download, slug, toCsv } from '../download';
import { formatDate } from '../format';
import { useMatch, useSetting } from '../hooks';
import { boxPlayerIds, boxScoreCsv, eventLogRows, LOG_HEADER, playerName } from '../reportUtils';
import './report.css';

type SetFilter = 'all' | number;

export function ReportPage({ matchId }: { matchId: string }) {
  const data = useMatch(matchId);
  const teamName = useSetting('teamName', 'My team');
  const minAttempts = useSetting('minAttempts', 5);
  const [setFilter, setSetFilter] = useState<SetFilter>('all');
  const [showBox, setShowBox] = useState(true);
  const [showRot, setShowRot] = useState(true);
  const [hiddenPlayers, setHiddenPlayers] = useState<Set<string>>(new Set());

  const rallies = useMemo(() => {
    if (!data) return [];
    const r = data.summary.statRallies;
    return setFilter === 'all' ? r : r.filter((x) => x.set === setFilter);
  }, [data, setFilter]);
  const box = useMemo(() => boxScore(rallies), [rallies]);
  const rotations = useMemo(() => rotationRows(rallies), [rallies]);

  if (data === undefined) return <main className="page">Loading…</main>;
  if (data === null) return <main className="page empty">This match no longer exists.</main>;

  const { match, players, events, summary } = data;
  const setNumbers = summary.replay.sets.map((s) => s.set).filter((n) => !match.excludedSets.includes(n));
  const boxIds = boxPlayerIds(box, players);
  const resultText = `${summary.setsUs}–${summary.setsThem}`;
  const won = summary.setsUs > summary.setsThem;
  const fileBase = slug(`${match.date} ${match.opponent}`);
  const setLabel = setFilter === 'all' ? 'All sets' : `Set ${setFilter}`;

  const togglePlayer = (id: string) => {
    const next = new Set(hiddenPlayers);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setHiddenPlayers(next);
  };

  const exportBoxCsv = () => {
    const visible = new Set(boxIds.filter((id) => !hiddenPlayers.has(id)));
    download(`${fileBase}-box-score.csv`, toCsv(boxScoreCsv(box, players, visible)), 'text/csv');
  };
  const exportLogCsv = () => {
    download(`${fileBase}-events.csv`, toCsv([LOG_HEADER, ...eventLogRows(summary, events, players)]), 'text/csv');
  };

  return (
    <main className="page">
      <div className="print-only">
        <h1>
          {teamName} vs {match.opponent}
        </h1>
        <p>
          {formatDate(match.date)} · {match.home ? 'Home' : 'Away'}
          {match.competition ? ` · ${match.competition}` : ''} · Result {resultText}
          {summary.setScores.length > 0 && ` (${summary.setScores.join(', ')})`}
          {summary.leaguePoints !== null && ` · ${summary.leaguePoints} league points`} · {setLabel}
        </p>
      </div>

      <div className="page-head no-print">
        <div>
          <h1>
            vs {match.opponent || 'Unknown opponent'}{' '}
            {summary.finished && <span className={`badge ${won ? 'good' : 'bad'}`}>{won ? 'Won' : 'Lost'}</span>}
          </h1>
          <div className="subtle">
            {formatDate(match.date)} · {match.home ? 'Home' : 'Away'}
            {match.competition ? ` · ${match.competition}` : ''}
            {!match.countsInSeason && ' · Friendly (not in season stats)'}
          </div>
        </div>
        <div className="row">
          <a className="btn" href={`#/match/${match.id}/live`}>
            Live entry
          </a>
          <a className="btn" href={`#/match/${match.id}/edit`}>
            Edit events
          </a>
        </div>
      </div>

      <div className="card">
        <div className="row spread">
          <div>
            <div style={{ fontSize: '1.6rem', fontWeight: 800 }}>{resultText}</div>
            <div className="subtle">{summary.setScores.length ? summary.setScores.join(' · ') : 'No sets played yet'}</div>
          </div>
          <div style={{ textAlign: 'right' }}>
            {summary.leaguePoints !== null ? (
              <>
                <div style={{ fontSize: '1.6rem', fontWeight: 800 }}>{summary.leaguePoints}</div>
                <div className="subtle">league points</div>
              </>
            ) : (
              <span className="badge warn">{match.forfeit ? 'Forfeit' : 'In progress'}</span>
            )}
          </div>
        </div>
      </div>

      {match.forfeit ? (
        <div className="banner info">
          {match.forfeit === 'them' ? 'The opponent forfeited.' : 'We forfeited.'} The result counts as {resultText}, but no stats
          are recorded for this match.
        </div>
      ) : (
        <>
          <div className="card no-print">
            <div className="row" style={{ marginBottom: 10 }}>
              <span className="subtle small">Show</span>
              <button className={setFilter === 'all' ? 'selected small' : 'small'} onClick={() => setSetFilter('all')}>
                All sets
              </button>
              {setNumbers.map((n) => (
                <button key={n} className={setFilter === n ? 'selected small' : 'small'} onClick={() => setSetFilter(n)}>
                  Set {n}
                </button>
              ))}
              {match.excludedSets.length > 0 && (
                <span className="small subtle">Excluded from stats: set {match.excludedSets.join(', ')}</span>
              )}
            </div>
            <details>
              <summary>Export and PDF options</summary>
              <div className="stack" style={{ marginTop: 10 }}>
                <div className="row">
                  <span className="small subtle">Sections:</span>
                  <label className="check">
                    <input type="checkbox" checked={showBox} onChange={(e) => setShowBox(e.target.checked)} /> Box score
                  </label>
                  <label className="check">
                    <input type="checkbox" checked={showRot} onChange={(e) => setShowRot(e.target.checked)} /> Rotation analysis
                  </label>
                </div>
                {boxIds.length > 0 && (
                  <div className="row">
                    <span className="small subtle">Players:</span>
                    {boxIds.map((id) => (
                      <label className="check small" key={id}>
                        <input type="checkbox" checked={!hiddenPlayers.has(id)} onChange={() => togglePlayer(id)} />
                        {playerName(players, id)}
                      </label>
                    ))}
                  </div>
                )}
                <p className="small subtle">
                  The PDF shows {setLabel.toLowerCase()} (change it above). Unticked sections and players are left out of the PDF
                  and the box score CSV.
                </p>
                <div className="row">
                  <button className="primary" onClick={() => window.print()}>
                    Print / Save as PDF
                  </button>
                  <button onClick={exportBoxCsv}>Box score CSV</button>
                  <button onClick={exportLogCsv}>Event log CSV</button>
                </div>
              </div>
            </details>
          </div>

          <section className={`card${showBox ? '' : ' print-hide'}`}>
            <h2>Box score · {setLabel}</h2>
            <BoxScoreTable box={box} players={players} minAttempts={minAttempts} printHidden={hiddenPlayers} />
          </section>

          <section className={`card${showRot ? '' : ' print-hide'}`}>
            <h2>Rotation analysis · {setLabel}</h2>
            <p className="small subtle no-print">
              R1–R6 is the zone the setter is in. Sideout % = rallies won when the opponent served; break % = rallies won on our
              serve.
            </p>
            <RotationTable rows={rotations} players={players} />
          </section>
        </>
      )}
    </main>
  );
}
