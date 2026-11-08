"""Data retention (DECISIONS #030): compacting old runs keeps their answers, drops raw rows."""

import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.models import Event, HeartbeatSpan, Incident, Run, TelemetrySample, TopicSample
from app.realtime.hub import Hub
from app.services.retention_service import RetentionService
from app.workers.sweeper import Sweeper
from openfleet_cli.main import EXIT_ERROR, EXIT_OK, main
from tests.conftest import Clock
from tests.helpers import T0, batch, ingest, register, start_run

LOG = {"level": "WARN", "source": "lidar_driver", "message": "scan timeout"}


async def recorded_run(
    client: httpx.AsyncClient, clock: Clock, robot: str = "robot-01", offset_s: float = 0
) -> str:
    """20 batches with a degraded /scan (an incident), a log line per batch and a
    heartbeat, then ended 20 s after it started (T0 + offset_s). Two runs on one robot need
    different offsets: with equal started_at the latest run is decided by its random id
    (#016), and the new run could be aborted as superseded by the finished one."""
    start = T0 + timedelta(seconds=offset_s)
    run_id = await start_run(client, robot, started_at=start)
    for seq in range(20):
        clock.advance(1)
        ts = (start + timedelta(seconds=seq)).isoformat()
        body = batch(seq, hz={"/scan": 4.0 if 5 <= seq < 12 else 15.0})
        body["logs"] = [{**LOG, "ts": ts}]
        await ingest(client, run_id, body)
        hb = await client.post(f"/api/v1/robots/{robot}/heartbeat", json={"ts": ts})
        assert hb.status_code == 204
    resp = await client.post(
        f"/api/v1/runs/{run_id}/end",
        json={"status": "SUCCESS", "ended_at": (start + timedelta(seconds=20)).isoformat()},
    )
    assert resp.status_code == 200, resp.text
    return run_id


async def count(db: AsyncSession, model, run_id: str, *where) -> int:
    stmt = select(func.count()).select_from(model).where(model.run_id == uuid.UUID(run_id), *where)
    return int(await db.scalar(stmt) or 0)


