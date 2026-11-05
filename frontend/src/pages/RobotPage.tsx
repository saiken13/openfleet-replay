import { useCallback, useEffect, useReducer, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api/client";
import type { Run, ServerMessage } from "../api/types";
import { RunStatusBadge, StatusBadge } from "../components/Badges";
import { RobotView } from "../components/RobotView";
import { useLiveSocket, useNow } from "../hooks/useLiveSocket";
import { fmtAgo, fmtDateTime, shortId } from "../lib/format";
import { emptyState, robotReducer, stateFromSnapshot } from "../state/robotReducer";
import { useConnection } from "../App";

export function RobotPage() {
  const { robotId = "" } = useParams();
  const [state, dispatch] = useReducer(robotReducer, robotId, emptyState);
  const [runs, setRuns] = useState<Run[]>([]);
  const [error, setError] = useState<string | null>(null);
  const now = useNow();

  // Load current state over REST on mount and on every WS (re)connect, then apply deltas.
  const load = useCallback(async () => {
    try {
      const [robot, topics, nodes, samples, events, incidents, runPage, topicSamples] = await Promise.all([
        api.robot(robotId),
        api.topics(robotId),
        api.nodes(robotId),
        api.telemetry(robotId, 180),
        api.events(robotId, 200),
        api.incidents({ status: "open", robot_id: robotId }),
        api.runs({ robot_id: robotId, limit: 10 }),
        api.topicHistory(robotId, 180),
      ]);
      dispatch({
        type: "reset",
        state: stateFromSnapshot({ robot, topics, nodes, samples, events, incidents, topicSamples }),
      });
      setRuns(runPage.items);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, [robotId]);

  useEffect(() => {
    dispatch({ type: "reset", state: emptyState(robotId) });
    void load();
  }, [robotId, load]);

  const onMessage = useCallback((m: ServerMessage) => {
    if (m.type === "frame") dispatch({ type: "frame", frame: m.data });
    else if (m.type === "robot") dispatch({ type: "robot", robot: m.data });
    else if (m.type === "incident") dispatch({ type: "incident", incident: m.data });
  }, []);
  useConnection(useLiveSocket(`/ws/robots/${robotId}`, onMessage, load));

  const r = state.robot;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <Link to="/" className="text-sm text-slate-400 hover:text-slate-200">Fleet</Link>
        <span className="text-slate-600">/</span>
        <h1 className="text-xl font-semibold">{r?.name ?? robotId}</h1>
        <StatusBadge status={state.status} />
        <span className="font-mono text-xs text-slate-500">
          {robotId} · {r?.model ?? ""} · v{r?.software_version ?? "?"} · seen {fmtAgo(r?.last_seen_at, now)}
        </span>
        {r?.current_run_id && (
          <Link to={`/runs/${r.current_run_id}`} className="ml-auto text-sm text-cyan-400 hover:underline">
            current run {shortId(r.current_run_id)} →
          </Link>
        )}
      </div>
      {error && <div className="card border-rose-500/40 px-4 py-3 text-sm text-rose-300">{error}</div>}

      <RobotView state={state} live />

      <div className="card">
        <div className="card-h">Recent runs</div>
        <table className="w-full text-sm">
          <tbody>
            {runs.map((run) => (
              <tr key={run.id} className="table-row">
                <td className="px-4 py-2 font-mono text-xs">
                  <Link to={`/runs/${run.id}`} className="text-cyan-400 hover:underline">{shortId(run.id)}</Link>
                </td>
                <td className="px-2 py-2"><RunStatusBadge status={run.status} /></td>
                <td className="px-2 py-2 text-xs text-slate-400">v{run.software_version ?? "?"}</td>
                <td className="px-4 py-2 text-right text-xs text-slate-500">{fmtDateTime(run.started_at)}</td>
              </tr>
            ))}
            {!runs.length && <tr><td className="px-4 py-3 text-xs text-slate-500">No runs.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}
