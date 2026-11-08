"""Scenario runner: the edge regression tier (docs/BENCHMARKS.md, Phase 4).

Runs YAML scenarios (edge/e2e/scenarios/*.yaml) against the compose stack: start services,
inject faults, assert what OpenFleet shows, and *measure* how long each effect took. It ends
each scenario's run and re-runs today's detection rules over it (`redetect`), so a recorded
edge run is also a regression check on the rules. Writes a JSON report per scenario plus a
Markdown summary.

    pip install httpx pyyaml
    python edge/e2e/run_scenarios.py                      # every scenario
    python edge/e2e/run_scenarios.py device_sensor_stops firmware_watchdog
    python edge/e2e/run_scenarios.py --out build/scenarios --list

Timing. A `mark` records the server's clock (the offset between this host and the database
is measured once). A `measure` on an expectation is the server-side timestamp of the effect
minus the mark: an incident's started_at (when it first opened, at whatever severity) or
resolved_at, or the received_at of the matching event. Where no such timestamp exists, the
runner falls back to when it first observed the condition (polling every 0.25 s) and says
so (`basis: observed`).

Environment. Each report says where it ran (host architecture, CI or not) and what was
real: the backend and agent are real processes; devices are simulated (device-sim) or
emulated (Renode). Nothing here runs on robot hardware.

Scenario format (all keys optional except name and steps):

    name: device_sensor_stops
    description: ...
    profiles: [edge]                 # compose profiles to enable
    services: [device-sim, edge-agent, edge-worker]
    env: simulated device            # what the device is, for the report
    steps:
      - expect: {topic: /mcu1/range, status: HEALTHY}
        timeout: 60
      - mark: fault                  # then the step's action, if any
        device_sim: stop-sensor 2
      - expect: {incident: topic_rate:/mcu1/range, open: true, severity: CRITICAL}
        measure: {name: detection_s, from: fault}
    redetect: {caught_min: 1, missed_max: 0}

Actions: device_sim (a device-sim command), renode_poke [offset, value], compose
[verb, service] (stop/start/restart/kill), exec [service, shell command] (detached),
sleep (s), sample_agent {seconds, label} (agent CPU and memory from /proc).
Expectations: topic (status, min_hz, max_hz), node (state), incident (open: true|false,
severity), no_incident (key: never opened during the scenario), log (fragment),
batches_since (mark: a batch received after it), no_batch_lost, absent {log, for: s}.
A step's `since: <mark>` limits its expectation to effects after that mark.
"""

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = Path(__file__).resolve().parent / "scenarios"
API = "http://localhost:8000/api/v1"
AUTH = {"Authorization": "Bearer ofu_demo_viewer_public-not-secret"}  # public demo viewer
ROBOT = "edge-01"
FAULT_CTL = 0x2001FE00  # firmware/board/stm32f4/board.h
POLL_S = 0.25


class ScenarioFailed(Exception):
    pass


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass
class Context:
    scenario: dict
    compose: list[str]
    clock_offset: timedelta = timedelta(0)  # server clock - local clock
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    run_id: str = ""
    marks: dict[str, datetime] = field(default_factory=dict)
    metrics: dict[str, dict] = field(default_factory=dict)
    steps: list[dict] = field(default_factory=list)
    detached: list[subprocess.Popen] = field(default_factory=list)

    # --- plumbing ----------------------------------------------------------------------

    def sh(self, *args: str, timeout: float = 600, check: bool = True) -> str:
        out = subprocess.run(
            [*self.compose, *args], check=False, capture_output=True, text=True, timeout=timeout
        )
        if check and out.returncode != 0:
            raise ScenarioFailed(f"docker compose {' '.join(args)}: {out.stderr.strip()[-400:]}")
        return out.stdout

    def server_now(self) -> datetime:
        return datetime.now(UTC) + self.clock_offset

    def measure_clock(self) -> None:
        sql = "select extract(epoch from clock_timestamp())"
        best: tuple[float, timedelta] | None = None
        for _ in range(3):  # keep the sample with the shortest round trip
            t0 = time.time()
            out = self.sh(
                "exec", "-T", "postgres", "psql", "-U", "openfleet", "-d", "openfleet", "-tAc", sql
            )
            t1 = time.time()
            server = float(out.strip())
            offset = timedelta(seconds=server - (t0 + t1) / 2)
            if best is None or t1 - t0 < best[0]:
                best = (t1 - t0, offset)
        assert best is not None
        self.clock_offset = best[1]

    def psql(self, sql: str) -> str:
        return self.sh(
            "exec", "-T", "postgres", "psql", "-U", "openfleet", "-d", "openfleet", "-tAc", sql
        ).strip()

    # --- API reads ---------------------------------------------------------------------

    def topic(self, name: str) -> dict:
        t = next(t for t in get(f"/robots/{ROBOT}/topics") if t["topic"] == name)
        if ts(t["updated_at"]) < self.started_at:
            raise KeyError(f"{name}: no data since the scenario started")
        return t

    def node(self, name: str) -> dict:
        n = next(n for n in get(f"/robots/{ROBOT}/nodes") if n["node"] == name)
        if ts(n["updated_at"]) < self.started_at:
            raise KeyError(f"{name}: no data since the scenario started")
        return n

    def events(self, since: datetime) -> list[dict]:
        return [
            e for e in get(f"/robots/{ROBOT}/events?limit=500") if ts(e["received_at"]) >= since
        ]

    def incidents(self, key: str) -> list[dict]:
        return [
            i
            for i in get(f"/incidents?status=all&robot_id={ROBOT}&limit=200")
            if i["dedup_key"] == key and ts(i["started_at"]) >= self.started_at
        ]


