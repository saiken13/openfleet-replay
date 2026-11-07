import base64
import binascii
import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Event, Run, TelemetrySample, TopicSample
from app.domain.enums import Severity
from app.domain.errors import InvalidRequestError, NotFoundError
from app.realtime.messages import frame_for
from app.repositories.analysis_repository import AnalysisRepository
from app.repositories.incident_repository import IncidentRepository
from app.repositories.run_repository import RunRepository
from app.schemas.analysis import (
    MetricsSummary,
    RunComparison,
    RunStream,
    RunSummary,
    TopicComparison,
    TopicSummary,
    as_float,
)
from app.schemas.incident import IncidentRead
from app.schemas.run import RunRead
from app.services.retention_service import frozen_metrics

# Merged stream order: (ts, kind rank, id). Samples first at equal ts, then topics, events.
_SOURCES: tuple[tuple[type[TelemetrySample] | type[TopicSample] | type[Event], int], ...] = (
    (TelemetrySample, 0),
    (TopicSample, 1),
    (Event, 2),
)
_RANK = {TelemetrySample: 0, TopicSample: 1, Event: 2}


class AnalysisService:
    """Read-only: replay stream (DECISIONS #008), run summary, run comparison."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.runs = RunRepository(session)
        self.analysis = AnalysisRepository(session)
        self.incidents = IncidentRepository(session)

    async def _run(self, run_id: uuid.UUID) -> Run:
        run = await self.runs.get(run_id)
        if run is None:
            raise NotFoundError(f"run {run_id} not found")
        return run

    async def stream(self, run_id: uuid.UUID, cursor: str | None, limit: int) -> RunStream:
        run = await self._run(run_id)
        after = _decode_cursor(cursor) if cursor else None
        rows: list[TelemetrySample | TopicSample | Event] = []
        any_source_full = False
        for model, rank in _SOURCES:
            chunk = await self.analysis.stream_after(model, rank, run_id, after, limit)
            any_source_full |= len(chunk) == limit
            rows.extend(chunk)
        rows.sort(key=lambda r: (r.ts, _RANK[type(r)], r.id))
        page = rows[:limit]
        more = len(rows) > limit or any_source_full
        next_cursor = _encode_cursor(page[-1]) if more and page else None
        return RunStream(
            run=RunRead.model_validate(run),
            frames=[frame_for(r) for r in page],
            next_cursor=next_cursor,
        )

    async def summary(self, run_id: uuid.UUID) -> RunSummary:
        run = await self._run(run_id)
        if run.compacted_at is not None and run.summary is not None:
            # Retention removed the raw rows; these are their aggregates, frozen (#030).
            m = frozen_metrics(run.summary)
            topic_rows = run.summary["topics"]
            event_counts = run.summary["events"]
        else:
            m = await self.analysis.metrics(run_id)
            topic_rows = await self.analysis.topics(run_id)
            event_counts = await self.analysis.event_counts(run_id)
        end = run.ended_at or m["last_ts"]
        duration = (end - run.started_at).total_seconds() if end else None
        incidents = await self.incidents.search(
            status="all", robot_id=None, run_id=run_id, severity=None, limit=500
        )
        by_sev = {s.value: 0 for s in Severity}
        for inc in incidents:
            by_sev[inc.severity] += 1
        return RunSummary(
            run=RunRead.model_validate(run),
            duration_s=duration,
            metrics=MetricsSummary(
                samples=m["samples"],
                **{
                    k: as_float(m[k])
                    for k in (
                        "cpu_avg",
                        "cpu_max",
                        "mem_avg",
                        "mem_max",
                        "battery_start",
                        "battery_end",
                        "battery_min",
                        "latency_avg",
                    )
                },
            ),
            topics=[
                TopicSummary(
                    topic=t["topic"],
                    expected_hz=as_float(t["expected_hz"]),
                    samples=t["samples"],
                    **{
                        k: float(t[k])
                        for k in (
                            "avg_hz",
                            "min_hz",
                            "max_hz",
                            "p05_hz",
                            "healthy_pct",
                            "degraded_pct",
                            "failed_pct",
                        )
                    },
                )
                for t in topic_rows
            ],
            incidents_by_severity=by_sev,
            incidents=[IncidentRead.model_validate(i) for i in reversed(incidents)],
            events_by_type=event_counts,
        )

    async def compare(self, base_id: uuid.UUID, candidate_id: uuid.UUID) -> RunComparison:
        base = await self.summary(base_id)
        cand = await self.summary(candidate_id)
        b_topics = {t.topic: t for t in base.topics}
        c_topics = {t.topic: t for t in cand.topics}
        topics = []
        for name in sorted(b_topics.keys() | c_topics.keys()):
            b, c = b_topics.get(name), c_topics.get(name)
            delta = None
            if b and c and b.avg_hz:
                delta = 100.0 * (c.avg_hz - b.avg_hz) / b.avg_hz
            topics.append(
                TopicComparison(
                    topic=name,
                    base_avg_hz=b.avg_hz if b else None,
                    candidate_avg_hz=c.avg_hz if c else None,
                    avg_hz_delta_pct=delta,
                    base_failed_pct=b.failed_pct if b else None,
                    candidate_failed_pct=c.failed_pct if c else None,
                )
            )
        metrics_delta = {}
        for key in ("cpu_avg", "cpu_max", "mem_avg", "mem_max", "battery_min", "latency_avg"):
            bv, cv = getattr(base.metrics, key), getattr(cand.metrics, key)
            metrics_delta[key] = None if bv is None or cv is None else cv - bv
        notes = []
        if base.run.robot_id != cand.run.robot_id:
            notes.append("Runs are from different robots; hardware differences may dominate.")
        if base.run.software_version == cand.run.software_version:
            notes.append("Both runs report the same software version.")
        return RunComparison(
            base=base,
            candidate=cand,
            topics=topics,
            metrics_delta=metrics_delta,
            incidents_delta={
                k: cand.incidents_by_severity.get(k, 0) - base.incidents_by_severity.get(k, 0)
                for k in base.incidents_by_severity
            },
            notes=notes,
        )


def _encode_cursor(row: TelemetrySample | TopicSample | Event) -> str:
    raw = f"{row.ts.isoformat()}|{_RANK[type(row)]}|{row.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, int, int]:
    try:
        ts, rank, row_id = base64.urlsafe_b64decode(cursor.encode()).decode().split("|")
        return datetime.fromisoformat(ts), int(rank), int(row_id)
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise InvalidRequestError("invalid cursor") from exc
