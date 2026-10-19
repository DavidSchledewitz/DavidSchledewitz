import { useLiveQuery } from 'dexie-react-hooks';
import { useEffect, useState } from 'react';
import { db, newId, setSetting } from '../../db/db';
import type { Gender, Player, Position } from '../../domain/types';
import { usePlayers, useSetting } from '../hooks';

const POSITIONS: Position[] = ['S', 'OH', 'MB', 'OPP', 'L'];
const POSITION_NAMES: Record<Position, string> = { S: 'Setter', OH: 'Outside', MB: 'Middle', OPP: 'Opposite', L: 'Libero' };

export function TeamPage() {
  const players = usePlayers();
  const teamName = useSetting('teamName', 'My team');
  const seasons = useLiveQuery(() => db.seasons.orderBy('createdAt').reverse().toArray(), []);
  const [showInactive, setShowInactive] = useState(false);
  const [seasonName, setSeasonName] = useState('');
  // Row order is fixed while the page is open, so rows don't jump when a number changes.
  const [order, setOrder] = useState<string[] | null>(null);
  useEffect(() => {
    if (players && order === null) setOrder(players.map((p) => p.id));
  }, [players, order]);

  if (!players || !seasons || !order) return null;
  const rank = (p: Player) => (order.includes(p.id) ? order.indexOf(p.id) : order.length);
  const shown = players.filter((p) => showInactive || p.active).sort((a, b) => rank(a) - rank(b));
  const dupNumbers = new Set(
    players.filter((p) => p.active).map((p) => p.number).filter((n, i, all) => all.indexOf(n) !== i),
  );
  const women = players.filter((p) => p.active && p.gender === 'F').length;
  const men = players.filter((p) => p.active && p.gender === 'M').length;

  const update = (p: Player, patch: Partial<Player>) => db.players.update(p.id, patch);
  const addPlayer = () => {
    const next = Math.max(0, ...players.map((p) => p.number)) + 1;
    const id = newId();
    setOrder([...order, id]);
    db.players.add({ id, name: '', number: next, positions: [], active: true });
  };
  const addSeason = async () => {
    const name = seasonName.trim();
    if (!name) return;
    const id = newId();
    await db.seasons.add({ id, name, createdAt: Date.now() });
    await setSetting('activeSeasonId', id);
    setSeasonName('');
  };

  return (
    <main className="page">
      <div className="page-head">
        <h1>Team</h1>
      </div>

      <section className="card">
        <label className="field" style={{ maxWidth: 360 }}>
          Team name
          <input defaultValue={teamName} onBlur={(e) => setSetting('teamName', e.target.value.trim() || 'My team')} />
        </label>
      </section>

      <section className="card">
        <div className="row spread">
          <h2>Roster</h2>
          <div className="row">
            <span className="subtle small">
              {players.filter((p) => p.active).length} active · {women} women · {men} men
            </span>
            <label className="check small">
              <input type="checkbox" checked={showInactive} onChange={(e) => setShowInactive(e.target.checked)} />
              Show inactive
            </label>
            <button className="primary" onClick={addPlayer}>
              Add player
            </button>
          </div>
        </div>
        <p className="subtle small">
          Gender is optional; set it to get the 3 + 3 gender parity check. Changes save automatically.
        </p>
        {shown.length === 0 ? (
          <p className="empty">No players yet. Add your roster to start recording matches.</p>
        ) : (
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th className="left">#</th>
                  <th className="left">Name</th>
                  <th className="left">Gender</th>
                  <th className="left">Positions</th>
                  <th className="left">Active</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((p) => (
                  <tr key={p.id} style={p.active ? undefined : { opacity: 0.55 }}>
                    <td className="left">
                      <input
                        type="number"
                        min={0}
                        max={999}
                        defaultValue={p.number}
                        style={{ width: 64, borderColor: dupNumbers.has(p.number) ? 'var(--bad)' : undefined }}
                        title={dupNumbers.has(p.number) ? 'Two active players share this number' : undefined}
                        onBlur={(e) => {
                          const n = Number(e.target.value);
                          if (Number.isInteger(n) && n >= 0) update(p, { number: n });
                        }}
                      />
                    </td>
                    <td className="left">
                      <input
                        defaultValue={p.name}
                        placeholder="Name"
                        autoFocus={!p.name}
                        onBlur={(e) => update(p, { name: e.target.value.trim() })}
                      />
                    </td>
                    <td className="left">
                      <select
                        value={p.gender ?? ''}
                        onChange={(e) => update(p, { gender: (e.target.value || undefined) as Gender | undefined })}
                      >
                        <option value="">–</option>
                        <option value="F">Woman</option>
                        <option value="M">Man</option>
                      </select>
                    </td>
                    <td className="left">
                      <div className="row" style={{ gap: 4 }}>
                        {POSITIONS.map((pos) => (
                          <button
                            key={pos}
                            className={`small ${p.positions.includes(pos) ? 'selected' : ''}`}
                            title={POSITION_NAMES[pos]}
                            onClick={() =>
                              update(p, {
                                positions: p.positions.includes(pos)
                                  ? p.positions.filter((x) => x !== pos)
                                  : [...p.positions, pos],
                              })
                            }
                          >
                            {pos}
                          </button>
                        ))}
                      </div>
                    </td>
                    <td className="left">
                      <input type="checkbox" checked={p.active} onChange={(e) => update(p, { active: e.target.checked })} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {dupNumbers.size > 0 && (
          <p className="text-bad small">Two active players share a jersey number; codes use the number, so make them unique.</p>
        )}
      </section>

      <section className="card">
        <h2>Seasons</h2>
        <div className="row" style={{ marginBottom: 12 }}>
          <input
            placeholder="e.g. 2025/26"
            value={seasonName}
            onChange={(e) => setSeasonName(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && addSeason()}
          />
          <button onClick={addSeason} disabled={!seasonName.trim()}>
            Add season
          </button>
        </div>
        {seasons.length === 0 ? (
          <p className="subtle">No seasons yet. One is created automatically with your first match.</p>
        ) : (
          <ul className="stack" style={{ listStyle: 'none', padding: 0, margin: 0 }}>
            {seasons.map((s) => (
              <li key={s.id}>
                <input defaultValue={s.name} onBlur={(e) => e.target.value.trim() && db.seasons.update(s.id, { name: e.target.value.trim() })} />
              </li>
            ))}
          </ul>
        )}
      </section>
    </main>
  );
}
