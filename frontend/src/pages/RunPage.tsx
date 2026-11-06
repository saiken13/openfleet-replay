import { useEffect, useReducer, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api, fetchWholeStream } from "../api/client";
import type { RunSummary } from "../api/types";
import { Badge, RunStatusBadge, SeverityBadge, StatusBadge } from "../components/Badges";
import { RedetectPanel } from "../components/RedetectPanel";
import { RobotView } from "../components/RobotView";
import { fmtDateTime, fmtDuration, fmtNum, fmtPct, fmtTime, shortId } from "../lib/format";
import { ReplayClock } from "../replay/ReplayClock";
import { emptyState, robotReducer } from "../state/robotReducer";

const SPEEDS = [1, 2, 4, 8, 16];

/** Replay a recorded run through the same reducer + view as the live robot page. */
export function RunPage() {
  const { runId = "" } = useParams();
  const [params] = useSearchParams();
  const [summary, setSummary] = useState<RunSummary | null>(null);
  const [state, dispatch] = useReducer(robotReducer, "", emptyState);
  const [pos, setPos] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(4);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const clock = useRef<ReplayClock | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    (async () => {
      try {
        const [s, stream] = await Promise.all([api.runSummary(runId), fetchWholeStream(runId)]);
        if (cancelled) return;
        setSummary(s);
        const robotId = stream.run.robot_id;
        dispatch({ type: "reset", state: emptyState(robotId) });
        const c = new ReplayClock(stream.frames, {
          onFrames: (frames) => dispatch({ type: "frames", frames }),
          onReset: () => dispatch({ type: "reset", state: emptyState(robotId) }),
          onTick: (p, isPlaying) => {
            setPos(p);
            setPlaying(isPlaying);
          },
        });
        c.speed = speed;
        clock.current = c;
        const startAt = params.get("t");
        c.seek(startAt ? Math.max(0, Date.parse(startAt) - c.t0) : 0);
        setLoading(false);
      } catch (e) {
        if (!cancelled) setError(String(e));
      }
    })();
    return () => {
      cancelled = true;
      clock.current?.dispose();
      clock.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId]);

  const c = clock.current;
  const run = summary?.run;
  // Incident markers on the scrubber (incident times are server time; close enough to
  // robot time for navigation, see ARCHITECTURE §4).
  const markers = c
    ? (summary?.incidents ?? []).map((i) => ({
        pct: c.durationMs ? ((Date.parse(i.started_at) - c.t0) / c.durationMs) * 100 : 0,
        i,
      }))
    : [];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <Link to="/runs" className="text-sm text-slate-400 hover:text-slate-200">Runs</Link>
        <span className="text-slate-600">/</span>
        <h1 className="text-xl font-semibold">Run {shortId(runId)}</h1>
        {run && <RunStatusBadge status={run.status} />}
        {run && (
          <span className="text-sm text-slate-400">
            <Link to={`/robots/${run.robot_id}`} className="text-cyan-400 hover:underline">{run.robot_id}</Link>
            {" · "}v{run.software_version ?? "?"} · {fmtDateTime(run.started_at)} · {fmtDuration(summary?.duration_s)}
          </span>
        )}
        {run && (
          <Link to={`/compare?candidate=${run.id}`} className="btn ml-auto">Compare…</Link>
        )}
      </div>
      {error && <div className="card border-rose-500/40 px-4 py-3 text-sm text-rose-300">{error}</div>}

      <div className="card sticky top-0 z-10 bg-slate-900/95 p-3 backdrop-blur">
        <div className="flex flex-wrap items-center gap-3">
          <Badge value="REPLAY" className="!bg-cyan-500/20 !text-cyan-200" />
          <button className="btn-primary w-20" disabled={!c || loading} onClick={() => (playing ? c?.pause() : c?.play())}>
            {playing ? "Pause" : "Play"}
          </button>
          <button className="btn" disabled={!c} onClick={() => c?.restart()}>Restart</button>
          <div className="flex overflow-hidden rounded-md border border-slate-700">
            {SPEEDS.map((s) => (
              <button
                key={s}
                className={`px-2 py-1 text-xs ${speed === s ? "bg-cyan-600 text-white" : "bg-slate-800 hover:bg-slate-700"}`}
                onClick={() => {
                  setSpeed(s);
                  c?.setSpeed(s);
                }}
              >
                {s}×
              </button>
            ))}
          </div>
          <span className="font-mono text-xs text-slate-400">
            {c ? `${fmtTime(new Date(c.t0 + pos).toISOString())}  (${fmtDuration(pos / 1000)} / ${fmtDuration(c.durationMs / 1000)})` : loading ? "loading…" : ""}
          </span>
          <StatusBadge status={state.status} />
        </div>
        <div className="relative mt-3">
          <input
            type="range"
            min={0}
            max={c?.durationMs ?? 0}
            step={250}
            value={pos}
            disabled={!c}
            onChange={(e) => c?.seek(Number(e.target.value))}
            className="w-full accent-cyan-500"
          />
          {markers.map(({ pct, i }) => (
            <button
              key={i.id}
              title={`${i.severity}: ${i.title}`}
              onClick={() => c?.seek(Date.parse(i.started_at) - c.t0 - 5000)}
              className={`absolute -top-1 h-2 w-2 -translate-x-1/2 rounded-full ${i.severity === "CRITICAL" ? "bg-rose-500" : "bg-amber-400"}`}
              style={{ left: `${Math.min(100, Math.max(0, pct))}%` }}
            />
          ))}
        </div>
      </div>

      <RobotView state={state} live={false} />

      {summary && <SummaryPanel s={summary} />}

      <RedetectPanel
        runId={runId}
        onSeek={(iso) => {
          c?.seek(Math.max(0, Date.parse(iso) - c.t0 - 5000));
          window.scrollTo({ top: 0, behavior: "smooth" });
        }}
      />
    </div>
  );
}

