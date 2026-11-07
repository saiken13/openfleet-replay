"""Re-detection replay (DECISIONS #023): run the current (or overridden) detection rules
over a recorded run and compare with the incidents recorded live. Read-only."""

import uuid
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import Incident, Run
from app.detection.model import DetectionConfig
from app.detection.replay import (
    RecordedBatch,
    RecordedContact,
    RecordedNodeChange,
    RecordedTopic,
    ReplayIncident,
    replay_run,
)
from app.domain.enums import NodeState, RunStatus, Severity
from app.domain.errors import InvalidRequestError, NotFoundError
from app.repositories.analysis_repository import AnalysisRepository
from app.repositories.heartbeat_repository import HeartbeatRepository
from app.repositories.incident_repository import IncidentRepository
from app.repositories.run_repository import RunRepository
from app.schemas.incident import IncidentRead
from app.schemas.redetect import RedetectIncident, RedetectKey, RedetectRequest, RedetectResult
from app.schemas.run import RunRead

_MAX_RECORDED_INCIDENTS = 1000


class RedetectService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.settings = settings
        self.runs = RunRepository(session)
        self.analysis = AnalysisRepository(session)
        self.incidents = IncidentRepository(session)
        self.heartbeats = HeartbeatRepository(session)

    async def redetect(self, run_id: uuid.UUID, req: RedetectRequest) -> RedetectResult:
        run = await self.runs.get(run_id)
        if run is None:
            raise NotFoundError(f"run {run_id} not found")
        if run.compacted_at is not None:
            raise InvalidRequestError(
                f"run {run_id} was compacted by data retention on "
                f"{run.compacted_at:%Y-%m-%d}; its telemetry is gone, so it can't be replayed"
            )
        n = await self.analysis.sample_count(run_id)
        if n > self.settings.redetect_max_samples:
            raise InvalidRequestError(
                f"run has {n} telemetry batches; re-detection is limited to "
                f"{self.settings.redetect_max_samples} (OPENFLEET_REDETECT_MAX_SAMPLES)"
            )

        cfg, resolve_after_s, overrides = self._config(req)
        batches = await self._batches(run_id)
        changes = [
            RecordedNodeChange(ts=e.ts, node=e.payload["node"], state=NodeState(e.payload["to"]))
            for e in await self.analysis.node_events(run_id)
        ]
        replayed = replay_run(
            robot_id=run.robot_id,
            run_id=run.id,
            software_version=run.software_version,
            batches=batches,
            node_changes=changes,
            ended_at=run.ended_at,
            contacts=[
                RecordedContact(start=s.started_at, end=s.ended_at)
                for s in await self.heartbeats.for_run(run_id)
            ],
            cfg=cfg,
            resolve_after_s=resolve_after_s,
        )
        recorded = list(
            reversed(
                await self.incidents.search(
                    status="all",
                    robot_id=None,
                    run_id=run_id,
                    severity=None,
                    limit=_MAX_RECORDED_INCIDENTS,
                )
            )
        )
        pairs = _pair(replayed, recorded)
        keys = _keys(run, replayed, recorded)

        notes = []
        if run.status == RunStatus.RUNNING:
            notes.append("The run is still RUNNING; the replay covers the batches so far.")
        if not batches:
            notes.append("The run has no telemetry batches.")
        if overrides:
            changed = ", ".join(f"{k}={v:g}" for k, v in sorted(overrides.items()))
            notes.append(f"Thresholds differ from the server's settings: {changed}.")
        if recorded:
            notes.append(
                "Recorded times are server time, replay times are robot time; offsets are "
                "comparable only when the robot clock was roughly right."
            )

        return RedetectResult(
            run=RunRead.model_validate(run),
            config={**asdict(cfg), "incident_resolve_after_s": resolve_after_s},
            overrides=overrides,
            batches=len(batches),
            incidents=[
                RedetectIncident(
                    **vars(r),
                    opened_offset_s=_offset(run, r.opened_at),
                    recorded_incident_id=pairs.get(id(r)),
                )
                for r in replayed
            ],
            recorded=[IncidentRead.model_validate(i) for i in recorded],
            keys=keys,
            caught=sum(k.verdict == "both" for k in keys),
            missed=sum(k.verdict == "recorded_only" for k in keys),
            new=sum(k.verdict == "replay_only" for k in keys),
            notes=notes,
        )

    def _config(self, req: RedetectRequest) -> tuple[DetectionConfig, float, dict[str, float]]:
        current = {
            **asdict(DetectionConfig.from_settings(self.settings)),
            "incident_resolve_after_s": self.settings.incident_resolve_after_s,
        }
        given = req.model_dump(exclude_none=True)
        overrides = {k: v for k, v in given.items() if v != current[k]}
        merged = {**current, **given}
        if merged["topic_failed_ratio"] > merged["topic_degraded_ratio"]:
            raise InvalidRequestError("topic_failed_ratio must not exceed topic_degraded_ratio")
        if merged["battery_critical_pct"] > merged["battery_low_pct"]:
            raise InvalidRequestError("battery_critical_pct must not exceed battery_low_pct")
        resolve_after_s = merged.pop("incident_resolve_after_s")
        return DetectionConfig(**merged), resolve_after_s, overrides

    async def _batches(self, run_id: uuid.UUID) -> list[RecordedBatch]:
        topics: dict[int, list[RecordedTopic]] = defaultdict(list)
        for t in await self.analysis.topic_samples(run_id):
            topics[t.seq].append(
                RecordedTopic(t.topic, t.expected_hz, t.actual_hz, t.last_msg_age_s)
            )
        return [
            RecordedBatch(
                ts=s.ts,
                cpu_pct=s.cpu_pct,
                mem_pct=s.mem_pct,
                battery_pct=s.battery_pct,
                topics=tuple(topics.get(s.seq, ())),
            )
            for s in await self.analysis.samples(run_id)
        ]


