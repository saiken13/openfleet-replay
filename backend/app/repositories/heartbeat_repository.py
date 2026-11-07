import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import HeartbeatSpan


class HeartbeatRepository:
    """Per-run heartbeat spans (DECISIONS #028). Callers hold the robot row lock, so a
    robot's heartbeats are recorded one at a time."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record(
        self, run_id: uuid.UUID, robot_id: str, ts: datetime, max_gap: timedelta
    ) -> None:
        """Extend the run's latest span if `ts` is within `max_gap` of it, else start one."""
        latest = await self.session.scalar(
            select(HeartbeatSpan)
            .where(HeartbeatSpan.run_id == run_id)
            .order_by(HeartbeatSpan.ended_at.desc(), HeartbeatSpan.id.desc())
            .limit(1)
        )
        if latest is not None and latest.started_at - max_gap <= ts <= latest.ended_at + max_gap:
            latest.started_at = min(latest.started_at, ts)
            latest.ended_at = max(latest.ended_at, ts)
            latest.heartbeats += 1
            return
        self.session.add(
            HeartbeatSpan(
                run_id=run_id, robot_id=robot_id, started_at=ts, ended_at=ts, heartbeats=1
            )
        )

    async def for_run(self, run_id: uuid.UUID) -> list[HeartbeatSpan]:
        rows = await self.session.scalars(
            select(HeartbeatSpan)
            .where(HeartbeatSpan.run_id == run_id)
            .order_by(HeartbeatSpan.started_at)
        )
        return list(rows)
