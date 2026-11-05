# Handoff

## Context
- **AI assistant/model:** Claude Code, Opus 5.5 (`claude-opus-5-5`)
- **Date:** 2026-11-05
- **Branch:** `main` (remote: https://github.com/saiken13/openfleet-replay)
- **Latest commit:** see `git log -1`

## Current state
Phase 1, step 2 is done: the first schema plus robot registration. Phase 1, step 1 (the backend skeleton) was done earlier.

## Completed
- Design docs. DECISIONS 001–015 are all **Accepted**.
- Backend skeleton: FastAPI app factory, `pydantic-settings` config, `GET /api/v1/health` (200 when the DB is up, 503 when it isn't), Docker Compose, and GitHub Actions CI.
- **Schema:** SQLAlchemy `Base` with a constraint naming convention (`app/db/base.py`). Models `Robot` and `Run` (`app/db/models.py`). Migration `alembic/versions/0001_robots_and_runs.py`, including the circular FK `robots.current_run_id → runs.id` (ON DELETE SET NULL).
- **Session:** `app/db/session.py :: get_session`, one `AsyncSession` per request with `expire_on_commit=False`. Services own the commit.
- **Robot registration:**
  - `PUT /api/v1/robots/{robot_id}` returns 201 on create and 200 on update. It replaces only the identity fields (`name`, `model`), and the body rejects state fields (422).
  - `GET /api/v1/robots/{robot_id}` returns 200 or 404.
  - Layers: router → `RobotService` → `RobotRepository`.
- **Test infrastructure (`tests/conftest.py`):**
  - Tests use a dedicated `openfleet_test` DB, created automatically and migrated once per session.
  - Each test gets an outer transaction that's rolled back afterwards, and the app's session joins it with `create_savepoint`.
- **Tests (20):**
  - health
  - robot API: create, idempotency, replace, state untouched, validation, 404, isolation
  - deterministic concurrency test for racing first registrations
  - migration round-trip (each revision down, then up)
- CI also runs `alembic check` (models vs migrations).

## Currently working
Nothing in progress.

## Partially implemented
- The `runs` table exists, but there are no run endpoints yet.

## Known bugs
None.

## Gotchas
- **Host port 5433:** the owner's Mac runs Homebrew Postgres 15 and 16 on 5432, so compose publishes Postgres on host port 5433. CI uses 5432.
- **Concurrency test:** the test that commits for real (`test_robot_registration_concurrency.py`) can't use `db_session`. It deletes its own rows in a `finally`. Follow that pattern for any future test that needs real commits.
- **Alembic in tests:** `env.py` calls `asyncio.run()`, so tests run Alembic commands through `run_in_fresh_thread()` to stay off the pytest event loop's thread.
- **Enum changes:** changing a `StrEnum` in `app/domain/enums.py` needs a migration that updates the CHECK constraint, and `alembic check` won't catch a missing one (DECISIONS #015).
- **Timestamps in tests:** `now()` is the transaction start time, and a whole test is one transaction, so you can't assert on `updated_at` changing inside the rolled-back tests.

## How to start the system
```bash
docker compose up --build
curl localhost:8000/api/v1/health
curl -X PUT localhost:8000/api/v1/robots/robot-01 -H 'content-type: application/json' -d '{"name":"Robot 1"}'
```

## How to run tests
```bash
docker compose up -d postgres
cd backend && .venv/bin/pytest -v
```
(First time: `python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"`.)

## Recommended next task
**Phase 1, step 3: runs.**
1. `PUT /api/v1/runs/{run_id}`: the client generates the UUID, and the body is `{robot_id, software_version, started_at}`. Make it idempotent. Return 409 if the run already exists for a *different* robot (DECISIONS #013). Return 404 if the robot is unknown.
2. On start, set `robots.current_run_id`, and if the version changed, set `software_version` too. Events come later, when the `events` table exists.
3. Ending a run: something like `POST /api/v1/runs/{run_id}/end {status, ended_at}`. Only allow RUNNING → terminal transitions, and make it idempotent when the same terminal status is sent again.
4. `GET /api/v1/robots/{robot_id}/runs`, newest first (uses `ix_runs_robot_id_started_at`).
5. Add a domain-error → HTTP mapping (404 and 409) in one place instead of per-router `HTTPException`s.
