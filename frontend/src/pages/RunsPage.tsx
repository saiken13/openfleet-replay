import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { Run } from "../api/types";
import { RunStatusBadge } from "../components/Badges";
import { fmtDateTime, fmtDuration, shortId } from "../lib/format";

export function RunsPage() {
  const [runs, setRuns] = useState<Run[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const navigate = useNavigate();

  const load = useCallback(async (from: string | null) => {
    try {
      const page = await api.runs({ limit: 50, cursor: from });
      setRuns((prev) => (from ? [...prev, ...page.items] : page.items));
      setCursor(page.next_cursor);
    } catch (e) {
      setError(String(e));
    }
  }, []);
  useEffect(() => void load(null), [load]);

  const toggle = (id: string) =>
    setSelected((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id].slice(-2)));

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-4">
        <h1 className="text-xl font-semibold">Runs</h1>
        <span className="text-sm text-slate-400">Select two runs to compare (older = base).</span>
        <button
          className="btn-primary ml-auto"
          disabled={selected.length !== 2}
          onClick={() => {
            const [a, b] = selected.map((id) => runs.find((r) => r.id === id)!).sort((x, y) => x.started_at.localeCompare(y.started_at));
            navigate(`/compare?base=${a.id}&candidate=${b.id}`);
          }}
        >
          Compare {selected.length}/2
        </button>
      </div>
      {error && <div className="card px-4 py-3 text-sm text-rose-300">{error}</div>}
      <div className="card overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-left text-xs text-slate-500">
            <tr>
              <th className="w-8 px-4 py-2" />
              <th className="px-2 py-2">Run</th>
              <th className="px-2 py-2">Robot</th>
              <th className="px-2 py-2">Version</th>
              <th className="px-2 py-2">Status</th>
              <th className="px-2 py-2">Started</th>
              <th className="px-4 py-2 text-right">Duration</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((r) => (
              <tr key={r.id} className="table-row hover:bg-slate-800/40">
                <td className="px-4 py-2">
                  <input type="checkbox" className="accent-cyan-500" checked={selected.includes(r.id)} onChange={() => toggle(r.id)} />
                </td>
                <td className="px-2 py-2 font-mono text-xs">
                  <Link to={`/runs/${r.id}`} className="text-cyan-400 hover:underline">{shortId(r.id)}</Link>
                </td>
                <td className="px-2 py-2 font-mono text-xs">{r.robot_id}</td>
                <td className="px-2 py-2 text-xs">{r.software_version ?? "—"}</td>
                <td className="px-2 py-2"><RunStatusBadge status={r.status} /></td>
                <td className="px-2 py-2 text-xs text-slate-400">{fmtDateTime(r.started_at)}</td>
                <td className="px-4 py-2 text-right text-xs text-slate-400">
                  {r.ended_at ? fmtDuration((Date.parse(r.ended_at) - Date.parse(r.started_at)) / 1000) : "running"}
                </td>
              </tr>
            ))}
            {!runs.length && <tr><td colSpan={7} className="px-4 py-6 text-center text-xs text-slate-500">No runs yet.</td></tr>}
          </tbody>
        </table>
        {cursor && (
          <div className="border-t border-slate-800 p-3 text-center">
            <button className="btn" onClick={() => load(cursor)}>Load more</button>
          </div>
        )}
      </div>
    </div>
  );
}
