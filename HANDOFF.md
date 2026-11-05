# Handoff

## Context
- **AI assistant/model:** Claude Code, Opus 5.5 (`claude-opus-5-5`)
- **Date:** 2026-11-05
- **Branch:** `main` (remote: https://github.com/saiken13/openfleet-replay)
- **Latest commit:** see `git log -1`

## Current state
Phase 1, step 1 is done. The backend skeleton runs in Docker Compose and in CI. It has no domain tables or features yet.

## Completed
- Design docs. All DECISIONS 001–014 are **Accepted** (owner approved on 2026-11-05).
- Owner's open questions resolved: `pyproject.toml` packaging, Python 3.12, Apache-2.0 (DECISIONS #014).
- `backend/`: FastAPI app factory (`app/main.py`), `pydantic-settings` config (`app/core/config.py`), async engine and `ping()` (`app/db/engine.py`), `GET /api/v1/health` (200 when the DB answers, 503 otherwise).
- Alembic initialized for async; reads the URL from settings and has no revisions yet.
- `docker-compose.yml` with Postgres 16 and the backend. The backend runs `alembic upgrade head` and then uvicorn.
- Tests: `tests/test_health.py` covers the OK path against real Postgres and the 503 path against an unreachable DB.
- GitHub Actions CI (`.github/workflows/ci.yml`): ruff lint and format check, `alembic upgrade head`, and pytest with a Postgres service container.

## Currently working
Nothing in progress.

## Partially implemented
None.

## Known bugs
None.

## Gotchas
- **Host port 5433:** compose publishes Postgres on host port 5433 because the owner's machine already runs Homebrew Postgres on 5432. Inside the compose network it's still `postgres:5432`. CI uses 5432 (a clean runner).
- Tests use whatever `OPENFLEET_DATABASE_URL` points at. The default is the compose DB on `localhost:5433`.
- `app = create_app()` runs at import time (needed by uvicorn). Tests call `create_app(settings)` to get isolated instances.

## How to start the system
```bash
docker compose up --build
curl localhost:8000/api/v1/health
```

## How to run tests
```bash
docker compose up -d postgres
cd backend && .venv/bin/alembic upgrade head && .venv/bin/pytest -v
```
(First time: `python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"`.)

## Recommended next task
**Phase 1, step 2: first schema.**
1. Add a SQLAlchemy declarative `Base` and an async session dependency (one session per request).
2. Write the first Alembic revision with `robots` and `runs` (ARCHITECTURE §3) and test both upgrade and downgrade.
3. Set up test isolation: run each test in a transaction that is rolled back (DECISIONS #012).
4. Add `PUT /api/v1/robots/{robot_id}` (idempotent upsert, DECISIONS #013) with tests.
