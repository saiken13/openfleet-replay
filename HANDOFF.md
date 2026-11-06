# Handoff

## Context
- **AI assistant/model:** Claude Code, Opus 5.5 (`claude-opus-5-5`)
- **Date:** 2026-11-05
- **Branch:** `main` (remote: https://github.com/saiken13/openfleet-replay)
- **Latest commit:** see `git log -1`

## Current state
**Auth step 1 done (2026-11-05): per-robot API keys on all robot writes** (DECISIONS #021). Verified with the backend/simulator/collector suites, the live compose demo, the CI regression gate run locally, and the ROS 2 Jazzy e2e. Not committed yet unless `git log` shows it.

**ROS 2 collector done (2026-11-06).** `collector/` runs on real ROS 2 and is verified end to end on Jazzy in CI. Earlier summary below.

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
- **CLI** (`backend/openfleet_cli/`): `openfleet check RUN -c regression.yaml [--baseline RUN] [--json]`, with exit code 0, 1 or 2. `openfleet keys create|list|revoke` (direct DB).
- **Robot auth** (#021): `robot_api_keys` (sha256, migration 0003), `app/api/auth.py` (`RobotAuth`, `require_robot`), ownership via `caller_robot_id` in RunService.end / TelemetryService.ingest. 401 before validation, 403 for another robot's id/run. Startup seeding from `OPENFLEET_ROBOT_KEYS_FILE`.
- **Simulator** (`simulator/`):
  - Seeded YAML scenarios: `demo`, `healthy`, `lidar_regression`.
  - Failure kinds: topic_rate, node_state, cpu, memory, battery_drain, offline, log.
  - Modes: real time, `--fast`, and `--loop`.
- **Frontend** (`frontend/`): fleet, robot (live), incidents, incident detail (charts, timeline, first signal, triage), runs, run replay, and compare.
- **Tests:**
  - Backend: 139, against real Postgres, including concurrency races and mutation-checked guards (auth: `tests/test_auth.py`, CLI keys in `tests/test_cli.py`).
  - Simulator: 13.
  - Collector: 24.
  - Frontend: 11 (reducer and ReplayClock).
- **Docs:** README, ARCHITECTURE, FLOW (real file:function paths), DECISIONS 001–019, BUGS 001–003, CONSTRAINTS.

## Currently working
Nothing in progress.

## Not built yet
- **Auth steps 2–4:** dashboard/user login with viewer/operator roles, read-API auth, WebSocket auth. Then multi-tenant support.
- **Re-detection replay:** run current rules over a recorded run's snapshots.
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
- **Robot keys in tests:** the `client` fixture signs every request as the robot it acts for (`RobotKeyAuth` in conftest, deterministic `test-robot-key-<id>`, issued inside the test transaction). To test auth itself, pass `auth=None` plus explicit headers.
- **Demo keys:** `examples/demo-robot-keys.json` is public and seeded by compose (backend and simulator mount it). Add any new scenario robot or collector test robot there; `test_demo_keys_file_covers_every_bundled_robot` enforces it. The simulator needs `--keys` / `OPENFLEET_ROBOT_KEYS_FILE`; the collector needs `OPENFLEET_API_KEY` or `api_key`.
- **Wrong Vite version:** running `npx vite` may pick up a different global Vite. Use `npm run dev`.

## How to start the system
```bash
docker compose --profile demo up --build      # dashboard http://localhost:8080
```

## Diagnosing a live system
```bash
docker compose exec -T postgres psql -U openfleet -d openfleet -q < scripts/integrity_checks.sql   # 24 invariants, 0 = OK
docker compose logs backend | grep -iE "error|traceback|sweeper"
```

## How to run tests
```bash
docker compose up -d postgres
(cd backend && .venv/bin/pytest)
(cd simulator && .venv/bin/pytest)
(cd frontend && npm test && npm run typecheck)
```

## Collector notes
- **Code:** `collector/openfleet_collector/`. `rates.py`, `batch.py` and `uplink.py` are pure and unit-tested without ROS. `node.py` is the thin rclpy glue.
- **ROS 2 e2e:** `python collector/ros_test/e2e.py` (Docker, `--profile ros`, ROS_DOMAIN_ID 42). It needs the backend on localhost:8000.
- **Startup grace:** 5 s (BUG-005). Topics are reported only after one full window since they were subscribed.
- **Known limitation:** `/rosout` lines logged right after a node starts can be missed before discovery completes.

## Recommended next task
**Authentication, step 2+** (step 1, robot keys, is done):
1. User login for the dashboard and read APIs, with viewer/operator roles (operators can triage incidents: `PATCH /incidents/{id}`). The `openfleet check` CLI reads summaries, so it needs a credential too (e.g. a read-only token).
2. WebSocket auth (`/ws/...`).
3. Keep `docker compose --profile demo` zero-setup (e.g. a seeded demo user, like the demo robot keys).
