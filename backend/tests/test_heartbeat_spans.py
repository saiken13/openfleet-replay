"""Heartbeats stored per run as spans, and re-detection using them (DECISIONS #028)."""

from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import HeartbeatSpan
from app.detection.replay import RecordedContact
from app.workers.sweeper import Sweeper
from tests.conftest import Clock
from tests.helpers import T0, batch, ingest, register, start_run
from tests.test_redetect_replay import at, b, replay


async def heartbeat(client: httpx.AsyncClient, seq: float | None, robot: str = "robot-01") -> None:
    body = {"ts": (T0 + timedelta(seconds=seq)).isoformat()} if seq is not None else {}
    resp = await client.post(f"/api/v1/robots/{robot}/heartbeat", json=body)
    assert resp.status_code == 204, resp.text


async def spans(session: AsyncSession) -> list[tuple[float, float, int]]:
    rows = await session.scalars(select(HeartbeatSpan).order_by(HeartbeatSpan.started_at))
    return [
        ((r.started_at - T0).total_seconds(), (r.ended_at - T0).total_seconds(), r.heartbeats)
        for r in rows
    ]


# --- storage -------------------------------------------------------------------------------


async def test_heartbeats_during_a_run_merge_into_spans(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await register(client)
    await heartbeat(client, 0)  # no run yet: not stored
    await start_run(client)
    for s in (1, 2, 3, 7):  # 4 s apart is within the 5 s merge gap
        await heartbeat(client, s)
    for s in (20, 21):  # 13 s gap: a new span
        await heartbeat(client, s)
    await heartbeat(client, 0.5)  # late arrival, before the first span: extends nothing
    assert await spans(db_session) == [(0.5, 0.5, 1), (1, 7, 4), (20, 21, 2)]


async def test_heartbeat_without_ts_is_stored_in_server_time(
    client: httpx.AsyncClient, db_session: AsyncSession, clock: Clock
) -> None:
    await register(client)
    await start_run(client)
    await heartbeat(client, None)
    [row] = list(await db_session.scalars(select(HeartbeatSpan)))
    assert row.started_at == row.ended_at == clock.now


# --- replay (pure) -------------------------------------------------------------------------


def stall_batches() -> list:
    """/scan degraded for 0..9 s, no batches 10..39 s, healthy batches 40..49 s."""
    return [b(s, scan_hz=5) for s in range(10)] + [b(s) for s in range(40, 50)]


def test_telemetry_stall_with_heartbeats_is_not_silence() -> None:
    contacts = [RecordedContact(at(10), at(39))]
    [inc] = replay(stall_batches(), ended_at=at(50), contacts=contacts)
    # Live, each heartbeat re-detects it while /scan's data is fresh (until 9 + 10 s); then
    # the finding disappears with the robot still live, and hysteresis clears it 10 s later.
    assert inc.dedup_key == "topic_rate:/scan"
    assert inc.last_detected_at == at(19)
    assert inc.resolve_reason == "condition cleared" and inc.resolved_at == at(29)


def test_without_heartbeats_the_same_gap_is_silence() -> None:
    incs = replay(stall_batches(), ended_at=at(50))
    assert {i.dedup_key for i in incs} == {"topic_rate:/scan", "heartbeat_missing"}
    hb = next(i for i in incs if i.rule == "heartbeat_missing")
    assert hb.opened_at == at(19) + timedelta(milliseconds=1)


def test_silence_after_heartbeats_stop_opens_at_the_last_heartbeat_plus_timeout() -> None:
    # Heartbeats 10..15 s, then nothing until the batches resume at 40 s.
    incs = replay(stall_batches(), ended_at=at(50), contacts=[RecordedContact(at(10), at(15))])
    hb = next(i for i in incs if i.rule == "heartbeat_missing")
    assert hb.opened_at == at(25) + timedelta(milliseconds=1)
    # Silent until the batches resume at 40 s; less than a hysteresis delay of contact is
    # left before the run ends, and heartbeat incidents outlive the run (as live).
    assert hb.last_detected_at == at(40) - timedelta(milliseconds=1) and hb.resolved_at is None


def test_return_by_heartbeat_clears_the_heartbeat_incident() -> None:
    # Silent 10..24 s, heartbeats again from 25 s (no batches until 40 s).
    incs = replay(stall_batches(), ended_at=at(50), contacts=[RecordedContact(at(25), at(39))])
    hb = next(i for i in incs if i.rule == "heartbeat_missing")
    assert hb.opened_at == at(19) + timedelta(milliseconds=1)
    # Last detected just before contact resumed; cleared one hysteresis delay later,
    # without waiting for the next batch.
    assert hb.resolve_reason == "condition cleared"
    assert hb.resolved_at == hb.last_detected_at + timedelta(seconds=10)
    assert hb.resolved_at < at(40)


def test_silence_at_the_end_of_the_run_keeps_the_heartbeat_incident_open() -> None:
    batches = [b(s) for s in range(10)]
    incs = replay(batches, ended_at=at(60), contacts=[RecordedContact(at(10), at(20))])
    [hb] = incs
    assert hb.rule == "heartbeat_missing" and hb.opened_at == at(30) + timedelta(milliseconds=1)
    assert hb.resolved_at is None  # heartbeat incidents outlive the run, as live


# --- live parity ---------------------------------------------------------------------------


async def stalled_run(
    client: httpx.AsyncClient, clock: Clock, sweeper: Sweeper, *, heartbeats_until: int
) -> str:
    """/scan degraded for 0..9 s; telemetry stalls 10..39 s with heartbeats up to
    `heartbeats_until`, and the sweeper ticking; healthy batches 40..49 s; ended at 50 s.
    Server clock and robot clock advance together."""
    run_id = await start_run(client)
    for seq in range(50):
        clock.advance(1)
        if seq < 10 or seq >= 40:
            await ingest(client, run_id, batch(seq, hz={"/scan": 5.0 if seq < 10 else 15.0}))
        elif seq < heartbeats_until:
            await heartbeat(client, seq)
        await sweeper.tick(clock.now)
    clock.advance(1)
    resp = await client.post(
        f"/api/v1/runs/{run_id}/end",
        json={"status": "SUCCESS", "ended_at": (T0 + timedelta(seconds=50)).isoformat()},
    )
    assert resp.status_code == 200, resp.text
    return run_id


async def redetect(client: httpx.AsyncClient, run_id: str) -> dict[str, Any]:
    resp = await client.post(f"/api/v1/runs/{run_id}/redetect", json={})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_replay_matches_live_through_a_telemetry_stall(
    client: httpx.AsyncClient, clock: Clock, sweeper: Sweeper
) -> None:
    await register(client)
    run_id = await stalled_run(client, clock, sweeper, heartbeats_until=40)
    r = await redetect(client, run_id)
    # Before #028 the replay saw the stall as silence: a `new` heartbeat_missing incident.
    assert [(k["dedup_key"], k["verdict"]) for k in r["keys"]] == [("topic_rate:/scan", "both")]
    [inc] = r["incidents"]
    live = r["recorded"][0]
    assert inc["resolve_reason"] == "condition cleared"
    assert live["status"] == "RESOLVED"


async def test_replay_matches_live_when_heartbeats_stop_too(
    client: httpx.AsyncClient, clock: Clock, sweeper: Sweeper
) -> None:
    await register(client)
    run_id = await stalled_run(client, clock, sweeper, heartbeats_until=20)
    r = await redetect(client, run_id)
    assert {(k["dedup_key"], k["verdict"]) for k in r["keys"]} == {
        ("topic_rate:/scan", "both"),
        ("heartbeat_missing", "both"),
    }
    hb = next(i for i in r["incidents"] if i["rule"] == "heartbeat_missing")
    assert hb["opened_offset_s"] == 29.001  # last heartbeat at 19 s + 10 s timeout + 1 ms
