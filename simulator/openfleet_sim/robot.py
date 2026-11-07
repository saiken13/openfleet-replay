"""One simulated robot. `step(t)` is deterministic for a given seed: same scenario, same
batches (except for wall-clock timestamps, which the caller supplies)."""

import math
import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from openfleet_sim.scenario import (
    BatteryDrainFailure,
    CpuFailure,
    LogFailure,
    MemoryFailure,
    NodeStateFailure,
    OfflineFailure,
    Scenario,
    TelemetryStallFailure,
    TopicRateFailure,
)

BASE_DRAIN_PCT_PER_S = 0.02


@dataclass
class SimRobot:
    index: int
    robot_id: str
    scenario: Scenario
    rng: random.Random = field(init=False)
    battery: float = field(init=False)
    seq: int = 0
    silent_since: dict[str, float] = field(default_factory=dict)  # topic -> t it hit 0 Hz
    started_failures: set[int] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.rng = random.Random(self.scenario.seed * 1000 + self.index)
        self.battery = 95.0 - 6.0 * (self.index % 5)

    def _active(self, t: float) -> list[Any]:
        return [f for f in self.scenario.failures if f.applies_to(self.index) and f.active(t)]

    def is_offline(self, t: float) -> bool:
        return any(isinstance(f, OfflineFailure) for f in self._active(t))

    def is_stalled(self, t: float) -> bool:
        """Heartbeating, but sending no telemetry."""
        return not self.is_offline(t) and any(
            isinstance(f, TelemetryStallFailure) for f in self._active(t)
        )

    def step(self, t: float, ts: datetime) -> dict[str, Any] | None:
        """Advance to scenario time t (seconds since run start); `ts` is the robot-clock
        timestamp to stamp the batch with. Returns the telemetry batch, or None while the
        robot is offline (it sends nothing) or stalled (it only heartbeats)."""
        sc = self.scenario
        active = self._active(t)

        drain = BASE_DRAIN_PCT_PER_S + sum(
            f.pct_per_s for f in active if isinstance(f, BatteryDrainFailure)
        )
        self.battery = max(0.0, self.battery - drain * sc.tick_s)

        if self.is_offline(t) or self.is_stalled(t):
            return None

        r = self.rng
        cpu = next((f.pct for f in active if isinstance(f, CpuFailure)), None)
        mem = next((f.pct for f in active if isinstance(f, MemoryFailure)), None)
        metrics = {
            "cpu_pct": _clamp(
                (cpu if cpu is not None else 35 + 8 * math.sin(t / 20)) + r.gauss(0, 2)
            ),
            "mem_pct": _clamp((mem if mem is not None else 45 + t / 60) + r.gauss(0, 1)),
            "battery_pct": round(self.battery, 2),
            "net_latency_ms": round(max(1.0, r.gauss(18, 4)), 1),
        }

        topics = []
        for topic, expected in sc.topics.items():
            override = next(
                (f.hz for f in active if isinstance(f, TopicRateFailure) and f.topic == topic),
                None,
            )
            nominal = expected if override is None else override
            if nominal <= 0:
                self.silent_since.setdefault(topic, t)
                actual, age = 0.0, round(t - self.silent_since[topic] + 1.0, 2)
            else:
                self.silent_since.pop(topic, None)
                actual = round(max(0.0, nominal * r.gauss(1.0, 0.03)), 2)
                age = round((1.0 / actual) * r.uniform(0.5, 1.5), 3) if actual else None
            topics.append(
                {
                    "topic": topic,
                    "expected_hz": expected,
                    "actual_hz": actual,
                    "last_msg_age_s": age,
                }
            )

        nodes = []
        for node in sc.nodes:
            state = next(
                (f.state for f in active if isinstance(f, NodeStateFailure) and f.node == node),
                "RUNNING",
            )
            nodes.append({"node": node, "state": state})

        logs = []
        for i, f in enumerate(sc.failures):
            if not f.applies_to(self.index) or i in self.started_failures or t < f.at_s:
                continue
            self.started_failures.add(i)
            if isinstance(f, LogFailure):
                logs.append(_log(ts, f.log_level, f.source, f.message))
            elif f.log:
                source = getattr(f, "node", None) or getattr(f, "topic", None) or "system"
                logs.append(_log(ts, f.log_level, source, f.log))

        angle = t / 30.0 + self.index
        batch = {
            "seq": self.seq,
            "ts": ts.isoformat(),
            "metrics": metrics,
            "pose": {
                "x": round(5 * math.cos(angle) + 3 * self.index, 3),
                "y": round(5 * math.sin(angle), 3),
                "yaw": round(angle + math.pi / 2, 3),
            },
            "topics": topics,
            "nodes": nodes,
            "logs": logs,
        }
        self.seq += 1
        return batch


def _clamp(v: float) -> float:
    return round(min(100.0, max(0.0, v)), 2)


def _log(ts: datetime, level: str, source: str, message: str) -> dict[str, Any]:
    return {"ts": ts.isoformat(), "level": level, "source": source, "message": message}
