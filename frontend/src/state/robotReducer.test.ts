import { describe, expect, it } from "vitest";
import type { FleetEvent, Frame, Incident, Robot } from "../api/types";
import { MAX_SAMPLES, emptyState, openIncidents, robotReducer } from "./robotReducer";

const T = (s: number) => new Date(Date.UTC(2026, 10, 5, 12, 0, s)).toISOString();

function sample(seq: number, cpu = 30): Frame {
  return {
    kind: "sample",
    ts: T(seq),
    data: {
      id: seq + 1, run_id: "run", robot_id: "r1", seq, ts: T(seq), received_at: T(seq),
      cpu_pct: cpu, mem_pct: 40, battery_pct: 90, net_latency_ms: 10, pose_x: 0, pose_y: 0, pose_yaw: 0,
    },
  };
}

function topic(seq: number, hz: number, status: "HEALTHY" | "DEGRADED" | "FAILED"): Frame {
  return {
    kind: "topic",
    ts: T(seq),
    data: {
      id: 1000 + seq, run_id: "run", robot_id: "r1", seq, ts: T(seq), topic: "/scan",
      expected_hz: 15, actual_hz: hz, last_msg_age_s: 0.1, status,
    },
  };
}

function event(id: number, type: string, payload: Record<string, unknown>, message = ""): Frame {
  const e: FleetEvent = {
    id, robot_id: "r1", run_id: "run", ts: T(id), received_at: T(id), type,
    severity: "INFO", source: "openfleet", message, payload,
  };
  return { kind: "event", ts: e.ts, data: e };
}

describe("robotReducer", () => {
  it("applies samples, topics and events from frames", () => {
    const s = robotReducer(emptyState("r1"), {
      type: "frames",
      frames: [
        sample(0),
        topic(0, 15, "HEALTHY"),
        sample(1, 55),
        topic(1, 4, "DEGRADED"),
        event(2, "node_state_changed", { node: "lidar_driver", from: "RUNNING", to: "ERROR" }),
        event(3, "robot_status_changed", { from: "ONLINE", to: "DEGRADED" }),
      ],
    });
    expect(s.samples.map((x) => x.cpu_pct)).toEqual([30, 55]);
    expect(s.topics["/scan"].status).toBe("DEGRADED");
    expect(s.topicHistory["/scan"].map((p) => p.hz)).toEqual([15, 4]);
    expect(s.nodes["lidar_driver"].state).toBe("ERROR");
    expect(s.status).toBe("DEGRADED");
    expect(s.events.map((e) => e.id)).toEqual([3, 2]); // newest first
    expect(s.lastTs).toBe(T(3));
  });

  it("tracks incidents from events alone (replay has no incident rows)", () => {
    const s = robotReducer(emptyState("r1"), {
      type: "frames",
      frames: [
        event(1, "incident_opened", { incident_id: "i1", rule: "topic_rate", severity: "WARNING" }, "Incident opened: /scan degraded on r1"),
        event(2, "incident_escalated", { incident_id: "i1", from: "WARNING", to: "CRITICAL" }, "Incident escalated WARNING → CRITICAL: /scan stopped on r1"),
      ],
    });
    expect(openIncidents(s)).toEqual([
      expect.objectContaining({ id: "i1", severity: "CRITICAL", status: "OPEN", title: "/scan stopped on r1" }),
    ]);
    const resolved = robotReducer(s, {
      type: "frame",
      frame: event(3, "incident_resolved", { incident_id: "i1", reason: "condition cleared" }),
    });
    expect(openIncidents(resolved)).toEqual([]);
  });

  it("ignores duplicate events (REST snapshot + WS overlap)", () => {
    const f = event(7, "log", {}, "hello");
    const s = robotReducer(robotReducer(emptyState("r1"), { type: "frame", frame: f }), { type: "frame", frame: f });
    expect(s.events).toHaveLength(1);
  });

  it("caps the sample buffer", () => {
    const frames = Array.from({ length: MAX_SAMPLES + 50 }, (_, i) => sample(i));
    const s = robotReducer(emptyState("r1"), { type: "frames", frames });
    expect(s.samples).toHaveLength(MAX_SAMPLES);
    expect(s.samples[0].seq).toBe(50);
  });

  it("applies robot and incident messages only for its own robot", () => {
    const robot = { id: "r1", status: "ERROR" } as Robot;
    const other = { id: "r2", status: "ONLINE" } as Robot;
    let s = robotReducer(emptyState("r1"), { type: "robot", robot });
    s = robotReducer(s, { type: "robot", robot: other });
    expect(s.status).toBe("ERROR");
    const inc = { id: "i9", robot_id: "r2", severity: "CRITICAL", status: "OPEN", title: "x", rule: "r", started_at: T(0) } as Incident;
    expect(robotReducer(s, { type: "incident", incident: inc }).incidents).toEqual({});
  });

  it("is pure: does not mutate the previous state", () => {
    const s0 = emptyState("r1");
    const snapshot = JSON.stringify(s0);
    robotReducer(s0, { type: "frames", frames: [sample(0), topic(0, 15, "HEALTHY")] });
    expect(JSON.stringify(s0)).toBe(snapshot);
  });
});
