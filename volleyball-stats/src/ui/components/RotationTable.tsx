import { ratio, sumRotationRows, type RotationRow } from '../../domain/stats';
import type { Player } from '../../domain/types';
import { DASH, pct } from '../format';

interface Props {
  rows: RotationRow[];
  players: Player[];
}

function signed(n: number): string {
  return n > 0 ? `+${n}` : n < 0 ? `−${-n}` : '0';
}

export function RotationTable({ rows, players }: Props) {
  const total = sumRotationRows(rows);
  const all = [...rows.map((r) => ({ row: r, label: `R${r.rotation}`, total: false })), { row: total, label: 'Total', total: true }];
  const anyRated = total.byPass.some(([n]) => n > 0);
  const anyAttacks = total.attacks.size > 0;
  const jersey = (id: string | null) => (id ? `#${players.find((p) => p.id === id)?.number ?? '?'}` : 'Team');

  if (total.rallies === 0) return <p className="empty">No rallies recorded.</p>;

  return (
    <div>
      <div className="table-wrap">
        <table className="data">
          <thead>
            <tr>
              <th className="left">Rotation</th>
              <th>Rallies</th>
              <th>Won</th>
              <th>Lost</th>
              <th title="Points won minus points lost">+/−</th>
              <th className="sep" title="Rallies won when the opponent served">Sideout %</th>
              <th title="Sideouts won by the first attack after the reception (attack-tracked rallies)">FBSO %</th>
              <th title="Rallies won on our serve">Break %</th>
              {anyRated &&
                [0, 1, 2, 3].map((r) => (
                  <th key={r} className={r === 0 ? 'sep' : undefined} title={`Sideout % after a ${r}-pass`}>
                    SO after {r}
                  </th>
                ))}
            </tr>
          </thead>
          <tbody>
            {all.map(({ row, label, total: isTotal }) => {
              const diff = row.won - row.lost;
              return (
                <tr key={label} className={isTotal ? 'total' : undefined}>
                  <td className="left">{label}</td>
                  <td>{row.rallies}</td>
                  <td>{row.won}</td>
                  <td>{row.lost}</td>
                  <td className={diff > 0 ? 'text-good' : diff < 0 ? 'text-bad' : undefined}>{signed(diff)}</td>
                  <td className="sep">
                    {pct(ratio(row.sideouts, row.receive))} <span className="small subtle">{row.sideouts}/{row.receive}</span>
                  </td>
                  <td>{row.fbsoBase ? pct(ratio(row.fbso, row.fbsoBase)) : DASH}</td>
                  <td>
                    {pct(ratio(row.breaks, row.serve))} <span className="small subtle">{row.breaks}/{row.serve}</span>
                  </td>
                  {anyRated &&
                    row.byPass.map(([n, w], r) => (
                      <td key={r} className={r === 0 ? 'sep' : undefined}>
                        {n ? (
                          <>
                            {pct(w / n)} <span className="small subtle">{w}/{n}</span>
                          </>
                        ) : (
                          DASH
                        )}
                      </td>
                    ))}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {anyAttacks && (
        <>
          <h3 style={{ marginTop: 16 }}>Attack distribution</h3>
          <p className="small subtle">Kills / attacks per player in each rotation (rallies where attacks were tracked).</p>
          <div className="table-wrap">
            <table className="data">
              <tbody>
                {all.map(({ row, label, total: isTotal }) => {
                  const list = [...row.attacks.entries()].sort((a, b) => b[1].att - a[1].att);
                  return (
                    <tr key={label} className={isTotal ? 'total' : undefined}>
                      <td className="left" style={{ width: 70 }}>
                        {label}
                      </td>
                      <td className="left" style={{ whiteSpace: 'normal' }}>
                        {list.length === 0
                          ? DASH
                          : list.map(([id, s]) => (
                              <span key={id ?? 'team'} style={{ marginRight: 14, display: 'inline-block' }}>
                                {jersey(id)} <strong>{s.kills}</strong>/{s.att}
                              </span>
                            ))}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
