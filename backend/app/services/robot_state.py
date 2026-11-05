"""State changes on a (locked) robot row that several services share."""

import uuid
from datetime import datetime

from app.db.models import Robot
from app.domain.enums import EventSeverity, EventType
from app.services.base import Service


async def record_contact(svc: Service, robot: Robot, now: datetime) -> None:
    """The robot talked to us: liveness uses server time only (DECISIONS #010)."""
    robot.last_seen_at = now
    robot.updated_at = now
    await svc.session.flush()
    svc.publish_after_commit(robot)


async def record_software_version(
    svc: Service,
    robot: Robot,
    version: str | None,
    ts: datetime,
    run_id: uuid.UUID | None,
) -> None:
    if version is None or version == robot.software_version:
        return
    old = robot.software_version
    robot.software_version = version
    await svc.session.flush()
    await svc.emit(
        robot_id=robot.id,
        run_id=run_id,
        ts=ts,
        type=EventType.SOFTWARE_VERSION_CHANGED,
        severity=EventSeverity.INFO,
        source="openfleet",
        message=f"Software version {old or 'unknown'} → {version}",
        payload={"from": old, "to": version},
    )
    svc.publish_after_commit(robot)
