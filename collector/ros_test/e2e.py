"""End-to-end test: real ROS 2 (Jazzy) fake robot + collector + OpenFleet backend.

Run from the repo root (needs Docker and `pip install httpx`):

    python collector/ros_test/e2e.py

Drives `docker compose --profile ros` and asserts what OpenFleet observes at each step:
healthy rate -> degraded -> stopped -> node killed -> backend outage (no data loss) ->
recovery -> graceful shutdown. Exits non-zero on the first failed expectation.
"""

import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx

API = "http://localhost:8000/api/v1"
# Reads need a user (DECISIONS #022): the public demo viewer token that compose seeds.
AUTH = {"Authorization": "Bearer ofu_demo_viewer_public-not-secret"}
ROBOT = "ros-test-01"
COMPOSE = ["docker", "compose", "--profile", "ros"]


def sh(*args: str) -> None:
    subprocess.run([*COMPOSE, *args], check=True, capture_output=True)


def get(path: str) -> Any:
    r = httpx.get(API + path, timeout=5, headers=AUTH)
    r.raise_for_status()
    return r.json()


def wait_for(what: str, cond: Callable[[], bool], timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if cond():
                print(f"  ok  {what}")
                return
        except (httpx.HTTPError, KeyError, StopIteration) as exc:
            last_exc = exc
        time.sleep(1)
    print(f"  FAIL {what} (timeout {timeout}s; last error: {last_exc})")
    print(f"  robot={_safe(lambda: get(f'/robots/{ROBOT}'))}")
    print(f"  topics={_safe(lambda: get(f'/robots/{ROBOT}/topics'))}")
    sys.exit(1)


def _safe(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception as exc:  # diagnostics only
        return f"<{exc}>"


STARTED_AT = ""  # ISO time the test started; older current-state rows are ignored


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def fresh(row: dict) -> bool:
    return ts(row["updated_at"]) >= ts(STARTED_AT)


def topic(name: str) -> dict:
    t = next(t for t in get(f"/robots/{ROBOT}/topics") if t["topic"] == name)
    if not fresh(t):
        raise KeyError(f"{name}: no data since the test started")
    return t


def node(name: str) -> dict:
    n = next(n for n in get(f"/robots/{ROBOT}/nodes") if n["node"] == name)
    if not fresh(n):
        raise KeyError(f"{name}: no data since the test started")
    return n


def open_incident(key: str) -> dict | None:
    for i in get(f"/incidents?status=open&robot_id={ROBOT}"):
        if i["dedup_key"] == key:
            return i
    return None


def _server_now() -> str:
    """Server clock (current-state rows are stamped with it), as ISO-8601 UTC."""
    out = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "openfleet",
            "-d",
            "openfleet",
            "-tAc",
            "select to_char(now() at time zone 'utc', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def set_scan_hz(hz: float) -> None:
    sh("exec", "-T", "ros-robot", "sh", "-c", f"echo {hz} > /tmp/scan_hz")


def main() -> None:
    global STARTED_AT
    print("starting fake robot + collector")
    sh("up", "-d", "--build", "--wait", "postgres", "backend")
    STARTED_AT = get("/health") and _server_now()
    sh("up", "-d", "--build", "--force-recreate", "ros-robot", "ros-collector")
    set_scan_hz(15)

    print("1. healthy")
    run_id = ""

    def new_run() -> bool:
        nonlocal run_id
        run_id = get(f"/robots/{ROBOT}")["current_run_id"] or ""
        return bool(run_id) and ts(get(f"/runs/{run_id}")["started_at"]) >= ts(STARTED_AT)

    wait_for("collector registered and opened a new run", new_run, timeout=90)
    wait_for(
        "/scan measured at ~15 Hz and HEALTHY",
        lambda: topic("/scan")["status"] == "HEALTHY" and 13 <= topic("/scan")["actual_hz"] <= 17,
        timeout=90,  # first start: DDS discovery
    )
    wait_for("lidar_driver RUNNING", lambda: node("lidar_driver")["state"] == "RUNNING")
    wait_for(
        "battery from sensor_msgs/BatteryState",
        lambda: get(f"/robots/{ROBOT}")["battery_pct"] is not None,
    )
    wait_for(
        "never-published topic reported FAILED",
        lambda: topic("/never_published")["status"] == "FAILED",
    )

    print("2. LiDAR degrades to 4 Hz")
    set_scan_hz(4)
    wait_for("/scan DEGRADED", lambda: topic("/scan")["status"] == "DEGRADED")
    wait_for(
        "WARNING incident topic_rate:/scan",
        lambda: (open_incident("topic_rate:/scan") or {}).get("severity") == "WARNING",
    )
    incident_id = open_incident("topic_rate:/scan")["id"]
    wait_for(
        "rate change logged via /rosout",
        lambda: any(
            e["type"] == "log" and "4.0 Hz" in e["message"]
            for e in get(f"/robots/{ROBOT}/events?limit=200")
        ),
    )

    print("3. LiDAR stops")
    set_scan_hz(0)
    wait_for("/scan FAILED", lambda: topic("/scan")["status"] == "FAILED")
    wait_for(
        "same incident escalated to CRITICAL",
        lambda: (
            (open_incident("topic_rate:/scan") or {}).get("id") == incident_id
            and open_incident("topic_rate:/scan")["severity"] == "CRITICAL"
        ),
    )

    print("4. driver node killed")
    sh("stop", "-t", "2", "ros-robot")
    wait_for("lidar_driver STOPPED", lambda: node("lidar_driver")["state"] == "STOPPED")
    wait_for("node_health incident", lambda: open_incident("node_health:lidar_driver") is not None)

    print("5. backend outage: collector buffers, nothing is lost")
    subprocess.run(
        ["docker", "compose", "stop", "-t", "2", "backend"], check=True, capture_output=True
    )
    time.sleep(10)
    subprocess.run(
        ["docker", "compose", "up", "-d", "--wait", "backend"], check=True, capture_output=True
    )

    def no_gaps() -> bool:
        out = subprocess.run(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "postgres",
                "psql",
                "-U",
                "openfleet",
                "-d",
                "openfleet",
                "-tAc",
                "select count(*), max(seq)-min(seq)+1 from telemetry_samples "
                f"where run_id='{run_id}'",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        count, span = (int(x) for x in out.split("|"))
        return count == span and get(f"/runs/{run_id}")["status"] == "RUNNING"

    wait_for("all batches delivered after the outage (contiguous seq, same run)", no_gaps)

    print("6. recovery")
    sh("start", "ros-robot")
    set_scan_hz(15)
    wait_for("/scan HEALTHY again", lambda: topic("/scan")["status"] == "HEALTHY", timeout=60)
    wait_for("lidar_driver RUNNING again", lambda: node("lidar_driver")["state"] == "RUNNING")
    wait_for(
        "incidents auto-resolved (hysteresis)",
        lambda: (
            open_incident("topic_rate:/scan") is None
            and open_incident("node_health:lidar_driver") is None
        ),
        timeout=45,
    )

    print("7. graceful shutdown")
    sh("stop", "ros-collector")
    wait_for("run ended SUCCESS", lambda: get(f"/runs/{run_id}")["status"] == "SUCCESS")

    print("PASS: ROS 2 collector end-to-end")


if __name__ == "__main__":
    try:
        main()
    finally:
        subprocess.run([*COMPOSE, "stop", "ros-robot", "ros-collector"], capture_output=True)
