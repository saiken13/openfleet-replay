"""Scenario files (YAML). A scenario describes a fleet, its topics and nodes, and a timeline
of scripted failures, so every demo and CI run is reproducible."""

from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FailureBase(_Strict):
    robots: list[int] | Literal["all"] = "all"  # robot indices (0-based)
    at_s: float = Field(ge=0)
    duration_s: float | None = Field(None, gt=0)  # None = until the run ends
    log: str | None = None  # optional log line emitted when the failure starts
    log_level: Literal["INFO", "WARN", "ERROR", "FATAL"] = "ERROR"

    def active(self, t: float) -> bool:
        return self.at_s <= t and (self.duration_s is None or t < self.at_s + self.duration_s)

    def applies_to(self, index: int) -> bool:
        return self.robots == "all" or index in self.robots


class TopicRateFailure(FailureBase):
    kind: Literal["topic_rate"]
    topic: str
    hz: float = Field(ge=0)


class NodeStateFailure(FailureBase):
    kind: Literal["node_state"]
    node: str
    state: Literal["DEGRADED", "STOPPED", "ERROR"]


class CpuFailure(FailureBase):
    kind: Literal["cpu"]
    pct: float = Field(ge=0, le=100)


class MemoryFailure(FailureBase):
    kind: Literal["memory"]
    pct: float = Field(ge=0, le=100)


class BatteryDrainFailure(FailureBase):
    kind: Literal["battery_drain"]
    pct_per_s: float = Field(gt=0)


class OfflineFailure(FailureBase):
    kind: Literal["offline"]  # the robot stops sending anything


class TelemetryStallFailure(FailureBase):
    # No telemetry batches, but heartbeats continue: the robot's uplink is alive while its
    # data pipeline (e.g. the ROS graph) is not. Re-detection must not call this "silent".
    kind: Literal["telemetry_stall"]


class LogFailure(FailureBase):
    kind: Literal["log"]  # a single log line at at_s
    source: str = "system"
    message: str


Failure = Annotated[
    TopicRateFailure
    | NodeStateFailure
    | CpuFailure
    | MemoryFailure
    | BatteryDrainFailure
    | OfflineFailure
    | TelemetryStallFailure
    | LogFailure,
    Field(discriminator="kind"),
]


class Fleet(_Strict):
    count: int = Field(3, ge=1, le=200)
    id_prefix: str = Field("amr", pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,40}$")
    model: str = "AMR-Sim"


class Scenario(_Strict):
    name: str
    description: str = ""
    seed: int = 42
    duration_s: float = Field(120, gt=0)
    tick_s: float = Field(1.0, gt=0)
    software_version: str = "1.0.0"
    outcome: Literal["SUCCESS", "FAILED"] = "SUCCESS"
    fleet: Fleet = Fleet()
    topics: dict[str, float] = Field(
        default_factory=lambda: {"/scan": 15, "/odom": 50, "/camera/image_raw": 30, "/cmd_vel": 10}
    )
    nodes: list[str] = Field(
        default_factory=lambda: ["lidar_driver", "nav2_controller", "localization", "camera_driver"]
    )
    failures: list[Failure] = []

    @model_validator(mode="after")
    def _check_references(self) -> "Scenario":
        for f in self.failures:
            if isinstance(f, TopicRateFailure) and f.topic not in self.topics:
                raise ValueError(f"failure references unknown topic {f.topic!r}")
            if isinstance(f, NodeStateFailure) and f.node not in self.nodes:
                raise ValueError(f"failure references unknown node {f.node!r}")
            if f.robots != "all" and any(i >= self.fleet.count for i in f.robots):
                raise ValueError(f"failure targets robot index outside fleet of {self.fleet.count}")
        return self

    def robot_ids(self) -> list[str]:
        return [f"{self.fleet.id_prefix}-{i + 1:02d}" for i in range(self.fleet.count)]


def load_scenario(path: str | Path) -> Scenario:
    return Scenario.model_validate(yaml.safe_load(Path(path).read_text()))
