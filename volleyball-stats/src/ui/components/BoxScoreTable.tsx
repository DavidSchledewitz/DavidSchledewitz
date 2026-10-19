import { blockTotal, errorsTotal, type BoxScore, type Line } from '../../domain/stats';
import type { Player } from '../../domain/types';
import { num } from '../format';
import { BOX_TABLES, boxPlayerIds, cellValue, covered, coverageNotes, hasActions, playerName, type BoxCol } from '../reportUtils';

interface Props {
  box: BoxScore;
  players: Player[];
  /** Ratios with fewer attempts than this are dimmed. */
  minAttempts?: number;
  /** Player rows left out of the printed PDF (still shown on screen). */
  printHidden?: Set<string>;
}

export function BoxScoreTable({ box, players, minAttempts = 0, printHidden }: Props) {
  const ids = boxPlayerIds(box, players);
  const rows: { key: string; label: string; line: Line; total?: boolean }[] = [
    ...ids.map((id) => ({ key: id, label: playerName(players, id), line: box.players.get(id)! })),
    ...(hasActions(box.unattributed) ? [{ key: 'unattributed', label: 'Unattributed', line: box.unattributed }] : []),
    { key: 'team', label: 'Team', line: box.team, total: true },
  ];

  const cell = (col: BoxCol, line: Line, isTotal: boolean, first: boolean) => {
    const value = cellValue(col, line, box.coverage);
    const small = !isTotal && col.attempts && covered(box.coverage, col.need) && col.attempts(line) < minAttempts;
    const cls = [first ? 'sep' : '', small ? 'dim' : ''].join(' ').trim() || undefined;
    return (
      <td key={col.key} className={cls} title={small ? `Only ${col.attempts!(line)} attempts` : undefined}>
        {value}
      </td>
    );
  };

  const t = box.team;
  const notes = coverageNotes(box.coverage);
  const hasStats = covered(box.coverage, { anyOf: ['S', 'R', 'A', 'B', 'D', 'E', 'F'], mode: 'any' });

  return (
    <div>
      {box.coverage.total === 0 ? (
        <p className="empty">No rallies recorded.</p>
      ) : !hasStats ? (
        <p className="subtle">Only the score was tracked, so there are no player stats. Rotation analysis below still works.</p>
      ) : (
        BOX_TABLES.map((groups, ti) => (
          <div className="table-wrap" key={ti} style={{ marginBottom: 12 }}>
            <table className="data">
              <thead>
                <tr className="group">
                  <th />
                  {groups.map((g) => (
                    <th key={g.name} colSpan={g.cols.length} className="sep">
                      {g.name}
                    </th>
                  ))}
                </tr>
                <tr>
                  <th className="left">Player</th>
                  {groups.map((g) =>
                    g.cols.map((c, i) => (
                      <th key={c.key} title={c.title} className={i === 0 ? 'sep' : undefined}>
                        {c.label}
                      </th>
                    )),
                  )}
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.key} className={[r.total ? 'total' : '', printHidden?.has(r.key) ? 'print-hide' : ''].join(' ').trim() || undefined}>
                    <td className="left">{r.label}</td>
                    {groups.map((g) => g.cols.map((c, i) => cell(c, r.line, !!r.total, i === 0)))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))
      )}
      {box.coverage.total > 0 && (
        <p className="small">
          <strong>Points won {box.pointsWon}</strong>
          {covered(box.coverage, { anyOf: ['S', 'A', 'B'], mode: 'all' }) &&
            `: kills ${t.kills}, aces ${t.aces}, blocks ${num(blockTotal(t))}`}
          {`, opponent errors ${box.oppErrors}`}
          {box.ourPoints > 0 && `, other points ${box.ourPoints}`}
          {' · '}
          <strong>Points lost {box.pointsLost}</strong>
          {hasStats && `: our errors ${errorsTotal(t)}`}
          {`${hasStats ? ',' : ':'} opponent points ${box.oppPoints}, red cards ${box.penalties}`}
        </p>
      )}
      {notes.length > 0 && (
        <p className="small subtle">
          {notes.join(' ')} Columns marked – were not tracked.
        </p>
      )}
    </div>
  );
}
