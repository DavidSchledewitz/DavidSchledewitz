import type { Player } from '../domain/types';

export const DASH = '–';

export function pct(v: number | null, digits = 0): string {
  return v === null ? DASH : `${(v * 100).toFixed(digits)}%`;
}

/** Hitting % is conventionally shown as .xxx */
export function hit(v: number | null): string {
  if (v === null) return DASH;
  const s = v.toFixed(3);
  return s.replace(/^(-?)0\./, '$1.');
}

export function avg(v: number | null, digits = 2): string {
  return v === null ? DASH : v.toFixed(digits);
}

export function num(v: number): string {
  return Number.isInteger(v) ? String(v) : v.toFixed(1);
}

export function playerLabel(p: Player | undefined): string {
  return p ? `#${p.number} ${p.name}` : 'Team';
}

export function byNumber(a: Player, b: Player) {
  return a.number - b.number;
}

export function formatDate(iso: string): string {
  const d = new Date(`${iso}T12:00:00`);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' });
}
