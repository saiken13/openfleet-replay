"""Collector configuration (YAML). Example: collector/collector.example.yaml."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class CollectorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_url: str = "http://localhost:8000"
    robot_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    robot_name: str | None = None
    robot_model: str | None = None
    software_version: str | None = None

    window_s: float = Field(1.0, gt=0, le=10, description="aggregation window = batch period")
    startup_grace_s: float = Field(
        5.0, ge=0, description="before reporting not-yet-discovered topics/nodes as missing"
    )
    # topic -> expected Hz (null = monitor the rate, no expectation)
    topics: dict[str, float | None] = {}
    # ROS node names to watch (as shown by `ros2 node list`, with or without leading '/')
    nodes: list[str] = []
    battery_topic: str | None = Field(None, description="sensor_msgs/BatteryState topic")
    forward_log_level: Literal["DEBUG", "INFO", "WARN", "ERROR", "FATAL"] = "WARN"

    buffer_max_batches: int = Field(600, ge=10, description="batches kept while offline")
    request_timeout_s: float = 5.0

    def node_names(self) -> list[str]:
        return [n if n.startswith("/") else f"/{n}" for n in self.nodes]


def load_config(path: str | Path) -> CollectorConfig:
    return CollectorConfig.model_validate(yaml.safe_load(Path(path).read_text()) or {})
