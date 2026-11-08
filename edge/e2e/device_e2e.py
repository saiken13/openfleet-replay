"""End-to-end test of the device gateway (docs/PROTOCOL.md, DECISIONS #033): a simulated
microcontroller (openfleet-device-sim) -> TCP "UART" -> the C++ edge agent -> OpenFleet.

Run from the repo root (needs Docker and `pip install httpx`):

    python edge/e2e/device_e2e.py

Asserts what OpenFleet observes: sensors as topics and tasks as nodes; a sensor that stops
opens a topic-rate incident; resets (with and without their HELLO) become events; corrupted,
truncated, duplicated, fragmented and oversized input is counted and never crashes the agent;
a silent device and a failed task open node incidents; the link reconnects.

Everything is simulated: the device is a host process, not a microcontroller.
"""

import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx

API = "http://localhost:8000/api/v1"
AUTH = {"Authorization": "Bearer ofu_demo_viewer_public-not-secret"}  # public demo viewer
ROBOT = "edge-01"
COMPOSE = ["docker", "compose", "--profile", "edge"]
STARTED_AT = ""


def sh(*args: str) -> None:
    subprocess.run([*COMPOSE, *args], check=True, capture_output=True)


def ctl(command: str) -> None:
    """A command for the simulated device (see DeviceSim::kCommands)."""
    sh("exec", "-T", "device-sim", "sh", "-c", f"echo '{command}' >> /tmp/devsim.ctl")


def get(path: str) -> Any:
    r = httpx.get(API + path, timeout=5, headers=AUTH)
    r.raise_for_status()
    return r.json()


