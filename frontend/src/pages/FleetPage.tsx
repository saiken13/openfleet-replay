import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { Incident, Robot, ServerMessage } from "../api/types";
import { SeverityBadge, StatusBadge } from "../components/Badges";
import { useLiveSocket, useNow } from "../hooks/useLiveSocket";
import { fmtAgo, fmtNum } from "../lib/format";
import { useConnection } from "../App";

export function FleetPage() {
  const [robots, setRobots] = useState<Record<string, Robot>>({});
  const [incidents, setIncidents] = useState<Record<string, Incident>>({});
  const [error, setError] = useState<string | null>(null);
  const now = useNow();

  const load = useCallback(async () => {
    try {
      const [rs, is] = await Promise.all([api.robots(), api.incidents({ status: "open", limit: 200 })]);
      setRobots(Object.fromEntries(rs.map((r) => [r.id, r])));
      setIncidents(Object.fromEntries(is.map((i) => [i.id, i])));
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => void load(), [load]);

  const onMessage = useCallback((m: ServerMessage) => {
    if (m.type === "robot") setRobots((rs) => ({ ...rs, [m.data.id]: m.data }));
    if (m.type === "incident")
      setIncidents((is) => {
        const next = { ...is };
        if (m.data.status === "RESOLVED") delete next[m.data.id];
        else next[m.data.id] = m.data;
        return next;
      });
  }, []);
  useConnection(useLiveSocket("/ws/fleet", onMessage, load));

  const list = Object.values(robots).sort((a, b) => a.id.localeCompare(b.id));
  const open = Object.values(incidents).sort((a, b) => b.started_at.localeCompare(a.started_at));
  const counts = list.reduce<Record<string, number>>((acc, r) => ({ ...acc, [r.status]: (acc[r.status] ?? 0) + 1 }), {});

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Fleet</h1>
          <p className="text-sm text-slate-400">
            {list.length} robots · {(["ONLINE", "DEGRADED", "ERROR", "OFFLINE"] as const).map((s) => `${counts[s] ?? 0} ${s.toLowerCase()}`).join(" · ")}
          </p>
        </div>
      </div>
      {error && <div className="card border-rose-500/40 px-4 py-3 text-sm text-rose-300">{error}</div>}

      {!list.length && !error && (
        <div className="card px-6 py-10 text-center text-sm text-slate-400">
          No robots yet. Start the simulator:
          <pre className="mt-3 inline-block rounded bg-slate-950 px-3 py-2 text-left text-xs text-cyan-300">
            docker compose --profile demo up
          </pre>
        </div>
      )}

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {list.map((r) => (
          <Link key={r.id} to={`/robots/${r.id}`} className="card block p-4 transition hover:border-cyan-700">
            <div className="flex items-center justify-between">
              <div className="font-semibold">{r.name}</div>
              <StatusBadge status={r.status} />
            </div>
            <div className="mt-1 font-mono text-xs text-slate-500">
              {r.id} · v{r.software_version ?? "?"}
            </div>
            <div className="mt-3 space-y-1.5">
              <Bar label="CPU" v={r.cpu_pct} warn={90} />
              <Bar label="MEM" v={r.mem_pct} warn={90} />
              <Bar label="BAT" v={r.battery_pct} warn={25} low />
            </div>
            <div className="mt-3 flex justify-between text-[11px] text-slate-500">
              <span>{r.current_run_id ? "● in run" : "idle"}</span>
              <span>seen {fmtAgo(r.last_seen_at, now)}</span>
            </div>
          </Link>
        ))}
      </div>

      <div className="card">
        <div className="card-h flex items-center justify-between">
          <span>Open incidents ({open.length})</span>
          <Link to="/incidents" className="normal-case text-cyan-400 hover:underline">all incidents →</Link>
        </div>
        <ul className="divide-y divide-slate-800">
          {open.map((i) => (
            <li key={i.id}>
              <Link to={`/incidents/${i.id}`} className="flex items-center gap-3 px-4 py-2 text-sm hover:bg-slate-800/40">
                <SeverityBadge severity={i.severity} />
                <span className="flex-1">{i.title}</span>
                <span className="text-xs text-slate-500">{i.status}</span>
                <span className="w-20 text-right text-xs text-slate-500">{fmtAgo(i.started_at, now)}</span>
              </Link>
            </li>
          ))}
          {!open.length && <li className="px-4 py-3 text-xs text-slate-500">All clear.</li>}
        </ul>
      </div>
    </div>
  );
}

function Bar({ label, v, warn, low }: { label: string; v: number | null; warn: number; low?: boolean }) {
  const bad = v !== null && (low ? v <= warn : v >= warn);
  return (
    <div className="flex items-center gap-2 text-[11px]">
      <span className="w-8 text-slate-500">{label}</span>
      <div className="h-1.5 flex-1 overflow-hidden rounded bg-slate-800">
        <div className={`h-full ${bad ? "bg-rose-500" : "bg-cyan-500"}`} style={{ width: `${Math.min(100, v ?? 0)}%` }} />
      </div>
      <span className="w-10 text-right font-mono text-slate-400">{fmtNum(v, 0)}%</span>
    </div>
  );
}
