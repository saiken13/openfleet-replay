"""End-to-end: a microcontroller on a ROS 2 robot, reported by that robot's collector.

    device-sim --TCP "UART"--> openfleet-ros-bridge --ROS 2--> openfleet-collector --> OpenFleet

Run from the repo root (needs Docker and `pip install httpx`):

    python edge/ros/e2e.py

Drives `docker compose --profile ros` and asserts what OpenFleet shows for robot ros-mcu-01:
sensors as topics at their rates, the device and its tasks as nodes (through /diagnostics),
a stopped sensor, a watchdog reset (through /rosout), a failed task, a silent device, and the
bridge itself going away and coming back, all in one run (DECISIONS #037).

Everything is simulated: the device is a host process, ROS 2 runs in containers.
"""

import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx

API = "http://localhost:8000/api/v1"
AUTH = {"Authorization": "Bearer ofu_demo_viewer_public-not-secret"}
ROBOT = "ros-mcu-01"
COMPOSE = ["docker", "compose", "--profile", "ros"]
SERVICES = ["ros-device-sim", "ros-bridge", "ros-mcu-collector"]
STARTED_AT = ""


def sh(*args: str) -> None:
    subprocess.run([*COMPOSE, *args], check=True, capture_output=True)


def ctl(command: str) -> None:
    sh("exec", "-T", "ros-device-sim", "sh", "-c", f"echo '{command}' >> /tmp/devsim.ctl")


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
                print(f"  ok  {what}", flush=True)
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


def fresh(row: dict, name: str) -> dict:
    if ts(row["updated_at"]) < ts(STARTED_AT):
        raise KeyError(f"{name}: no data since the test started")
    return row


def topic(name: str) -> dict:
    return fresh(next(t for t in get(f"/robots/{ROBOT}/topics") if t["topic"] == name), name)


def node(name: str) -> dict:
    return fresh(next(n for n in get(f"/robots/{ROBOT}/nodes") if n["node"] == name), name)


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


def main() -> None:
    global STARTED_AT
    print("starting the backend, the device simulator, the ROS bridge and the collector")
    # Recreated so it seeds this robot's demo key (examples/demo-robot-keys.json).
    sh("up", "-d", "--build", "--force-recreate", "--wait", "postgres", "backend")
    STARTED_AT = psql(
        "select to_char(now() at time zone 'utc', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')"
    )
    sh("up", "-d", "--build", "--force-recreate", *SERVICES)
    run_id = ""

    def new_run() -> bool:
        nonlocal run_id
        run_id = get(f"/robots/{ROBOT}")["current_run_id"] or ""
        return bool(run_id) and ts(get(f"/runs/{run_id}")["started_at"]) >= ts(STARTED_AT)

    wait_for(f"{ROBOT} opened a new run (the collector)", new_run, timeout=90)

    print("1. sensors are ROS topics; the device and its tasks are nodes via /diagnostics")
    wait_for(
        "/mcu1/imu ~50 Hz HEALTHY, measured on the ROS topic",
        lambda: (t := topic("/mcu1/imu"))["status"] == "HEALTHY" and 45 <= t["actual_hz"] <= 55,
        timeout=60,
    )
    wait_for("/mcu1/range ~10 Hz HEALTHY", lambda: topic("/mcu1/range")["status"] == "HEALTHY")
    for n in ("mcu1", "mcu1/sensor", "mcu1/comms", "mcu1/health", "openfleet_ros_bridge"):
        wait_for(f"{n} RUNNING", lambda n=n: node(n)["state"] == "RUNNING")

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

    print("3. a watchdog reset: a WARN on /rosout, the device DEGRADED for the reset hold")
    ctl("reset watchdog")
    wait_for("reset logged with its reason", lambda: logged("mcu1 reset (watchdog)"))
    wait_for("mcu1 DEGRADED (diagnostics WARN)", lambda: node("mcu1")["state"] == "DEGRADED")
    wait_for("node_health:mcu1 opened", lambda: open_incident("node_health:mcu1") is not None)
    wait_for(
        "resolved after the reset hold (20 s) and hysteresis",
        lambda: open_incident("node_health:mcu1") is None and node("mcu1")["state"] == "RUNNING",
        timeout=60,
    )

    print("4. a task fails")
    ctl("task 2 error")
    wait_for("mcu1/comms ERROR", lambda: node("mcu1/comms")["state"] == "ERROR")
    wait_for("node_health:mcu1/comms", lambda: open_incident("node_health:mcu1/comms") is not None)
    ctl("task 2 running")
    wait_for(
        "mcu1/comms resolved", lambda: open_incident("node_health:mcu1/comms") is None, timeout=45
    )

    print("5. the device goes silent")
    ctl("silence 10")
    wait_for("mcu1 STOPPED (diagnostics STALE)", lambda: node("mcu1")["state"] == "STOPPED")
    wait_for("node_health incident for mcu1", lambda: open_incident("node_health:mcu1") is not None)
    wait_for("mcu1 RUNNING again", lambda: node("mcu1")["state"] == "RUNNING", timeout=30)

    print("6. the bridge goes away and comes back")
    sh("stop", "-t", "2", "ros-bridge")
    wait_for(
        "openfleet_ros_bridge STOPPED (gone from the ROS graph)",
        lambda: node("openfleet_ros_bridge")["state"] == "STOPPED",
    )
    wait_for(
        "mcu1 STOPPED (its diagnostics stopped arriving)",
        lambda: node("mcu1")["state"] == "STOPPED",
    )
    wait_for("/mcu1/imu FAILED", lambda: topic("/mcu1/imu")["status"] == "FAILED")
    sh("start", "ros-bridge")
    wait_for(
        "bridge, device and imu back",
        lambda: (
            node("openfleet_ros_bridge")["state"] == "RUNNING"
            and node("mcu1")["state"] == "RUNNING"
            and topic("/mcu1/imu")["status"] == "HEALTHY"
        ),
        timeout=60,
    )

    wait_for(
        "one run throughout, still RUNNING",
        lambda: (
            get(f"/robots/{ROBOT}")["current_run_id"] == run_id
            and get(f"/runs/{run_id}")["status"] == "RUNNING"
        ),
    )
    print("PASS: microcontroller on a ROS robot end-to-end")


if __name__ == "__main__":
    try:
        main()
    finally:
        subprocess.run([*COMPOSE, "stop", *SERVICES], check=False, capture_output=True)
