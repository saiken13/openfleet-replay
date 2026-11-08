"""End-to-end test: the C++ edge agent (container) + the OpenFleet backend.

Run from the repo root (needs Docker and `pip install httpx`):

    python edge/e2e/e2e.py

Drives `docker compose --profile edge` and asserts what OpenFleet observes at each step:
registered with live metrics -> CPU load opens high_cpu, which resolves -> the watched
process stops and restarts -> backend outage (no batch lost) -> SIGTERM ends the run ABORTED.
Exits non-zero on the first failed expectation.

Everything here is simulated: the "robot computer" is a Linux container, its CPU is the
Docker VM's, and the load comes from stress-ng. No hardware is involved.
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
ROBOT = "edge-01"
COMPOSE = ["docker", "compose", "--profile", "edge"]
# The server ABORTs a run whose robot is silent for RUN_ABANDON_AFTER_S (60 s). The agent's
# silence is the outage plus the backend's restart plus up to one backoff (<= 10 s), so the
# no-loss check uses an outage well inside that. Longer outages take the 409 -> fresh-run
# path, covered by the unit tests (edge/tests/uplink_test.cpp).
OUTAGE_S = 30


def sh(*args: str) -> None:
    subprocess.run([*COMPOSE, *args], check=True, capture_output=True)


def get(path: str) -> Any:
    r = httpx.get(API + path, timeout=5, headers=AUTH)
    r.raise_for_status()
    return r.json()


def psql(sql: str) -> str:
    out = subprocess.run(
        [
            *COMPOSE,
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "openfleet",
            "-d",
            "openfleet",
        ]
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
    print(f"  robot={_safe(lambda: get(f'/robots/{ROBOT}'))}")
    print(f"  nodes={_safe(lambda: get(f'/robots/{ROBOT}/nodes'))}")
    print(f"  incidents={_safe(lambda: get(f'/incidents?status=open&robot_id={ROBOT}'))}")
    sys.exit(1)


def _safe(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 (diagnostics only)
        return f"<{exc}>"


STARTED_AT = ""  # server time when the test started; older rows are ignored


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def node(name: str) -> dict:
    n = next(n for n in get(f"/robots/{ROBOT}/nodes") if n["node"] == name)
    if ts(n["updated_at"]) < ts(STARTED_AT):
        raise KeyError(f"{name}: no data since the test started")
    return n


def open_incident(key: str) -> dict | None:
    for i in get(f"/incidents?status=open&robot_id={ROBOT}"):
        if i["dedup_key"] == key:
            return i
    return None


def logged(fragment: str) -> bool:
    return any(
        e["type"] == "log" and fragment in (e.get("message") or "")
        for e in get(f"/robots/{ROBOT}/events?limit=200")
        if ts(e["ts"]) >= ts(STARTED_AT)
    )


def main() -> None:
    global STARTED_AT
    print("starting the backend and the edge agent")
    sh("up", "-d", "--build", "--wait", "postgres", "backend")
    STARTED_AT = psql(
        "select to_char(now() at time zone 'utc', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')"
    )
    sh("up", "-d", "--build", "--force-recreate", "edge-agent", "edge-worker")

    print("1. registered, with live metrics")
    run_id = ""

    def new_run() -> bool:
        nonlocal run_id
        run_id = get(f"/robots/{ROBOT}")["current_run_id"] or ""
        return bool(run_id) and ts(get(f"/runs/{run_id}")["started_at"]) >= ts(STARTED_AT)

    wait_for("edge-01 registered and opened a new run", new_run, timeout=60)
    wait_for(
        "CPU and memory from /proc",
        lambda: (
            (r := get(f"/robots/{ROBOT}"))["cpu_pct"] is not None
            and r["mem_pct"] is not None
            and r["status"] == "ONLINE"
        ),
    )
    wait_for("edge_agent RUNNING", lambda: node("edge_agent")["state"] == "RUNNING")
    wait_for("demo-worker RUNNING", lambda: node("demo-worker")["state"] == "RUNNING")
    wait_for(
        "link latency recorded",
        lambda: (
            int(
                psql(
                    "select count(*) from telemetry_samples "
                    f"where run_id='{run_id}' and net_latency_ms is not null"
                )
            )
            > 0
        ),
    )
    wait_for("startup logged", lambda: logged("edge agent started"))

    print("2. CPU load (stress-ng on every CPU of the Docker VM)")
    # --temp-path: the agent's container runs unprivileged in /, which isn't writable.
    load = ["stress-ng", "--cpu", "0", "--timeout", "40s", "--temp-path", "/tmp"]
    stress = subprocess.Popen(
        [*COMPOSE, "exec", "-T", "edge-agent", *load],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for("high_cpu incident opened", lambda: open_incident("high_cpu") is not None)
    finally:
        stress.wait(timeout=60)
    wait_for(
        "high_cpu resolved after the load (hysteresis)",
        lambda: open_incident("high_cpu") is None,
        timeout=45,
    )

    print("3. watched process stops, then restarts")
    sh("stop", "-t", "1", "edge-worker")
    wait_for("demo-worker STOPPED", lambda: node("demo-worker")["state"] == "STOPPED")
    wait_for(
        "node_health incident",
        lambda: open_incident("node_health:demo-worker") is not None,
    )
    sh("start", "edge-worker")
    wait_for("demo-worker RUNNING again", lambda: node("demo-worker")["state"] == "RUNNING")
    wait_for(
        "node_health resolved",
        lambda: open_incident("node_health:demo-worker") is None,
        timeout=45,
    )

    print(f"4. backend outage ({OUTAGE_S} s): the agent buffers, nothing is lost")
    subprocess.run(
        ["docker", "compose", "stop", "-t", "2", "backend"],
        check=True,
        capture_output=True,
    )
    time.sleep(OUTAGE_S)
    subprocess.run(
        ["docker", "compose", "up", "-d", "--wait", "backend"],
        check=True,
        capture_output=True,
    )

    def no_gaps() -> bool:
        count, span = (
            int(x)
            for x in psql(
                "select count(*), max(seq)-min(seq)+1 from telemetry_samples "
                f"where run_id='{run_id}'"
            ).split("|")
        )
        return count == span and get(f"/runs/{run_id}")["status"] == "RUNNING"

    wait_for("all batches delivered (contiguous seq, same run)", no_gaps, timeout=60)
    wait_for("the agent reported its outage", lambda: logged("was unreachable for"))

    print("5. SIGTERM")
    sh("stop", "edge-agent")
    wait_for("run ended ABORTED", lambda: get(f"/runs/{run_id}")["status"] == "ABORTED")

    print("PASS: C++ edge agent end-to-end")


if __name__ == "__main__":
    try:
        main()
    finally:
        subprocess.run(
            [*COMPOSE, "stop", "edge-agent", "edge-worker"],
            check=False,
            capture_output=True,
        )