def psql(sql: str) -> str:
    out = subprocess.run(
        [*COMPOSE, "exec", "-T", "postgres", "psql", "-U", "openfleet", "-d", "openfleet"]
        + ["-tAc", sql],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def wait_for(what: str, cond: Callable[[], bool], timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if cond():
                print(f"  ok  {what}")
                return
        except (httpx.HTTPError, KeyError, StopIteration, ValueError) as exc:
            last_exc = exc
        time.sleep(1)
    print(f"  FAIL {what} (timeout {timeout}s; last error: {last_exc})")
    print(f"  topics={_safe(lambda: get(f'/robots/{ROBOT}/topics'))}")
    print(f"  nodes={_safe(lambda: get(f'/robots/{ROBOT}/nodes'))}")
    print(f"  incidents={_safe(lambda: get(f'/incidents?status=open&robot_id={ROBOT}'))}")
    sys.exit(1)


def _safe(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 (diagnostics only)
        return f"<{exc}>"


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def topic(name: str) -> dict:
    t = next(t for t in get(f"/robots/{ROBOT}/topics") if t["topic"] == name)
    if ts(t["updated_at"]) < ts(STARTED_AT):
        raise KeyError(f"{name}: no data since the test started")
    return t


def node(name: str) -> dict:
    n = next(n for n in get(f"/robots/{ROBOT}/nodes") if n["node"] == name)
    if ts(n["updated_at"]) < ts(STARTED_AT):
        raise KeyError(f"{name}: no data since the test started")
    return n


def open_incident(key: str) -> dict | None:
    return next(
        (i for i in get(f"/incidents?status=open&robot_id={ROBOT}") if i["dedup_key"] == key),
        None,
    )


def logged(fragment: str) -> bool:
    return any(
        e["type"] == "log" and fragment in e["message"] and ts(e["ts"]) >= ts(STARTED_AT)
        for e in get(f"/robots/{ROBOT}/events?limit=200")
    )


def agent_running() -> bool:
    out = subprocess.run(
        [*COMPOSE, "ps", "--status", "running", "-q", "edge-agent"],
        check=True,
        capture_output=True,
        text=True,
    )
    return bool(out.stdout.strip())


def main() -> None:
    global STARTED_AT
    print("starting the backend, the edge agent and the simulated device")
    sh("up", "-d", "--build", "--wait", "postgres", "backend")
    STARTED_AT = psql(
        "select to_char(now() at time zone 'utc', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')"
    )
    sh("up", "-d", "--build", "--force-recreate", "edge-agent", "edge-worker", "device-sim")
    run_id = ""

    def new_run() -> bool:
        nonlocal run_id
        run_id = get(f"/robots/{ROBOT}")["current_run_id"] or ""
        return bool(run_id) and ts(get(f"/runs/{run_id}")["started_at"]) >= ts(STARTED_AT)

    wait_for("edge-01 opened a new run", new_run, timeout=60)

    print("1. sensors are topics, tasks are nodes")
    wait_for(
        "/mcu1/imu ~50 Hz HEALTHY (expected rate from DESCRIBE)",
        lambda: (
            (t := topic("/mcu1/imu"))["status"] == "HEALTHY"
            and t["expected_hz"] == 50
            and 45 <= t["actual_hz"] <= 55
        ),
        timeout=45,
    )
    wait_for("/mcu1/range ~10 Hz HEALTHY", lambda: topic("/mcu1/range")["status"] == "HEALTHY")
    for n in ("mcu1", "mcu1/sensor", "mcu1/comms", "mcu1/health"):
        wait_for(f"{n} RUNNING", lambda n=n: node(n)["state"] == "RUNNING")
    wait_for("device connection logged", lambda: logged("device link tcp://device-sim:4000"))

    print("2. a sensor stops")
    ctl("stop-sensor 2")
    wait_for("/mcu1/range FAILED", lambda: topic("/mcu1/range")["status"] == "FAILED")
    wait_for(
        "CRITICAL incident topic_rate:/mcu1/range",
        lambda: (open_incident("topic_rate:/mcu1/range") or {}).get("severity") == "CRITICAL",
    )
    ctl("start-sensor 2")
    wait_for(
        "range recovered and its incident resolved",
        lambda: (
            topic("/mcu1/range")["status"] == "HEALTHY"
            and open_incident("topic_rate:/mcu1/range") is None
        ),
        timeout=45,
    )

    print("3. resets become events")
    ctl("reset watchdog")
    wait_for("watchdog reset logged with its reason", lambda: logged("mcu1 reset (watchdog)"))
    ctl("reset-quiet")
    wait_for("reset without HELLO detected", lambda: logged("its HELLO was lost"))
    wait_for("sensors flow again after the resets", lambda: topic("/mcu1/imu")["actual_hz"] > 40)

    print("4. a bad link: corrupt, truncate, duplicate, fragment, garbage, oversize")
    for c in ("corrupt 0.1", "truncate 0.05", "duplicate 0.2", "fragment on"):
        ctl(c)
    ctl("garbage 3000")
    ctl("oversize")
    wait_for("bad frames counted and reported", lambda: logged("bad frames (crc"), timeout=40)
    time.sleep(5)
    wait_for("agent still running", agent_running)
    wait_for(
        "imu still delivered (duplicates not double-counted)",
        lambda: 30 <= topic("/mcu1/imu")["actual_hz"] <= 55,
    )
    ctl("faults off")

    print("5. the device goes silent")
    ctl("silence 10")
    wait_for("mcu1 STOPPED", lambda: node("mcu1")["state"] == "STOPPED")
    wait_for("node_health incident for mcu1", lambda: open_incident("node_health:mcu1") is not None)
    wait_for("mcu1 RUNNING again", lambda: node("mcu1")["state"] == "RUNNING", timeout=30)
    wait_for(
        "node_health:mcu1 resolved", lambda: open_incident("node_health:mcu1") is None, timeout=45
    )

    print("6. a task fails")
    ctl("task 2 error")
    wait_for("mcu1/comms ERROR", lambda: node("mcu1/comms")["state"] == "ERROR")
    wait_for("node_health:mcu1/comms", lambda: open_incident("node_health:mcu1/comms") is not None)
    ctl("task 2 running")
    wait_for(
        "mcu1/comms resolved",
        lambda: open_incident("node_health:mcu1/comms") is None,
        timeout=45,
    )

    print("7. the link drops and comes back")
    sh("stop", "-t", "1", "device-sim")
    wait_for("link loss logged", lambda: logged("device link tcp://device-sim:4000 lost"))
    wait_for("mcu1 STOPPED", lambda: node("mcu1")["state"] == "STOPPED")
    sh("start", "device-sim")
    wait_for("mcu1 RUNNING after reconnect", lambda: node("mcu1")["state"] == "RUNNING", timeout=40)

    wait_for(
        "one run throughout, still RUNNING",
        lambda: (
            get(f"/robots/{ROBOT}")["current_run_id"] == run_id
            and get(f"/runs/{run_id}")["status"] == "RUNNING"
        ),
    )
    print("PASS: device gateway end-to-end")


if __name__ == "__main__":
    try:
        main()
    finally:
        subprocess.run(
            [*COMPOSE, "stop", "edge-agent", "edge-worker", "device-sim"],
            check=False,
            capture_output=True,
        )