def _offset(run: Run, at: datetime) -> float:
    return (at - run.started_at).total_seconds()


def _pair(replayed: list[ReplayIncident], recorded: list[Incident]) -> dict[int, uuid.UUID]:
    """Per dedup_key, the n-th replayed incident pairs with the n-th recorded one."""
    by_key: dict[str, list[Incident]] = defaultdict(list)
    for inc in recorded:
        by_key[inc.dedup_key].append(inc)
    out: dict[int, uuid.UUID] = {}
    seen: dict[str, int] = defaultdict(int)
    for r in replayed:
        i = seen[r.dedup_key]
        seen[r.dedup_key] += 1
        if i < len(by_key[r.dedup_key]):
            out[id(r)] = by_key[r.dedup_key][i].id
    return out


def _max_severity(values: list[Severity]) -> Severity | None:
    return max(values, key=lambda s: s.rank) if values else None


def _keys(run: Run, replayed: list[ReplayIncident], recorded: list[Incident]) -> list[RedetectKey]:
    rep: dict[str, list[ReplayIncident]] = defaultdict(list)
    for r in replayed:
        rep[r.dedup_key].append(r)
    rec: dict[str, list[Incident]] = defaultdict(list)
    for i in recorded:
        rec[i.dedup_key].append(i)
    out = []
    for key in sorted(rep.keys() | rec.keys()):
        a, b = rec.get(key, []), rep.get(key, [])
        out.append(
            RedetectKey(
                dedup_key=key,
                rule=(b[0].rule if b else a[0].rule),
                verdict="both" if a and b else ("recorded_only" if a else "replay_only"),
                recorded_count=len(a),
                replayed_count=len(b),
                recorded_severity=_max_severity([Severity(i.severity) for i in a]),
                replayed_severity=_max_severity([r.severity for r in b]),
                first_recorded_offset_s=_offset(run, a[0].started_at) if a else None,
                first_replayed_offset_s=_offset(run, b[0].opened_at) if b else None,
            )
        )
    return out
