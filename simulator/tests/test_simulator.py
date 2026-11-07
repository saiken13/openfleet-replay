import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from openfleet_sim.client import ApiError, FleetApi
from openfleet_sim.main import main, run_scenario
from openfleet_sim.robot import SimRobot
from openfleet_sim.scenario import Scenario, load_scenario

SCENARIOS = Path(__file__).resolve().parents[1] / "scenarios"
T0 = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


def at(t: float) -> datetime:
    return T0 + timedelta(seconds=t)


@pytest.mark.parametrize("path", sorted(SCENARIOS.glob("*.yaml")), ids=lambda p: p.name)
def test_bundled_scenarios_are_valid(path: Path) -> None:
    assert load_scenario(path).name


def test_same_seed_same_batches() -> None:
    sc = load_scenario(SCENARIOS / "demo.yaml")
    a = [SimRobot(1, "amr-02", sc).step(t, at(t)) for t in range(0, 90)]
    b = [SimRobot(1, "amr-02", sc).step(t, at(t)) for t in range(0, 90)]
    assert a == b


def test_failures_apply_in_their_window_and_only_to_targets() -> None:
    sc = load_scenario(SCENARIOS / "demo.yaml")
    target, other = SimRobot(1, "amr-02", sc), SimRobot(0, "amr-01", sc)

    def scan(batch):
        return next(t for t in batch["topics"] if t["topic"] == "/scan")["actual_hz"]

    rates = {t: (scan(target.step(t, at(t))), scan(other.step(t, at(t)))) for t in range(0, 95)}
    assert 13 < rates[10][0] < 17  # before: nominal 15 Hz with noise
    assert 3 < rates[40][0] < 5  # degraded window
    assert rates[70][0] == 0  # stopped window
    assert 13 < rates[93][0] < 17  # recovered
    assert all(13 < o < 17 for _, o in rates.values())  # control robot untouched


def test_offline_robot_sends_nothing_and_logs_fire_once() -> None:
    sc = load_scenario(SCENARIOS / "demo.yaml")
    r = SimRobot(3, "amr-04", sc)
    batches = {t: r.step(t, at(t)) for t in range(0, 140)}
    assert batches[99] is not None and batches[100] is None and batches[129] is None
    assert batches[130] is not None
    seqs = [b["seq"] for b in batches.values() if b]
    assert seqs == list(range(len(seqs)))  # contiguous: no gaps from the offline window

    crash = SimRobot(1, "amr-02", sc)
    logs = [log for t in range(0, 90) for log in crash.step(t, at(t))["logs"]]
    assert [lg["message"] for lg in logs] == [
        "LaserScan rate dropping: USB bandwidth contention",
        "lidar_driver: device /dev/ttyUSB0 disconnected",
    ]


def test_scenario_validation() -> None:
    with pytest.raises(ValidationError, match="unknown topic"):
        Scenario.model_validate(
            {
                "name": "x",
                "failures": [{"kind": "topic_rate", "topic": "/nope", "hz": 1, "at_s": 0}],
            }
        )
    with pytest.raises(ValidationError, match="outside fleet"):
        Scenario.model_validate(
            {
                "name": "x",
                "fleet": {"count": 1},
                "failures": [{"kind": "cpu", "robots": [3], "pct": 99, "at_s": 0}],
            }
        )


# --- client / runner against a fake API ----------------------------------------------------


class FakeApi:
    """Records requests; optionally fails the first N calls with 503 to exercise retries."""

    def __init__(self, fail_first: int = 0) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.auth: list[tuple[str, str | None]] = []  # (path, Authorization header)
        self.fail_first = fail_first

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.fail_first > 0:
            self.fail_first -= 1
            return httpx.Response(503)
        body = json.loads(request.content) if request.content else {}
        self.calls.append((request.method, request.url.path, body))
        self.auth.append((request.url.path, request.headers.get("Authorization")))
        if request.url.path.endswith("/health"):
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, json={})


async def test_run_scenario_drives_the_full_lifecycle() -> None:
    fake = FakeApi()
    api = FleetApi("http://api", transport=httpx.MockTransport(fake), backoff_s=0)
    sc = Scenario.model_validate({"name": "t", "duration_s": 5, "fleet": {"count": 2}})

    runs = await run_scenario(api, sc, fast=True)

    paths = [(m, p.split("/")[3]) for m, p, _ in fake.calls]
    assert paths.count(("PUT", "robots")) == 2
    assert paths.count(("PUT", "runs")) == 2
    telemetry = [b for m, p, b in fake.calls if p.endswith("/telemetry")]
    assert len(telemetry) == 10  # 2 robots x 5 ticks
    ends = [b for m, p, b in fake.calls if p.endswith("/end")]
    assert [e["status"] for e in ends] == ["SUCCESS", "SUCCESS"]
    assert {r["robot_id"] for r in runs} == {"amr-01", "amr-02"}