async def get(client: httpx.AsyncClient, path: str, **params) -> dict:
    resp = await client.get(path, params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_compacting_keeps_summary_compare_and_incidents_but_drops_raw_rows(
    client: httpx.AsyncClient, clock: Clock, db_session: AsyncSession
) -> None:
    await register(client)
    run_id = await recorded_run(client, clock)
    base = await recorded_run(client, clock, offset_s=30)
    before = await get(client, f"/api/v1/runs/{run_id}/summary")
    compare_before = await get(client, "/api/v1/runs/compare", base=base, candidate=run_id)
    incidents_before = await count(db_session, Incident, run_id)
    state_events = await count(db_session, Event, run_id, Event.type != "log")
    assert incidents_before >= 1 and await count(db_session, HeartbeatSpan, run_id) == 1

    done = await RetentionService(db_session).compact(uuid.UUID(run_id), clock.now)

    assert done is not None
    assert (done.samples, done.topic_samples, done.heartbeat_spans, done.log_events) == (
        20,
        40,  # /scan and /odom
        1,
        20,
    )
    for model in (TelemetrySample, TopicSample, HeartbeatSpan):
        assert await count(db_session, model, run_id) == 0
    assert await count(db_session, Event, run_id, Event.type == "log") == 0
    assert await count(db_session, Event, run_id) == state_events  # changes are kept
    assert await count(db_session, Incident, run_id) == incidents_before

    after = await get(client, f"/api/v1/runs/{run_id}/summary")
    assert after["run"]["compacted_at"] is not None
    before["run"]["compacted_at"] = after["run"]["compacted_at"]
    assert after == before  # identical numbers, from the frozen aggregates
    compare_after = await get(client, "/api/v1/runs/compare", base=base, candidate=run_id)
    compare_before["candidate"]["run"]["compacted_at"] = after["run"]["compacted_at"]
    assert compare_after == compare_before

    resp = await client.post(f"/api/v1/runs/{run_id}/redetect", json={})
    assert resp.status_code == 422 and "compacted by data retention" in resp.json()["detail"]
    run = await get(client, f"/api/v1/runs/{run_id}")
    assert run["compacted_at"] is not None


async def test_only_finished_old_uncompacted_runs_are_due(
    client: httpx.AsyncClient, clock: Clock, db_session: AsyncSession
) -> None:
    await register(client)
    old = await recorded_run(client, clock)
    await register(client, "robot-02")  # its own robot: a new run would supersede `old`'s
    running = await start_run(client, "robot-02", started_at=T0 - timedelta(days=30))
    svc = RetentionService(db_session)
    now = T0 + timedelta(days=10)

    assert await svc.due(now, timedelta(days=11), 10) == []  # not old enough
    assert await svc.due(now, timedelta(days=9), 10) == [uuid.UUID(old)]
    assert await svc.compact(uuid.UUID(running), now) is None  # RUNNING: never
    assert await svc.compact(uuid.UUID(old), now) is not None
    assert await svc.compact(uuid.UUID(old), now) is None  # already compacted
    assert await svc.due(now, timedelta(days=9), 10) == []


async def test_sweeper_compacts_at_most_once_per_interval(
    client: httpx.AsyncClient, clock: Clock, db_session: AsyncSession, settings: Settings
) -> None:
    runs = set()
    for robot in ("robot-01", "robot-02", "robot-03"):  # all recorded before any tick
        await register(client, robot)
        runs.add(await recorded_run(client, clock, robot))
    maker = async_sessionmaker(
        bind=db_session.bind, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    cfg = settings.model_copy(
        update={"retention_days": 1.0, "retention_interval_s": 3600.0, "retention_batch_runs": 1}
    )
    sweeper = Sweeper(maker, Hub(), cfg)
    now = T0 + timedelta(days=2)

    [a] = (await sweeper.tick(now)).compacted_runs  # one per pass (batch size 1)
    assert (await sweeper.tick(now + timedelta(minutes=59))).compacted_runs == []
    [b] = (await sweeper.tick(now + timedelta(minutes=61))).compacted_runs
    assert {a, b} < runs and a != b

    [left] = runs - {a, b}
    off = Sweeper(maker, Hub(), settings)  # retention_days unset: never
    assert (await off.tick(now + timedelta(days=30))).compacted_runs == []
    assert (await db_session.get(Run, uuid.UUID(left))).compacted_at is None


# --- openfleet retention (real commits under the "concurrency-cli-" prefix) -----------------


async def _commit_old_run(url: str) -> str:
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.db.models import Robot

    engine = create_async_engine(url)
    run_id = uuid.uuid4()
    ended = datetime(2020, 1, 1, tzinfo=UTC)
    try:
        async with AsyncSession(engine) as s:
            s.add(Robot(id="concurrency-cli-ret", name="ret"))
            await s.flush()
            s.add(
                Run(
                    id=run_id,
                    robot_id="concurrency-cli-ret",
                    status="SUCCESS",
                    started_at=ended - timedelta(seconds=5),
                    ended_at=ended,
                )
            )
            await s.flush()
            for seq in range(5):
                s.add(
                    TelemetrySample(
                        run_id=run_id,
                        robot_id="concurrency-cli-ret",
                        seq=seq,
                        ts=ended - timedelta(seconds=5 - seq),
                        cpu_pct=10.0 + seq,
                    )
                )
            await s.commit()
    finally:
        await engine.dispose()
    return str(run_id)


@pytest.fixture
def cli_db(migrated_database: str):
    from tests.test_cli import _purge_cli_robots

    _purge_cli_robots(migrated_database)
    yield ["retention", "--database-url", migrated_database]
    _purge_cli_robots(migrated_database)


def test_cli_dry_run_then_compact(cli_db: list[str], migrated_database: str, capsys) -> None:
    import asyncio

    run_id = asyncio.run(_commit_old_run(migrated_database))
    # 2000 days reaches back to mid-2021: only runs that ended before then, i.e. this one.
    assert main([*cli_db, "--days", "2000", "--dry-run", "--json"]) == EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] and [(r["run_id"], r["samples"]) for r in out["runs"]] == [(run_id, 5)]

    assert main([*cli_db, "--days", "2000"]) == EXIT_OK
    text = capsys.readouterr().out
    assert f"compacted {run_id}: 5 samples" in text and "compacted 1 run(s)" in text
    assert main([*cli_db, "--days", "2000", "--json"]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["runs"] == []  # nothing left to do


def test_cli_requires_days(cli_db: list[str], capsys) -> None:
    assert main(cli_db) == EXIT_ERROR
    assert "--days" in capsys.readouterr().err
