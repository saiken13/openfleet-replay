import { Link } from "react-router-dom";
import type { FleetEvent } from "../api/types";
import { fmtNum, fmtTime } from "../lib/format";
import { type RobotViewState, openIncidents } from "../state/robotReducer";
import { Badge, Dot, SeverityBadge } from "./Badges";
import { LineChart, Sparkline } from "./LineChart";

/**
 * Renders a robot's state. Used unchanged by the live Robot page and the Run replay page:
 * it only knows reducer state, never where the frames came from.
 */
export function RobotView({ state, live }: { state: RobotViewState; live: boolean }) {
  const last = state.samples.at(-1);
  const t = (iso: string) => Date.parse(iso);
  const window = state.samples.slice(-180);
  const incidents = openIncidents(state);

  return (
    <div className="grid gap-4 lg:grid-cols-3">
      <div className="space-y-4 lg:col-span-2">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Tile label="CPU" value={fmtNum(last?.cpu_pct)} unit="%" warn={(last?.cpu_pct ?? 0) >= 90} />
          <Tile label="Memory" value={fmtNum(last?.mem_pct)} unit="%" warn={(last?.mem_pct ?? 0) >= 90} />
          <Tile label="Battery" value={fmtNum(last?.battery_pct)} unit="%" warn={(last?.battery_pct ?? 100) <= 25} />
          <Tile label="Latency" value={fmtNum(last?.net_latency_ms, 0)} unit="ms" />
        </div>

        <div className="card">
          <div className="card-h">Topic rates (Hz)</div>
          <div className="p-3">
            <LineChart
              height={170}
              series={Object.entries(state.topicHistory).map(([topic, pts], i) => ({
                label: topic,
                color: PALETTE[i % PALETTE.length],
                points: pts.slice(-180).map((p) => ({ t: t(p.ts), v: p.hz })),
              }))}
              unit=" Hz"
            />
          </div>
        </div>

        <div className="card">
          <div className="card-h">System metrics</div>
          <div className="p-3">
            <LineChart
              yMax={100}
              series={[
                { label: "cpu", color: "#22d3ee", points: window.map((s) => ({ t: t(s.ts), v: s.cpu_pct ?? 0 })) },
                { label: "mem", color: "#a78bfa", points: window.map((s) => ({ t: t(s.ts), v: s.mem_pct ?? 0 })) },
                { label: "battery", color: "#34d399", points: window.map((s) => ({ t: t(s.ts), v: s.battery_pct ?? 0 })) },
              ]}
              unit="%"
            />
          </div>
        </div>

        <div className="card">
          <div className="card-h">Topics</div>
          <table className="w-full text-sm">
            <thead className="text-left text-xs text-slate-500">
              <tr>
                <th className="px-4 py-2">Topic</th>
                <th className="px-2 py-2 text-right">Actual</th>
                <th className="px-2 py-2 text-right">Expected</th>
                <th className="px-2 py-2">Trend</th>
                <th className="px-4 py-2">Status</th>
              </tr>
            </thead>
            <tbody>
              {Object.values(state.topics).map((tp) => (
                <tr key={tp.topic} className="table-row">
                  <td className="px-4 py-1.5 font-mono text-xs">{tp.topic}</td>
                  <td className="px-2 py-1.5 text-right font-mono text-xs">{tp.actual_hz.toFixed(1)}</td>
                  <td className="px-2 py-1.5 text-right font-mono text-xs text-slate-500">{tp.expected_hz ?? "—"}</td>
                  <td className="px-2 py-1.5">
                    <Sparkline
                      values={(state.topicHistory[tp.topic] ?? []).slice(-60).map((p) => p.hz)}
                      max={(tp.expected_hz ?? 0) * 1.2 || undefined}
                      color={tp.status === "HEALTHY" ? "#34d399" : tp.status === "DEGRADED" ? "#fbbf24" : "#f43f5e"}
                    />
                  </td>
                  <td className="px-4 py-1.5"><Badge value={tp.status} /></td>
                </tr>
              ))}
              {!Object.keys(state.topics).length && (
                <tr><td colSpan={5} className="px-4 py-3 text-xs text-slate-500">No topic data yet.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      <div className="space-y-4">
        <div className="card">
          <div className="card-h">Open incidents</div>
          <ul className="divide-y divide-slate-800">
            {incidents.map((i) => (
              <li key={i.id} className="flex items-start gap-2 px-4 py-2 text-sm">
                <SeverityBadge severity={i.severity} />
                {live ? (
                  <Link to={`/incidents/${i.id}`} className="hover:text-cyan-300">{i.title}</Link>
                ) : (
                  <span>{i.title}</span>
                )}
              </li>
            ))}
            {!incidents.length && <li className="px-4 py-3 text-xs text-slate-500">None</li>}
          </ul>
        </div>

        <div className="card">
          <div className="card-h">Nodes</div>
          <ul className="divide-y divide-slate-800">
            {Object.values(state.nodes).map((n) => (
              <li key={n.node} className="flex items-center gap-2 px-4 py-1.5 text-sm">
                <Dot value={n.state} />
                <span className="font-mono text-xs">{n.node}</span>
                <span className="ml-auto text-[11px] text-slate-500">{n.state}</span>
              </li>
            ))}
            {!Object.keys(state.nodes).length && <li className="px-4 py-3 text-xs text-slate-500">No node data yet.</li>}
          </ul>
        </div>

        <div className="card">
          <div className="card-h">Events</div>
          <EventList events={state.events.slice(0, 60)} />
        </div>
      </div>
    </div>
  );
}

const PALETTE = ["#22d3ee", "#a78bfa", "#34d399", "#fbbf24", "#f472b6", "#60a5fa"];

function Tile({ label, value, unit, warn }: { label: string; value: string; unit: string; warn?: boolean }) {
  return (
    <div className={`card px-4 py-3 ${warn ? "border-rose-500/50" : ""}`}>
      <div className="text-[11px] uppercase tracking-wide text-slate-500">{label}</div>
      <div className={`mt-1 font-mono text-xl ${warn ? "text-rose-300" : "text-slate-100"}`}>
        {value}
        <span className="ml-0.5 text-xs text-slate-500">{unit}</span>
      </div>
    </div>
  );
}

const SEV_TEXT = { INFO: "text-slate-400", WARNING: "text-amber-300", ERROR: "text-rose-300" } as const;

export function EventList({ events, highlight }: { events: FleetEvent[]; highlight?: Set<number> }) {
  if (!events.length) return <div className="px-4 py-3 text-xs text-slate-500">No events.</div>;
  return (
    <ul className="max-h-[420px] divide-y divide-slate-800/60 overflow-y-auto">
      {events.map((e) => (
        <li key={e.id} className={`px-4 py-1.5 text-xs ${highlight?.has(e.id) ? "bg-rose-500/10" : ""}`}>
          <div className="flex items-center gap-2">
            <span className="font-mono text-slate-500">{fmtTime(e.ts)}</span>
            <span className={`font-semibold ${SEV_TEXT[e.severity]}`}>{e.type}</span>
            <span className="truncate font-mono text-slate-500">{e.source}</span>
          </div>
          <div className="mt-0.5 text-slate-300">{e.message}</div>
        </li>
      ))}
    </ul>
  );
}
