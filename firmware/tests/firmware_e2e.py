"""End-to-end test: the FreeRTOS firmware on Renode's emulated STM32F4 -> UART over TCP ->
the C++ edge agent's gateway -> OpenFleet (docs/FIRMWARE.md, DECISIONS #034).

Run from the repo root (needs Docker and `pip install httpx`):

    python firmware/tests/firmware_e2e.py

1. the firmware's sensors and tasks appear as mcu2 topics and nodes;
2. a stalled task (injected through Renode's monitor) -> the supervisor stops kicking ->
   the IWDG resets the MCU -> OpenFleet shows the reset, the cause, and an incident that
   resolves once the device has run cleanly again;
3. a slow link: readings are dropped and the topic degrades, but nothing resets.

Emulated hardware throughout: Renode's STM32F4 model, not a board; no real-time claims.
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
COMPOSE = ["docker", "compose", "--profile", "edge", "--profile", "firmware"]
FAULT_CTL = 0x2001FE00  # firmware/board/stm32f4/board.h
STARTED_AT = ""


def sh(*args: str, timeout: float = 600) -> None:
    subprocess.run([*COMPOSE, *args], check=True, capture_output=True, timeout=timeout)


def poke(offset: int, value: int) -> None:
    """Write a fault-injection word through Renode's monitor."""
    cmd = f"sysbus WriteDoubleWord {FAULT_CTL + offset:#x} {value}"
    # -N: close after sending; timeout: the monitor never closes its side.
    sh(
        "exec",
        "-T",
        "renode",
        "sh",
        "-c",
        f"echo '{cmd}' | timeout 3 nc -N localhost 1234 >/dev/null; true",
        timeout=30,
    )


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


def log_events() -> list[dict]:
    return [
        e
        for e in get(f"/robots/{ROBOT}/events?limit=200")
        if e["type"] == "log" and ts(e["ts"]) >= ts(STARTED_AT)
    ]


def logged(fragment: str, since: str = "") -> bool:
    return any(
        fragment in e["message"] and (not since or ts(e["ts"]) >= ts(since)) for e in log_events()
    )


def server_now() -> str:
    return psql("select to_char(now() at time zone 'utc', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')")


def main() -> None:
    global STARTED_AT
    print("starting the backend, Renode (firmware) and the edge agent")
    sh("up", "-d", "--build", "--wait", "postgres", "backend")
    STARTED_AT = server_now()
    sh("up", "-d", "--build", "--force-recreate", "renode", "edge-agent", "edge-worker")

    print("1. the firmware's sensors and tasks")
    wait_for(
        "/mcu2/imu ~50 Hz HEALTHY",
        lambda: (
            (t := topic("/mcu2/imu"))["status"] == "HEALTHY"
            and t["expected_hz"] == 50
            and 45 <= t["actual_hz"] <= 55
        ),
        timeout=90,  # Renode's start-up
    )
    wait_for("/mcu2/range ~10 Hz HEALTHY", lambda: topic("/mcu2/range")["status"] == "HEALTHY")
    for n in ("mcu2", "mcu2/sensor", "mcu2/comms", "mcu2/health"):
        wait_for(f"{n} RUNNING", lambda n=n: node(n)["state"] == "RUNNING")
    wait_for(
        "firmware identified (HELLO or first frame)",
        lambda: logged("openfleet-fw 0.1") or logged("mcu2 first frame"),
    )

    print("2. a task stalls -> watchdog reset -> incident -> recovery")
    since = server_now()
    poke(0x00, 1)  # stall_task = sensor
    wait_for(
        "the firmware reported the stall before resetting",
        lambda: logged("watchdog: task sensor stopped checking in", since),
    )
    wait_for("OpenFleet saw the watchdog reset", lambda: logged("mcu2 reset (watchdog)", since))
    wait_for(
        "the next boot named the stalled task",
        lambda: logged("previous boot ended by the watchdog: task sensor", since),
    )
    wait_for(
        "incident node_health:mcu2 opened",
        lambda: open_incident("node_health:mcu2") is not None,
    )
    wait_for("sampling again after the reset", lambda: topic("/mcu2/imu")["actual_hz"] > 45)
    wait_for(
        "incident resolved once the device ran cleanly",
        lambda: open_incident("node_health:mcu2") is None and node("mcu2")["state"] == "RUNNING",
        timeout=75,  # reset hold (20 s) + resolve hysteresis
    )

    print("3. a slow link: readings dropped, nothing resets")
    since = server_now()
    poke(0x04, 60)  # comms_delay_ms
    wait_for(
        "mcu2/sensor DEGRADED (its readings don't all get out)",
        lambda: node("mcu2/sensor")["state"] == "DEGRADED",
    )
    wait_for("/mcu2/imu DEGRADED", lambda: topic("/mcu2/imu")["status"] == "DEGRADED")
    wait_for("dropped readings reported", lambda: logged("readings dropped", since))
    time.sleep(5)
    wait_for("no reset during the slow link", lambda: not logged("mcu2 reset", since))
    poke(0x04, 0)
    wait_for(
        "recovered when the link is fast again",
        lambda: (
            topic("/mcu2/imu")["status"] == "HEALTHY" and node("mcu2/sensor")["state"] == "RUNNING"
        ),
        timeout=45,
    )
    print("PASS: firmware on Renode end-to-end")


if __name__ == "__main__":
    try:
        main()
    finally:
        subprocess.run(
            [*COMPOSE, "stop", "renode", "edge-agent", "edge-worker"],
            check=False,
            capture_output=True,
        )
