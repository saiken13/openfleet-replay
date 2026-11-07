import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api/client";
import type { IncidentTimeline } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { Badge, SeverityBadge } from "../components/Badges";
import { LineChart } from "../components/LineChart";
import { EventList } from "../components/RobotView";
import { canTriage } from "../lib/auth";
import { fmtDateTime, fmtDuration } from "../lib/format";

const PALETTE = ["#22d3ee", "#a78bfa", "#34d399", "#fbbf24", "#f472b6", "#60a5fa"];

export function IncidentPage() {
  const { incidentId = "" } = useParams();
  const [tl, setTl] = useState<IncidentTimeline | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const { user } = useAuth();
  const operator = canTriage(user);

  const load = useCallback(() => api.timeline(incidentId).then(setTl, (e) => setError(String(e))), [incidentId]);
  useEffect(() => void load(), [load]);

  if (error) return <div className="card px-4 py-3 text-sm text-rose-300">{error}</div>;
  if (!tl) return <div className="text-sm text-slate-500">Loading…</div>;

  const inc = tl.incident;
  const opened = tl.events.find((e) => e.type === "incident_opened" && e.payload.incident_id === inc.id);
  const resolved = tl.events.find((e) => e.type === "incident_resolved" && e.payload.incident_id === inc.id);
  const markers = [
    { t: Date.parse(opened?.ts ?? inc.started_at), label: "opened", color: "#f43f5e" },
    ...(resolved ? [{ t: Date.parse(resolved.ts), label: "resolved", color: "#34d399" }] : []),
  ];
  // "What changed first?": the earliest warning/error event in the lookback window.
  // Only robot-side signals count (not OpenFleet's own incident/status bookkeeping).
  const firstSignal = tl.events.find(
    (e) =>
      e.severity !== "INFO" &&
      !e.type.startsWith("incident_") &&
      e.type !== "robot_status_changed" &&
      Date.parse(e.ts) <= markers[0].t,
  );
  const byTopic = tl.topic_samples.reduce<Record<string, { t: number; v: number }[]>>((acc, s) => {
    (acc[s.topic] ??= []).push({ t: Date.parse(s.ts), v: s.actual_hz });
    return acc;
  }, {});
  const focusTopic = typeof inc.evidence.topic === "string" ? inc.evidence.topic : null;
  const expected = focusTopic ? tl.topic_samples.find((s) => s.topic === focusTopic)?.expected_hz : null;
  const range: [number, number] = [Date.parse(tl.window_start), Date.parse(tl.window_end)];

  const setStatus = async (status: "INVESTIGATING" | "RESOLVED") => {
    setBusy(true);
    try {
      await api.setIncidentStatus(inc.id, status);
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <Link to="/incidents" className="text-sm text-slate-400 hover:text-slate-200">Incidents</Link>
        <span className="text-slate-600">/</span>
        <SeverityBadge severity={inc.severity} />
        <h1 className="text-xl font-semibold">{inc.title}</h1>
        <Badge value={inc.status} />
        <div className="ml-auto flex gap-2">
          {operator && inc.status === "OPEN" && (
            <button className="btn" disabled={busy} onClick={() => setStatus("INVESTIGATING")}>Investigate</button>
          )}
          {operator && inc.status !== "RESOLVED" && (
            <button className="btn" disabled={busy} onClick={() => setStatus("RESOLVED")}>Resolve</button>
          )}
          {!operator && inc.status !== "RESOLVED" && (
            <span className="self-center text-xs text-slate-500" title="Triage needs the operator role">
              View only
            </span>
          )}
          {inc.run_id && (
            <Link
              className="btn-primary"
              to={`/runs/${inc.run_id}?t=${encodeURIComponent(new Date(Date.parse(inc.started_at) - 15000).toISOString())}`}
            >
              Replay from 15s before ▶
            </Link>
          )}
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <div className="card space-y-1 p-4 text-sm lg:col-span-1">
          <Row k="Robot"><Link to={`/robots/${inc.robot_id}`} className="text-cyan-400 hover:underline">{inc.robot_id}</Link></Row>
          <Row k="Rule"><span className="font-mono text-xs">{inc.rule}</span></Row>
          <Row k="Dedup key"><span className="font-mono text-xs">{inc.dedup_key}</span></Row>
          <Row k="Software">{inc.software_version ?? "—"}</Row>
          <Row k="Started">{fmtDateTime(inc.started_at)}</Row>
          <Row k="Last detected">{fmtDateTime(inc.last_detected_at)}</Row>
          <Row k="Resolved">{fmtDateTime(inc.resolved_at)}</Row>
          <Row k="Triage">
            <Link to={`/triage?incident_id=${inc.id}`} className="text-cyan-400 hover:underline">history</Link>
          </Row>
          <Row k="Duration">
            {fmtDuration(((inc.resolved_at ? Date.parse(inc.resolved_at) : Date.now()) - Date.parse(inc.started_at)) / 1000)}
          </Row>
          <p className="pt-2 text-slate-300">{inc.description}</p>
          {firstSignal && (
            <div className="mt-3 rounded border border-amber-500/30 bg-amber-500/5 p-2 text-xs">
              <div className="font-semibold text-amber-300">First warning signal in the lead-up</div>
              <div className="mt-1 text-slate-300">
                {fmtDateTime(firstSignal.ts)} · <span className="font-mono">{firstSignal.source}</span>: {firstSignal.message}
              </div>
            </div>
          )}
          <details className="pt-2">
            <summary className="cursor-pointer text-xs text-slate-500">Evidence (rule snapshot)</summary>
            <pre className="mt-1 overflow-x-auto rounded bg-slate-950 p-2 text-[11px] text-slate-300">
              {JSON.stringify(inc.evidence, null, 2)}
            </pre>
          </details>
        </div>

        <div className="space-y-4 lg:col-span-2">
          <div className="card">
            <div className="card-h">Topic rates around the incident</div>
            <div className="p-3">
              <LineChart
                height={180}
                xRange={range}
                markers={markers}
                refLine={expected ? { v: expected, label: `${focusTopic} expected ${expected} Hz` } : undefined}
                series={Object.entries(byTopic)
                  .sort(([a], [b]) => (a === focusTopic ? -1 : b === focusTopic ? 1 : a.localeCompare(b)))
                  .map(([topic, points], i) => ({ label: topic, color: PALETTE[i % PALETTE.length], points }))}
                unit=" Hz"
              />
            </div>
          </div>
          <div className="card">
            <div className="card-h">System metrics around the incident</div>
            <div className="p-3">
              <LineChart
                xRange={range}
                yMax={100}
                markers={markers}
                series={[
                  { label: "cpu", color: "#22d3ee", points: tl.samples.map((s) => ({ t: Date.parse(s.ts), v: s.cpu_pct ?? 0 })) },
                  { label: "mem", color: "#a78bfa", points: tl.samples.map((s) => ({ t: Date.parse(s.ts), v: s.mem_pct ?? 0 })) },
                  { label: "battery", color: "#34d399", points: tl.samples.map((s) => ({ t: Date.parse(s.ts), v: s.battery_pct ?? 0 })) },
                ]}
                unit="%"
              />
            </div>
          </div>
        </div>
      </div>

      <div className="card">
        <div className="card-h">
          Timeline · {fmtDateTime(tl.window_start)} → {fmtDateTime(tl.window_end)} ({tl.events.length} events)
        </div>
        <EventList events={tl.events} highlight={new Set([opened?.id, resolved?.id, firstSignal?.id].filter((x): x is number => !!x))} />
      </div>
    </div>
  );
}

function Row({ k, children }: { k: string; children: React.ReactNode }) {
  return (
    <div className="flex justify-between gap-4">
      <span className="text-slate-500">{k}</span>
      <span className="text-right">{children}</span>
    </div>
  );
}
