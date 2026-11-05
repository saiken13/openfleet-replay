# Handoff

## Context
- **AI assistant/model:** Claude Code, Opus 5.5 (`claude-opus-5-5`)
- **Date:** 2026-11-05
- **Branch:** `main` (remote: https://github.com/saiken13/openfleet-replay)
- **Latest commit:** see `git log -1`

## Current state
Phase 1, step 3 is done: the run lifecycle API. Steps 1 (skeleton) and 2 (schema and robot registration) were done earlier.

## Completed
- **Docs:** DECISIONS 001–016 are all **Accepted**.
- **Skeleton:**
  - FastAPI app factory and `pydantic-settings` config
  - `GET /api/v1/health`
  - Docker Compose, plus GitHub Actions CI (ruff, `alembic upgrade`, `alembic check`, pytest)
- **Schema:** `robots` and `runs` (migration `0001`). Status columns are TEXT + CHECK (#015).
- **Robots:**
  - `PUT /api/v1/robots/{id}`: idempotent identity upsert
  - `GET /api/v1/robots/{id}`
  - `GET /api/v1/robots/{id}/runs?limit=&cursor=`: newest first, keyset pagination on (started_at, id), opaque cursor
- **Runs:**
  - `PUT /api/v1/runs/{run_id}`: client UUID. Returns 201 on create, 200 for an identical retry, 409 if the run exists for another robot or with different values, and 404 for an unknown robot.
  - `POST /api/v1/runs/{run_id}/end`: RUNNING → SUCCESS, FAILED or ABORTED. The same terminal status again returns 200; a different one returns 409. An `ended_at` before `started_at` returns 422.
  - `GET /api/v1/runs/{run_id}`
  - **One RUNNING run per robot (#016):** a newer start aborts older RUNNING runs, and late-synced older runs never become current or roll the version back.
- **Errors:** services raise `app/domain/errors.py` exceptions (NotFound, Conflict, InvalidRequest). `app/api/errors.py` maps them to 404, 409 and 422.
- **Tests (55):**
  - health, robots, runs, migration round-trip
  - `tests/test_concurrency.py` with real commits:
    - registration race
    - 6 concurrent run starts leave exactly one RUNNING run
    - run start racing run end never deadlocks. A mutation check confirmed it: reversing the lock order produced `DeadlockDetectedError`.

## Currently working
Nothing in progress.

## Partially implemented
- `robots.software_version` changes aren't recorded as events yet, because the `events` table doesn't exist. Add a `software_version_changed` event when it does.
- Nothing sets `robots.status` yet. It stays OFFLINE until heartbeats exist.

## Known bugs
None.

## Gotchas
- **Host port 5433:** the owner's Mac runs Homebrew Postgres on 5432, so compose publishes Postgres on host port 5433. CI uses 5432.
- **Lock order:** always lock the robot row before any run rows. `RunService.start()` and `end()` both follow this, and new code that touches both must too (#016).
- **Real-commit tests:** tests that commit for real go in `tests/test_concurrency.py`. Use robot ids starting with `concurrency-`; the `sessions` fixture deletes those rows afterwards.
- **Alembic in tests:** tests run Alembic through `run_in_fresh_thread()`, because `env.py` calls `asyncio.run()`.
- **Enum changes:** changing a `StrEnum` needs a migration that updates the CHECK constraint, and `alembic check` won't catch it (#015).
- **Timestamps in tests:** `now()` is constant within a rolled-back test, so don't assert on `updated_at` changes there.
- **422 literal:** `app/api/errors.py` uses a literal 422, because Starlette renamed that status constant.

## How to start the system
```bash
docker compose up --build
curl -X PUT localhost:8000/api/v1/robots/robot-01 -H 'content-type: application/json' -d '{"name":"Robot 1"}'
curl -X PUT localhost:8000/api/v1/runs/$(uuidgen) -H 'content-type: application/json' \
  -d '{"robot_id":"robot-01","software_version":"1.0.0","started_at":"2026-11-05T10:00:00Z"}'
```

## How to run tests
```bash
docker compose up -d postgres
cd backend && .venv/bin/pytest -v
```
(First time: `python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"`.)

## Recommended next task
**Phase 1, step 4: heartbeat and liveness** (FLOW.md §2).
1. `POST /api/v1/robots/{robot_id}/heartbeat {ts, software_version?}` sets `last_seen_at` to **server** time (#010) and returns 204. A heartbeat from an OFFLINE robot marks it ONLINE for now; DEGRADED and ERROR come with incidents later.
2. Settings: `HEARTBEAT_TIMEOUT_S`, `SWEEP_INTERVAL_S`, `RUN_ABANDON_AFTER_S`.
3. Sweeper (`app/workers/sweeper.py`): a periodic task started in the lifespan.
   - `mark_stale_offline(now)` marks robots OFFLINE when `last_seen_at < now - timeout`.
   - `abort_abandoned(now)` aborts RUNNING runs on robots that have been offline longer than `RUN_ABANDON_AFTER_S`.
   - Make `tick(now)` callable directly, so tests don't depend on wall-clock time.
4. Decide whether to build the `events` table now (for `robot_status_changed` and `software_version_changed`) or in the next step. Recommendation: now, since the heartbeat is the first producer of events.
