"""Multi-replica e2e: realtime events reach WebSockets on every backend replica.

Needs the `replicas` compose profile (backend on :8000, backend-2 on :8001, nginx lb on :8090):

    docker compose --profile replicas up -d --build --wait
    python scripts/multi_replica_e2e.py

Checks, each with a fresh marker so a stale message can't satisfy it:
  1. Cross-replica, both directions: a fleet WebSocket on one replica sees a robot heartbeat
     sent to the other (Redis bus, DECISIONS #025).
  2. Through the round-robin load balancer: every heartbeat reaches a WebSocket that is also
     behind the load balancer, wherever each request landed.
  3. Bus loss: killing Redis's pub/sub connections closes the WebSockets with 1012 (clients
     reload over REST), and cross-replica delivery works again once the bus resubscribes.

Exit code 0 if every check passed, 1 otherwise. Uses the public demo credentials.
"""

import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import websockets

ROOT = Path(__file__).resolve().parent.parent
VIEWER_TOKEN = "ofu_demo_viewer_public-not-secret"
ROBOT_KEYS = json.loads((ROOT / "examples" / "demo-robot-keys.json").read_text())
TIMEOUT_S = 5.0


class CheckFailed(Exception):
    pass


def ws_url(http: str) -> str:
    return http.replace("http://", "ws://", 1) + "/api/v1/ws/fleet"


async def open_fleet(http: str) -> Any:
    ws = await websockets.connect(
        ws_url(http), additional_headers={"Authorization": f"Bearer {VIEWER_TOKEN}"}
    )
    hello = json.loads(await ws.recv())
    if hello.get("type") != "hello":
        raise CheckFailed(f"{http}: expected hello, got {hello.get('type')}")
    return ws


async def heartbeat(client: httpx.AsyncClient, http: str, robot: str, marker: str) -> None:
    r = await client.post(
        f"{http}/api/v1/robots/{robot}/heartbeat",
        json={"software_version": marker},
        headers={"Authorization": f"Bearer {ROBOT_KEYS[robot]}"},
    )
    if r.status_code != 204:
        raise CheckFailed(f"heartbeat {robot} via {http}: HTTP {r.status_code} {r.text[:200]}")


E2E_ROBOTS = ("amr-02", "amr-03", "amr-04")


async def register(client: httpx.AsyncClient, http: str, robot: str) -> None:
    """Idempotent PUT, so the script also works on a fresh database (as in CI)."""
    r = await client.put(
        f"{http}/api/v1/robots/{robot}",
        json={"name": robot.upper(), "model": "AMR-Sim"},  # as the demo simulator registers it
        headers={"Authorization": f"Bearer {ROBOT_KEYS[robot]}"},
    )
    if r.status_code >= 300:
        raise CheckFailed(f"register {robot} via {http}: HTTP {r.status_code} {r.text[:200]}")


async def wait_for(ws: Any, robot: str, marker: str, where: str) -> None:
    try:
        async with asyncio.timeout(TIMEOUT_S):
            while True:
                m = json.loads(await ws.recv())
                d = m.get("data") or {}
                if (
                    m.get("type") == "robot"
                    and d.get("id") == robot
                    and d.get("software_version") == marker
                ):
                    return
    except TimeoutError:
        raise CheckFailed(f"{where}: no update for {robot} ({marker}) in {TIMEOUT_S}s") from None


async def check_cross_replica(client: httpx.AsyncClient, a: str, b: str) -> None:
    ws_a, ws_b = await open_fleet(a), await open_fleet(b)
    try:
        for src, dst, ws, robot in ((b, a, ws_a, "amr-02"), (a, b, ws_b, "amr-03")):
            marker = f"e2e-{uuid.uuid4().hex[:8]}"
            await heartbeat(client, src, robot, marker)
            await wait_for(ws, robot, marker, f"socket on {dst}, heartbeat via {src}")
            print(f"  ok  heartbeat via {src} -> socket on {dst}")
    finally:
        await ws_a.close()
        await ws_b.close()


async def check_load_balancer(client: httpx.AsyncClient, lb: str, n: int = 6) -> None:
    ws = await open_fleet(lb)
    try:
        for i in range(n):
            marker = f"e2e-lb-{i}-{uuid.uuid4().hex[:6]}"
            await heartbeat(client, lb, "amr-04", marker)
            await wait_for(ws, "amr-04", marker, f"socket via {lb}, heartbeat #{i + 1} via {lb}")
        print(f"  ok  {n}/{n} heartbeats through {lb} reached a socket behind it")
    finally:
        await ws.close()


async def check_bus_loss(client: httpx.AsyncClient, a: str, b: str, compose: list[str]) -> None:
    sockets = {a: await open_fleet(a), b: await open_fleet(b)}
    kill = await asyncio.create_subprocess_exec(
        *compose,
        "exec",
        "-T",
        "redis",
        "redis-cli",
        "CLIENT",
        "KILL",
        "TYPE",
        "pubsub",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await kill.communicate()
    if kill.returncode != 0:
        raise CheckFailed(f"could not kill Redis pub/sub clients: {err.decode()[:200]}")
    for http, ws in sockets.items():
        try:
            async with asyncio.timeout(TIMEOUT_S):
                while True:
                    await ws.recv()
        except websockets.ConnectionClosed as exc:
            code = exc.rcvd.code if exc.rcvd else None
            if code != 1012:
                raise CheckFailed(f"socket on {http} closed with {code}, expected 1012") from None
            print(f"  ok  socket on {http} closed with 1012 after the bus dropped")
        except TimeoutError:
            raise CheckFailed(f"socket on {http} stayed open after the bus dropped") from None
    # The bus resubscribes with backoff; the first publish after a kill may hit a stale
    # connection (a gap the receiver repairs with a resync). Retry until delivery works.
    deadline = time.monotonic() + 15
    while True:
        try:
            await check_cross_replica(client, a, b)
            print("  ok  cross-replica delivery recovered")
            return
        except (CheckFailed, websockets.ConnectionClosed) as exc:
            if time.monotonic() > deadline:
                raise CheckFailed(f"cross-replica delivery did not recover: {exc}") from None
            await asyncio.sleep(1)


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--a", default="http://localhost:8000", help="replica 1")
    p.add_argument("--b", default="http://localhost:8001", help="replica 2")
    p.add_argument("--lb", default="http://localhost:8090", help="load balancer")
    p.add_argument("--compose-file", default=str(ROOT / "docker-compose.yml"))
    args = p.parse_args()
    compose = ["docker", "compose", "-f", args.compose_file]
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            for robot in E2E_ROBOTS:
                await register(client, args.a, robot)
            print("cross-replica delivery")
            await check_cross_replica(client, args.a, args.b)
            print("load balancer")
            await check_load_balancer(client, args.lb)
            print("bus loss and recovery")
            await check_bus_loss(client, args.a, args.b, compose)
        except CheckFailed as exc:
            print(f"FAIL {exc}", file=sys.stderr)
            return 1
    print("all multi-replica checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
