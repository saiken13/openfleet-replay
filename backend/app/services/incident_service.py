import uuid
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import Incident
from app.domain.enums import EventSeverity, EventType, IncidentStatus, Severity
from app.domain.errors import ConflictError, NotFoundError
from app.realtime.hub import Hub
from app.repositories.event_repository import EventRepository
from app.repositories.incident_repository import IncidentRepository
from app.repositories.robot_repository import RobotRepository
from app.repositories.telemetry_repository import TelemetryRepository
from app.repositories.triage_repository import TriageRepository
from app.schemas.incident import IncidentRead, IncidentTimeline
from app.schemas.telemetry import EventRead, SampleRead, TopicSampleRead
from app.services.base import Service
from app.services.detection_service import Detector

TIMELINE_MAX_EVENTS = 2000
TIMELINE_MAX_SAMPLES = 300
TIMELINE_MAX_TOPIC_POINTS = 150


class IncidentService(Service):
    def __init__(self, session: AsyncSession, hub: Hub | None, settings: Settings) -> None:
        super().__init__(session, hub)
        self.settings = settings
        self.incidents = IncidentRepository(session)
        self.robots = RobotRepository(session)

    async def get(self, incident_id: uuid.UUID) -> Incident:
        inc = await self.incidents.get(incident_id)
        if inc is None:
            raise NotFoundError(f"incident {incident_id} not found")
        return inc

    async def search(
        self,
        status: str,
        robot_id: str | None,
        run_id: uuid.UUID | None,
        severity: Severity | None,
        limit: int,
    ) -> list[Incident]:
        return await self.incidents.search(
            status=status, robot_id=robot_id, run_id=run_id, severity=severity, limit=limit
        )

    async def update_status(
        self,
        incident_id: uuid.UUID,
        status: IncidentStatus,
        now: datetime,
        *,
        actor: str | None = None,
    ) -> Incident:
        """Manual triage: OPEN -> INVESTIGATING, or resolve by hand. Resolved is final; if the
        condition persists, detection opens a *new* incident. `actor` (the signed-in
        operator) is recorded in the triage audit log (DECISIONS #026) and on the resolution
        event. A no-op (already in that status) records nothing."""
        unlocked = await self.get(incident_id)
        robot = await self.robots.get(unlocked.robot_id, for_update=True)  # robot first
        inc = await self.incidents.get(incident_id, for_update=True)
        assert robot is not None and inc is not None
        if inc.status == status:
            await self.commit()
            return inc
        if inc.status == IncidentStatus.RESOLVED:
            raise ConflictError(f"incident {incident_id} is already RESOLVED")

        await TriageRepository(self.session).record(
            incident=inc, actor=actor, from_status=inc.status, to_status=status, at=now
        )
        inc.status = status
        inc.updated_at = now
        if status == IncidentStatus.RESOLVED:
            inc.resolved_at = now
            await self.emit(
                robot_id=robot.id,
                run_id=inc.run_id,
                ts=now,
                type=EventType.INCIDENT_RESOLVED,
                severity=EventSeverity.INFO,
                source="openfleet",
                message=f"Incident resolved (manually{f' by {actor}' if actor else ''}): "
                f"{inc.title}",
                payload={"incident_id": str(inc.id), "reason": "manual", "actor": actor},
            )
        await self.session.flush()
        self.publish_after_commit(inc)
        # Robot status depends on open incidents.
        await Detector(self, self.settings).evaluate(robot, now)
        await self.commit()
        return inc

    async def timeline(self, incident_id: uuid.UUID, now: datetime) -> IncidentTimeline:
        """events ∪ downsampled samples for the robot over
        [started_at - lookback, resolved_at or now], filtered on server receive time and
        ordered by robot time (ARCHITECTURE §5). Computed, never stored."""
        inc = await self.get(incident_id)
        start = inc.started_at - timedelta(seconds=self.settings.timeline_lookback_s)
        end = inc.resolved_at or now
        events = await EventRepository(self.session).window_for_robot(
            inc.robot_id, start, end, TIMELINE_MAX_EVENTS
        )
        tel = TelemetryRepository(self.session)
        samples = await tel.downsampled_window(inc.robot_id, start, end, TIMELINE_MAX_SAMPLES)
        topics = await tel.downsampled_topic_window(
            inc.robot_id, start, end, TIMELINE_MAX_TOPIC_POINTS
        )
        return IncidentTimeline(
            incident=IncidentRead.model_validate(inc),
            window_start=start,
            window_end=end,
            events=[EventRead.model_validate(e) for e in events],
            samples=[SampleRead.model_validate(s) for s in samples],
            topic_samples=[TopicSampleRead.model_validate(t) for t in topics],
        )
