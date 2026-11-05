import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import NodeStateRow, Robot, Run, TopicState
from app.detection.topic_health import classify_topic
from app.domain.enums import (
    EventSeverity,
    EventType,
    LogLevel,
    NodeState,
    RunStatus,
    TopicStatus,
)
from app.domain.errors import ConflictError, NotFoundError
from app.realtime.hub import Hub
from app.repositories.robot_repository import RobotRepository
from app.repositories.run_repository import RunRepository
from app.repositories.state_repository import StateRepository
from app.repositories.telemetry_repository import TelemetryRepository
from app.schemas.telemetry import TelemetryBatch
from app.services.base import Service
from app.services.detection_service import Detector
from app.services.robot_state import record_contact

_TOPIC_EVENT_SEVERITY = {
    TopicStatus.HEALTHY: EventSeverity.INFO,
    TopicStatus.UNKNOWN: EventSeverity.INFO,
    TopicStatus.DEGRADED: EventSeverity.WARNING,
    TopicStatus.FAILED: EventSeverity.ERROR,
}
_NODE_EVENT_SEVERITY = {
    NodeState.RUNNING: EventSeverity.INFO,
    NodeState.DEGRADED: EventSeverity.WARNING,
    NodeState.STOPPED: EventSeverity.ERROR,
    NodeState.ERROR: EventSeverity.ERROR,
}
_LOG_SEVERITY = {
    LogLevel.DEBUG: EventSeverity.INFO,
    LogLevel.INFO: EventSeverity.INFO,
    LogLevel.WARN: EventSeverity.WARNING,
    LogLevel.ERROR: EventSeverity.ERROR,
    LogLevel.FATAL: EventSeverity.ERROR,
}


