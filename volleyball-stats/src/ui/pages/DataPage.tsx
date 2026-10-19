import { useEffect, useState, type ChangeEvent } from 'react';
import { exportBackup, matchEvents, parseBackup, restoreBackup, setSetting } from '../../db/db';
import { CodeHelp } from '../components/CodeHelp';
import { download, toCsv } from '../download';
import { formatDate } from '../format';
import { useAllMatches, usePlayers, useSetting } from '../hooks';
import { eventLogRows, LOG_HEADER } from '../reportUtils';

export function DataPage() {
  const teamName = useSetting('teamName', 'My team');
  const lastBackupAt = useSetting<number | null>('lastBackupAt', null);
  const matches = useAllMatches();
  const players = usePlayers();
  const [name, setName] = useState(teamName);
  const [message, setMessage] = useState<{ kind: 'good' | 'error'; text: string } | null>(null);

  useEffect(() => setName(teamName), [teamName]);

  const saveName = () => {
    const v = name.trim();
    if (v && v !== teamName) setSetting('teamName', v);
  };

  const doExport = async () => {
    const backup = await exportBackup();
    const day = new Date().toISOString().slice(0, 10);
    download(`volleyball-stats-backup-${day}.json`, JSON.stringify(backup, null, 2), 'application/json');
    await setSetting('lastBackupAt', Date.now());
    setMessage({ kind: 'good', text: 'Backup downloaded. Keep it somewhere safe (e.g. cloud drive).' });
  };

  const doImport = async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (!file) return;
    try {
      const backup = parseBackup(await file.text());
      const ok = window.confirm(
        `Restore the backup from ${new Date(backup.exportedAt).toLocaleString()}?\n\n` +
          `It has ${backup.players.length} players and ${backup.matches.length} matches. ` +
          'Everything currently stored in this browser will be replaced.',
      );
      if (!ok) return;
      await restoreBackup(backup);
      setMessage({ kind: 'good', text: `Backup restored: ${backup.players.length} players, ${backup.matches.length} matches.` });
    } catch (err) {
      setMessage({ kind: 'error', text: err instanceof Error ? err.message : 'Could not read that file.' });
    }
  };

  const exportAllEvents = async () => {
    if (!matches || !players) return;
    const rows: unknown[][] = [['Date', 'Opponent', ...LOG_HEADER]];
    for (const s of [...matches].reverse()) {
      const events = await matchEvents(s.match.id);
      for (const r of eventLogRows(s, events, players)) rows.push([s.match.date, s.match.opponent, ...r]);
    }
    download('volleyball-stats-all-events.csv', toCsv(rows), 'text/csv');
  };

  return (
    <main className="page">
      <div className="page-head">
        <h1>Data &amp; help</h1>
      </div>

      {message && <div className={`banner ${message.kind}`}>{message.text}</div>}

      <section className="card">
        <h2>Team</h2>
        <label className="field" style={{ maxWidth: 360 }}>
          Team name
          <input value={name} onChange={(e) => setName(e.target.value)} onBlur={saveName} onKeyDown={(e) => e.key === 'Enter' && saveName()} />
        </label>
      </section>

      <section className="card">
        <h2>Backup</h2>
        <p>
          All data lives only in this browser on this device. Export a backup regularly, and use it to move your data to
          another laptop.
        </p>
        <p className="subtle small">
          {lastBackupAt ? `Last backup: ${new Date(lastBackupAt).toLocaleString()}` : 'No backup made yet.'}
          {matches && ` · ${matches.length} matches stored`}
        </p>
        <div className="row">
          <button className="primary" onClick={doExport}>
            Export backup (JSON)
          </button>
          <label className="btn">
            Import backup…
            <input type="file" accept="application/json,.json" onChange={doImport} style={{ display: 'none' }} />
          </label>
          <button onClick={exportAllEvents} disabled={!matches?.length}>
            All events CSV
          </button>
        </div>
        {matches && matches.length > 0 && (
          <p className="small subtle" style={{ marginTop: 8 }}>
            Oldest match {formatDate(matches[matches.length - 1].match.date)}, newest {formatDate(matches[0].match.date)}.
          </p>
        )}
      </section>

      <section className="card">
        <h2>Keyboard codes</h2>
        <CodeHelp />
      </section>
    </main>
  );
}
