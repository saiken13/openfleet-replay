# Handoff

## Context
- **AI assistant/model:** Claude Code, Opus 5.5 (`claude-opus-5-5`)
- **Date:** 2026-11-05
- **Branch:** `main` (remote: https://github.com/saiken13/openfleet-replay)
- **Latest commit:** see `git log -1`

## Current state
**The MVP is complete.** Everything in the README's "What it does" section is implemented, tested, and checked end to end with the real stack:
- Docker Compose: Postgres + backend + nginx frontend + simulator
- Screenshots taken of the fleet, incident and replay pages

CI has four jobs: backend, simulator, frontend, and `e2e-regression-check`.

## Completed
- **Backend** (`backend/app/`):
  - Registration, runs (#013, #016), telemetry ingest, heartbeat.
  - Sweeper.
  - Pure detection rules plus the reconciler (dedup #007, escalation, hysteresis, offline semantics #019, run-data freshness #017).
  - Incidents API with triage and timeline.
  - Realtime hub and WebSockets.
  - Replay stream, run summary, and compare.
- **CLI** (`backend/openfleet_cli/`): `openfleet check RUN -c regression.yaml [--baseline RUN] [--json]`, with exit code 0, 1 or 2.
- **Simulator** (`simulator/`):
  - Seeded YAML scenarios: `demo`, `healthy`, `lidar_regression`.
  - Failure kinds: topic_rate, node_state, cpu, memory, battery_drain, offline, log.
  - Modes: real time, `--fast`, and `--loop`.
- **Frontend** (`frontend/`): fleet, robot (live), incidents, incident detail (charts, timeline, first signal, triage), runs, run replay, and compare.
- **Tests:**
  - Backend: 121, against real Postgres, including concurrency races and mutation-checked guards.
  - Simulator: 9.
  - Frontend: 11 (reducer and ReplayClock).
- **Docs:** README, ARCHITECTURE, FLOW (real file:function paths), DECISIONS 001–019, BUGS 001–003, CONSTRAINTS.

## Currently working
Nothing in progress.

## Not built yet (deliberately out of MVP scope)
- **ROS 2 collector:** a `rclpy` node that measures topic Hz and node state and posts batches. The API contract is the simulator's.
- **Re-detection replay:** run current rules over a recorded run's snapshots.
- **Authentication and multi-tenant support.**
- **rosbag2/MCAP storage:** object storage.
- **Scaling work** (ARCHITECTURE §10): advisory lock for the sweeper, Redis/NATS fan-out, TimescaleDB, and retention.

## Known bugs
None open. Fixed bugs are recorded in docs/BUGS.md.

## Gotchas
- **Ports:**
  - Postgres is on host port **5433**, because the owner's Mac runs Homebrew Postgres on 5432.
  - Dashboard on **8080** (nginx); dev server on 5173.
- **Lock order:** robot → runs → incidents, everywhere.
- **Realtime publishing:** services must publish only through `Service.commit()` / `publish_after_commit()`.
- **Test fixtures:**
  - The `clock` fixture controls server time for API tests.
  - The `sweeper` fixture shares the test transaction; call `await sweeper.tick(now)`.
- **Real-commit tests** go in `tests/test_concurrency.py`, using ids prefixed `concurrency-`. `purge_robots()` cleans them up.
- **Alembic in tests:** runs through `run_in_fresh_thread()`.
- **Enum changes:** changing a `StrEnum` needs a migration that updates the CHECK constraint (#015).
- **Clocks:** incident times are server time and frame times are robot time. They're aligned when robot clocks are roughly correct, which the simulator ensures in real-time mode (BUG-003).
- **Fast mode:** in `openfleet-sim --fast`, incidents don't auto-resolve within the run, because hysteresis uses server time. This is expected.
- **Wrong Vite version:** running `npx vite` may pick up a different global Vite. Use `npm run dev`.

## How to start the system
```bash
docker compose --profile demo up --build      # dashboard http://localhost:8080
```

## How to run tests
```bash
docker compose up -d postgres
(cd backend && .venv/bin/pytest)
(cd simulator && .venv/bin/pytest)
(cd frontend && npm test && npm run typecheck)
```

## Recommended next task
**ROS 2 collector** (`collector/`):
1. An `rclpy` node with a configurable topic list and expected rates.
2. Measure Hz and last-message age per topic over a 1 s window, and node liveness via the ROS graph.
3. POST the same `TelemetryBatch` the simulator sends.
4. Reuse the simulator's `FleetApi` retry semantics.
5. Test it in a ROS 2 Humble/Jazzy container in CI.
