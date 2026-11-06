// Mirrors backend/app/schemas/*. Timestamps are ISO-8601 strings.

export type RobotStatus = "ONLINE" | "OFFLINE" | "DEGRADED" | "ERROR";
export type RunStatus = "RUNNING" | "SUCCESS" | "FAILED" | "ABORTED";
export type TopicStatus = "HEALTHY" | "DEGRADED" | "FAILED" | "UNKNOWN";
export type NodeState = "RUNNING" | "DEGRADED" | "STOPPED" | "ERROR";
export type Severity = "INFO" | "WARNING" | "CRITICAL";
export type EventSeverity = "INFO" | "WARNING" | "ERROR";
export type IncidentStatus = "OPEN" | "INVESTIGATING" | "RESOLVED";

export interface Robot {
  id: string;
  name: string;
  model: string | null;
  created_at: string;
  status: RobotStatus;
  software_version: string | null;
  last_seen_at: string | null;
  current_run_id: string | null;
  battery_pct: number | null;
  cpu_pct: number | null;
  mem_pct: number | null;
  updated_at: string;
}

export interface Run {
  id: string;
  robot_id: string;
  software_version: string | null;
  status: RunStatus;
  started_at: string;
  ended_at: string | null;
}

export interface RunPage {
  items: Run[];
  next_cursor: string | null;
}

export interface Sample {
  id: number;
  run_id: string;
  robot_id: string;
  seq: number;
  ts: string;
  received_at: string;
  cpu_pct: number | null;
  mem_pct: number | null;
  battery_pct: number | null;
  net_latency_ms: number | null;
  pose_x: number | null;
  pose_y: number | null;
  pose_yaw: number | null;
}

export interface TopicSample {
  id: number;
  run_id: string;
  robot_id: string;
  seq: number;
  ts: string;
  topic: string;
  expected_hz: number | null;
  actual_hz: number;
  last_msg_age_s: number | null;
  status: TopicStatus;
}

export interface TopicState {
  robot_id: string;
  topic: string;
  expected_hz: number | null;
  actual_hz: number;
  last_msg_age_s: number | null;
  status: TopicStatus;
  status_since: string;
  updated_at: string;
}

export interface NodeStateRow {
  robot_id: string;
  node: string;
  state: NodeState;
  state_since: string;
  updated_at: string;
}

export interface FleetEvent {
  id: number;
  robot_id: string;
  run_id: string | null;
  ts: string;
  received_at: string;
  type: string;
  severity: EventSeverity;
  source: string;
  message: string;
  payload: Record<string, unknown>;
}

export interface Incident {
  id: string;
  robot_id: string;
  run_id: string | null;
  rule: string;
  dedup_key: string;
  severity: Severity;
  status: IncidentStatus;
  title: string;
  description: string;
  software_version: string | null;
  evidence: Record<string, unknown>;
  started_at: string;
  last_detected_at: string;
  resolved_at: string | null;
  created_at: string;
  updated_at: string;
}

export type Frame =
  | { kind: "sample"; ts: string; data: Sample }
  | { kind: "topic"; ts: string; data: TopicSample }
  | { kind: "event"; ts: string; data: FleetEvent };

export type ServerMessage =
  | { type: "hello"; channel: string }
  | { type: "robot"; data: Robot }
  | { type: "incident"; data: Incident }
  | { type: "frame"; data: Frame };

export interface IncidentTimeline {
  incident: Incident;
  window_start: string;
  window_end: string;
  events: FleetEvent[];
  samples: Sample[];
  topic_samples: TopicSample[];
}

export interface RunStream {
  run: Run;
  frames: Frame[];
  next_cursor: string | null;
}

export interface TopicSummary {
  topic: string;
  expected_hz: number | null;
  samples: number;
  avg_hz: number;
  min_hz: number;
  max_hz: number;
  p05_hz: number;
  healthy_pct: number;
  degraded_pct: number;
  failed_pct: number;
}

export interface RunSummary {
  run: Run;
  duration_s: number | null;
  metrics: {
    samples: number;
    cpu_avg: number | null;
    cpu_max: number | null;
    mem_avg: number | null;
    mem_max: number | null;
    battery_start: number | null;
    battery_end: number | null;
    battery_min: number | null;
    latency_avg: number | null;
  };
  topics: TopicSummary[];
  incidents_by_severity: Record<Severity, number>;
  incidents: Incident[];
  events_by_type: Record<string, number>;
}

export interface RunComparison {
  base: RunSummary;
  candidate: RunSummary;
  topics: {
    topic: string;
    base_avg_hz: number | null;
    candidate_avg_hz: number | null;
    avg_hz_delta_pct: number | null;
    base_failed_pct: number | null;
    candidate_failed_pct: number | null;
  }[];
  metrics_delta: Record<string, number | null>;
  incidents_delta: Record<Severity, number>;
  notes: string[];
}

/** Re-detection replay (DECISIONS #023). Thresholds by name, as in the server settings. */
export const REDETECT_THRESHOLDS = [
  "heartbeat_timeout_s",
  "topic_degraded_ratio",
  "topic_failed_ratio",
  "topic_stale_after_s",
  "cpu_high_pct",
  "mem_high_pct",
  "battery_low_pct",
  "battery_critical_pct",
  "incident_resolve_after_s",
] as const;
export type RedetectThreshold = (typeof REDETECT_THRESHOLDS)[number];
export type RedetectOverrides = Partial<Record<RedetectThreshold, number>>;

export type RedetectVerdict = "both" | "replay_only" | "recorded_only";

export interface RedetectIncident {
  rule: string;
  dedup_key: string;
  severity: Severity;
  initial_severity: Severity;
  title: string;
  description: string;
  evidence: Record<string, unknown>;
  opened_at: string; // robot time
  opened_offset_s: number;
  last_detected_at: string;
  escalated_at: string | null;
  resolved_at: string | null;
  resolve_reason: string | null;
  detections: number;
  recorded_incident_id: string | null;
}

export interface RedetectKey {
  dedup_key: string;
  rule: string;
  verdict: RedetectVerdict;
  recorded_count: number;
  replayed_count: number;
  recorded_severity: Severity | null;
  replayed_severity: Severity | null;
  first_recorded_offset_s: number | null;
  first_replayed_offset_s: number | null;
}

export interface RedetectResult {
  run: Run;
  config: Record<RedetectThreshold, number>;
  overrides: RedetectOverrides;
  batches: number;
  incidents: RedetectIncident[];
  recorded: Incident[];
  keys: RedetectKey[];
  caught: number;
  missed: number;
  new: number;
  notes: string[];
}
