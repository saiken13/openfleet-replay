import type {
  FleetEvent,
  Incident,
  IncidentStatus,
  IncidentTimeline,
  NodeStateRow,
  RedetectOverrides,
  RedetectResult,
  Robot,
  Run,
  RunComparison,
  RunPage,
  RunStream,
  RunSummary,
  Sample,
  TopicSample,
  TopicState,
  TriagePage,
} from "./types";

import { CLIENT_HEADER, notifyUnauthorized, type User } from "../lib/auth";

const BASE = "/api/v1";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(BASE + path, {
    ...init,
    credentials: "same-origin", // the HttpOnly session cookie
    headers: { "content-type": "application/json", [CLIENT_HEADER]: "dashboard", ...(init?.headers ?? {}) },
  });
  // A wrong password is just a failed login; any other 401 means the session is gone.
  if (resp.status === 401 && path !== "/auth/login") notifyUnauthorized();
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const body = await resp.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* not JSON */
    }
    throw new ApiError(resp.status, detail);
  }
  if (resp.status === 204) return undefined as T;
  return (await resp.json()) as T;
}

function qs(params: Record<string, string | number | undefined | null>): string {
  const entries = Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "");
  return entries.length ? "?" + new URLSearchParams(entries.map(([k, v]) => [k, String(v)])) : "";
}

export const api = {
  auth: {
    config: () => request<{ login_hint: string | null }>("/auth/config"),
    me: () => request<User>("/auth/me"),
    login: (username: string, password: string) =>
      request<User>("/auth/login", { method: "POST", body: JSON.stringify({ username, password }) }),
    logout: () => request<void>("/auth/logout", { method: "POST" }),
  },

  robots: () => request<Robot[]>("/robots"),
  robot: (id: string) => request<Robot>(`/robots/${id}`),
  topics: (id: string) => request<TopicState[]>(`/robots/${id}/topics`),
  nodes: (id: string) => request<NodeStateRow[]>(`/robots/${id}/nodes`),
  events: (id: string, limit = 200) => request<FleetEvent[]>(`/robots/${id}/events${qs({ limit })}`),
  topicHistory: (id: string, seconds = 180) =>
    request<TopicSample[]>(`/robots/${id}/topic-history${qs({ seconds })}`),
  telemetry: (id: string, seconds = 180) =>
    request<Sample[]>(`/robots/${id}/telemetry${qs({ seconds })}`),

  runs: (params: { robot_id?: string; limit?: number; cursor?: string | null } = {}) =>
    request<RunPage>(`/runs${qs(params)}`),
  run: (id: string) => request<Run>(`/runs/${id}`),
  runSummary: (id: string) => request<RunSummary>(`/runs/${id}/summary`),
  runStream: (id: string, cursor?: string | null, limit = 2000) =>
    request<RunStream>(`/runs/${id}/stream${qs({ cursor, limit })}`),
  redetect: (id: string, overrides: RedetectOverrides = {}) =>
    request<RedetectResult>(`/runs/${id}/redetect`, { method: "POST", body: JSON.stringify(overrides) }),
  compare: (base: string, candidate: string) =>
    request<RunComparison>(`/runs/compare${qs({ base, candidate })}`),

  incidents: (
    params: { status?: "open" | "resolved" | "all"; robot_id?: string; run_id?: string; limit?: number } = {},
  ) => request<Incident[]>(`/incidents${qs(params)}`),
  incident: (id: string) => request<Incident>(`/incidents/${id}`),
  timeline: (id: string) => request<IncidentTimeline>(`/incidents/${id}/timeline`),
  setIncidentStatus: (id: string, status: Exclude<IncidentStatus, "OPEN">) =>
    request<Incident>(`/incidents/${id}`, { method: "PATCH", body: JSON.stringify({ status }) }),
  triage: (
    params: { actor?: string; robot_id?: string; incident_id?: string; limit?: number; cursor?: string | null } = {},
  ) => request<TriagePage>(`/triage${qs(params)}`),
};

/** Fetch every page of a run's replay stream. */
export async function fetchWholeStream(runId: string): Promise<RunStream> {
  const first = await api.runStream(runId);
  const frames = [...first.frames];
  let cursor = first.next_cursor;
  while (cursor) {
    const page = await api.runStream(runId, cursor);
    frames.push(...page.frames);
    cursor = page.next_cursor;
  }
  return { run: first.run, frames, next_cursor: null };
}

export function wsUrl(path: string): string {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${window.location.host}${BASE}${path}`;
}
