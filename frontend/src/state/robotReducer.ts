/**
 * The single reducer for a robot's view (ARCHITECTURE §7, DECISIONS #008).
 *
 * Both the live WebSocket and the ReplayClock dispatch the same `frame` actions, so the
 * dashboard cannot tell live data from replay data and we don't build two UIs.
 * Pure: no I/O, no clock.
 */
import type {
  FleetEvent,
  Frame,
  Incident,
  NodeState,
  NodeStateRow,
  Robot,
  RobotStatus,
  Sample,
  Severity,
  TopicSample,
  TopicState,
  TopicStatus,
} from "../api/types";

export const MAX_SAMPLES = 600;
export const MAX_TOPIC_POINTS = 300;
export const MAX_EVENTS = 300;

export interface TopicView {
  topic: string;
  expected_hz: number | null;
  actual_hz: number;
  last_msg_age_s: number | null;
  status: TopicStatus;
  ts: string;
}

export interface NodeView {
  node: string;
  state: NodeState;
  since: string;
}

/** What we know about an incident: full rows from REST/WS, or a sketch built from events. */
export interface IncidentView {
  id: string;
  title: string;
  severity: Severity;
  status: "OPEN" | "INVESTIGATING" | "RESOLVED";
  rule?: string;
  started_at?: string;
}

export interface RobotViewState {
  robotId: string;
  robot: Robot | null;
  status: RobotStatus | null;
  topics: Record<string, TopicView>;
  topicHistory: Record<string, { ts: string; hz: number }[]>;
  nodes: Record<string, NodeView>;
  samples: Sample[]; // oldest first
  events: FleetEvent[]; // newest first
  incidents: Record<string, IncidentView>;
  lastTs: string | null; // robot-clock time of the latest frame applied
}

export type RobotAction =
  | { type: "reset"; state: RobotViewState }
  | { type: "frame"; frame: Frame }
  | { type: "frames"; frames: Frame[] }
  | { type: "robot"; robot: Robot }
  | { type: "incident"; incident: Incident };

export function emptyState(robotId: string): RobotViewState {
  return {
    robotId,
    robot: null,
    status: null,
    topics: {},
    topicHistory: {},
    nodes: {},
    samples: [],
    events: [],
    incidents: {},
    lastTs: null,
  };
}

/** Initial live state from REST (ARCHITECTURE §8: load over REST, then apply WS deltas). */
export function stateFromSnapshot(snapshot: {
  robot: Robot;
  topics: TopicState[];
  nodes: NodeStateRow[];
  samples: Sample[];
  events: FleetEvent[];
  incidents: Incident[];
  topicSamples?: TopicSample[];
}): RobotViewState {
  const s = emptyState(snapshot.robot.id);
  s.robot = snapshot.robot;
  s.status = snapshot.robot.status;
  for (const t of snapshot.topics) {
    s.topics[t.topic] = {
      topic: t.topic,
      expected_hz: t.expected_hz,
      actual_hz: t.actual_hz,
      last_msg_age_s: t.last_msg_age_s,
      status: t.status,
      ts: t.updated_at,
    };
  }
  for (const n of snapshot.nodes) s.nodes[n.node] = { node: n.node, state: n.state, since: n.state_since };
  s.samples = snapshot.samples.slice(-MAX_SAMPLES);
  s.events = snapshot.events.slice(0, MAX_EVENTS);
  for (const i of snapshot.incidents) s.incidents[i.id] = incidentView(i);
  for (const t of snapshot.topicSamples ?? []) {
    (s.topicHistory[t.topic] ??= []).push({ ts: t.ts, hz: t.actual_hz });
  }
  for (const h of Object.values(s.topicHistory)) h.splice(0, Math.max(0, h.length - MAX_TOPIC_POINTS));
  s.lastTs = s.samples.at(-1)?.ts ?? null;
  return s;
}

function incidentView(i: Incident): IncidentView {
  return {
    id: i.id,
    title: i.title,
    severity: i.severity,
    status: i.status,
    rule: i.rule,
    started_at: i.started_at,
  };
}

