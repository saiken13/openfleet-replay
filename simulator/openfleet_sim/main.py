"""openfleet-sim: drive simulated robots through a scenario against the OpenFleet API.

openfleet-sim --scenario scenarios/demo.yaml                 # real time
openfleet-sim --scenario scenarios/demo.yaml --fast          # as fast as possible (CI)
openfleet-sim --scenario scenarios/demo.yaml --loop          # repeat forever (demo)
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from openfleet_sim.client import ApiError, FleetApi
from openfleet_sim.robot import SimRobot
from openfleet_sim.scenario import OfflineFailure, Scenario, load_scenario

log = logging.getLogger("openfleet_sim")


async def run_scenario(
    api: FleetApi,
    sc: Scenario,
    *,
    speed: float = 1.0,
    fast: bool = False,
    version: str | None = None,
) -> list[dict[str, Any]]:
    """One pass of the scenario: register, start runs, stream batches, end runs.
    Returns [{"robot_id", "run_id", "software_version"}]."""
    version = version or sc.software_version
    ids = sc.robot_ids()
    robots = [SimRobot(i, rid, sc) for i, rid in enumerate(ids)]
    t0 = datetime.now(UTC)
    runs = {r.robot_id: str(uuid.uuid4()) for r in robots}

    for r in robots:
        await api.register(r.robot_id, r.robot_id.upper(), sc.fleet.model)
        await api.start_run(runs[r.robot_id], r.robot_id, version, t0.isoformat())
    log.info(
        "scenario %r: %d robots, %.0fs, version %s", sc.name, len(robots), sc.duration_s, version
    )

    ticks = int(sc.duration_s / sc.tick_s)
    loop = asyncio.get_running_loop()
    start_wall = loop.time()
    for i in range(ticks):
        t = i * sc.tick_s
        # Robot clock: real wall time in real-time mode (as a real robot would report);
        # synthetic, evenly spaced time in --fast mode.
        ts = t0 + timedelta(seconds=t) if fast else datetime.now(UTC)
        batches = [(r, r.step(t, ts)) for r in robots]
        await asyncio.gather(
            *(api.telemetry(runs[r.robot_id], b) for r, b in batches if b is not None)
        )
        if not fast:
            target = start_wall + (i + 1) * sc.tick_s / speed
            await asyncio.sleep(max(0.0, target - loop.time()))

    end_t = ticks * sc.tick_s
    ended_at = (t0 + timedelta(seconds=end_t) if fast else datetime.now(UTC)).isoformat()
    for r in robots:
        if r.is_offline(end_t):
            log.info("%s is offline at the end; leaving its run for the sweeper", r.robot_id)
            continue
        failed_here = sc.outcome == "FAILED" and any(
            f.applies_to(r.index) and not isinstance(f, OfflineFailure) for f in sc.failures
        )
        await api.end_run(runs[r.robot_id], "FAILED" if failed_here else "SUCCESS", ended_at)
    return [
        {"robot_id": rid, "run_id": run_id, "software_version": version}
        for rid, run_id in runs.items()
    ]


async def idle(api: FleetApi, sc: Scenario, seconds: float, version: str | None) -> None:
    """Between runs robots are powered on and heartbeating."""
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    while loop.time() < end:
        await asyncio.gather(
            *(api.heartbeat(rid, version or sc.software_version) for rid in sc.robot_ids())
        )
        await asyncio.sleep(2)


async def amain(args: argparse.Namespace) -> int:
    sc = load_scenario(args.scenario)
    if args.robots:
        sc.fleet.count = args.robots
    if args.duration:
        sc.duration_s = args.duration
    api = FleetApi(args.api)
    try:
        await api.wait_until_healthy()
        all_runs: list[dict[str, Any]] = []
        while True:
            all_runs += await run_scenario(
                api, sc, speed=args.speed, fast=args.fast, version=args.version
            )
            for r in all_runs[-sc.fleet.count :]:
                print(f"{r['robot_id']}\t{r['run_id']}")
            if args.output:
                await asyncio.to_thread(
                    Path(args.output).write_text, json.dumps(all_runs, indent=2)
                )
            if not args.loop:
                return 0
            await idle(api, sc, args.idle, args.version)
    except ApiError as exc:
        log.error("%s", exc)
        return 2
    finally:
        await api.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="openfleet-sim", description=__doc__.split("\n")[0])
    p.add_argument("--api", default=os.environ.get("OPENFLEET_API", "http://localhost:8000"))
    p.add_argument("--scenario", "-s", required=True)
    p.add_argument("--speed", type=float, default=1.0, help="time multiplier (real-time mode)")
    p.add_argument("--fast", action="store_true", help="send batches as fast as possible")
    p.add_argument("--loop", action="store_true", help="repeat the scenario forever")
    p.add_argument("--idle", type=float, default=15, help="seconds idle between loops")
    p.add_argument("--robots", type=int, help="override fleet size")
    p.add_argument("--duration", type=float, help="override duration_s")
    p.add_argument("--version", help="override software_version (e.g. for a regression run)")
    p.add_argument("--output", "-o", help="write produced run ids as JSON")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
