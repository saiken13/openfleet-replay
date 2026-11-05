import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import type { Incident } from "../api/types";
import { Badge, SeverityBadge } from "../components/Badges";
import { fmtDateTime, fmtDuration } from "../lib/format";

type Filter = "open" | "resolved" | "all";

export function IncidentsPage() {
  const [params, setParams] = useSearchParams();
  const filter = (params.get("status") as Filter) ?? "open";
  const [rows, setRows] = useState<Incident[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setRows(null);
    api.incidents({ status: filter, limit: 300 }).then(setRows, (e) => setError(String(e)));
  }, [filter]);

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-4">
        <h1 className="text-xl font-semibold">Incidents</h1>
        <div className="flex overflow-hidden rounded-md border border-slate-700 text-sm">
          {(["open", "resolved", "all"] as Filter[]).map((f) => (
            <button
              key={f}
              onClick={() => setParams({ status: f })}
              className={`px-3 py-1 ${filter === f ? "bg-cyan-600 text-white" : "bg-slate-800 hover:bg-slate-700"}`}
            >
              {f}
            </button>
          ))}
        </div>
      </div>
      {error && <div className="card px-4 py-3 text-sm text-rose-300">{error}</div>}
      <div className="card overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-left text-xs text-slate-500">
            <tr>
              <th className="px-4 py-2">Severity</th>
              <th className="px-2 py-2">Incident</th>
              <th className="px-2 py-2">Robot</th>
              <th className="px-2 py-2">Version</th>
              <th className="px-2 py-2">Status</th>
              <th className="px-2 py-2">Started</th>
              <th className="px-4 py-2 text-right">Duration</th>
            </tr>
          </thead>
          <tbody>
            {rows?.map((i) => (
              <tr key={i.id} className="table-row hover:bg-slate-800/40">
                <td className="px-4 py-2"><SeverityBadge severity={i.severity} /></td>
                <td className="px-2 py-2">
                  <Link to={`/incidents/${i.id}`} className="hover:text-cyan-300">{i.title}</Link>
                  <div className="font-mono text-[11px] text-slate-500">{i.dedup_key}</div>
                </td>
                <td className="px-2 py-2 font-mono text-xs">
                  <Link to={`/robots/${i.robot_id}`} className="text-cyan-400 hover:underline">{i.robot_id}</Link>
                </td>
                <td className="px-2 py-2 text-xs text-slate-400">{i.software_version ?? "—"}</td>
                <td className="px-2 py-2"><Badge value={i.status} /></td>
                <td className="px-2 py-2 text-xs text-slate-400">{fmtDateTime(i.started_at)}</td>
                <td className="px-4 py-2 text-right text-xs text-slate-400">
                  {fmtDuration(((i.resolved_at ? Date.parse(i.resolved_at) : Date.now()) - Date.parse(i.started_at)) / 1000)}
                </td>
              </tr>
            ))}
            {rows && !rows.length && (
              <tr><td colSpan={7} className="px-4 py-6 text-center text-xs text-slate-500">No {filter} incidents.</td></tr>
            )}
            {!rows && !error && (
              <tr><td colSpan={7} className="px-4 py-6 text-center text-xs text-slate-500">Loading…</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
