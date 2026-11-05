import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import type { Run, RunComparison, RunSummary } from "../api/types";
import { RunStatusBadge } from "../components/Badges";
import { fmtDateTime, fmtDuration, fmtNum, fmtPct, shortId } from "../lib/format";

export function ComparePage() {
  const [params, setParams] = useSearchParams();
  const base = params.get("base") ?? "";
  const candidate = params.get("candidate") ?? "";
  const [runs, setRuns] = useState<Run[]>([]);
  const [cmp, setCmp] = useState<RunComparison | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.runs({ limit: 100 }).then((p) => setRuns(p.items), (e) => setError(String(e)));
  }, []);
  useEffect(() => {
    setCmp(null);
    if (!base || !candidate) return;
    api.compare(base, candidate).then(setCmp, (e) => setError(String(e)));
  }, [base, candidate]);

  const pick = (key: "base" | "candidate", id: string) => {
    const next = new URLSearchParams(params);
    next.set(key, id);
    setParams(next);
  };

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Compare runs</h1>
      <div className="grid gap-3 sm:grid-cols-2">
        <RunPicker label="Base" value={base} runs={runs} onChange={(id) => pick("base", id)} />
        <RunPicker label="Candidate" value={candidate} runs={runs} onChange={(id) => pick("candidate", id)} />
      </div>
      {error && <div className="card px-4 py-3 text-sm text-rose-300">{error}</div>}
      {!cmp && base && candidate && !error && <div className="text-sm text-slate-500">Loading…</div>}
      {cmp && <Comparison cmp={cmp} />}
    </div>
  );
}

function RunPicker({ label, value, runs, onChange }: { label: string; value: string; runs: Run[]; onChange: (id: string) => void }) {
  return (
    <label className="card block p-3 text-sm">
      <span className="text-xs uppercase tracking-wide text-slate-500">{label}</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="mt-1 w-full rounded border border-slate-700 bg-slate-950 px-2 py-1.5 font-mono text-xs"
      >
        <option value="">select a run…</option>
        {runs.map((r) => (
          <option key={r.id} value={r.id}>
            {shortId(r.id)} · {r.robot_id} · v{r.software_version ?? "?"} · {r.status} · {fmtDateTime(r.started_at)}
          </option>
        ))}
      </select>
    </label>
  );
}

function delta(v: number | null, digits = 1, invert = false) {
  if (v === null || Math.abs(v) < 1e-9) return <span className="text-slate-500">±0</span>;
  const bad = invert ? v > 0 : v < 0;
  return <span className={bad ? "text-rose-300" : "text-emerald-300"}>{v > 0 ? "+" : ""}{v.toFixed(digits)}</span>;
}

function Comparison({ cmp }: { cmp: RunComparison }) {
  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2">
        <Header s={cmp.base} label="Base" />
        <Header s={cmp.candidate} label="Candidate" />
      </div>
      {cmp.notes.map((n) => (
        <div key={n} className="rounded border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs text-amber-200">{n}</div>
      ))}

      <div className="card overflow-x-auto">
        <div className="card-h">Topics</div>
        <table className="w-full text-sm">
          <thead className="text-left text-xs text-slate-500">
            <tr>
              <th className="px-4 py-2">Topic</th>
              <th className="px-2 py-2 text-right">Base avg Hz</th>
              <th className="px-2 py-2 text-right">Candidate avg Hz</th>
              <th className="px-2 py-2 text-right">Δ %</th>
              <th className="px-2 py-2 text-right">Base failed</th>
              <th className="px-4 py-2 text-right">Candidate failed</th>
            </tr>
          </thead>
          <tbody className="font-mono text-xs">
            {cmp.topics.map((t) => (
              <tr key={t.topic} className="table-row">
                <td className="px-4 py-1.5">{t.topic}</td>
                <td className="px-2 py-1.5 text-right">{fmtNum(t.base_avg_hz)}</td>
                <td className="px-2 py-1.5 text-right">{fmtNum(t.candidate_avg_hz)}</td>
                <td className="px-2 py-1.5 text-right">{delta(t.avg_hz_delta_pct)}{t.avg_hz_delta_pct !== null && "%"}</td>
                <td className="px-2 py-1.5 text-right">{fmtPct(t.base_failed_pct)}</td>
                <td className="px-4 py-1.5 text-right">{fmtPct(t.candidate_failed_pct)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <div className="card">
          <div className="card-h">Metrics Δ (candidate − base)</div>
          <ul className="divide-y divide-slate-800 font-mono text-xs">
            {Object.entries(cmp.metrics_delta).map(([k, v]) => (
              <li key={k} className="flex justify-between px-4 py-1.5">
                <span>{k}</span>
                {delta(v, 1, k !== "battery_min")}
              </li>
            ))}
          </ul>
        </div>
        <div className="card">
          <div className="card-h">Incidents Δ</div>
          <ul className="divide-y divide-slate-800 font-mono text-xs">
            {Object.entries(cmp.incidents_delta).map(([k, v]) => (
              <li key={k} className="flex justify-between px-4 py-1.5">
                <span>{k}</span>
                <span>
                  {cmp.base.incidents_by_severity[k as keyof typeof cmp.base.incidents_by_severity]} →{" "}
                  {cmp.candidate.incidents_by_severity[k as keyof typeof cmp.candidate.incidents_by_severity]} ({delta(v, 0, true)})
                </span>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  );
}

function Header({ s, label }: { s: RunSummary; label: string }) {
  return (
    <div className="card p-4 text-sm">
      <div className="flex items-center gap-2">
        <span className="text-xs uppercase tracking-wide text-slate-500">{label}</span>
        <Link to={`/runs/${s.run.id}`} className="font-mono text-cyan-400 hover:underline">{shortId(s.run.id)}</Link>
        <RunStatusBadge status={s.run.status} />
      </div>
      <div className="mt-1 text-slate-300">
        {s.run.robot_id} · <b>v{s.run.software_version ?? "?"}</b> · {fmtDuration(s.duration_s)} · {s.metrics.samples} samples
      </div>
    </div>
  );
}
