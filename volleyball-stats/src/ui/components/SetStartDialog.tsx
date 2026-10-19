import { useState } from 'react';
import type { EventBody, Player, RulesPreset } from '../../domain/types';
import { Modal } from './Modal';

type SetStart = Extract<EventBody, { type: 'set_start' }>;

const LAYOUT = [4, 3, 2, 5, 6, 1];

export function SetStartDialog({
  setNumber,
  players,
  rules,
  previous,
  editing = false,
  onStart,
  onClose,
}: {
  setNumber: number;
  players: Player[];
  rules: RulesPreset;
  /** The set start to begin from: the previous set's, or the one being edited. */
  previous: SetStart | null;
  editing?: boolean;
  onStart: (body: SetStart) => void;
  onClose: () => void;
}) {
  const active = players.filter((p) => p.active);
  const [lineup, setLineup] = useState<string[]>(() => previous?.lineup.filter((id) => active.some((p) => p.id === id)).length === 6 ? [...previous.lineup] : Array(6).fill(''));
  const [setterId, setSetterId] = useState<string>(() => previous?.setterId ?? active.find((p) => p.positions.includes('S'))?.id ?? '');
  const [liberos, setLiberos] = useState<string[]>(
    () => previous?.liberos ?? active.filter((p) => p.positions.includes('L')).slice(0, rules.maxLiberos).map((p) => p.id),
  );
  const [replaces, setReplaces] = useState<string[] | null>(() => previous?.liberoReplaces ?? null);
  const [weServe, setWeServe] = useState<boolean>(() => (previous ? (editing ? previous.weServe : !previous.weServe) : true));

  const byId = new Map(players.map((p) => [p.id, p]));
  const filled = lineup.filter(Boolean);
  const complete = filled.length === 6 && new Set(filled).size === 6;
  // Until chosen, the libero replaces the middles in the lineup.
  const finalReplaces =
    replaces === null
      ? lineup.filter((id) => byId.get(id)?.positions.includes('MB'))
      : replaces.filter((id) => lineup.includes(id));

  const genders = filled.map((id) => byId.get(id)?.gender);
  const f = genders.filter((g) => g === 'F').length;
  const m = genders.filter((g) => g === 'M').length;
  const parityMsg =
    complete && rules.genderParity && genders.every(Boolean) && (f !== rules.genderParity.F || m !== rules.genderParity.M)
      ? `This lineup has ${f} women and ${m} men (rule: ${rules.genderParity.F} + ${rules.genderParity.M}).`
      : null;

  const setZone = (zone: number, id: string) => {
    const next = lineup.map((x) => (x === id ? '' : x));
    next[zone - 1] = id;
    setLineup(next);
    setLiberos(liberos.filter((l) => l !== id));
  };

  const start = () =>
    onStart({
      type: 'set_start',
      set: setNumber,
      lineup,
      setterId: lineup.includes(setterId) ? setterId : null,
      liberos: liberos.filter((id) => !lineup.includes(id)),
      liberoReplaces: liberos.length ? finalReplaces : [],
      weServe,
    });

  const option = (p: Player) => (
    <option key={p.id} value={p.id}>
      #{p.number} {p.name} {p.gender ? `(${p.gender === 'F' ? 'W' : 'M'})` : ''}
    </option>
  );

  return (
    <Modal title={editing ? `Edit set ${setNumber} lineup` : `Start set ${setNumber}`} onClose={onClose} wide>
      <p className="subtle small">Starting positions as written on the lineup slip. Zone 1 serves first when we serve.</p>
      <div className="court" style={{ maxWidth: 520 }}>
        <div className="net">NET</div>
        {LAYOUT.map((zone) => (
          <div className="zone" key={zone} style={{ cursor: 'default' }}>
            <span className="zn">{zone}</span>
            <select
              aria-label={`Zone ${zone}`}
              value={lineup[zone - 1]}
              onChange={(e) => setZone(zone, e.target.value)}
              style={{ width: '100%', marginTop: 14 }}
            >
              <option value="">Choose…</option>
              {active.map(option)}
            </select>
          </div>
        ))}
      </div>
      {parityMsg && <div className="banner warn" style={{ marginTop: 12 }}>{parityMsg}</div>}

      <div className="form-grid" style={{ marginTop: 16 }}>
        <label className="field">
          Setter (defines rotations R1–R6)
          <select value={lineup.includes(setterId) ? setterId : ''} onChange={(e) => setSetterId(e.target.value)}>
            <option value="">No setter (R1 = zone-1 starter)</option>
            {filled.map((id) => option(byId.get(id)!))}
          </select>
        </label>
        <div className="field">
          We serve first?
          <div className="row">
            <label className="check">
              <input type="radio" checked={weServe} onChange={() => setWeServe(true)} /> We serve
            </label>
            <label className="check">
              <input type="radio" checked={!weServe} onChange={() => setWeServe(false)} /> They serve
            </label>
          </div>
        </div>
      </div>

      <h3 style={{ marginTop: 16 }}>Libero{rules.maxLiberos > 1 ? `s (up to ${rules.maxLiberos})` : ''}</h3>
      <div className="row">
        {active
          .filter((p) => !lineup.includes(p.id))
          .map((p) => (
            <label key={p.id} className="check" style={{ marginRight: 8 }}>
              <input
                type="checkbox"
                checked={liberos.includes(p.id)}
                onChange={(e) => setLiberos(e.target.checked ? [...liberos, p.id] : liberos.filter((x) => x !== p.id))}
              />
              #{p.number} {p.name}
            </label>
          ))}
      </div>
      {liberos.length > rules.maxLiberos && <p className="text-warn small">More than {rules.maxLiberos} liberos.</p>}
      {liberos.length > 0 && (
        <>
          <p className="subtle small" style={{ marginTop: 8 }}>
            The libero comes in automatically for these players when they are in the back row (not while they serve):
          </p>
          <div className="row">
            {filled.map((id) => (
              <label key={id} className="check" style={{ marginRight: 8 }}>
                <input
                  type="checkbox"
                  checked={finalReplaces.includes(id)}
                  onChange={(e) => setReplaces(e.target.checked ? [...finalReplaces, id] : finalReplaces.filter((x) => x !== id))}
                />
                #{byId.get(id)!.number} {byId.get(id)!.name}
              </label>
            ))}
          </div>
        </>
      )}

      <div className="modal-actions">
        <button onClick={onClose}>Cancel</button>
        <button className="primary" disabled={!complete} onClick={start}>
          {editing ? 'Save lineup' : `Start set ${setNumber}`}
        </button>
      </div>
    </Modal>
  );
}
