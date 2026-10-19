import { useLiveQuery } from 'dexie-react-hooks';
import { db, setSetting } from '../../db/db';
import { useAllMatches, usePlayers, useSetting } from '../hooks';
import { formatDate } from '../format';

const BACKUP_REMINDER_DAYS = 14;

export function MatchesPage() {
  const all = useAllMatches();
  const players = usePlayers();
  const seasons = useLiveQuery(() => db.seasons.orderBy('createdAt').reverse().toArray(), []);
  const activeSeasonId = useSetting<string | null>('activeSeasonId', null);
  const lastBackupAt = useSetting<number | null>('lastBackupAt', null);

  if (!all || !players || !seasons) return null;
  const seasonId = activeSeasonId && seasons.some((s) => s.id === activeSeasonId) ? activeSeasonId : seasons[0]?.id;
  const list = all.filter((m) => m.match.seasonId === seasonId);
  const backupDue =
    all.length > 0 && (lastBackupAt === null || Date.now() - lastBackupAt > BACKUP_REMINDER_DAYS * 86400_000);
  const totalPoints = list.reduce((sum, m) => sum + (m.match.countsInSeason ? m.leaguePoints ?? 0 : 0), 0);

  return (
    <main className="page">
      <div className="page-head">
        <h1>Matches</h1>
        <div className="row">
          {seasons.length > 0 && (
            <select value={seasonId} onChange={(e) => setSetting('activeSeasonId', e.target.value)} aria-label="Season">
              {seasons.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </select>
          )}
          <a className="btn primary" href="#/match/new">
            New match
          </a>
        </div>
      </div>

      {backupDue && (
        <div className="banner warn row spread">
          <span>
            {lastBackupAt === null
              ? 'You have never made a backup. Your data only lives in this browser.'
              : `Last backup was ${Math.floor((Date.now() - lastBackupAt) / 86400_000)} days ago.`}
          </span>
          <a className="btn small" href="#/data">
            Back up now
          </a>
        </div>
      )}

      {players.length === 0 && (
        <div className="banner info row spread">
          <span>Start by adding your roster: jersey numbers are how you enter stats.</span>
          <a className="btn small primary" href="#/team">
            Add players
          </a>
        </div>
      )}

      {list.length === 0 ? (
        <div className="card empty">No matches in this season yet.</div>
      ) : (
        <section className="card">
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th className="left">Date</th>
                  <th className="left">Opponent</th>
                  <th className="left">Result</th>
                  <th className="left">Sets</th>
                  <th>League pts</th>
                  <th className="left"></th>
                </tr>
              </thead>
              <tbody>
                {list.map((m) => {
                  const status = m.match.forfeit
                    ? 'Forfeit'
                    : m.finished
                      ? m.setsUs > m.setsThem
                        ? 'Won'
                        : 'Lost'
                      : m.replay.rallies.length
                        ? 'In progress'
                        : 'Not started';
                  return (
                    <tr key={m.match.id}>
                      <td className="left">{formatDate(m.match.date)}</td>
                      <td className="left">
                        {m.match.opponent || 'Opponent'} <span className="subtle small">{m.match.home ? 'home' : 'away'}</span>{' '}
                        {!m.match.countsInSeason && <span className="badge">friendly</span>}
                      </td>
                      <td className="left">
                        <span className={`badge ${status === 'Won' ? 'good' : status === 'Lost' ? 'bad' : ''}`}>{status}</span>{' '}
                        <strong>
                          {m.setsUs}–{m.setsThem}
                        </strong>
                      </td>
                      <td className="left subtle">{m.setScores.join(', ')}</td>
                      <td>{m.leaguePoints ?? '–'}</td>
                      <td className="left">
                        <div className="row" style={{ gap: 4 }}>
                          {!m.match.forfeit && (
                            <a className="btn small" href={`#/match/${m.match.id}/live`}>
                              Live
                            </a>
                          )}
                          <a className="btn small" href={`#/match/${m.match.id}/report`}>
                            Report
                          </a>
                          <a className="btn small" href={`#/match/${m.match.id}/edit`}>
                            Edit
                          </a>
                          <a className="btn small" href={`#/match/${m.match.id}/setup`}>
                            Setup
                          </a>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
              <tfoot>
                <tr className="total">
                  <td className="left" colSpan={4}>
                    League points (official matches)
                  </td>
                  <td>{totalPoints}</td>
                  <td></td>
                </tr>
              </tfoot>
            </table>
          </div>
        </section>
      )}
    </main>
  );
}