async def test_stalled_robot_heartbeats_in_robot_time_instead_of_sending_telemetry() -> None:
    fake = FakeApi()
    api = FleetApi("http://api", transport=httpx.MockTransport(fake), backoff_s=0)
    sc = load_scenario(SCENARIOS / "telemetry_stall.yaml")  # stalled for 20..44 s of 60

    await run_scenario(api, sc, fast=True)

    telemetry = [b for m, p, b in fake.calls if p.endswith("/telemetry")]
    beats = [b for m, p, b in fake.calls if p.endswith("/heartbeat")]
    assert len(telemetry) == 35 and len(beats) == 25
    ts = [datetime.fromisoformat(b["ts"]) for b in beats]
    run_start = datetime.fromisoformat(telemetry[0]["ts"])
    assert [(t - run_start).total_seconds() for t in ts] == [float(s) for s in range(20, 45)]
    assert {b["software_version"] for b in beats} == {sc.software_version}


async def test_client_retries_5xx_but_not_4xx() -> None:
    fake = FakeApi(fail_first=2)
    api = FleetApi("http://api", transport=httpx.MockTransport(fake), backoff_s=0)
    await api.register("r1", "R1", "m")  # succeeds on the 3rd attempt
    assert len(fake.calls) == 1

    def reject(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "conflict"})

    api409 = FleetApi("http://api", transport=httpx.MockTransport(reject), backoff_s=0)
    with pytest.raises(ApiError, match="409"):
        await api409.register("r1", "R1", "m")


async def test_cancellation_ends_open_runs_as_aborted() -> None:
    """SIGTERM / Ctrl-C cancel the task: runs must be ended, not left for the sweeper."""
    import asyncio

    fake = FakeApi()
    api = FleetApi("http://api", transport=httpx.MockTransport(fake), backoff_s=0)
    sc = Scenario.model_validate({"name": "t", "duration_s": 60, "fleet": {"count": 2}})

    task = asyncio.create_task(run_scenario(api, sc, speed=1.0))  # real time: ~60s
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    ends = [(p, b) for m, p, b in fake.calls if p.endswith("/end")]
    assert len(ends) == 2
    assert {b["status"] for _, b in ends} == {"ABORTED"}
    assert any(p.endswith("/telemetry") for _, p, _ in fake.calls)


async def test_each_request_is_signed_with_its_own_robots_key() -> None:
    fake = FakeApi()
    keys = {"amr-01": "key-for-amr-01-xxxx", "amr-02": "key-for-amr-02-xxxx"}
    api = FleetApi("http://api", keys, transport=httpx.MockTransport(fake), backoff_s=0)
    sc = Scenario.model_validate({"name": "t", "duration_s": 3, "fleet": {"count": 2}})
    runs = await run_scenario(api, sc, fast=True)

    run_robot = {r["run_id"]: r["robot_id"] for r in runs}
    assert len(fake.auth) == 2 + 2 + 6 + 2  # register, start, telemetry, end
    for path, header in fake.auth:
        parts = path.split("/")  # /api/v1/{robots|runs}/{id}/...
        robot_id = parts[4] if parts[3] == "robots" else run_robot[parts[4]]
        assert header == f"Bearer {keys[robot_id]}", path


def test_main_requires_a_key_for_every_robot(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("OPENFLEET_ROBOT_KEYS_FILE", raising=False)
    scenario = str(SCENARIOS / "demo.yaml")
    assert main(["-s", scenario, "--api", "http://127.0.0.1:1"]) == 2  # no keys at all

    partial = tmp_path / "keys.json"
    partial.write_text(json.dumps({"amr-01": "key-for-amr-01-xxxx"}))
    assert main(["-s", scenario, "--keys", str(partial), "--api", "http://127.0.0.1:1"]) == 2

    partial.write_text("[1, 2]")
    assert main(["-s", scenario, "--keys", str(partial), "--api", "http://127.0.0.1:1"]) == 2


def test_bundled_demo_keys_cover_every_scenario() -> None:
    from openfleet_sim.client import load_keys

    keys = load_keys(str(SCENARIOS.parents[1] / "examples" / "demo-robot-keys.json"))
    for path in SCENARIOS.glob("*.yaml"):
        assert set(load_scenario(path).robot_ids()) <= set(keys), path.name
