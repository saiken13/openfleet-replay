"""Uplink behaviour against a fake OpenFleet API (httpx.MockTransport)."""

import json

import httpx

from openfleet_collector.uplink import Uplink


class FakeServer:
    def __init__(self) -> None:
        self.down = False
        self.telemetry_status = 200  # status to return for telemetry
        self.write_status = 0  # if set, every request gets this status (e.g. 401)
        self.auth: list[str | None] = []
        self.calls: list[tuple[str, str, dict]] = []
        self.runs: list[str] = []
        self.received: list[tuple[str, int]] = []  # (run_id, seq)

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("connection refused")
        body = json.loads(req.content) if req.content else {}
        path = req.url.path
        self.calls.append((req.method, path, body))
        self.auth.append(req.headers.get("Authorization"))
        if self.write_status:
            return httpx.Response(self.write_status, json={"detail": "bad key"})
        if req.method == "PUT" and path.startswith("/api/v1/runs/"):
            self.runs.append(path.rsplit("/", 1)[1])
            return httpx.Response(201, json={})
        if path.endswith("/telemetry"):
            if self.telemetry_status != 200:
                return httpx.Response(self.telemetry_status, json={"detail": "nope"})
            self.received.append((path.split("/")[4], body["seq"]))
            return httpx.Response(200, json={"accepted": True, "duplicate": False})
        return httpx.Response(200, json={})


def make(server: FakeServer, **kw) -> Uplink:
    return Uplink(
        "http://api",
        "robot-01",
        software_version="2.0",
        transport=httpx.MockTransport(server),
        sleep=lambda s: None,
        **kw,
    )


def batch(u: Uplink) -> dict:
    return {"seq": u.next_seq(), "ts": "2026-11-05T12:00:00+00:00"}


def drain(u: Uplink, limit: int = 100) -> None:
    for _ in range(limit):
        if not u.pump():
            return


def test_start_registers_and_opens_run() -> None:
    srv = FakeServer()
    u = make(srv)
    u.start()
    assert [(m, p.split("/")[3]) for m, p, _ in srv.calls] == [("PUT", "robots"), ("PUT", "runs")]
    run_body = srv.calls[1][2]
    assert run_body["robot_id"] == "robot-01" and run_body["software_version"] == "2.0"
    assert u.run_id == srv.runs[0]


def test_start_retries_until_api_is_reachable() -> None:
    srv = FakeServer()
    srv.down = True
    attempts = []

    def sleep(s: float) -> None:
        attempts.append(s)
        if len(attempts) == 3:
            srv.down = False

    u = Uplink("http://api", "robot-01", transport=httpx.MockTransport(srv), sleep=sleep)
    u.start()
    assert u.run_id is not None
    assert attempts == [0.5, 1.0, 2.0]  # exponential backoff


def test_batches_delivered_in_order() -> None:
    srv = FakeServer()
    u = make(srv)
    u.start()
    for _ in range(5):
        u.enqueue(batch(u))
    drain(u)
    assert [s for _, s in srv.received] == [0, 1, 2, 3, 4]
    assert u.pending() == 0 and u.sent == 5


def test_outage_buffers_then_resends_same_seq_in_order() -> None:
    srv = FakeServer()
    u = make(srv)
    u.start()
    u.enqueue(batch(u))
    drain(u)
    srv.down = True
    for _ in range(3):
        u.enqueue(batch(u))
    assert u.pump() is False and u.pending() == 3  # nothing lost while down
    srv.down = False
    drain(u)
    assert [s for _, s in srv.received] == [0, 1, 2, 3]


def test_5xx_is_retried_not_dropped() -> None:
    srv = FakeServer()
    u = make(srv)
    u.start()
    u.enqueue(batch(u))
    srv.telemetry_status = 503
    assert u.pump() is False and u.pending() == 1
    srv.telemetry_status = 200
    drain(u)
    assert u.pending() == 0 and u.dropped == 0


