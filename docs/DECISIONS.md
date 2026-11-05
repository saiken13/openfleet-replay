# Decisions

Each entry records *why* we chose something. Status is one of: **Proposed** (not yet agreed), **Accepted**, or **Superseded by #N**.
Don't edit old decisions to change them. Add a new entry that supersedes the old one.

---

## 001 — Modular monolith, not microservices
**Status:** Accepted (2026-11-05)
**Decision:** One FastAPI process with internal modules (api, services, repositories, detection, replay, realtime). The simulator and the frontend are separate processes.
**Why:** At MVP scale the hard problems are data modeling, detection correctness, and replay, not independent deployment. A monolith keeps calls in-process, keeps transactions simple, and makes local debugging easy.
**Alternatives considered:** Separate ingestion, detection, and API services connected by a queue.
**Tradeoffs:** We can't scale ingestion independently of the API yet. The module boundaries (services never import routers; detection is pure) keep a later split cheap. The expected split points are listed in ARCHITECTURE.md §10.
**Date:** 2026-11-05

## 002 — PostgreSQL as the only datastore for the MVP
**Status:** Accepted (2026-11-05)
**Decision:** Postgres stores identity, current state, time series, events, and incidents.
**Why:** It gives us relational integrity (robots → runs → incidents), JSONB for event payloads, partial unique indexes (incident dedup), and `ON CONFLICT` (idempotent ingest). The volume is about one row per robot per second, well within a single instance at demo scale.
**Alternatives considered:** TimescaleDB, InfluxDB/Prometheus for metrics, MongoDB.
**Tradeoffs:** Postgres isn't optimized for very large time series. TimescaleDB is a Postgres extension, so switching later keeps our SQL and schema. A separate TSDB would split the data model before we have a reason to.
**Date:** 2026-11-05

## 003 — Edge-aggregated telemetry (no raw ROS messages)
**Status:** Accepted (2026-11-05)
**Decision:** The collector/simulator computes summaries (topic Hz, last-message age, node state, system metrics) and sends one batch per robot about every second. Raw messages are never sent to the API.
**Why:** A 15 Hz LaserScan is too much data, and the information we need is its *rate*, not its contents. Aggregating at the edge is what makes Postgres viable and keeps the API protocol stable whether the source is the simulator or ROS 2.
**Alternatives considered:** Forwarding every message; storing rosbags directly.
**Tradeoffs:** We can't inspect message *contents* (e.g. NaNs in a scan). That comes with rosbag2/MCAP recordings in object storage (Phase 3).
**Date:** 2026-11-05

## 004 — Hybrid storage: typed time-series tables + one normalized `events` table
**Status:** Accepted (2026-11-05)
**Decision:** Dense numeric data goes in typed tables (`telemetry_samples`, `topic_samples`). Sparse, discrete *changes* go in a single `events` table with `type` + `payload JSONB`. "Now" lives in current-state tables (`robots` state columns, `topic_states`, `node_states`).
**Why:** Timeline and replay need *one ordered stream of things that happened*, so a single events table fits naturally. CPU and topic Hz need typed columns for aggregation (compare runs, charts), and putting them in JSONB would make those queries slow and untyped. Current-state tables mean the dashboard and detection never scan history.
**Alternatives considered:** (a) Everything in one generic event table: simple to write, but poor for aggregation, weak typing, and a huge table. (b) A table per event type: strongly typed, but the timeline becomes a 10-way UNION and every new event type needs a migration.
**Tradeoffs:** Event payloads are only validated by Pydantic, not the DB. Replay has to merge three sources (a single query, but not as trivial as one table). Current state is duplicated data and must be updated in the same transaction as the history.
**Date:** 2026-11-05

## 005 — Async FastAPI + SQLAlchemy 2.0 async (asyncpg)
**Status:** Accepted (2026-11-05)
**Decision:** Use async endpoints and an async SQLAlchemy session per request.
**Why:** WebSockets, the sweeper, and replay streaming all live on the event loop. Async DB access avoids mixing thread pools with the loop and makes all I/O look the same.
**Alternatives considered:** Sync SQLAlchemy in FastAPI's threadpool, which is simpler and has fewer pitfalls.
**Tradeoffs:** Async SQLAlchemy forbids implicit lazy loading (you must load relationships explicitly), and blocking calls on the loop are a hidden hazard. We accept this and will document it in the learning notes.
**Date:** 2026-11-05