function later(a: string | null, b: string): string {
  return a === null || b > a ? b : a;
}

function applyFrame(s: RobotViewState, frame: Frame): RobotViewState {
  switch (frame.kind) {
    case "sample": {
      const samples = [...s.samples, frame.data];
      if (samples.length > MAX_SAMPLES) samples.splice(0, samples.length - MAX_SAMPLES);
      return { ...s, samples, lastTs: later(s.lastTs, frame.ts) };
    }
    case "topic": {
      const t = frame.data;
      const history = [...(s.topicHistory[t.topic] ?? []), { ts: t.ts, hz: t.actual_hz }];
      if (history.length > MAX_TOPIC_POINTS) history.splice(0, history.length - MAX_TOPIC_POINTS);
      return {
        ...s,
        topics: {
          ...s.topics,
          [t.topic]: {
            topic: t.topic,
            expected_hz: t.expected_hz,
            actual_hz: t.actual_hz,
            last_msg_age_s: t.last_msg_age_s,
            status: t.status,
            ts: t.ts,
          },
        },
        topicHistory: { ...s.topicHistory, [t.topic]: history },
        lastTs: later(s.lastTs, frame.ts),
      };
    }
    case "event":
      return applyEvent({ ...s, lastTs: later(s.lastTs, frame.ts) }, frame.data);
  }
}

function applyEvent(s: RobotViewState, e: FleetEvent): RobotViewState {
  if (s.events.some((x) => x.id === e.id)) return s; // WS can repeat what REST already loaded
  const events = [e, ...s.events];
  if (events.length > MAX_EVENTS) events.length = MAX_EVENTS;
  let next: RobotViewState = { ...s, events };
  const p = e.payload as Record<string, string | undefined>;

  switch (e.type) {
    case "node_state_changed":
      if (p.node && p.to) {
        next = { ...next, nodes: { ...next.nodes, [p.node]: { node: p.node, state: p.to as NodeState, since: e.ts } } };
      }
      break;
    case "robot_status_changed":
      if (p.to) next = { ...next, status: p.to as RobotStatus };
      break;
    case "incident_opened":
    case "incident_escalated":
    case "incident_resolved": {
      const id = p.incident_id;
      if (!id) break;
      const prev = next.incidents[id];
      const severity = (e.type === "incident_opened" ? p.severity : e.type === "incident_escalated" ? p.to : prev?.severity) as Severity;
      // Messages look like "Incident opened: <title>" / "Incident escalated A → B: <title>".
      const fromMessage = e.message.includes(": ") ? e.message.slice(e.message.indexOf(": ") + 2) : e.message;
      const title = e.type === "incident_resolved" ? (prev?.title ?? fromMessage) : fromMessage;
      next = {
        ...next,
        incidents: {
          ...next.incidents,
          [id]: {
            id,
            title,
            severity: severity ?? "WARNING",
            status: e.type === "incident_resolved" ? "RESOLVED" : (prev?.status ?? "OPEN"),
            rule: prev?.rule ?? p.rule,
            started_at: prev?.started_at ?? e.ts,
          },
        },
      };
      break;
    }
  }
  return next;
}

export function robotReducer(s: RobotViewState, a: RobotAction): RobotViewState {
  switch (a.type) {
    case "reset":
      return a.state;
    case "frame":
      return applyFrame(s, a.frame);
    case "frames":
      return a.frames.reduce(applyFrame, s);
    case "robot":
      return a.robot.id === s.robotId ? { ...s, robot: a.robot, status: a.robot.status } : s;
    case "incident":
      if (a.incident.robot_id !== s.robotId) return s;
      return { ...s, incidents: { ...s.incidents, [a.incident.id]: incidentView(a.incident) } };
  }
}

export function openIncidents(s: RobotViewState): IncidentView[] {
  const rank = { CRITICAL: 0, WARNING: 1, INFO: 2 } as const;
  return Object.values(s.incidents)
    .filter((i) => i.status !== "RESOLVED")
    .sort((a, b) => rank[a.severity] - rank[b.severity]);
}
