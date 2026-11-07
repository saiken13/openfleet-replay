import { useCallback, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import type { TriageAction } from "../api/types";
import { Badge } from "../components/Badges";
import { fmtDateTime } from "../lib/format";

const FILTERS = ["actor", "robot_id", "incident_id"] as const;
type Filter = (typeof FILTERS)[number];

/** Triage audit log (DECISIONS #026): who changed which incident's status, and when. */
export function TriagePage() {
  const [params, setParams] = useSearchParams();
  const filters = Object.fromEntries(FILTERS.map((k) => [k, params.get(k) ?? ""])) as Record<Filter, string>;
  const [draft, setDraft] = useState({ actor: filters.actor, robot_id: filters.robot_id });
  const [rows, setRows] = useState<TriageAction[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const { actor, robot_id, incident_id } = filters;
  const load = useCallback(
    async (from: string | null) => {
      try {
        setError(null);
        const page = await api.triage({ actor, robot_id, incident_id, limit: 50, cursor: from });
        setRows((prev) => (from ? [...prev, ...page.items] : page.items));
        setCursor(page.next_cursor);
      } catch (e) {
        setError(String(e));
      }
    },
    [actor, robot_id, incident_id],
  );
  useEffect(() => void load(null), [load]);
  useEffect(() => setDraft({ actor, robot_id }), [actor, robot_id]);

  const apply = (next: Partial<Record<Filter, string>>) => {
    const merged = { ...filters, ...next };
    setParams(Object.fromEntries(Object.entries(merged).filter(([, v]) => v.trim() !== "")));
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-semibold">Triage audit</h1>
        <span className="text-sm text-slate-400">Every manual incident status change, newest first.</span>
      </div>
      <form
        className="card flex flex-wrap items-end gap-3 p-3 text-sm"
        onSubmit={(e) => {
          e.preventDefault();
          apply(draft);
        }}
      >
        <label className="flex flex-col gap-1 text-xs text-slate-400">
          Operator
          <input
            className="input"
            placeholder="username"
            value={draft.actor}
            onChange={(e) => setDraft({ ...draft, actor: e.target.value })}
          />
        </label>
        <label className="flex flex-col gap-1 text-xs text-slate-400">
          Robot
          <input
            className="input"
            placeholder="robot id"
            value={draft.robot_id}
            onChange={(e) => setDraft({ ...draft, robot_id: e.target.value })}
          />
        </label>
        <button className="btn" type="submit">Filter</button>
        {incident_id && (
          <span className="flex items-center gap-2 text-xs text-slate-400">
            incident <Link to={`/incidents/${incident_id}`} className="font-mono text-cyan-400 hover:underline">{incident_id.slice(0, 8)}</Link>
            <button type="button" className="text-slate-500 hover:text-slate-200" onClick={() => apply({ incident_id: "" })}>
              ✕
            </button>
          </span>
        )}
        {(actor || robot_id || incident_id) && (
          <button type="button" className="ml-auto text-xs text-slate-400 hover:text-slate-200" onClick={() => setParams({})}>
            Clear filters
          </button>
        )}
      </form>
      {error && <div className="card px-4 py-3 text-sm text-rose-300">{error}</div>}
      <div className="card overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-left text-xs text-slate-500">
            <tr>
              <th className="px-4 py-2">When</th>
              <th className="px-2 py-2">Operator</th>
              <th className="px-2 py-2">Change</th>
              <th className="px-2 py-2">Incident</th>
              <th className="px-4 py-2">Robot</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((a) => (
              <tr key={a.id} className="table-row hover:bg-slate-800/40">
                <td className="whitespace-nowrap px-4 py-2 text-xs text-slate-400">{fmtDateTime(a.at)}</td>
                <td className="px-2 py-2 text-xs">
                  {a.actor ? (
                    <button className="text-cyan-400 hover:underline" onClick={() => apply({ actor: a.actor ?? "" })}>
                      {a.actor}
                    </button>
                  ) : (
                    <span className="text-slate-500" title="Recorded before the audit log existed">unknown</span>
                  )}
                </td>
                <td className="whitespace-nowrap px-2 py-2">
                  {a.from_status ? <Badge value={a.from_status} /> : <span className="text-xs text-slate-500">?</span>}
                  <span className="px-1.5 text-slate-500">→</span>
                  <Badge value={a.to_status} />
                </td>
                <td className="px-2 py-2">
                  <Link to={`/incidents/${a.incident_id}`} className="text-cyan-400 hover:underline">{a.incident_title}</Link>
                </td>
                <td className="px-4 py-2 font-mono text-xs">
                  <Link to={`/robots/${a.robot_id}`} className="hover:underline">{a.robot_id}</Link>
                </td>
              </tr>
            ))}
            {!rows.length && !error && (
              <tr><td colSpan={5} className="px-4 py-6 text-center text-xs text-slate-500">No triage actions{actor || robot_id || incident_id ? " match these filters" : " yet"}.</td></tr>
            )}
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
