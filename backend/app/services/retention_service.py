"""Data retention (DECISIONS #030): compact finished runs older than the retention age.

Compacting a run freezes its summary aggregates into `runs.summary` (the same queries the
summary endpoint runs, so `openfleet check` and compare give identical answers afterwards).
Then it deletes the bulky per-second rows: telemetry and topic samples, heartbeat spans and
log events. The run, its incidents (with their evidence), its state-change and incident
events, and the triage audit are kept.

One run per transaction, with the run row locked, so a compaction is all-or-nothing and two
replicas (or the CLI and a sweeper) can't compact the same run at once.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Event, HeartbeatSpan, Run, TelemetrySample, TopicSample
from app.domain.enums import EventType, RunStatus
from app.repositories.analysis_repository import AnalysisRepository

SUMMARY_VERSION = 1


@dataclass(frozen=True)
class Compacted:
    run_id: uuid.UUID
    samples: int
    topic_samples: int
    heartbeat_spans: int
    log_events: int


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def frozen_metrics(summary: dict[str, Any]) -> dict[str, Any]:
    """`AnalysisRepository.metrics()` as it was before compaction."""
    m = dict(summary["metrics"])
    for k in ("first_ts", "last_ts"):
        if m.get(k) is not None:
            m[k] = datetime.fromisoformat(m[k])
    return m


class RetentionService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.analysis = AnalysisRepository(session)

    async def due(self, now: datetime, older_than: timedelta, limit: int) -> list[uuid.UUID]:
        """Finished, uncompacted runs that ended before `now - older_than`, oldest first.
        `ended_at` is the robot's clock, like every run time."""
        rows = await self.session.scalars(
            select(Run.id)
            .where(
                Run.compacted_at.is_(None),
                Run.status != RunStatus.RUNNING,
                Run.ended_at < now - older_than,
            )
            .order_by(Run.ended_at)
            .limit(limit)
        )
        result = list(rows)
        await self.session.rollback()  # end the read transaction before per-run work
        return result

    async def counts(self, run_id: uuid.UUID) -> Compacted:
        """What compacting this run would delete (for --dry-run)."""

        async def n(model: Any, *where: Any) -> int:
            stmt = select(func.count()).select_from(model).where(model.run_id == run_id, *where)
            return int(await self.session.scalar(stmt) or 0)

        return Compacted(
            run_id=run_id,
            samples=await n(TelemetrySample),
            topic_samples=await n(TopicSample),
            heartbeat_spans=await n(HeartbeatSpan),
            log_events=await n(Event, Event.type == EventType.LOG),
        )

    async def compact(self, run_id: uuid.UUID, now: datetime) -> Compacted | None:
        """Compact one run; None if it's gone, still RUNNING, or already compacted."""
        run = await self.session.scalar(select(Run).where(Run.id == run_id).with_for_update())
        if run is None or run.status == RunStatus.RUNNING or run.compacted_at is not None:
            await self.session.rollback()
            return None
        metrics = await self.analysis.metrics(run_id)
        topics = await self.analysis.topics(run_id)
        events = await self.analysis.event_counts(run_id)
        run.summary = {
            "version": SUMMARY_VERSION,
            "metrics": {k: _jsonable(v) for k, v in metrics.items()},
            "topics": [{k: _jsonable(v) for k, v in t.items()} for t in topics],
            "events": events,
        }
        deleted = []
        for model, *where in (
            (TelemetrySample,),
            (TopicSample,),
            (HeartbeatSpan,),
            (Event, Event.type == EventType.LOG),
        ):
            result = await self.session.execute(delete(model).where(model.run_id == run_id, *where))
            deleted.append(result.rowcount)
        run.compacted_at = now
        await self.session.commit()
        return Compacted(run_id, *deleted)

    async def run(self, now: datetime, older_than: timedelta, limit: int) -> list[Compacted]:
        done = []
        for run_id in await self.due(now, older_than, limit):
            result = await self.compact(run_id, now)
            if result is not None:
                done.append(result)
        return done
