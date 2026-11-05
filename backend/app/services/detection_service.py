"""Detection: snapshot -> pure rules -> incident reconciliation -> derived robot status.

Called inside the caller's transaction, with the robot row already locked (lock order:
robot -> runs -> incidents). Two triggers (DECISIONS #006): after ingest/heartbeat for that
robot, and from the sweeper for robots that went silent.
"""

from dataclasses import replace
from datetime import datetime, timedelta

from app.core.config import Settings
from app.db.models import Incident, Robot
from app.detection.model import DetectionConfig, Finding, NodeView, RobotSnapshot, TopicView
from app.detection.rules import evaluate_all
from app.domain.enums import (
    EventSeverity,
    EventType,
    IncidentStatus,
    NodeState,
    Severity,
    TopicStatus,
)
from app.domain.status import derive_robot_status
from app.repositories.incident_repository import IncidentRepository
from app.repositories.state_repository import StateRepository
from app.services.base import Service

_EVENT_SEVERITY = {
    Severity.INFO: EventSeverity.INFO,
    Severity.WARNING: EventSeverity.WARNING,
    Severity.CRITICAL: EventSeverity.ERROR,
}


class Detector:
    def __init__(self, svc: Service, settings: Settings) -> None:
        self.svc = svc
        self.settings = settings
        self.cfg = DetectionConfig.from_settings(settings)
        self.incidents = IncidentRepository(svc.session)
        self.states = StateRepository(svc.session)

    async def snapshot(self, robot: Robot, now: datetime) -> RobotSnapshot:
        """Run data (topics, nodes, system metrics) only counts while the robot is in a run
        and only if refreshed within the heartbeat timeout: telemetry flows only during runs,
        and the last values of a finished run must not keep firing rules (DECISIONS #017)."""
        base = RobotSnapshot(
            robot_id=robot.id,
            last_seen_at=robot.last_seen_at,
            current_run_id=robot.current_run_id,
            software_version=robot.software_version,
        )
        if robot.current_run_id is None:
            return base
        fresh_after = now - timedelta(seconds=self.cfg.heartbeat_timeout_s)
        topics = [
            t for t in (await self.states.topics(robot.id)).values() if t.updated_at >= fresh_after
        ]
        nodes = [
            n for n in (await self.states.nodes(robot.id)).values() if n.updated_at >= fresh_after
        ]
        return replace(
            base,
            cpu_pct=robot.cpu_pct,
            mem_pct=robot.mem_pct,
            battery_pct=robot.battery_pct,
            topics=tuple(
                TopicView(
                    topic=t.topic,
                    status=TopicStatus(t.status),
                    expected_hz=t.expected_hz,
                    actual_hz=t.actual_hz,
                    last_msg_age_s=t.last_msg_age_s,
                )
                for t in topics
            ),
            nodes=tuple(NodeView(node=n.node, state=NodeState(n.state)) for n in nodes),
        )

    async def resolve_run_incidents(self, robot: Robot, run_id, now: datetime) -> None:
        """The robot reported its run as ended: problems observed in that run's data are
        over. Heartbeat incidents stay: they are about the robot, not the run's data."""
        for inc in await self.incidents.unresolved_for_robot(robot.id, for_update=True):
            if inc.run_id == run_id and inc.rule != "heartbeat_missing":
                await self._resolve(robot, inc, now, reason="run ended")

    async def evaluate(self, robot: Robot, now: datetime) -> None:
        snap = await self.snapshot(robot, now)
        live = snap.is_live(now, self.cfg.heartbeat_timeout_s)
        findings = evaluate_all(snap, now, self.cfg)
        open_incidents = await self.reconcile(robot, findings, now, live)
        await self._apply_status(robot, live, [Severity(i.severity) for i in open_incidents], now)

    async def reconcile(
        self, robot: Robot, findings: list[Finding], now: datetime, live: bool
    ) -> list[Incident]:
        """Open / escalate / auto-resolve. Returns incidents still unresolved afterwards."""
        existing = {
            i.dedup_key: i
            for i in await self.incidents.unresolved_for_robot(robot.id, for_update=True)
        }
        still_open: list[Incident] = []

        for f in findings:
            inc = existing.pop(f.dedup_key, None)
            if inc is None:
                inc = await self._open(robot, f, now)
                if inc is None:  # lost a race to a concurrent opener; it is open anyway
                    continue
            else:
                inc.last_detected_at = now
                if f.severity.rank > Severity(inc.severity).rank:
                    await self._escalate(robot, inc, f, now)
            still_open.append(inc)

        # Findings that went away. Only trust absence while the robot is live: if it is
        # offline we can't observe that a problem cleared (and must not auto-resolve).
        resolve_before = now - timedelta(seconds=self.settings.incident_resolve_after_s)
        for inc in existing.values():
            if live and inc.last_detected_at <= resolve_before:
                await self._resolve(robot, inc, now, reason="condition cleared")
            else:
                still_open.append(inc)
        return still_open

    async def _open(self, robot: Robot, f: Finding, now: datetime) -> Incident | None:
        inc = await self.incidents.open_if_absent(
            {
                "robot_id": robot.id,
                "run_id": robot.current_run_id,
                "rule": f.rule,
                "dedup_key": f.dedup_key,
                "severity": f.severity,
                "status": IncidentStatus.OPEN,
                "title": f.title,
                "description": f.description,
                "software_version": robot.software_version,
                "evidence": f.evidence,
                "started_at": now,
                "last_detected_at": now,
                "created_at": now,
                "updated_at": now,
            }
        )
        if inc is None:
            return None
        await self.svc.emit(
            robot_id=robot.id,
            run_id=robot.current_run_id,
            ts=now,
            type=EventType.INCIDENT_OPENED,
            severity=_EVENT_SEVERITY[f.severity],
            source="openfleet",
            message=f"Incident opened: {f.title}",
            payload={"incident_id": str(inc.id), "rule": f.rule, "severity": f.severity.value},
        )
        self.svc.publish_after_commit(inc)
        return inc

    async def _escalate(self, robot: Robot, inc: Incident, f: Finding, now: datetime) -> None:
        old = inc.severity
        inc.severity = f.severity
        inc.title = f.title
        inc.description = f.description
        inc.evidence = f.evidence
        inc.updated_at = now
        await self.svc.session.flush()
        await self.svc.emit(
            robot_id=robot.id,
            run_id=robot.current_run_id,
            ts=now,
            type=EventType.INCIDENT_ESCALATED,
            severity=_EVENT_SEVERITY[f.severity],
            source="openfleet",
            message=f"Incident escalated {old} → {f.severity}: {f.title}",
            payload={"incident_id": str(inc.id), "from": old, "to": f.severity.value},
        )
        self.svc.publish_after_commit(inc)

    async def _resolve(self, robot: Robot, inc: Incident, now: datetime, reason: str) -> None:
        inc.status = IncidentStatus.RESOLVED
        inc.resolved_at = now
        inc.updated_at = now
        await self.svc.session.flush()
        await self.svc.emit(
            robot_id=robot.id,
            run_id=robot.current_run_id,
            ts=now,
            type=EventType.INCIDENT_RESOLVED,
            severity=EventSeverity.INFO,
            source="openfleet",
            message=f"Incident resolved ({reason}): {inc.title}",
            payload={"incident_id": str(inc.id), "reason": reason},
        )
        self.svc.publish_after_commit(inc)

    async def _apply_status(
        self, robot: Robot, live: bool, open_severities: list[Severity], now: datetime
    ) -> None:
        new = derive_robot_status(live, open_severities)
        if new == robot.status:
            return
        old = robot.status
        robot.status = new
        robot.updated_at = now
        await self.svc.session.flush()
        severity = {
            "ERROR": EventSeverity.ERROR,
            "DEGRADED": EventSeverity.WARNING,
            "OFFLINE": EventSeverity.WARNING,
        }.get(new, EventSeverity.INFO)
        await self.svc.emit(
            robot_id=robot.id,
            run_id=robot.current_run_id,
            ts=now,
            type=EventType.ROBOT_STATUS_CHANGED,
            severity=severity,
            source="openfleet",
            message=f"{robot.id} {old} → {new}",
            payload={"from": old, "to": new.value},
        )
        self.svc.publish_after_commit(robot)
