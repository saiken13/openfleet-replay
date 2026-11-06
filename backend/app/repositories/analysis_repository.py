"""Read-only aggregate queries over a run (summary / compare / CI checks)."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Event, TelemetrySample, TopicSample
from app.domain.enums import EventType

_METRICS_SQL = text(
    """
    SELECT count(*)                                        AS samples,
           min(ts)                                         AS first_ts,
           max(ts)                                         AS last_ts,
           avg(cpu_pct)                                    AS cpu_avg,
           max(cpu_pct)                                    AS cpu_max,
           avg(mem_pct)                                    AS mem_avg,
           max(mem_pct)                                    AS mem_max,
           min(battery_pct)                                AS battery_min,
           (array_agg(battery_pct ORDER BY ts) FILTER (WHERE battery_pct IS NOT NULL))[1]
                                                           AS battery_start,
           (array_agg(battery_pct ORDER BY ts DESC) FILTER (WHERE battery_pct IS NOT NULL))[1]
                                                           AS battery_end,
           avg(net_latency_ms)                             AS latency_avg
    FROM telemetry_samples
    WHERE run_id = :run_id
    """
)

_TOPICS_SQL = text(
    """
    SELECT topic,
           max(expected_hz)                                                  AS expected_hz,
           count(*)                                                          AS samples,
           avg(actual_hz)                                                    AS avg_hz,
           min(actual_hz)                                                    AS min_hz,
           max(actual_hz)                                                    AS max_hz,
           percentile_cont(0.05) WITHIN GROUP (ORDER BY actual_hz)           AS p05_hz,
           100.0 * count(*) FILTER (WHERE status = 'HEALTHY')  / count(*)    AS healthy_pct,
           100.0 * count(*) FILTER (WHERE status = 'DEGRADED') / count(*)    AS degraded_pct,
           100.0 * count(*) FILTER (WHERE status = 'FAILED')   / count(*)    AS failed_pct
    FROM topic_samples
    WHERE run_id = :run_id
    GROUP BY topic
    ORDER BY topic
    """
)

_EVENTS_SQL = text(
    "SELECT type, count(*) AS n FROM events WHERE run_id = :run_id GROUP BY type ORDER BY type"
)


class AnalysisRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def metrics(self, run_id: uuid.UUID) -> dict[str, Any]:
        row = (await self.session.execute(_METRICS_SQL, {"run_id": run_id})).mappings().one()
        return dict(row)

    async def topics(self, run_id: uuid.UUID) -> list[dict[str, Any]]:
        rows = (await self.session.execute(_TOPICS_SQL, {"run_id": run_id})).mappings().all()
        return [dict(r) for r in rows]

    async def event_counts(self, run_id: uuid.UUID) -> dict[str, int]:
        rows = (await self.session.execute(_EVENTS_SQL, {"run_id": run_id})).all()
        return {t: n for t, n in rows}

    # --- replay stream ---------------------------------------------------------------------

    async def stream_after(
        self,
        model: type[TelemetrySample] | type[TopicSample] | type[Event],
        rank: int,
        run_id: uuid.UUID,
        after: tuple[datetime, int, int] | None,
        limit: int,
    ) -> list[Any]:
        """Rows of one source ordered by (ts, id), strictly after the merged-stream cursor
        (ts, kind_rank, id) given that this source has kind rank `rank`."""
        stmt = select(model).where(model.run_id == run_id)
        if after is not None:
            c_ts, c_rank, c_id = after
            if rank > c_rank:
                stmt = stmt.where(model.ts >= c_ts)
            elif rank < c_rank:
                stmt = stmt.where(model.ts > c_ts)
            else:
                stmt = stmt.where((model.ts > c_ts) | ((model.ts == c_ts) & (model.id > c_id)))
        stmt = stmt.order_by(model.ts, model.id).limit(limit)
        return list(await self.session.scalars(stmt))

    # --- re-detection (DECISIONS #023) -----------------------------------------------------

    async def sample_count(self, run_id: uuid.UUID) -> int:
        stmt = select(func.count()).where(TelemetrySample.run_id == run_id)
        return int(await self.session.scalar(stmt) or 0)

    async def samples(self, run_id: uuid.UUID) -> list[TelemetrySample]:
        stmt = (
            select(TelemetrySample)
            .where(TelemetrySample.run_id == run_id)
            .order_by(TelemetrySample.ts, TelemetrySample.id)
        )
        return list(await self.session.scalars(stmt))

    async def topic_samples(self, run_id: uuid.UUID) -> list[TopicSample]:
        stmt = (
            select(TopicSample)
            .where(TopicSample.run_id == run_id)
            .order_by(TopicSample.ts, TopicSample.id)
        )
        return list(await self.session.scalars(stmt))

    async def node_events(self, run_id: uuid.UUID) -> list[Event]:
        stmt = (
            select(Event)
            .where(Event.run_id == run_id, Event.type == EventType.NODE_STATE_CHANGED)
            .order_by(Event.ts, Event.id)
        )
        return list(await self.session.scalars(stmt))