def get(path: str) -> Any:
    r = httpx.get(API + path, timeout=5, headers=AUTH)
    r.raise_for_status()
    return r.json()


def post(path: str, body: dict) -> Any:
    r = httpx.post(API + path, json=body, timeout=120, headers=AUTH)
    r.raise_for_status()
    return r.json()


# --- actions ---------------------------------------------------------------------------


def do_action(ctx: Context, step: dict) -> str | None:
    if "device_sim" in step:
        cmd = step["device_sim"]
        ctx.sh("exec", "-T", "device-sim", "sh", "-c", f"echo '{cmd}' >> /tmp/devsim.ctl")
        return f"device-sim: {cmd}"
    if "renode_poke" in step:
        offset, value = step["renode_poke"]
        line = f"sysbus WriteDoubleWord {FAULT_CTL + int(offset):#x} {int(value)}"
        ctx.sh(
            "exec",
            "-T",
            "renode",
            "sh",
            "-c",
            f"echo '{line}' | timeout 3 nc -N localhost 1234 >/dev/null; true",
            timeout=30,
        )
        return f"renode: {line}"
    if "compose" in step:
        verb, service = step["compose"]
        args = {
            "stop": ["stop", "-t", "2"],
            "kill": ["kill"],
            "start": ["start"],
            "restart": ["restart", "-t", "2"],
        }[verb]
        ctx.sh(*args, service)
        if verb in ("start", "restart") and service in ("backend", "postgres"):
            ctx.sh("up", "-d", "--wait", service)
        return f"compose {verb} {service}"
    if "exec" in step:
        service, command = step["exec"]
        ctx.detached.append(
            subprocess.Popen(
                [*ctx.compose, "exec", "-T", service, "sh", "-c", command],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
        return f"exec in {service}: {command}"
    if "sleep" in step:
        time.sleep(float(step["sleep"]))
        return f"sleep {step['sleep']} s"
    if "sample_agent" in step:
        return sample_agent(ctx, **step["sample_agent"])
    return None


def sample_agent(ctx: Context, seconds: float, label: str = "agent") -> str:
    """CPU share and memory of the agent process, read from its /proc inside the container."""
    # utime and stime are fields 14 and 15 of /proc/<pid>/stat; the comm field
    # ("openfleet-edge-", truncated to 15 chars) has no spaces, so plain field splitting works.
    script = (
        "pid=$(pidof openfleet-edge-agent) && echo $(getconf CLK_TCK) "
        "$(cut -d' ' -f14,15 /proc/$pid/stat) "
        "$(awk '/^(VmRSS|VmHWM|Threads):/ {print $2}' /proc/$pid/status)"
    )

    def read() -> list[int]:
        return [int(x) for x in ctx.sh("exec", "-T", "edge-agent", "sh", "-c", script).split()]

    t0 = time.monotonic()
    tck, u0, s0, *_ = read()
    time.sleep(float(seconds))
    tck, u1, s1, hwm_kb, rss_kb, threads = read()  # status order: VmHWM, VmRSS, Threads
    wall = time.monotonic() - t0
    cpu = 100 * ((u1 + s1) - (u0 + s0)) / tck / wall
    basis = f"/proc of the agent process over {wall:.0f} s"
    ctx.metrics[f"{label}_cpu_pct"] = {
        "value": round(cpu, 2),
        "unit": "% of one core",
        "basis": basis,
    }
    ctx.metrics[f"{label}_rss_mib"] = {
        "value": round(rss_kb / 1024, 2),
        "unit": "MiB",
        "basis": "VmRSS",
    }
    ctx.metrics[f"{label}_peak_rss_mib"] = {
        "value": round(hwm_kb / 1024, 2),
        "unit": "MiB",
        "basis": "VmHWM",
    }
    ctx.metrics[f"{label}_threads"] = {"value": threads, "unit": "threads", "basis": "Threads"}
    return f"{label}: {cpu:.2f} % CPU, {rss_kb / 1024:.1f} MiB RSS, {threads} threads"


# --- expectations ----------------------------------------------------------------------


def check(ctx: Context, exp: dict, since: datetime) -> tuple[bool, datetime | None, str]:
    """(met, server-side time of the effect if known, description)."""
    if "topic" in exp:
        t = ctx.topic(exp["topic"])
        ok = (
            ("status" not in exp or t["status"] == exp["status"])
            and ("min_hz" not in exp or t["actual_hz"] >= exp["min_hz"])
            and ("max_hz" not in exp or t["actual_hz"] <= exp["max_hz"])
        )
        when = None
        if ok and "status" in exp:
            when = _first_event(ctx, since, "topic_status_changed", exp["topic"], exp["status"])
        return ok, when, f"topic {exp['topic']} {t['status']} {t['actual_hz']:.1f} Hz"
    if "node" in exp:
        n = ctx.node(exp["node"])
        ok = n["state"] == exp["state"]
        when = (
            _first_event(ctx, since, "node_state_changed", exp["node"], exp["state"])
            if ok
            else None
        )
        return ok, when, f"node {exp['node']} {n['state']}"
    if "incident" in exp:
        found = ctx.incidents(exp["incident"])
        if exp.get("open", True):
            hit = [
                i
                for i in found
                if i["status"] != "RESOLVED"
                and ("severity" not in exp or i["severity"] == exp["severity"])
            ]
            if hit:
                return True, ts(hit[0]["started_at"]), f"{exp['incident']} {hit[0]['severity']}"
            return False, None, f"{exp['incident']} not open"
        if any(i["status"] != "RESOLVED" for i in found):
            return False, None, f"{exp['incident']} still open"
        done = [i for i in found if i["resolved_at"] and ts(i["resolved_at"]) >= since]
        when = max((ts(i["resolved_at"]) for i in done), default=None)
        return True, when, f"{exp['incident']} not open"
    if "no_incident" in exp:  # never opened at all during this scenario (any status)
        found = ctx.incidents(exp["no_incident"])
        return (
            not found,
            None,
            f"no {exp['no_incident']} incident"
            if not found
            else f"{exp['no_incident']} opened at {found[0]['started_at']}",
        )
    if "log" in exp:
        hit = [e for e in ctx.events(since) if e["type"] == "log" and exp["log"] in e["message"]]
        return bool(hit), ts(hit[-1]["received_at"]) if hit else None, f"log {exp['log']!r}"
    if "batches_since" in exp:
        mark = ctx.marks[exp["batches_since"]]
        rows = get(f"/robots/{ROBOT}/telemetry?seconds=120&limit=600")
        after = sorted(ts(r["received_at"]) for r in rows if ts(r["received_at"]) >= mark)
        return bool(after), after[0] if after else None, f"{len(after)} batches since mark"
    if "no_batch_lost" in exp:
        out = ctx.psql(
            f"select count(*), coalesce(max(seq) - min(seq) + 1, 0) "
            f"from telemetry_samples where run_id = '{ctx.run_id}'"
        )
        count, span = (int(x) for x in out.split("|"))
        return count == span and count > 0, None, f"{count} batches, seq span {span}"
    raise ScenarioFailed(f"unknown expectation {exp}")


def _first_event(ctx: Context, since: datetime, kind: str, source: str, to: str) -> datetime | None:
    hits = [
        ts(e["received_at"])
        for e in ctx.events(since)
        if e["type"] == kind and e["source"] == source and e["payload"].get("to") == to
    ]
    return min(hits) if hits else None


def wait(
    ctx: Context, exp: dict, timeout: float, since: datetime
) -> tuple[datetime, datetime | None, str]:
    deadline = time.monotonic() + timeout
    detail, last_exc = "", None
    while time.monotonic() < deadline:
        try:
            ok, when, detail = check(ctx, exp, since)
            if ok:
                return ctx.server_now(), when, detail
        except (httpx.HTTPError, KeyError, StopIteration, ValueError) as exc:
            last_exc = exc
        time.sleep(POLL_S)
    raise ScenarioFailed(f"timeout {timeout:g} s waiting for {exp} (last: {detail or last_exc})")


def absent(ctx: Context, spec: dict, since: datetime) -> str:
    end = time.monotonic() + float(spec["for"])
    while time.monotonic() < end:
        hit = [e for e in ctx.events(since) if e["type"] == "log" and spec["log"] in e["message"]]
        if hit:
            raise ScenarioFailed(f"unexpected log {hit[0]['message']!r}")
        time.sleep(1)
    return f"no log {spec['log']!r} for {spec['for']} s"


# --- a scenario ------------------------------------------------------------------------


def run_scenario(sc: dict, out_dir: Path, keep: bool) -> dict:
    profiles = [a for p in sc.get("profiles", ["edge"]) for a in ("--profile", p)]
    ctx = Context(scenario=sc, compose=["docker", "compose", *profiles])
    services = sc.get("services", ["device-sim", "edge-agent", "edge-worker"])
    report: dict[str, Any] = {
        "scenario": sc["name"],
        "description": sc.get("description", ""),
        "env": environment(sc),
        "passed": False,
        "steps": ctx.steps,
        "metrics": ctx.metrics,
    }
    t_start = time.monotonic()
    print(f"== {sc['name']}: {sc.get('description', '')}")
    try:
        ctx.sh("up", "-d", "--build", "--wait", "postgres", "backend")
        ctx.measure_clock()
        ctx.started_at = ctx.server_now()
        ctx.sh("up", "-d", "--build", "--force-recreate", *services)
        ctx.run_id = _wait_new_run(ctx)
        report["run_id"] = ctx.run_id
        for i, step in enumerate(sc["steps"], 1):
            since = ctx.started_at
            if "mark" in step:
                ctx.marks[step["mark"]] = ctx.server_now()
            action = do_action(ctx, step)
            if "mark" in step:
                since = ctx.marks[step["mark"]]
            if "since" in step:
                since = ctx.marks[step["since"]]
            record: dict[str, Any] = {"step": i}
            if action:
                record["action"] = action
            if "expect" in step:
                observed, effect, detail = wait(
                    ctx, step["expect"], float(step.get("timeout", 30)), since
                )
                record["expect"] = detail
                if "measure" in step:
                    m = step["measure"]
                    origin = ctx.marks[m["from"]]
                    basis = "server-event" if effect else "observed"
                    value = ((effect or observed) - origin).total_seconds()
                    if value < 0:  # an effect stamped before the mark: use observation
                        value, basis = (observed - origin).total_seconds(), "observed"
                    ctx.metrics[m["name"]] = {
                        "value": round(value, 3),
                        "unit": "s",
                        "basis": basis,
                        "what": m.get("what", ""),
                    }
                    if "max_s" in m and value > float(m["max_s"]):
                        raise ScenarioFailed(f"{m['name']} = {value:.2f} s > budget {m['max_s']} s")
                    detail += f"  [{m['name']} {value:.2f} s, {basis}]"
                print(f"  ok  {detail}")
            elif "absent" in step:
                record["expect"] = absent(ctx, step["absent"], since)
                print(f"  ok  {record['expect']}")
            elif action:
                print(f"  ..  {action}")
            ctx.steps.append(record)
        if "redetect" in sc:
            report["redetect"] = redetect(ctx, sc["redetect"])
        report["passed"] = True
    except ScenarioFailed as exc:
        report["error"] = str(exc)
        print(f"  FAIL {exc}")
        diagnostics(ctx)
    finally:
        for p in ctx.detached:
            p.kill()
        if not keep:
            ctx.sh("stop", "-t", "5", *services, check=False)
        report["duration_s"] = round(time.monotonic() - t_start, 1)
    (out_dir / f"{sc['name']}.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def _wait_new_run(ctx: Context) -> str:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            run_id = get(f"/robots/{ROBOT}")["current_run_id"]
            if run_id and ts(get(f"/runs/{run_id}")["started_at"]) >= ctx.started_at - timedelta(
                seconds=2
            ):
                print(f"  ok  {ROBOT} opened run {run_id}")
                return run_id
        except httpx.HTTPError:
            pass
        time.sleep(POLL_S)
    raise ScenarioFailed(f"{ROBOT} opened no new run")


def redetect(ctx: Context, spec: dict) -> dict:
    """End the run (SIGTERM -> ABORTED), then replay today's rules over it."""
    ctx.sh("stop", "-t", "10", "edge-agent")
    deadline = time.monotonic() + 30
    while get(f"/runs/{ctx.run_id}")["status"] == "RUNNING":
        if time.monotonic() > deadline:
            raise ScenarioFailed("the run did not end after SIGTERM")
        time.sleep(0.5)
    r = post(f"/runs/{ctx.run_id}/redetect", {})
    summary = {
        "batches": r["batches"],
        "caught": r["caught"],
        "missed": r["missed"],
        "new": r["new"],
        "keys": [{"key": k["dedup_key"], "verdict": k["verdict"]} for k in r["keys"]],
    }
    line = (
        f"redetect over {r['batches']} batches: caught {r['caught']}, missed {r['missed']}, "
        f"new {r['new']}"
    )
    if (
        r["caught"] < spec.get("caught_min", 0)
        or r["missed"] > spec.get("missed_max", 0)
        or r["new"] > spec.get("new_max", 10**6)
    ):
        raise ScenarioFailed(f"{line}; keys {summary['keys']}")
    print(f"  ok  {line}")
    return summary


def diagnostics(ctx: Context) -> None:
    for what, path in (
        ("topics", f"/robots/{ROBOT}/topics"),
        ("nodes", f"/robots/{ROBOT}/nodes"),
        ("open incidents", f"/incidents?status=open&robot_id={ROBOT}"),
    ):
        try:
            print(f"  {what}: {json.dumps(get(path))[:1500]}")
        except Exception as exc:  # noqa: BLE001 (diagnostics only)
            print(f"  {what}: <{exc}>")


def environment(sc: dict) -> dict:
    arch = subprocess.run(
        ["docker", "info", "--format", "{{.Architecture}}"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return {
        "device": sc.get("env", "simulated device (openfleet-device-sim, a host process)"),
        "agent": f"native {arch or platform.machine()} Linux container",
        "host": "GitHub Actions runner (shared VM)"
        if os.environ.get("GITHUB_ACTIONS")
        else f"developer machine ({platform.system()} {platform.machine()}, Docker VM)",
        "note": "Real backend and agent; no robot hardware. Latencies include the agent's "
        "1 s sampling period and the backend's detection/sweeper cadence.",
    }


# --- report ----------------------------------------------------------------------------


def markdown(reports: list[dict]) -> str:
    lines = ["# Edge scenarios", "", "| scenario | result | time | metrics |", "|---|---|---|---|"]
    for r in reports:
        metrics = "<br>".join(
            f"{k} = {v['value']} {v['unit']}"
            + (f" ({v['basis']})" if v.get("basis") in ("observed",) else "")
            for k, v in r["metrics"].items()
        )
        if "redetect" in r:
            d = r["redetect"]
            metrics += f"<br>redetect: caught {d['caught']}, missed {d['missed']}, new {d['new']}"
        result = "PASS" if r["passed"] else f"FAIL: {r.get('error', '')[:120]}"
        lines.append(f"| {r['scenario']} | {result} | {r['duration_s']} s | {metrics} |")
    if reports:
        env = reports[0]["env"]
        lines += ["", f"Agent: {env['agent']}. Host: {env['host']}. {env['note']}"]
    return "\n".join(lines) + "\n"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("names", nargs="*", help="scenario names (default: all)")
    p.add_argument("--out", default=str(ROOT / "edge/build/scenarios"))
    p.add_argument("--list", action="store_true")
    p.add_argument("--keep", action="store_true", help="leave the services running")
    args = p.parse_args()
    files = sorted(SCENARIOS.glob("*.yaml"))
    scenarios = [yaml.safe_load(f.read_text()) for f in files]
    if args.list:
        for sc in scenarios:
            print(f"{sc['name']:28} {sc.get('description', '')}")
        return
    if args.names:
        unknown = set(args.names) - {s["name"] for s in scenarios}
        if unknown:
            sys.exit(f"unknown scenario(s): {', '.join(sorted(unknown))}")
        scenarios = [s for s in scenarios if s["name"] in args.names]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    reports = [run_scenario(sc, out, args.keep) for sc in scenarios]
    md = markdown(reports)
    (out / "scenarios.md").write_text(md)
    print()
    print(md)
    failed = [r["scenario"] for r in reports if not r["passed"]]
    if failed:
        sys.exit(f"FAILED: {', '.join(failed)}")


if __name__ == "__main__":
    main()
