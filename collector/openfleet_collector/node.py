"""The ROS 2 node. Thin glue: everything it computes is done by the pure modules
(rates, batch) and delivered by the uplink, which are unit-tested without ROS.

Requires a sourced ROS 2 environment (rclpy, rosidl_runtime_py, rcl_interfaces,
sensor_msgs). Tested on Jazzy; uses only APIs also available on Humble.
"""

import logging
import math
import time
from datetime import datetime, timezone

import psutil
import rclpy
import rclpy.executors
from rcl_interfaces.msg import Log
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, qos_profile_sensor_data
from rosidl_runtime_py.utilities import get_message

from openfleet_collector.batch import LogBuffer, build_batch, node_states
from openfleet_collector.config import CollectorConfig
from openfleet_collector.diagnostics import DiagnosticsTracker, merge_nodes
from openfleet_collector.rates import RateTracker
from openfleet_collector.uplink import Uplink

log = logging.getLogger(__name__)

# BEST_EFFORT subscriptions match both best-effort and reliable publishers, so we never miss a
# topic because of a QoS mismatch. Raw mode skips deserialization: we only count arrivals.
MONITOR_QOS = QoSProfile(depth=10, reliability=ReliabilityPolicy.BEST_EFFORT)


class CollectorNode(Node):
    def __init__(self, cfg: CollectorConfig, uplink: Uplink) -> None:
        super().__init__("openfleet_collector")
        self.cfg = cfg
        self.uplink = uplink
        self.started_at = time.monotonic()
        self.rates = RateTracker(
            cfg.topics, cfg.window_s, started_at=self.started_at, grace_s=cfg.startup_grace_s
        )
        self.logs = LogBuffer(cfg.forward_log_level)
        self.battery_pct: float | None = None
        self.diagnostics = DiagnosticsTracker(cfg.diagnostics_stale_s)
        self._subscribed: dict[str, str] = {}  # topic -> type name
        psutil.cpu_percent(interval=None)  # prime the CPU counter

        self.create_subscription(Log, "/rosout", self._on_rosout, 100)
        if cfg.battery_topic:
            from sensor_msgs.msg import BatteryState

            self.create_subscription(
                BatteryState, cfg.battery_topic, self._on_battery, qos_profile_sensor_data
            )
        if cfg.diagnostics_topic:
            from diagnostic_msgs.msg import DiagnosticArray

            self.create_subscription(
                DiagnosticArray, cfg.diagnostics_topic, self._on_diagnostics, 50
            )
        self.create_timer(cfg.window_s, self._tick)
        self._discover_topics()

    # --- subscriptions -------------------------------------------------------------------

    def _discover_topics(self) -> None:
        """Subscribe to configured topics once they exist (their type is only known then).
        A topic that never appears is reported at 0 Hz, which OpenFleet classifies FAILED."""
        missing = [t for t in self.cfg.topics if t not in self._subscribed]
        if not missing:
            return
        types = dict(self.get_topic_names_and_types())
        for topic in missing:
            if topic not in types or not types[topic]:
                continue
            type_name = types[topic][0]
            try:
                msg_type = get_message(type_name)
            except (AttributeError, ModuleNotFoundError, ValueError) as exc:
                self.get_logger().warning(f"cannot load type {type_name} for {topic}: {exc}")
                continue
            self.create_subscription(
                msg_type,
                topic,
                lambda _msg, t=topic: self.rates.record(t, time.monotonic()),
                MONITOR_QOS,
                raw=True,
            )
            self._subscribed[topic] = type_name
            self.rates.mark_subscribed(topic, time.monotonic())
            self.get_logger().info(f"monitoring {topic} [{type_name}]")

    def _on_rosout(self, msg: Log) -> None:
        if msg.name == self.get_name():
            return  # don't echo our own logs back
        self.logs.add(msg.level, msg.name, msg.msg)

    def _on_diagnostics(self, msg) -> None:  # diagnostic_msgs/DiagnosticArray
        now = time.monotonic()
        for status in msg.status:
            level = status.level
            self.diagnostics.record(
                status.name, level[0] if isinstance(level, bytes) else int(level), now
            )

    def _on_battery(self, msg) -> None:  # sensor_msgs/BatteryState
        p = msg.percentage
        self.battery_pct = None if (p is None or math.isnan(p)) else p * 100.0

    # --- every window --------------------------------------------------------------------

    def _tick(self) -> None:
        self._discover_topics()
        now = time.monotonic()
        in_grace = now - self.started_at < self.cfg.startup_grace_s
        present = {f"{ns.rstrip('/')}/{name}" for name, ns in self.get_node_names_and_namespaces()}
        batch = build_batch(
            seq=self.uplink.next_seq(),
            ts=datetime.now(timezone.utc),
            rates=self.rates.snapshot(now),
            nodes=merge_nodes(
                node_states(self.cfg.node_names(), present, report_missing=not in_grace),
                self.diagnostics.snapshot(now),
            ),
            cpu_pct=psutil.cpu_percent(interval=None),
            mem_pct=psutil.virtual_memory().percent,
            battery_pct=self.battery_pct,
            logs=self.logs.drain(),
        )
        self.uplink.enqueue(batch)


def spin(cfg: CollectorConfig, uplink: Uplink) -> None:
    rclpy.init()
    node = CollectorNode(cfg, uplink)
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, rclpy.executors.ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
