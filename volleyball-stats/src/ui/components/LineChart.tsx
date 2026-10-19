// Dependency-free SVG line chart: one y-axis, 2px lines, crosshair + tooltip listing
// every series, legend for 2+ series, end labels for up to 4, and a table view.

import { useEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from 'react';
import './LineChart.css';

export interface ChartSeries {
  id: string;
  label: string;
  /** Categorical slot 1–8; follows the entity, not its rank. */
  slot: number;
  values: (number | null)[];
  /** Points with too few attempts: drawn hollow and muted. */
  weak?: boolean[];
  /** Extra text for the tooltip per point, e.g. "4/9". */
  notes?: (string | null)[];
}

interface Props {
  title: string;
  xLabels: string[];
  /** Longer x descriptions for the tooltip and table. */
  xTitles?: string[];
  series: ChartSeries[];
  format: (v: number) => string;
  yMin?: number;
  yMax?: number;
  height?: number;
}

const M = { top: 12, right: 12, bottom: 28, left: 44 };

const color = (slot: number) => `var(--series-${((slot - 1) % 8) + 1})`;

function niceTicks(min: number, max: number, count = 4): number[] {
  if (min === max) {
    min -= 1;
    max += 1;
  }
  const raw = (max - min) / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw;
  const ticks: number[] = [];
  for (let v = Math.floor(min / step) * step; v <= max + step * 1e-9; v += step) ticks.push(Number(v.toFixed(10)));
  return ticks;
}

export function LineChart({ title, xLabels, xTitles, series, format, yMin, yMax, height = 240 }: Props) {
  const wrap = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(640);
  const [hover, setHover] = useState<number | null>(null);

  const values = series.flatMap((s) => s.values.filter((v): v is number => v !== null));
  const hasData = xLabels.length > 0 && values.length > 0;

  useEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => setWidth(Math.max(280, entry.contentRect.width)));
    ro.observe(el);
    return () => ro.disconnect();
  }, [hasData]);

  if (!hasData) {
    return <p className="empty">No data for this chart yet.</p>;
  }

  const showEndLabels = series.length >= 2 && series.length <= 4;
  const right = M.right + (showEndLabels ? 90 : 0);
  const ticks = niceTicks(yMin ?? Math.min(...values), yMax ?? Math.max(...values));
  const lo = ticks[0];
  const hi = ticks[ticks.length - 1];
  const plotW = width - M.left - right;
  const plotH = height - M.top - M.bottom;
  const n = xLabels.length;
  const x = (i: number) => M.left + (n === 1 ? plotW / 2 : (i / (n - 1)) * plotW);
  const y = (v: number) => M.top + plotH - ((v - lo) / (hi - lo || 1)) * plotH;
  const labelEvery = Math.max(1, Math.ceil(n / Math.max(1, Math.floor(plotW / 64))));

  // Line segments break at missing values.
  const paths = series.map((s) => {
    let d = '';
    let pen = false;
    s.values.forEach((v, i) => {
      if (v === null) {
        pen = false;
        return;
      }
      d += `${pen ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
      pen = true;
    });
    return d;
  });

  // End labels at each series' last value; dropped if any two would collide.
  const ends = series.map((s) => {
    const i = s.values.map((v, k) => (v === null ? -1 : k)).filter((k) => k >= 0).pop();
    return i === undefined ? null : { i, v: s.values[i]!, s };
  });
  const endYs = ends.filter(Boolean).map((e) => y(e!.v)).sort((a, b) => a - b);
  const collide = endYs.some((v, k) => k > 0 && v - endYs[k - 1] < 13);

  const pick = (clientX: number) => {
    const rect = wrap.current!.getBoundingClientRect();
    const px = clientX - rect.left;
    const i = n === 1 ? 0 : Math.round(((px - M.left) / plotW) * (n - 1));
    setHover(Math.min(n - 1, Math.max(0, i)));
  };
  const onKey = (e: KeyboardEvent) => {
    if (e.key === 'ArrowRight') setHover((h) => Math.min(n - 1, (h ?? -1) + 1));
    else if (e.key === 'ArrowLeft') setHover((h) => Math.max(0, (h ?? n) - 1));
    else if (e.key === 'Escape') setHover(null);
  };

  const tooltipLeft = hover === null ? 0 : x(hover) + 12 + 170 > width ? x(hover) - 12 - 170 : x(hover) + 12;

  return (
    <div className="viz-root">
      {series.length >= 2 && (
        <div className="viz-legend" aria-hidden="true">
          {series.map((s) => (
            <span key={s.id}>
              <span className="viz-key" style={{ background: color(s.slot) }} />
              {s.label}
            </span>
          ))}
        </div>
      )}
      <div ref={wrap} style={{ position: 'relative' }}>
        <svg
          viewBox={`0 0 ${width} ${height}`}
          height={height}
          role="img"
          aria-label={`${title}. Use left and right arrow keys to read values.`}
          tabIndex={0}
          onPointerMove={(e: PointerEvent) => pick(e.clientX)}
          onPointerLeave={() => setHover(null)}
          onKeyDown={onKey}
          onBlur={() => setHover(null)}
        >
          {ticks.map((t) => (
            <g key={t}>
              <line x1={M.left} x2={width - right} y1={y(t)} y2={y(t)} stroke={t === lo ? 'var(--viz-axis)' : 'var(--viz-grid)'} strokeWidth={1} />
              <text className="tick" x={M.left - 6} y={y(t)} dy="0.32em" textAnchor="end">
                {format(t)}
              </text>
            </g>
          ))}
          {xLabels.map((l, i) =>
            i % labelEvery === 0 || i === n - 1 ? (
              <text key={i} className="tick" x={x(i)} y={height - 8} textAnchor="middle">
                {l}
              </text>
            ) : null,
          )}
          {hover !== null && <line x1={x(hover)} x2={x(hover)} y1={M.top} y2={M.top + plotH} stroke="var(--viz-axis)" strokeWidth={1} />}
          {series.map((s, k) => (
            <path key={s.id} d={paths[k]} fill="none" stroke={color(s.slot)} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
          ))}
          {series.map((s) =>
            s.values.map((v, i) => {
              if (v === null) return null;
              const weak = s.weak?.[i];
              const active = hover === i;
              return (
                <circle
                  key={`${s.id}-${i}`}
                  cx={x(i)}
                  cy={y(v)}
                  r={active ? 5 : 4}
                  fill={weak ? 'var(--surface)' : color(s.slot)}
                  stroke={weak ? 'var(--muted)' : 'var(--surface)'}
                  strokeWidth={2}
                />
              );
            }),
          )}
          {showEndLabels &&
            !collide &&
            ends.map((e) =>
              e ? (
                <text key={e.s.id} className="end-label" x={x(e.i) + 8} y={y(e.v)} dy="0.32em">
                  {e.s.label.length > 12 ? `${e.s.label.slice(0, 11)}…` : e.s.label}
                </text>
              ) : null,
            )}
        </svg>
        {hover !== null && (
          <div className="viz-tooltip" style={{ left: tooltipLeft }} role="status">
            <div className="tt-head">{xTitles?.[hover] ?? xLabels[hover]}</div>
            {series.map((s) => {
              const v = s.values[hover];
              return (
                <div className="tt-row" key={s.id}>
                  <span className="viz-key" style={{ background: color(s.slot) }} />
                  <strong>{v === null ? '–' : format(v)}</strong>
                  <span className="muted">
                    {s.label}
                    {s.notes?.[hover] ? ` · ${s.notes[hover]}` : ''}
                    {s.weak?.[hover] && v !== null ? ' · small sample' : ''}
                  </span>
                </div>
              );
            })}
          </div>
        )}
      </div>
      <details className="small" style={{ marginTop: 6 }}>
        <summary>Show as table</summary>
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr>
                <th className="left">Match</th>
                {series.map((s) => (
                  <th key={s.id}>{s.label}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {xLabels.map((l, i) => (
                <tr key={i}>
                  <td className="left">{xTitles?.[i] ?? l}</td>
                  {series.map((s) => {
                    const v = s.values[i];
                    return (
                      <td key={s.id} className={s.weak?.[i] ? 'dim' : undefined}>
                        {v === null ? '–' : format(v)}
                        {s.notes?.[i] ? <span className="subtle"> ({s.notes[i]})</span> : null}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  );
}