class TelemetryService(Service):
    def __init__(self, session: AsyncSession, hub: Hub | None, settings: Settings) -> None:
        super().__init__(session, hub)
        self.settings = settings
        self.robots = RobotRepository(session)
        self.runs = RunRepository(session)
        self.telemetry = TelemetryRepository(session)
        self.states = StateRepository(session)

    async def ingest(self, run_id: uuid.UUID, batch: TelemetryBatch, now: datetime) -> bool:
        """Store one batch; returns False if it was a duplicate (idempotent retry).

        One transaction: samples, current state, change events, liveness, then detection.
        """
        unlocked = await self.runs.get(run_id)
        if unlocked is None:
            raise NotFoundError(f"run {run_id} not found")
        robot = await self.robots.get(unlocked.robot_id, for_update=True)  # robot -> run order
        run = await self.runs.get(run_id)
        assert robot is not None and run is not None
        if run.status != RunStatus.RUNNING:
            raise ConflictError(f"run {run_id} is {run.status}, not RUNNING")

        m = batch.metrics
        sample = await self.telemetry.insert_sample(
            {
                "run_id": run_id,
                "robot_id": robot.id,
                "seq": batch.seq,
                "ts": batch.ts,
                "received_at": now,
                "cpu_pct": m.cpu_pct,
                "mem_pct": m.mem_pct,
                "battery_pct": m.battery_pct,
                "net_latency_ms": m.net_latency_ms,
                "pose_x": batch.pose.x if batch.pose else None,
                "pose_y": batch.pose.y if batch.pose else None,
                "pose_yaw": batch.pose.yaw if batch.pose else None,
            }
        )
        if sample is None:
            await self.rollback()
            return False
        self.publish_after_commit(sample)

        await self._ingest_topics(robot, run, batch, now)
        await self._ingest_nodes(robot, run, batch, now)
        await self._ingest_logs(robot, run, batch)

        for field in ("cpu_pct", "mem_pct", "battery_pct"):
            value = getattr(m, field)
            if value is not None:
                setattr(robot, field, value)
        await record_contact(self, robot, now)
        await Detector(self, self.settings).evaluate(robot, now)
        await self.commit()
        return True

    async def _ingest_topics(
        self, robot: Robot, run: Run, batch: TelemetryBatch, now: datetime
    ) -> None:
        s = self.settings
        rows = []
        classified: dict[str, TopicStatus] = {}
        for t in batch.topics:
            status = classify_topic(
                t.expected_hz,
                t.actual_hz,
                t.last_msg_age_s,
                degraded_ratio=s.topic_degraded_ratio,
                failed_ratio=s.topic_failed_ratio,
                stale_after_s=s.topic_stale_after_s,
            )
            classified[t.topic] = status
            rows.append(
                {
                    "run_id": run.id,
                    "robot_id": robot.id,
                    "seq": batch.seq,
                    "ts": batch.ts,
                    "received_at": now,
                    "topic": t.topic,
                    "expected_hz": t.expected_hz,
                    "actual_hz": t.actual_hz,
                    "last_msg_age_s": t.last_msg_age_s,
                    "status": status,
                }
            )
        for row in await self.telemetry.insert_topic_samples(rows):
            self.publish_after_commit(row)

        current = await self.states.topics(robot.id)
        for t in batch.topics:
            status = classified[t.topic]
            state = current.get(t.topic)
            previous = TopicStatus(state.status) if state else None
            if state is None:
                state = TopicState(robot_id=robot.id, topic=t.topic, status_since=batch.ts)
                self.states.add(state)
            if previous != status:
                state.status_since = batch.ts
            state.expected_hz = t.expected_hz
            state.actual_hz = t.actual_hz
            state.last_msg_age_s = t.last_msg_age_s
            state.status = status
            state.updated_at = now
            # Events on change only; a brand-new healthy topic is not news.
            if previous != status and not (previous is None and status == TopicStatus.HEALTHY):
                expected = f"{t.expected_hz:g}" if t.expected_hz else "?"
                await self.emit(
                    robot_id=robot.id,
                    run_id=run.id,
                    ts=batch.ts,
                    type=EventType.TOPIC_STATUS_CHANGED,
                    severity=_TOPIC_EVENT_SEVERITY[status],
                    source=t.topic,
                    message=(
                        f"{t.topic} {previous or 'NEW'} → {status} "
                        f"({t.actual_hz:.1f}/{expected} Hz)"
                    ),
                    payload={
                        "topic": t.topic,
                        "from": previous.value if previous else None,
                        "to": status.value,
                        "expected_hz": t.expected_hz,
                        "actual_hz": t.actual_hz,
                    },
                )
        await self.states.flush()

    async def _ingest_nodes(
        self, robot: Robot, run: Run, batch: TelemetryBatch, now: datetime
    ) -> None:
        current = await self.states.nodes(robot.id)
        for n in batch.nodes:
            row = current.get(n.node)
            previous = NodeState(row.state) if row else None
            if row is None:
                row = NodeStateRow(robot_id=robot.id, node=n.node, state_since=batch.ts)
                self.states.add(row)
            if previous != n.state:
                row.state_since = batch.ts
            row.state = n.state
            row.updated_at = now
            if previous != n.state and not (previous is None and n.state == NodeState.RUNNING):
                await self.emit(
                    robot_id=robot.id,
                    run_id=run.id,
                    ts=batch.ts,
                    type=EventType.NODE_STATE_CHANGED,
                    severity=_NODE_EVENT_SEVERITY[n.state],
                    source=n.node,
                    message=f"{n.node} {previous or 'NEW'} → {n.state}",
                    payload={
                        "node": n.node,
                        "from": previous.value if previous else None,
                        "to": n.state.value,
                    },
                )
        await self.states.flush()

    async def _ingest_logs(self, robot: Robot, run: Run, batch: TelemetryBatch) -> None:
        for entry in batch.logs:
            await self.emit(
                robot_id=robot.id,
                run_id=run.id,
                ts=entry.ts,
                type=EventType.LOG,
                severity=_LOG_SEVERITY[entry.level],
                source=entry.source,
                message=entry.message,
                payload={"level": entry.level.value},
            )