## 006 — Detection = pure rules + stateful reconciler, with two triggers
**Status:** Accepted (2026-11-05)
**Decision:** Rules are pure `evaluate(snapshot, now) → findings`. `IncidentService` reconciles findings with open incidents. Detection runs after ingest (event-driven) and on a sweeper tick (time-driven).
**Why:** Pure rules can be unit-tested without a DB and reused unchanged in replay and CI. Absence of data (a missing heartbeat, a topic that stopped) can't trigger on ingest, so a periodic trigger is required.
**Alternatives considered:** One big `check_robot()` function; detection only in a background loop (adds latency); detection only on ingest (misses absence).
**Tradeoffs:** Two entry points can evaluate the same robot at the same moment. Correctness then relies on DB-level dedup (#007), not on the code being careful.
**Date:** 2026-11-05

## 007 — Incident dedup enforced by a partial unique index
**Status:** Accepted (2026-11-05)
**Decision:** `UNIQUE (robot_id, dedup_key) WHERE status <> 'RESOLVED'`, with opens done as `INSERT … ON CONFLICT DO NOTHING`. If the severity goes up, the existing incident is escalated. Auto-resolve happens after `resolve_after_s` without the finding.
**Why:** Checking "does an open incident exist?" in application code and then inserting is a race between the two triggers. The DB constraint makes dedup correct under concurrency. The hysteresis stops flapping topics from creating incident storms.
**Alternatives considered:** Application-level check-then-insert; a Redis lock.
**Tradeoffs:** `dedup_key` design matters. Too coarse merges unrelated failures; too fine creates duplicates. The rules own their key format.
**Date:** 2026-11-05

## 008 — Replay is a read API; pacing happens in the client
**Status:** Accepted (2026-11-05)
**Decision:** `GET /runs/{id}/stream` returns an ordered, paginated merged stream. The frontend's `ReplayClock` paces it and feeds the same reducer as live data.
**Why:** The server stays stateless, seek, pause, and speed are trivial, and live and replay share one UI code path. There's no `POST /runs/{id}/replay` because replaying doesn't change anything on the server.
**Alternatives considered:** A server-paced WebSocket replay session.
**Tradeoffs:** The client downloads the whole run (fine for minutes-long runs; later we'll need windowed fetching for multi-hour runs). Multi-viewer "watch together" replay isn't supported.
**Date:** 2026-11-05

## 009 — No Kafka/NATS/Redis in the MVP
**Status:** Accepted (2026-11-05)
**Decision:** Use HTTP ingest and an in-process pub/sub hub.
**Why:** No current requirement needs a broker: one backend process, demo-scale volume. A broker adds operational load (another service in compose, delivery semantics to reason about) with no benefit yet.
**When to revisit:** when we run multiple backend replicas (fan-out), when detection latency affects ingest latency, or when we need to buffer ingest bursts. All of these are triggers we'll *measure*.
**Tradeoffs:** Robots must retry over HTTP if the backend is down. Idempotent ingest (`UNIQUE(run_id, seq)`) makes retries safe.
**Date:** 2026-11-05

## 010 — Server time for liveness, robot time for ordering
**Status:** Accepted (2026-11-05)
**Decision:** Store both `ts` (robot) and `received_at` (server). Liveness and expiry use server time. Timelines within a run order by `ts`.
**Why:** Robot clocks may not be synchronized, and a robot whose clock is 5 minutes behind must not look offline. Within one run, a single robot clock is self-consistent, and it reflects when things *actually* happened on the robot.
**Tradeoffs:** Ordering across robots is best-effort.
**Date:** 2026-11-05

## 011 — Vite + React SPA instead of Next.js
**Status:** Accepted (2026-11-05)
**Decision:** React + TypeScript + Vite + Tailwind, served as static files.
**Why:** The dashboard is a client-side, authenticated-later, real-time app with no SEO or SSR needs. Next.js would add a second server runtime and its own data-fetching model on top of FastAPI.
**Tradeoffs:** If a public marketing or docs site is wanted later, it would be separate.
**Date:** 2026-11-05

## 012 — Tests run against real PostgreSQL, not SQLite
**Status:** Accepted (2026-11-05)
**Decision:** Integration and API tests use a Postgres container (compose service in dev, service container in CI). Each test runs inside a transaction that is rolled back.
**Why:** We rely on Postgres-only features (partial unique indexes, `ON CONFLICT`, JSONB, `TIMESTAMPTZ`). SQLite would make tests pass while production fails.
**Tradeoffs:** Tests need Docker. Pure units (rules, classifiers) need no DB and stay fast.
**Date:** 2026-11-05

## 013 — Client-generated IDs for runs; idempotent registration
**Status:** Accepted (2026-11-05)
**Decision:** `PUT /robots/{robot_id}` (upsert) and `PUT /runs/{run_id}` with a UUID generated by the collector.
**Why:** Robots reboot and retry. If the server generated run IDs, a lost response would create orphan runs. With PUT on a client-generated ID, retries are harmless, and a robot can start a run while offline and sync it later.
**Tradeoffs:** We have to validate that the client sent a proper UUID and that a run can't be moved to a different robot.
**Date:** 2026-11-05

## 014 — Python 3.12, `pyproject.toml` packaging, Apache-2.0 license
**Status:** Accepted (2026-11-05)
**Decision:** The backend targets Python 3.12 and is packaged with `pyproject.toml` (setuptools), with a `dev` extra for test and lint tools. The project is licensed Apache-2.0.
**Why:** `pyproject.toml` is the standard and lets us declare the future `openfleet` CLI as a console-script entry point. Python 3.12 is the newest version that all our dependencies support well, and it has `asyncio.timeout`. Apache-2.0 includes an explicit patent grant, which matters for robotics companies that might adopt the project, and it matches much of the ROS 2 ecosystem.
**Alternatives considered:** `requirements.txt` (no entry points or metadata); MIT (simpler, but no patent grant).
**Tradeoffs:** No lockfile yet, so dependency versions are lower-bounded but not pinned. We'll add a lockfile (pip-tools or uv) if CI becomes flaky because of upstream releases.
**Date:** 2026-11-05
