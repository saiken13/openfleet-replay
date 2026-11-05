import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Integer, cast, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import TelemetrySample, TopicSample


class TelemetryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def insert_sample(self, values: dict[str, Any]) -> TelemetrySample | None:
        """Returns None if (run_id, seq) already exists: an idempotent retry."""
        stmt = (
            insert(TelemetrySample)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["run_id", "seq"])
            .returning(TelemetrySample)
        )
        return (await self.session.scalars(stmt)).one_or_none()

    async def insert_topic_samples(self, rows: list[dict[str, Any]]) -> list[TopicSample]:
        if not rows:
            return []
        stmt = (
            insert(TopicSample)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["run_id", "seq", "topic"])
            .returning(TopicSample)
        )
        return list(await self.session.scalars(stmt))

    async def recent_samples(
        self, robot_id: str, since: datetime, limit: int
    ) -> list[TelemetrySample]:
        stmt = (
            select(TelemetrySample)
            .where(TelemetrySample.robot_id == robot_id, TelemetrySample.received_at >= since)
            .order_by(TelemetrySample.received_at.desc())
            .limit(limit)
        )
        return list(reversed(list(await self.session.scalars(stmt))))

    async def recent_topic_samples(
        self, robot_id: str, since: datetime, limit: int
    ) -> list[TopicSample]:
        stmt = (
            select(TopicSample)
            .where(TopicSample.robot_id == robot_id, TopicSample.received_at >= since)
            .order_by(TopicSample.received_at.desc(), TopicSample.id.desc())
            .limit(limit)
        )
        return list(reversed(list(await self.session.scalars(stmt))))

    async def downsampled_window(
        self, robot_id: str, start: datetime, end: datetime, max_points: int
    ) -> list[TelemetrySample]:
        """Samples received in [start, end], thinned to at most ~max_points (every n-th row)."""
        in_window = (TelemetrySample.robot_id == robot_id) & TelemetrySample.received_at.between(
            start, end
        )
        total = await self.session.scalar(select(func.count()).where(in_window))
        step = max(1, -(-(total or 0) // max_points))
        numbered = select(
            TelemetrySample.id,
            func.row_number().over(order_by=(TelemetrySample.ts, TelemetrySample.id)).label("rn"),
        ).where(in_window)
        sub = numbered.subquery()
        ids = select(sub.c.id).where((sub.c.rn - 1) % step == 0)
        stmt = (
            select(TelemetrySample)
            .where(TelemetrySample.id.in_(ids))
            .order_by(TelemetrySample.ts, TelemetrySample.id)
        )
        return list(await self.session.scalars(stmt))

    async def downsampled_topic_window(
        self, robot_id: str, start: datetime, end: datetime, max_points_per_topic: int
    ) -> list[TopicSample]:
        in_window = (TopicSample.robot_id == robot_id) & TopicSample.received_at.between(start, end)
        numbered = (
            select(
                TopicSample.id,
                func.row_number()
                .over(partition_by=TopicSample.topic, order_by=(TopicSample.ts, TopicSample.id))
                .label("rn"),
                func.count().over(partition_by=TopicSample.topic).label("n"),
            )
            .where(in_window)
            .subquery()
        )
        # step = ceil(n / max_points) per topic
        step = cast(
            func.greatest(1, func.ceil(numbered.c.n / float(max_points_per_topic))), Integer
        )
        ids = select(numbered.c.id).where(func.mod(numbered.c.rn - 1, step) == 0)
        stmt = (
            select(TopicSample)
            .where(TopicSample.id.in_(ids))
            .order_by(TopicSample.ts, TopicSample.topic, TopicSample.id)
        )
        return list(await self.session.scalars(stmt))

    async def run_sample_bounds(self, run_id: uuid.UUID) -> tuple[datetime | None, int]:
        row = (
            await self.session.execute(
                select(func.max(TelemetrySample.ts), func.count()).where(
                    TelemetrySample.run_id == run_id
                )
            )
        ).one()
        return row[0], row[1]
