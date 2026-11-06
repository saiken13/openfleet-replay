# Handoff

## Context
- **AI assistant/model:** Claude Code, Opus 5.5 (`claude-opus-5-5`)
- **Date:** 2026-11-06
- **Branch:** `main` (remote: https://github.com/saiken13/openfleet-replay)
- **Latest commit:** see `git log -1`

## Current state
**Realtime fan-out across replicas done (2026-11-06, DECISIONS #025).** With `OPENFLEET_REDIS_URL` set (compose sets it), `app/realtime/bus.py :: RedisBus` forwards hub messages over Redis pub/sub. Delivery is local-first, own echoes are skipped, and per-instance seq numbers plus a heartbeat detect loss. On a gap or a (re)subscribe the replica calls `Hub.resync()`, so its WebSockets close with 1012 and clients reload over REST (no frontend change).
- Tests: `tests/test_realtime_bus.py` (5 pure seq-logic tests and 4 against real Redis, skipped locally if Redis isn't on 6380, required in CI), plus hub forward/deliver/resync and WS 1012 tests in `tests/test_realtime.py`.
- Verified live with two replicas (`:8000` and a second container on `:8001`): a heartbeat through either replica updated a fleet WebSocket on the other. `CLIENT KILL TYPE pubsub` closed both sockets with 1012 and they resubscribed. With Redis stopped, the backend kept serving, and it recovered on restart.
- **Together with #024, several backend replicas can run.** There's no compose profile with 2 replicas behind nginx yet; the check was done by hand (see Gotchas).

**Sweeper is replica-safe (2026-11-06, DECISIONS #024).** Each tick takes a session-level `pg_try_advisory_lock` on its own connection (`app/workers/lock.py`); replicas that miss it skip the tick. Tests: `tests/test_sweeper_lock.py` (exclusion, release on error and on cancel, sweeper skip). Verified live with a second backend replica on the compose DB: while psql held the sweeper's key, neither replica marked a silent robot offline (the replica logged 12 skips), and the robot went OFFLINE within one interval of release.

**Re-detection replay done (2026-11-06, DECISIONS #023).** `POST /runs/{id}/redetect` runs the current rules (or overridden thresholds) over a recorded run and compares with the recorded incidents: caught / missed / new.
- Pure engine `app/detection/replay.py`, service `app/services/redetect_service.py`, run-page panel `frontend/src/components/RedetectPanel.tsx`, CLI `openfleet redetect RUN [--set k=v] [--fail-on-missed] [--json]`.
- Verified with all suites and on the live compose stack through nginx (viewer cookie, CSRF 403), on `lidar_regression` and all `demo` robots. On a `--fast` demo run it found the amr-04 offline window that live detection missed (fast mode compresses server time; replay uses robot time).
- Not committed yet (neither is auth) unless `git log` shows it. The panel was built and typechecked but not looked at in a browser.

**Auth done (2026-11-05), steps 1–4.**
- Per-robot API keys on robot writes (DECISIONS #021).
- Users with viewer/operator roles, a dashboard login (HttpOnly session cookie), API tokens for the CLI/CI, auth on every read, and authenticated WebSockets (DECISIONS #022).
- Verified with all suites, the live compose stack through nginx (cookie, CSRF header, roles, WS Origin, logout), the CI regression gate run locally with `OPENFLEET_TOKEN`, and the ROS 2 Jazzy e2e.
- Not committed yet unless `git log` shows it.

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
- **User auth** (#022):
  - `users` and `user_tokens` (migration 0004), `app/core/passwords.py` (scrypt), `app/services/user_service.py`, `app/api/v1/auth.py` (login/logout/me/config).
  - `current_user` / `require_viewer` / `require_operator` in `app/api/auth.py`; WS auth, Origin check and re-auth in `app/api/v1/ws.py`.
  - CLI: `openfleet users …`; `check --token` / `OPENFLEET_TOKEN`.
  - Frontend: `src/auth/AuthContext.tsx`, `src/pages/LoginPage.tsx`, `src/lib/auth.ts`.
- **Simulator** (`simulator/`):
  - Seeded YAML scenarios: `demo`, `healthy`, `lidar_regression`.
  - Failure kinds: topic_rate, node_state, cpu, memory, battery_drain, offline, log.
  - Modes: real time, `--fast`, and `--loop`.
- **Frontend** (`frontend/`): fleet, robot (live), incidents, incident detail (charts, timeline, first signal, triage), runs, run replay, and compare.
- **Tests:**
  - Backend: 214 (incl. 9 bus tests, 4 sweeper-lock tests, 15 pure replay-engine tests and 8 redetect API tests), against real Postgres, including concurrency races and mutation-checked guards. Auth tests: `tests/test_auth.py` (robots), `tests/test_user_auth.py` (users, with a route-coverage test), the WS auth in `tests/test_realtime.py`, and the CLI in `tests/test_cli.py`.
  - Simulator: 13.
  - Collector: 24.
  - Frontend: 17 (reducer, ReplayClock, auth helpers, redetect overrides).
- **Docs:** README, ARCHITECTURE, FLOW (real file:function paths), DECISIONS 001–025, BUGS 001–003, CONSTRAINTS.

## Currently working
Nothing in progress.

## Not built yet
- **Auth follow-ups:** login rate limiting (do it at the proxy for now), SSO/OIDC, multi-tenancy, sliding sessions.
- **rosbag2/MCAP storage:** object storage.
- **Scaling work** (ARCHITECTURE §10): TimescaleDB, retention, detection behind a queue. (Sweeper lock #024 and Redis fan-out #025 are done.)

## Known bugs
None open. Fixed bugs are recorded in docs/BUGS.md.

## Gotchas
- **Ports:**
  - Postgres is on host port **5433**, because the owner's Mac runs Homebrew Postgres on 5432.
  - Dashboard on **8080** (nginx); dev server on 5173.
  - Redis on host port **6380** (compose). Bus integration tests use `redis://localhost:6380/15` (`OPENFLEET_TEST_REDIS_URL`) and skip if it's down, so run `docker compose up -d redis` before the tests.
- **Second replica by hand:** `docker compose run -d --no-deps --name ofr-replica2 -p 8001:8000 backend`, then `docker rm -f ofr-replica2`. The script used for the fan-out check opens `/ws/fleet` on both ports with the demo viewer token and heartbeats through the other port.
- **Bus semantics:** never call `hub.deliver` for locally produced messages (that skips forwarding), and never `hub.publish` something received from the bus (that re-forwards it).
- **Lock order:** robot → runs → incidents, everywhere.
- **Realtime publishing:** services must publish only through `Service.commit()` / `publish_after_commit()`.
- **Test fixtures:**
  - The `clock` fixture controls server time for API tests.
  - The `sweeper` fixture shares the test transaction; call `await sweeper.tick(now)`.
  - That fixture has `lock=None` (no advisory lock); set `sweeper.lock = AdvisoryLock(engine, KEY)` to test locking, with a key other than the real sweeper's so a running dev backend can't interfere.
- **Real-commit tests** go in `tests/test_concurrency.py`, using ids prefixed `concurrency-`. `purge_robots()` cleans them up.
- **Alembic in tests:** runs through `run_in_fresh_thread()`.
- **Enum changes:** changing a `StrEnum` needs a migration that updates the CHECK constraint (#015).
- **Clocks:** incident times are server time and frame times are robot time. They're aligned when robot clocks are roughly correct, which the simulator ensures in real-time mode (BUG-003).
- **Fast mode:** in `openfleet-sim --fast`, incidents don't auto-resolve within the run, because hysteresis uses server time. This is expected.
- **Credentials in tests:** the `client` fixture signs every request (`RobotKeyAuth` in conftest).
  - Robot writes use the acting robot's key (`test-robot-key-<id>`). Everything else uses an OPERATOR user token (`ofu_test-operator-token`).
  - Both are issued inside the test transaction. `/auth/*` requests are left unsigned.
  - To test auth itself, pass `auth=None` plus explicit headers.
  - `passwords.SCRYPT_N` is lowered in conftest for speed.
  - WS tests need a really committed user (TestClient runs its own loop), so they use the `concurrency-ws-` prefix; `purge_robots()` also deletes users by that prefix.
- **New endpoints:** `test_every_api_route_requires_robot_or_user_auth` fails until a route has `dependencies=[VIEWER]`, an `Operator`, or `RobotAuth`. Its route count (24) also needs updating.
- **Demo users:** `examples/demo-users.json` (operator/operator-demo, viewer/viewer-demo, and the viewer token `ofu_demo_viewer_public-not-secret`, which CI and `collector/ros_test/e2e.py` use) is public and seeded by compose. A test keeps CI and the e2e in sync with it.
- **Cookies:** the session cookie is `Path=/api`, SameSite=Strict. Every dashboard request sends `X-OpenFleet-Client`; cookie writes without it get 403. nginx forwards `$http_host` (the WS same-origin check needs the port).
- **Demo keys:** `examples/demo-robot-keys.json` is public and seeded by compose (backend and simulator mount it). Add any new scenario robot or collector test robot there; `test_demo_keys_file_covers_every_bundled_robot` enforces it. The simulator needs `--keys` / `OPENFLEET_ROBOT_KEYS_FILE`; the collector needs `OPENFLEET_API_KEY` or `api_key`.
- **Re-detection parity:** `_Reconciler` in `app/detection/replay.py` mirrors `Detector.reconcile`. Change one, change the other; `test_replay_with_current_rules_matches_live_detection` catches drift.
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
Multi-replica (sweeper lock + Redis fan-out) is done. Pick one:
1. **Multi-replica e2e in CI:** a compose profile with 2 backend replicas behind nginx, plus a CI job that opens a WS on one and drives events through the other (the manual check, scripted).
2. **Auth hardening:** login rate limiting, an OIDC provider as another way to create a session, and an audit view of triage actions (the actor is already on events).
3. **Re-detection follow-ups:** store heartbeats per run so replay liveness matches live exactly; a `redetect` step in the CI e2e job (`--fail-on-missed` on the lidar_regression run).