def test_buffer_is_bounded_dropping_oldest() -> None:
    srv = FakeServer()
    u = make(srv, buffer_max=10)
    u.start()
    srv.down = True
    for _ in range(15):
        u.enqueue(batch(u))
    assert u.pending() == 10 and u.dropped == 5
    srv.down = False
    drain(u)
    assert [s for _, s in srv.received] == list(range(5, 15))


def test_409_run_aborted_starts_new_run_and_resets_seq() -> None:
    """E.g. a long outage: the sweeper aborted our run while we were buffering."""
    srv = FakeServer()
    u = make(srv)
    u.start()
    first_run = u.run_id
    u.enqueue(batch(u))
    u.enqueue(batch(u))
    srv.telemetry_status = 409
    assert u.pump() is True  # recovered: new run started
    assert u.run_id != first_run and len(srv.runs) == 2
    assert u.pending() == 0 and u.dropped == 2  # batches of the dead run are discarded
    srv.telemetry_status = 200
    u.enqueue(batch(u))
    drain(u)
    assert srv.received == [(u.run_id, 0)]  # seq restarts for the new run


def test_404_reregisters_robot() -> None:
    """E.g. the server's database was reset."""
    srv = FakeServer()
    u = make(srv)
    u.start()
    u.enqueue(batch(u))
    srv.telemetry_status = 404
    u.pump()
    puts = [p.split("/")[3] for m, p, _ in srv.calls if m == "PUT"]
    assert puts == ["robots", "runs", "robots", "runs"]


def test_invalid_batch_is_dropped_not_retried_forever() -> None:
    srv = FakeServer()
    u = make(srv)
    u.start()
    u.enqueue(batch(u))
    srv.telemetry_status = 422
    assert u.pump() is True
    assert u.pending() == 0 and u.dropped == 1


def test_end_flushes_then_ends_run() -> None:
    srv = FakeServer()
    u = make(srv)
    u.start()
    for _ in range(3):
        u.enqueue(batch(u))
    u.end("SUCCESS")
    assert [s for _, s in srv.received] == [0, 1, 2]
    method, path, body = srv.calls[-1]
    assert path.endswith(f"/runs/{u.run_id}/end") and body["status"] == "SUCCESS"


def test_batches_before_start_are_ignored() -> None:
    srv = FakeServer()
    u = make(srv)
    u.enqueue({"seq": 0, "ts": "x"})
    assert u.pending() == 0


def test_every_request_carries_the_api_key() -> None:
    srv = FakeServer()
    u = make(srv, api_key="ofk_test")
    u.start()
    u.enqueue(batch(u))
    drain(u)
    u.end()
    assert len(srv.auth) == 4 and set(srv.auth) == {"Bearer ofk_test"}


def test_rejected_key_keeps_retrying_start() -> None:
    srv = FakeServer()
    srv.write_status = 401
    u = make(srv, api_key="wrong")
    attempts = 0

    def sleep(_: float) -> None:  # the operator issues the key after a few retries
        nonlocal attempts
        attempts += 1
        if attempts == 3:
            srv.write_status = 0

    u.sleep = sleep
    u.start()
    assert attempts == 3 and u.run_id is not None


def test_rejected_key_buffers_batches_instead_of_dropping() -> None:
    srv = FakeServer()
    u = make(srv, api_key="k")
    u.start()
    u.enqueue(batch(u))
    u.enqueue(batch(u))
    srv.telemetry_status = 403
    assert u.pump() is False
    assert u.pending() == 2 and u.dropped == 0
    srv.telemetry_status = 200
    drain(u)
    assert [seq for _, seq in srv.received] == [0, 1] and u.dropped == 0


def test_api_key_from_environment_overrides_config(tmp_path, monkeypatch) -> None:
    from openfleet_collector.config import load_config

    path = tmp_path / "c.yaml"
    path.write_text("robot_id: r1\napi_key: from-file\n")
    monkeypatch.delenv("OPENFLEET_API_KEY", raising=False)
    assert load_config(path).api_key == "from-file"
    monkeypatch.setenv("OPENFLEET_API_KEY", "from-env")
    assert load_config(path).api_key == "from-env"
