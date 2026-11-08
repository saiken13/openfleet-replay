# OpenFleet Replay

[![CI](https://github.com/saiken13/openfleet-replay/actions/workflows/ci.yml/badge.svg)](https://github.com/saiken13/openfleet-replay/actions/workflows/ci.yml)
[![Nightly](https://github.com/saiken13/openfleet-replay/actions/workflows/nightly.yml/badge.svg)](https://github.com/saiken13/openfleet-replay/actions/workflows/nightly.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

**Observability, incident debugging and replay for robot fleets, from the microcontroller to the dashboard.**

When a robot fails in production, engineers need to know what happened, which node or sensor failed first, what changed in the seconds before, which software version was running, and whether the next version fixes it. OpenFleet Replay collects telemetry from ROS 2 robots, Linux robot computers and their microcontrollers; detects failures and opens deduplicated incidents with a timeline; and lets you **replay** any recorded run on the same dashboard used for live data, **re-run new detection rules** over old runs, or **gate CI** on a regression.

![Fleet dashboard: live robot health, sites, open incidents](docs/images/fleet.png)

| Incident with its lead-up timeline | Replaying a recorded run | Sign in with Google |
|---|---|---|
| ![Incident page](docs/images/incident.png) | ![Run replay](docs/images/replay.png) | ![Login page](docs/images/login.png) |

## Highlights

- **Full-stack platform:** FastAPI + PostgreSQL backend (modular monolith), React/TypeScript dashboard with live WebSocket updates, and a CLI. It runs as several replicas behind a load balancer: Redis pub/sub fan-out, and a Postgres advisory lock so only one replica runs the liveness sweeper.
- **Detection and incidents:** pure, unit-tested rules (topic rate, node health, heartbeat, CPU, memory, battery). Incidents are deduplicated by a partial unique index, escalate in place, and auto-resolve with hysteresis.
- **Replay and re-detection:** scrub through any recorded run at 1×–16×. Or run the *current* rules over an old run to see which failures they'd still catch, miss or newly flag, as a UI panel or a CI gate (`openfleet redetect --fail-on-missed`).
- **ROS 2:** a collector measures real topic rates, node liveness, `/rosout` and `/diagnostics` on the robot, and buffers through outages. It's tested end to end on ROS 2 Jazzy in CI.
- **Embedded, without hardware:**
  - a **C++20 edge agent** (threads, bounded queue, `/proc` sampling, a hardened systemd unit);
  - a **binary device protocol** (COBS + CRC-16, C11, fuzzed with libFuzzer);
  - **FreeRTOS firmware** on an emulated STM32F4 (Renode) with a supervised watchdog;
  - a **ROS 2 bridge** (rclcpp) that republishes microcontrollers as ROS topics and `/diagnostics`.

  Builds are tested with ASan, UBSan and TSan, cross-compiled to aarch64, and the agent is booted as a real systemd service on Debian arm64 in QEMU.
- **Security and multi-tenancy:** per-robot API keys; users with roles; sign in with **Google** (OIDC code flow + PKCE); sliding sessions; login throttling; a triage audit log; and **sites**, where a user scoped to one site gets a 404 for anything else.
- **Testing:** 450+ automated tests (backend 279, C/C++ 109, collector 28, frontend 17, simulator 15, Renode 8), more than 10 end-to-end suites against the real Docker stack in CI, and a nightly tier with fault-injection scenarios, fuzzing and benchmarks. 39 design decisions are recorded in [docs/DECISIONS.md](docs/DECISIONS.md).

**Tech:** Python 3.12 · FastAPI · SQLAlchemy 2 (async) · PostgreSQL · Alembic · Redis · React · TypeScript · Vite · Tailwind · C++20 · C11 · CMake · GoogleTest · FreeRTOS · Renode · ROS 2 Jazzy (rclpy, rclcpp) · QEMU · systemd · Docker Compose · GitHub Actions

## What it does

- **Fleet health:** robots register and heartbeat. OpenFleet derives ONLINE / OFFLINE / DEGRADED / ERROR from liveness (server time) and open incidents.
- **Topic and node monitoring:** expected vs. actual Hz per topic (`/scan` 15 Hz → 4 Hz → 0 Hz), plus node state.
- **Failure detection:** pure, unit-tested rules: heartbeat missing during a run, topic rate, node health, CPU, memory, battery.
- **Incidents:**
  - Opened automatically and deduplicated by a database constraint.
  - Escalated in place (WARNING → CRITICAL).
  - Auto-resolved with hysteresis. Never auto-resolved while the robot is offline.
  - Each has a timeline showing what happened first.
- **Runs:** every operating session is recorded with its software version. Starting and ending runs is idempotent (client-generated IDs).
- **Replay:** scrub through a past run at 1×–16×. The frontend feeds replay frames into the *same reducer* as the live WebSocket, so there's one UI, not two.
- **Re-detection replay:** run the *current* rules (or thresholds you're trying out) over a recorded run and see which recorded failures they'd still catch, which they'd miss, which are new, and when each would have fired. On the run page, or `openfleet redetect RUN [--set cpu_high_pct=80]`.
- **Compare and check:**
  - Diff two runs in the UI.
  - Or gate CI with `openfleet check RUN --config regression.yaml [--baseline RUN]`, which exits 1 on regression.
- **ROS 2 collector:** runs on the robot.
  - Measures every configured topic's real rate (raw, best-effort subscriptions, with message types auto-discovered).
  - Watches nodes in the ROS graph, forwards `/rosout` warnings, and reports battery, CPU and memory.
  - Optionally reads `/diagnostics`: each hardware status becomes a node OpenFleet can judge.
  - Keeps buffering through network outages without losing data.
  - Tested end to end on ROS 2 Jazzy in CI.
- **Simulator:** no robot required. Seeded YAML scenarios script failures, so every demo is reproducible.

## Quick start

Requires Docker.

```bash
git clone https://github.com/saiken13/openfleet-replay.git
cd openfleet-replay
docker compose --profile demo up --build
```

- **Dashboard:** http://localhost:8080
- **API docs:** http://localhost:8000/docs

The `demo` profile runs four simulated robots in a loop:
- **amr-02:** its LiDAR degrades and then stops, and its driver crashes.
- **amr-03:** CPU spikes.
- **amr-04:** drops off the network mid-run.
- **amr-01:** stays healthy as the control.

Open the dashboard, click an incident, then **Replay from 15s before ▶**.

Without `--profile demo` you get the platform with an empty fleet. Run the simulator yourself (see below).

Sign in as **operator / operator-demo** (can triage incidents) or **viewer / viewer-demo** (read-only). The login page shows these too.

The local stack seeds **public** demo credentials so it works with no setup: users and a viewer API token from `examples/demo-users.json`, and robot keys from `examples/demo-robot-keys.json`. Never expose that configuration (see [Authentication](#authentication)).

Postgres is published on host port **5433** (not 5432), so it doesn't clash with a locally installed Postgres. Override it with `OPENFLEET_PG_HOST_PORT`. Redis (realtime fan-out between replicas) is on host port **6380** (`OPENFLEET_REDIS_HOST_PORT`).

To run two backend replicas behind a round-robin nginx: `docker compose --profile replicas up -d --build --wait` (replicas on :8000 and :8001, load balancer on :8090), then `python scripts/multi_replica_e2e.py` to check that live updates reach every replica. CI runs the same check.

## Authentication

There are two kinds of credentials:
- **Robots** use a per-robot API key on their writes (register, heartbeat, runs, telemetry). A key only works for its own robot.
- **People** sign in to the dashboard with a password or **with Google** (OpenID Connect). Every read and live view needs a user. Roles:
  - **viewer:** reads everything.
  - **operator:** can also triage incidents. Every triage action is recorded with who did it, on the **Audit** page.
- **CLI and CI** (`openfleet check`) use a user API token.
- **Sites** (multi-tenancy): every robot belongs to a site (a site or a team); a user scoped to a site sees only its robots, runs, incidents, audit and live updates, and anything else is a 404. Users without a site see everything. New robots join `default`.

Manage everything on the server:

```bash
docker compose exec backend openfleet keys create ros-bot-01 --label "lab TurtleBot"   # robot key, printed once
docker compose exec backend openfleet keys list | revoke KEY_ID
docker compose exec backend openfleet users create alice --role operator             # prompts for a password
docker compose exec backend openfleet users list | set-role alice viewer | passwd alice | disable alice
docker compose exec backend openfleet users token create ci-bot --label ci --expires-days 90   # → OPENFLEET_TOKEN
docker compose exec backend openfleet sites create warehouse-a --name "Warehouse A"
docker compose exec backend openfleet sites assign warehouse-a amr-01 amr-02
docker compose exec backend openfleet users set-site alice warehouse-a     # or --all; users create --site too
docker compose exec backend openfleet retention --days 30 --dry-run   # what data retention would remove
```

- **Data retention:** off by default. Set `OPENFLEET_RETENTION_DAYS=30` and the backend compacts finished runs older than that: their summary is frozen (so `openfleet check` against an old baseline still works), and the per-second telemetry and log lines are deleted. Incidents and the triage audit are kept. See DECISIONS #030.
- **Rotating a robot key:** create a new one, deploy it, then revoke the old one. A robot can hold several active keys.
- **Sign in with Google:** create an OAuth client (Google Cloud console → Credentials → OAuth client ID, *Web application*, redirect URI `http://localhost:8080/api/v1/auth/oidc/callback`) and set `OPENFLEET_OIDC_CLIENT_ID` / `_CLIENT_SECRET` in `.env` (see `.env.example`). Anyone with a verified email in `OPENFLEET_OIDC_ALLOWED_DOMAINS` gets a viewer account on first sign-in; invite anyone else with `openfleet users create alice@example.com --sso [--role operator]`. Everyone else is refused. Any OIDC issuer works (`OPENFLEET_OIDC_ISSUER`). See DECISIONS #038.
- **Sessions:** HttpOnly SameSite=Strict cookies. A session ends after 12 h without use, and always 7 days after sign-in (`OPENFLEET_SESSION_TTL_S` / `OPENFLEET_SESSION_MAX_S`).
- **Login throttling:** 5 failed sign-ins per username, or 20 per client IP, in 5 minutes gets a 429 with `Retry-After`. Tune it with `OPENFLEET_LOGIN_*`. Counters are shared across replicas through Redis.
- **Production:** behind HTTPS, set `OPENFLEET_COOKIE_SECURE=true`. Drop the demo `*_FILE` and `LOGIN_HINT` settings. Don't expose the backend port directly, and set `FORWARDED_ALLOW_IPS` to your proxy's address. The proxy must overwrite `X-Forwarded-For` (see `frontend/nginx.conf`).

See [DECISIONS #021, #022, #027 and #029](docs/DECISIONS.md).

## Embedded & C++

A C++20 edge agent, a binary device protocol and FreeRTOS firmware on an emulated STM32F4, all without physical hardware; see the [roadmap](docs/EMBEDDED_ROADMAP.md).

- **Phase 0 (done):** the dev container (`scripts/edge-dev.sh`), CMake presets with sanitizers and an aarch64 cross-compile tested under QEMU in CI, and a Renode STM32F4 smoke test (`firmware/renode/smoke.sh`).
- **Phase 1 (done):** `openfleet-edge-agent`, a C++20 agent for Linux robot computers without ROS. It reports CPU, memory, link latency, watched processes and its own health as robot `edge-01`. It buffers through outages with the collector's delivery contract, and ends its run on SIGTERM. Try it: `docker compose --profile edge up -d --build`, then `python edge/e2e/e2e.py`.
- **Phase 2 (done):** the [device protocol](docs/PROTOCOL.md): COBS + CRC-16 frames from a C11 library shared with the firmware. The agent's gateway turns a microcontroller's sensors into topics, its tasks into nodes, and its resets into events. A simulated MCU with fault injection (`openfleet-device-sim`), two libFuzzer targets, and `python edge/e2e/device_e2e.py`.
- **Phase 3 (done):** [FreeRTOS firmware](docs/FIRMWARE.md) for an STM32F407, run on Renode's emulated STM32F4 (no board). It has sensor, comms, health and watchdog tasks. A stalled task causes a watchdog reset, which OpenFleet shows with its cause and as an incident that resolves. Try it: `docker compose --profile edge --profile firmware up -d --build`, then `python firmware/tests/firmware_e2e.py`.
- **ROS 2 bridge (done):** `openfleet-ros-bridge` (rclcpp, `edge/ros/`) republishes a microcontroller into ROS 2 (readings as topics, health on `/diagnostics`, logs on `/rosout`), so a ROS robot's own collector reports it. `python edge/ros/e2e.py`.
- **Full system (done):** the agent as a real systemd service on Debian 13 arm64 in `qemu-system-aarch64`: boot, crash restart, reboot and poweroff, plus its hardening (`systemd-analyze security` 1.2). `python edge/qemu/qemu_e2e.py`.
- **Phase 4 (done):** a [regression tier and benchmarks](docs/BENCHMARKS.md). YAML scenarios inject faults into the compose stack and measure how long OpenFleet takes to show them (`python edge/e2e/run_scenarios.py`). A Robot Framework suite runs the firmware on Renode in emulated time, and micro-benchmarks cover the protocol and gateway. A nightly workflow puts it all in one report, with every figure labelled native, qemu-user or Renode.

See [edge/README.md](edge/README.md).

## Connect a real ROS 2 robot

On the robot, inside its ROS 2 environment (Humble or Jazzy):

```bash
source /opt/ros/jazzy/setup.bash
python3 -m venv --system-site-packages ~/openfleet && ~/openfleet/bin/pip install ./collector
cp collector/collector.example.yaml collector.yaml   # set api_url, robot_id, topics + expected Hz, nodes
export OPENFLEET_API_KEY=ofk_...                     # from `openfleet keys create <robot_id>`
~/openfleet/bin/openfleet-collector --config collector.yaml
```

It's also available as a Docker image (`collector/Dockerfile`, based on `ros:jazzy-ros-base`). To watch it work against a fake robot (a real ROS 2 node publishing `/scan`), run:

```bash
python collector/ros_test/e2e.py   # scripts a degradation, a stop, a node crash and a backend outage, and asserts each
```

## Regression gate in CI

```bash
pip install -e backend -e simulator
export OPENFLEET_ROBOT_KEYS_FILE=examples/demo-robot-keys.json                   # robot keys
export OPENFLEET_TOKEN=ofu_demo_viewer_public-not-secret                         # read token
openfleet-sim -s simulator/scenarios/healthy.yaml --fast -o base.json            # v1.0.0
openfleet-sim -s simulator/scenarios/lidar_regression.yaml --fast -o cand.json   # v1.1.0
openfleet check "$(jq -r '.[0].run_id' cand.json)" -c examples/regression.yaml \
  --baseline "$(jq -r '.[0].run_id' base.json)"
```

```
Run c9b8e261-…  robot=ci-01  version=1.1.0
  [FAIL] topic /scan avg_hz                  7.63 (limit >= 10)
  [FAIL] baseline topic /scan avg_hz drop %  14.92 → 7.63 Hz (+48.8% drop, limit 10%)
  …
12/14 checks passed                                        (exit code 1)
```

This exact flow runs in this repo's CI (`e2e-regression-check`). The healthy run must pass and the regression must fail.

To check a rule or threshold change against recorded failures instead, use `openfleet redetect RUN --fail-on-missed [--set NAME=VALUE ...]`: it exits 1 if a failure recorded in that run would no longer be detected. CI runs this as a gate on the regression run, and also checks that the gate fails when a looser threshold stops catching the LiDAR drop.

> Replay never re-runs robot software: a recording always gives the same result. To regression-test a new build, produce a **new run** with it (simulator or ROS 2 sim in CI), then `check` it against a baseline. See [ARCHITECTURE §7](docs/ARCHITECTURE.md).

## Architecture

```mermaid
flowchart LR
    subgraph Robot["On the robot"]
        MCU["Microcontroller<br/>FreeRTOS (Renode STM32F4)"]
        EDGE["C++ edge agent<br/>(Linux, no ROS)"]
        BRIDGE["ROS 2 bridge"]
        COL["ROS 2 collector"]
    end
    SIM["Simulator"]
    MCU -- "COBS + CRC frames<br/>UART / TCP" --> EDGE
    MCU -- "same frames" --> BRIDGE
    BRIDGE -- "topics, /diagnostics, /rosout" --> COL
    EDGE -- "HTTP batches (~1/s)" --> API["FastAPI backend<br/>(1..n replicas)"]
    COL -- "HTTP batches" --> API
    SIM -- "HTTP batches" --> API
    API --> PG[("PostgreSQL")]
    API <-->|pub/sub| RD[("Redis")]
    API -- "WebSocket" --> UI["React dashboard"]
    CLI["openfleet CLI / CI gate"] --> API
    UI -. "Sign in with Google (OIDC)" .-> API
```

A single backend process (a modular monolith) contains:
- the API
- services
- a pure-function detection engine
- an incident reconciler
- a liveness sweeper
- a realtime hub (in-process; with Redis pub/sub between replicas, so you can run several backends)

Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · Why it's built this way: [docs/DECISIONS.md](docs/DECISIONS.md) · Request flows: [docs/FLOW.md](docs/FLOW.md)

## Development

```bash
docker compose up -d postgres

# Backend (Python 3.12)
cd backend
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/alembic upgrade head
.venv/bin/pytest                         # real Postgres, separate `openfleet_test` DB (auto-created)
OPENFLEET_ROBOT_KEYS_FILE=../examples/demo-robot-keys.json OPENFLEET_USERS_FILE=../examples/demo-users.json \
  .venv/bin/uvicorn app.main:app --reload  # http://localhost:8000 (seeds demo robot keys and users)

# Simulator
cd simulator
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
export OPENFLEET_ROBOT_KEYS_FILE=../examples/demo-robot-keys.json
.venv/bin/openfleet-sim -s scenarios/demo.yaml            # real time
.venv/bin/openfleet-sim -s scenarios/demo.yaml --speed 4  # compressed
# Other scenarios: healthy, lidar_regression, telemetry_stall (data stops, heartbeats continue)
.venv/bin/pytest

# Frontend (Node 22+) — from the repo root (or `cd frontend` and use the same commands)
npm install        # installs frontend/ dependencies
npm run dev        # http://localhost:5173, proxies /api to localhost:8000 (backend must be running)
npm test && npm run typecheck
```

## Repository layout

| Path | Contents |
|---|---|
| `backend/app/` | FastAPI app: `api/` (routers), `services/`, `repositories/` (SQL), `detection/` (pure rules), `realtime/` (hub), `workers/` (sweeper) |
| `backend/openfleet_cli/` | `openfleet check` / `redetect` (thin HTTP client); `openfleet keys`, `users`, `sites`, `retention` (direct DB) |
| `backend/alembic/` | Migrations (CI runs `alembic check` so models and migrations can't drift) |
| `collector/` | ROS 2 collector (runs on the robot) + ROS 2 end-to-end test |
| `edge/` | C++20 edge agent and device gateway, `ros/` (ROS 2 bridge), `qemu/` (full-system e2e), `e2e/` (scenarios), `bench/` |
| `protocol/` | The device protocol: a C11 library shared by the gateway and the firmware |
| `firmware/` | FreeRTOS firmware for an STM32F407, run on Renode, with Robot Framework tests |
| `simulator/` | Simulated robots + YAML failure scenarios (public API only) |
| `frontend/` | React + TypeScript + Vite + Tailwind dashboard |
| `examples/` | Example regression config; public demo robot keys and users |
| `docs/` | Architecture, 39 design decisions, request flows, protocol, firmware, benchmarks, bugs |

## Status and limits

A portfolio-scale system built to production habits: every feature is tested end to end against the real stack, and every design choice is written down. It runs as one process or several replicas. The known scaling limits and their evolution paths (time-series partitioning, detection behind a queue, object storage) are in [ARCHITECTURE §10](docs/ARCHITECTURE.md). Not built: rosbag/MCAP recordings; real robot hardware (embedded work runs on emulators and simulators, and every figure says which).

## License

[Apache-2.0](LICENSE)