function SummaryPanel({ s }: { s: RunSummary }) {
  const m = s.metrics;
  return (
    <div className="grid gap-4 lg:grid-cols-3">
      <div className="card lg:col-span-2">
        <div className="card-h">Run summary · topics</div>
        <table className="w-full text-sm">
          <thead className="text-left text-xs text-slate-500">
            <tr>
              <th className="px-4 py-2">Topic</th>
              <th className="px-2 py-2 text-right">Avg Hz</th>
              <th className="px-2 py-2 text-right">p05 Hz</th>
              <th className="px-2 py-2 text-right">Expected</th>
              <th className="px-2 py-2 text-right">Degraded</th>
              <th className="px-4 py-2 text-right">Failed</th>
            </tr>
          </thead>
          <tbody>
            {s.topics.map((t) => (
              <tr key={t.topic} className="table-row font-mono text-xs">
                <td className="px-4 py-1.5">{t.topic}</td>
                <td className="px-2 py-1.5 text-right">{fmtNum(t.avg_hz)}</td>
                <td className="px-2 py-1.5 text-right">{fmtNum(t.p05_hz)}</td>
                <td className="px-2 py-1.5 text-right text-slate-500">{t.expected_hz ?? "—"}</td>
                <td className={`px-2 py-1.5 text-right ${t.degraded_pct ? "text-amber-300" : ""}`}>{fmtPct(t.degraded_pct)}</td>
                <td className={`px-4 py-1.5 text-right ${t.failed_pct ? "text-rose-300" : ""}`}>{fmtPct(t.failed_pct)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="grid grid-cols-2 gap-2 border-t border-slate-800 px-4 py-3 text-xs text-slate-400 sm:grid-cols-4">
          <span>samples <b className="text-slate-200">{m.samples}</b></span>
          <span>cpu avg/max <b className="text-slate-200">{fmtNum(m.cpu_avg, 0)}/{fmtNum(m.cpu_max, 0)}%</b></span>
          <span>mem max <b className="text-slate-200">{fmtNum(m.mem_max, 0)}%</b></span>
          <span>battery <b className="text-slate-200">{fmtNum(m.battery_start, 0)} → {fmtNum(m.battery_end, 0)}%</b></span>
        </div>
      </div>
      <div className="card">
        <div className="card-h">Incidents in this run</div>
        <ul className="divide-y divide-slate-800">
          {s.incidents.map((i) => (
            <li key={i.id} className="px-4 py-2 text-sm">
              <Link to={`/incidents/${i.id}`} className="flex items-start gap-2 hover:text-cyan-300">
                <SeverityBadge severity={i.severity} />
                <span>{i.title}</span>
              </Link>
            </li>
          ))}
          {!s.incidents.length && <li className="px-4 py-3 text-xs text-slate-500">None</li>}
        </ul>
      </div>
    </div>
  );
}
