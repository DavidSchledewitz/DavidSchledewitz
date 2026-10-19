import type { LiveState } from '../../domain/engine';
import type { Player } from '../../domain/types';

// Zones as seen from behind our baseline: front row 4-3-2 at the net, back row 5-6-1.
const LAYOUT = [4, 3, 2, 5, 6, 1];

export function Court({
  state,
  players,
  picked,
  onPick,
}: {
  state: LiveState;
  players: Player[];
  picked?: string | null;
  onPick?: (playerId: string) => void;
}) {
  const byId = new Map(players.map((p) => [p.id, p]));
  return (
    <div className="court" aria-label="Court">
      <div className="net">NET</div>
      {LAYOUT.map((zone) => {
        const id = state.zones[zone - 1];
        const p = byId.get(id);
        const isLibero = state.liberos.includes(id);
        return (
          <div
            key={zone}
            className={`zone${isLibero ? ' libero' : ''}${picked === id ? ' picked' : ''}`}
            onClick={() => id && onPick?.(id)}
            role={onPick ? 'button' : undefined}
            title={p ? `#${p.number} ${p.name}` : undefined}
          >
            <span className="zn">{zone}</span>
            <div className="jersey">{p ? p.number : '–'}</div>
            <div className="pname">{p?.name || ' '}</div>
            <div className="tags">
              {id && id === state.setterId && <span className="badge accent">S</span>}
              {isLibero && <span className="badge warn">L</span>}
              {p?.gender && <span className="badge">{p.gender === 'F' ? 'W' : 'M'}</span>}
              {id && id === state.serverId && <span className="badge good">serve</span>}
            </div>
          </div>
        );
      })}
    </div>
  );
}
