/** Minimal dependency-free SVG time-series chart. */

export interface Series {
  label: string;
  color: string;
  points: { t: number; v: number }[]; // t = epoch ms
}

interface Props {
  series: Series[];
  height?: number;
  yMin?: number;
  yMax?: number;
  unit?: string;
  refLine?: { v: number; label: string };
  markers?: { t: number; label: string; color?: string }[];
  xRange?: [number, number];
}

const W = 600;

export function LineChart({ series, height = 140, yMin = 0, yMax, unit = "", refLine, markers = [], xRange }: Props) {
  const all = series.flatMap((s) => s.points);
  if (!all.length) {
    return <div className="flex items-center justify-center text-xs text-slate-500" style={{ height }}>no data</div>;
  }
  const t0 = xRange?.[0] ?? Math.min(...all.map((p) => p.t));
  const t1 = xRange?.[1] ?? Math.max(...all.map((p) => p.t));
  const maxV = yMax ?? Math.max(1, refLine?.v ?? 0, ...all.map((p) => p.v)) * 1.1;
  const pad = { l: 34, r: 8, t: 8, b: 18 };
  const iw = W - pad.l - pad.r;
  const ih = height - pad.t - pad.b;
  const x = (t: number) => pad.l + (t1 === t0 ? iw : ((t - t0) / (t1 - t0)) * iw);
  const y = (v: number) => pad.t + ih - ((v - yMin) / (maxV - yMin)) * ih;
  const ticks = [yMin, (yMin + maxV) / 2, maxV];
  const time = (t: number) => new Date(t).toLocaleTimeString([], { hour12: false });

  return (
    <div>
      <svg viewBox={`0 0 ${W} ${height}`} className="w-full" style={{ height }} preserveAspectRatio="none">
        {ticks.map((v) => (
          <g key={v}>
            <line x1={pad.l} x2={W - pad.r} y1={y(v)} y2={y(v)} className="stroke-slate-800" strokeWidth={1} />
            <text x={pad.l - 4} y={y(v) + 3} textAnchor="end" className="fill-slate-500" fontSize={9}>
              {v >= 100 ? v.toFixed(0) : v.toFixed(1)}
            </text>
          </g>
        ))}
        {refLine && (
          <g>
            <line x1={pad.l} x2={W - pad.r} y1={y(refLine.v)} y2={y(refLine.v)} stroke="#64748b" strokeDasharray="4 3" strokeWidth={1} />
            <text x={W - pad.r - 2} y={y(refLine.v) - 3} textAnchor="end" className="fill-slate-500" fontSize={9}>
              {refLine.label}
            </text>
          </g>
        )}
        {markers.map((m, i) => (
          <g key={i}>
            <line x1={x(m.t)} x2={x(m.t)} y1={pad.t} y2={pad.t + ih} stroke={m.color ?? "#f43f5e"} strokeWidth={1} strokeDasharray="2 2" />
            <text x={x(m.t) + 3} y={pad.t + 9} fill={m.color ?? "#f43f5e"} fontSize={9}>{m.label}</text>
          </g>
        ))}
        {series.map((s) => (
          <polyline
            key={s.label}
            fill="none"
            stroke={s.color}
            strokeWidth={1.6}
            vectorEffect="non-scaling-stroke"
            points={s.points.map((p) => `${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ")}
          />
        ))}
        <text x={pad.l} y={height - 4} className="fill-slate-500" fontSize={9}>{time(t0)}</text>
        <text x={W - pad.r} y={height - 4} textAnchor="end" className="fill-slate-500" fontSize={9}>{time(t1)}</text>
      </svg>
      <div className="mt-1 flex flex-wrap gap-3 px-1 text-[11px] text-slate-400">
        {series.map((s) => {
          const last = s.points.at(-1);
          return (
            <span key={s.label} className="flex items-center gap-1">
              <span className="inline-block h-0.5 w-3" style={{ background: s.color }} />
              {s.label}
              {last && <span className="font-mono text-slate-300">{last.v.toFixed(1)}{unit}</span>}
            </span>
          );
        })}
      </div>
    </div>
  );
}

export function Sparkline({ values, color = "#22d3ee", max, width = 90, height = 22 }: {
  values: number[]; color?: string; max?: number; width?: number; height?: number;
}) {
  if (values.length < 2) return <svg width={width} height={height} />;
  const m = max ?? Math.max(1, ...values);
  const pts = values
    .map((v, i) => `${((i / (values.length - 1)) * width).toFixed(1)},${(height - 1 - (Math.min(v, m) / m) * (height - 2)).toFixed(1)}`)
    .join(" ");
  return (
    <svg width={width} height={height} className="overflow-visible">
      <polyline fill="none" stroke={color} strokeWidth={1.4} points={pts} />
    </svg>
  );
}
