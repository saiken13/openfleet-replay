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
**Status:** Accepted (2026-11-05). Partly superseded by #025: Redis now carries realtime fan-out between replicas. Ingest is still plain HTTP.
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

## 015 — Status columns are TEXT + CHECK constraints, not native PostgreSQL enums
**Status:** Accepted (2026-11-05)
**Decision:** Columns such as `robots.status` and `runs.status` are `TEXT` with a named `CHECK (status IN (...))`. The allowed values come from Python `StrEnum`s in `app/domain/enums.py` through `app/db/base.py :: enum_check()`.
**Why:** Native PG enums are awkward to evolve. You can't remove a value, adding one used to be impossible inside a transaction, and Alembic autogenerate handles them poorly. A CHECK constraint is dropped and re-created in one ordinary migration. The storage and index cost is negligible at our scale.
**Alternatives considered:** Native `CREATE TYPE … AS ENUM`; lookup tables with FKs (heavier, and no benefit for fixed, code-owned value sets).
**Tradeoffs:** Changing an enum in Python needs a matching migration to update the CHECK constraint. `alembic check` in CI doesn't compare CHECK bodies, so a reviewer has to catch a forgotten one.
**Date:** 2026-11-05

## 016 — At most one RUNNING run per robot; a newer run start aborts older RUNNING runs
**Status:** Accepted (2026-11-05)
**Decision:** A robot's **latest-started run** (by `started_at`, then `id`) is the only run that may be RUNNING.
- When a run starts, every other RUNNING run for that robot becomes ABORTED, with `ended_at` set to the latest run's `started_at`.
- `robots.current_run_id` points at the latest run if it's RUNNING, and is NULL otherwise.
- `robots.software_version` is updated only when the new run is the latest one.
- Run starts and run ends both lock the robot row first, then run rows. That serializes them per robot and keeps the lock order consistent, so they can't deadlock.
**Why:**
- A robot that starts a new run has evidently stopped the old one: it crashed, rebooted, or lost the "end" request. Leaving the old run RUNNING would make liveness, dashboards, and incident attribution ambiguous.
- Ordering by `started_at` instead of arrival order handles runs that were recorded offline and synced late (DECISIONS #013). Those runs never become "current" and never roll the robot's software version back.
**Alternatives considered:**
- Allow several concurrent runs per robot. That's ambiguous about which run telemetry belongs to.
- Reject a new start while a run is RUNNING. A crashed robot couldn't recover without an operator.
- Let the sweeper alone abort stale runs. Slower, and the robot is already telling us.
**Tradeoffs:**
- A late "end" for an auto-aborted run returns 409 unless its status is ABORTED. The run's real outcome (SUCCESS or FAILED) is lost in that case.
- `ended_at` of an auto-aborted run is an upper bound, not the true end.
- The sweeper (`RUN_ABANDON_AFTER_S`) is still needed for robots that go silent without starting a new run.
**Date:** 2026-11-05

## 017 — Detection only trusts fresh, in-run data; ending a run resolves that run's data incidents
**Status:** Accepted (2026-11-05)
**Decision:**
- The detection snapshot includes topics, nodes and system metrics only while the robot has a current run, and only rows refreshed (server time) within `HEARTBEAT_TIMEOUT_S`.
- When the robot reports a run as ended, the unresolved incidents attached to that run are resolved with reason "run ended". `heartbeat_missing` incidents are the exception.
**Why:** Telemetry only flows during runs. Without this:
- The last values of a finished run would keep firing rules forever. A robot that ended its run with `/scan` degraded and then idled would hold a never-ending incident.
- A topic that stopped being reported would freeze in its last state.

Both showed up in the first end-to-end demo.
**Alternatives considered:** Delete `topic_states` and `node_states` at run end. That loses the "last known state" the dashboard shows.
**Tradeoffs:** A problem that really persists across runs (for example a broken LiDAR) gets one incident per run, not one long incident. That arguably matches how runs are compared anyway.
**Date:** 2026-11-05

## 018 — Ingest and detection run in one transaction per request
**Status:** Accepted (2026-11-05). This refines the planned flow in ARCHITECTURE §5 (commit, then detect).
**Decision:** `POST /runs/{id}/telemetry` stores the batch, updates current state, emits change events, runs detection and reconciles incidents in one transaction, holding the robot row lock. Heartbeats and run start/end do the same.
**Why:**
- The incident and robot-status changes are always consistent with the data that caused them.
- A crash between "data committed" and "detection ran" can't leave a robot DEGRADED with no incident, or the reverse.
- The robot lock already serializes everything per robot, and rule evaluation is microseconds.
**Tradeoffs:** Ingest latency includes detection. This is the first thing to move behind a queue at scale (ARCHITECTURE §10).
**Date:** 2026-11-05

## 019 — Liveness semantics: an offline robot's incidents are never auto-resolved; heartbeat loss is an incident only during a run
**Status:** Accepted (2026-11-05)
**Decision:**
- Auto-resolve (absence of a finding for `INCIDENT_RESOLVE_AFTER_S`) only happens while the robot is live.
- `HeartbeatMissingRule` fires only when the robot goes silent with a RUNNING run. An idle robot that powers off just becomes OFFLINE.
- The sweeper aborts the run after `RUN_ABANDON_AFTER_S`, but the heartbeat incident stays open until the robot is heard from again.
**Why:** While a robot is offline we can't observe that a problem cleared, so silence must not read as recovery. Robots that are switched off at the end of a shift shouldn't page anyone.
**Tradeoffs:** A robot that dies mid-run and never returns keeps an open incident until someone resolves it by hand. That's intentional.
**Date:** 2026-11-05

## 020 — ROS 2 collector: raw best-effort subscriptions, one run per collector process, self-healing uplink
**Status:** Accepted (2026-11-06)
**Decision:**
- **Measuring topics:** the collector (`collector/`) subscribes to each configured topic in **raw mode** (no deserialization) with **BEST_EFFORT** QoS, and counts arrivals in a sliding window.
  - BEST_EFFORT subscribers match both reliable and best-effort publishers, so a QoS mismatch can never hide a topic.
  - Topic types are discovered from the ROS graph, so no message packages need configuring.
  - A configured topic that never appears is reported at 0 Hz (FAILED) after a startup grace period (BUG-005).
- **Node liveness:** a node's presence in the ROS graph. A missing node is STOPPED.
- **Runs:** each collector process opens one run and ends it SUCCESS on SIGINT/SIGTERM.
- **Uplink:** a bounded, ordered buffer.
  - Retries reuse the same `seq`, so server-side dedup makes them safe.
  - A 409 (run aborted during a long outage) or 404 (server data reset) starts a new run.
  - Other 4xx responses drop the batch.
- **Pure core:** the ROS-specific part is a thin node. Rate tracking, batch building and the uplink are ROS-independent and unit-tested without ROS.
**Why:**
- The collector must never make a robot worse. Counting raw arrivals is cheap, and it never blocks the robot's own executor, because it runs its own process.
- Monitoring must survive network loss, which is normal for mobile robots.
- One run per process maps naturally to "the robot software was started".
**Alternatives considered:**
- `ros2 topic hz`-style deserializing subscribers: CPU cost on high-rate topics such as images.
- A C++ collector: faster, but much harder to ship and test. Python is fast enough at aggregate rates.
- Run boundaries driven by a mission/task API: robot-specific, so it's left for later.
**Tradeoffs:**
- A BEST_EFFORT subscription can under-count on a lossy network. The collector runs on the robot, so that isn't a practical issue.
- /rosout lines logged in the first moment after a node starts can be missed before discovery completes.
- The node graph shows presence, not health. A node that is alive but wedged shows up as a stale topic rate instead.
**Date:** 2026-11-06

## 021 — Robot authentication: per-robot API keys on the write endpoints
**Status:** Accepted (2026-11-05). Step 1 of auth; dashboard/read auth and WebSocket auth come next.
**Decision:**
- **Credential:** each robot holds one or more API keys and sends `Authorization: Bearer <key>` on the five robot-facing writes: register, heartbeat, run start, run end, and telemetry.
  - A missing, unknown or revoked key gets **401** (with `WWW-Authenticate: Bearer`). Auth runs before body validation, so an anonymous caller learns nothing about the request.
  - A valid key used for another robot gets **403**: another robot's id in the path, or `robot_id` in the run-start body, or another robot's run for end and telemetry. The ownership check for runs sits in the services (`caller_robot_id`), next to the run lookup it needs.
  - An unknown run with a valid key is still a 404.
- **Storage:** table `robot_api_keys` (migration 0003) stores `sha256(key)` with a unique index, plus a short display prefix, a label and `revoked_at`. The key itself is shown once, when it is issued.
  - There's no FK to `robots`, because a key must exist before the robot's first registration.
  - Several active keys per robot are allowed. That's how keys are rotated: issue the new key, deploy it, revoke the old one.
- **Issuing keys:** `openfleet keys create|list|revoke`. It talks to the database directly (`OPENFLEET_DATABASE_URL`), e.g. `docker compose exec backend openfleet keys create amr-05`. There's no admin HTTP endpoint and no extra admin secret.
- **Zero-setup demo:** `OPENFLEET_ROBOT_KEYS_FILE` points the backend at a JSON file of `{robot_id: key}`, which it seeds idempotently at startup. Seeding never un-revokes a key, and a key already issued to another robot is a startup error.
  - Compose seeds the **public** `examples/demo-robot-keys.json`, and the simulator (`--keys` / `OPENFLEET_ROBOT_KEYS_FILE`) and the ROS e2e config use the same file.
  - A test asserts that it covers every bundled scenario robot.
- **Clients:**
  - The simulator signs each request with that robot's key and refuses to start if a scenario robot has none.
  - The collector takes `api_key`, or `OPENFLEET_API_KEY`, which overrides it. On 401/403 it keeps batches buffered and retries with backoff instead of dropping them, so a key fixed on the server side recovers without data loss, within the buffer bound.
**Why:**
- Without auth, anyone who can reach the API can impersonate any robot, inject incidents, or end runs. Robot writes are the most exposed surface, and they're step 1.
- Keys are 256-bit random tokens (`ofk_` + 43 chars), so a single SHA-256 is safe. There is nothing to brute-force, and unlike bcrypt/argon2 it can be looked up by index on the 1 Hz ingest path. It costs one indexed SELECT per request.
- Issuing keys through direct DB access needs no bootstrap admin credential. Whoever can reach the database already controls everything.
**Alternatives considered:**
- Trust-on-first-use (registration returns a key): anyone can claim any unclaimed robot id.
- A fleet-wide shared key: one leaked robot compromises the fleet, and it can't be revoked per robot.
- mTLS: the strongest option, but heavy for robots behind NAT and for the zero-setup demo. Still possible later at the proxy layer.
- JWTs: they add expiry and signing-key management, but keys are long-lived machine credentials anyway, and revocation would still need a server-side lookup.
**Tradeoffs and migration:**
- **Breaking API change:** existing robots must be given a key (`openfleet keys create ROBOT_ID`) and configured with it before upgrading. Reads (dashboard, `openfleet check`) stay open until step 2.
- The demo keys are public. Any deployment reachable by others must drop `OPENFLEET_ROBOT_KEYS_FILE` and revoke the seeded keys.
- There's no `last_used_at` tracking, to keep the ingest path read-only on the key table.
**Date:** 2026-11-05

## 022 — User authentication: cookie sessions for the dashboard, API tokens for CLI/CI, viewer/operator roles, authenticated WebSockets
**Status:** Accepted (2026-11-05). Steps 2–4 of auth (step 1 is #021).
**Decision:**
- **Users:** a `users` table (migration 0004) with a username (stored lowercase), a password hash, a role and `disabled_at`.
  - Passwords are hashed with stdlib **scrypt** (N=2^14, r=8, p=1; ~50 ms). The hash string records its own cost parameters, so the cost can be raised later.
  - Hashing and checking run in a worker thread. An unknown user is checked against a dummy hash, so a login takes the same time either way, and every failure returns the same 401 message.
- **Roles:** `VIEWER` can read everything (REST and WebSockets). `OPERATOR` can also triage (`PATCH /incidents/{id}`), and the operator's username is recorded on the manual-resolution event.
  - A role change applies to existing sessions immediately, because the role is read on every request.
- **Credentials:** a `user_tokens` table holds two kinds of token, both stored as SHA-256 like robot keys.
  - **Sessions** (`ofs_…`) are created by `POST /auth/login` and live only in a cookie: `openfleet_session`, HttpOnly, SameSite=Strict, Path=/api, with an absolute TTL (`SESSION_TTL_S`, 12 h) and `Secure` when `COOKIE_SECURE` is set. `POST /auth/logout` revokes the session.
  - **API tokens** (`ofu_…`) are for the CLI and CI, sent as `Authorization: Bearer`. They have an optional expiry and are created with `openfleet users token create`. `openfleet check` reads `--token` / `OPENFLEET_TOKEN`.
  - A Bearer header wins over the cookie.
- **Enforcement:** every read route depends on `require_viewer` and triage on `require_operator`.
  - A structural test walks all routes and fails if any `/api/v1` route has neither robot nor user auth, apart from an explicit public list: health, `auth/config`, login and logout.
  - A robot key is never a user credential, and a user token is never a robot credential.
- **CSRF:** SameSite=Strict, plus a required `X-OpenFleet-Client` header on cookie-authenticated unsafe requests and on login. A cross-site page can't add that header without a CORS preflight, and we never allow credentialed CORS. The dashboard sends the header on every request.
- **WebSockets:** the handshake needs a live session cookie or a Bearer API token; otherwise it is closed with 1008 (HTTP 403).
  - Cookie handshakes must come from an allowed Origin (`CORS_ORIGINS` or the same host), because browsers don't apply CORS to WebSockets. nginx now forwards `$http_host` so the port survives that comparison.
  - An open socket re-checks its credential every `WS_REAUTH_INTERVAL_S` (30 s) and closes with 1008 once it is revoked, expired, or its user is disabled.
- **Management:** `openfleet users create|list|set-role|passwd|disable|enable|token create|list|revoke`, using direct DB access like `openfleet keys` (#021).
  - Passwords are read from a prompt or `--password-stdin`, never from argv.
  - `passwd` ends the user's sessions but keeps their API tokens.
  - `disable` revokes every token, and re-enabling a user doesn't revive any of them.
- **Zero-setup demo:** `OPENFLEET_USERS_FILE` seeds `{username: {password, role, token?}}` at startup. Seeding never overwrites an existing user's password or role, and never un-revokes a token.
  - Compose seeds the public `examples/demo-users.json` (operator/operator-demo, viewer/viewer-demo, plus a viewer API token for CI and the ROS e2e).
  - Compose also sets `OPENFLEET_LOGIN_HINT`, which the login page shows (`GET /auth/config`).
- **Dashboard:** an AuthProvider resolves `/auth/me`; with no session it shows the login page. Any later 401 returns to the login page, and a closed live socket triggers that check too.
  - The header shows the user and role and a sign-out button. Triage buttons appear only for operators; viewers see "View only".
**Why:**
- **Cookie, not bearer, for the browser:** an HttpOnly cookie can't be read by injected script, while tokens in localStorage can. A cookie also authenticates the WebSocket handshake with no token in the URL, where it would end up in logs and history.
- **Opaque server-side tokens, not JWTs:** logout, disable and role changes take effect immediately. It costs one indexed lookup per request.
- **scrypt:** it's in the stdlib, so no new dependency, and it's memory-hard.
- **Two roles:** viewer and operator are the only distinction the product has today. An admin role isn't needed while user management is a database-level CLI.
**Alternatives considered:**
- An OIDC/SSO provider: the right fit for organizations, but it would break the zero-setup demo. It can be added later as another way to create a session.
- JWTs in localStorage: XSS-readable, and can't be revoked without a server lookup anyway.
- bcrypt/argon2 via a dependency: comparable strength, but one more native wheel to ship.
- Passing the WS token as a query parameter: it leaks into proxy logs.
**Tradeoffs and migration:**
- **Breaking API change:** every read and every WebSocket now needs a user. Create users with `openfleet users create`, and give CI and other read scripts an API token (`OPENFLEET_TOKEN`).
- There's no login rate limiting or lockout yet. The ~50 ms hash slows guessing, but a reachable deployment should rate-limit `/auth/login` at the proxy. *(Superseded by #027: the app throttles logins itself.)*
- Session expiry is absolute, not sliding. A user is signed out after 12 h even while active. *(Replaced by #029: sliding sessions with a 7-day cap.)*
- An open WebSocket can outlive its revocation by up to `WS_REAUTH_INTERVAL_S`.
- The demo credentials are public. Never set the demo `USERS_FILE` or `LOGIN_HINT` on a reachable deployment.
**Date:** 2026-11-05

## 023 — Re-detection replay: pure rules + a pure reconciler mirror over the stored batches, in robot time, read-only
**Status:** Accepted (2026-11-06).
**Decision:**
- `POST /runs/{id}/redetect` (VIEWER, read-only) runs the **current** rule set, optionally with overridden thresholds in the body, over a recorded run and compares the result with the run's recorded incidents. An empty body answers "would today's rules have caught this?".
- **Engine:** `app/detection/replay.py :: replay_run()` is pure, like the rules. For each stored batch (in `ts` order) it rebuilds the `RobotSnapshot` the live detector would have seen, calls the same `evaluate_all`, and feeds the findings to `_Reconciler`, a pure mirror of `Detector.reconcile`: open, escalate in place, auto-resolve after `incident_resolve_after_s`, never resolve while offline, resolve data incidents at run end and keep heartbeat incidents open.
- **Snapshot reconstruction:**
  - Metrics: the last non-null value carries forward, as on the robot row.
  - Topics: classified **again** from the stored rates with the given thresholds (`classify_topic`); a topic counts only while it was reported within the heartbeat timeout (#017).
  - Nodes: from the run's `node_state_changed` events (change-only), applied at their `ts`.
  - Liveness: from the batches. A gap longer than `heartbeat_timeout_s` is the robot going silent: `heartbeat_missing` opens at `prev + timeout` and is re-detected until the next batch.
- **Comparison:** per `dedup_key`, a verdict of `both` (caught), `recorded_only` (missed) or `replay_only` (new), with counts, peak severities and first-detection offsets from the run start. The n-th replayed incident for a key pairs with the n-th recorded one.
- **Clients:** `openfleet redetect RUN [--set NAME=VALUE ...] [--fail-on-missed] [--json]` (exit 1 with `--fail-on-missed` when a recorded failure is no longer detected), and a panel on the run page with a threshold form and "first detected" links that seek the replay.
- **Limit:** `OPENFLEET_REDETECT_MAX_SAMPLES` (50 000 batches, ~14 h at 1 Hz), because the whole run is loaded into memory; larger runs get a 422.
**Why:**
- Reusing the pure rules (CONSTRAINTS) means the replay can't drift from live detection on *what* fires; a parity test (`test_replay_with_current_rules_matches_live_detection`) checks that a live run and its replay agree on keys, counts, severities and titles.
- A separate pure reconciler instead of running `Detector` against a scratch transaction: no writes, no locks, no events or realtime publishes to suppress, and it's unit-testable without a DB.
- Robot time instead of server time: a run uploaded with `--fast` replays with its real durations, so hysteresis behaves as it would have in real time (the live fast-mode gotcha doesn't apply).
- Read-only and stateless: nothing is stored, so changing thresholds and re-running is free.
**Alternatives considered:**
- Storing replay results as "shadow incidents": needs a schema and lifecycle that nothing needs yet.
- Replaying through the ingest path into a throwaway robot: real writes, real events, and cleanup.
- Running in a worker with progress reporting: not needed below the size limit (a 1-hour run is a few hundred ms).
**Tradeoffs:**
- Heartbeats aren't stored, so a robot that kept heartbeating but stopped sending telemetry mid-run shows as `heartbeat_missing` in the replay, where live it only saw stale data. *(Resolved by #028: heartbeats during a run are stored as spans.)*
- A node's state persists until its next change event; a node that stopped being reported keeps its last state (live, it goes stale after the heartbeat timeout).
- Recorded incident times are server time and replay times are robot time; the offsets are comparable only when the robot clock was roughly right (BUG-003).
- Rule *code* changes are picked up automatically (it's the deployed rule set); there is no way to replay with an older rule set.
**Date:** 2026-11-06

## 024 — Sweeper: a per-tick Postgres advisory lock, so any number of replicas can run it
**Status:** Accepted (2026-11-06).
**Decision:**
- Every backend replica still starts the sweeper loop. Each tick first calls `pg_try_advisory_lock(SWEEPER_LOCK_KEY)` on a dedicated pool connection (`app/workers/lock.py :: AdvisoryLock.try_hold`). The holder sweeps and then unlocks. Replicas that don't get it skip that tick (`TickResult.skipped`).
- The lock is **session-level**, because a tick commits once per robot and a transaction-level lock would be released at the first commit.
- If the unlock can't be confirmed (for example, it was cancelled mid-call), the connection is invalidated rather than returned to the pool, so Postgres drops the lock along with the session.
- `Sweeper(lock=None)` sweeps unconditionally (tests); `create_app` always passes a lock.
**Why:**
- No leader election and no lease to renew. The lock is held for milliseconds, and whichever replica asks first in an interval does the work, so losing a replica costs at most one interval.
- The per-robot work takes row locks and deduplicates incidents, but two sweepers still repeat each other's work every interval. The lock removes the overlap instead of relying on every future sweeper step being idempotent.
- No new infrastructure: Postgres is already the coordination point.
**Alternatives considered:**
- A long-lived leader lock held for the process lifetime: it fails over only when the holder's connection dies, and it needs a keepalive and re-acquire loop.
- A dedicated sweeper worker deployment: another deployable to run before it's needed. The lock keeps that option open, since a worker would simply be the only process with `SWEEPER_ENABLED=true`.
- `SKIP LOCKED` per-robot claiming: spreads the work across replicas, but a tick is a few cheap queries, so there's nothing worth spreading yet.
**Tradeoffs:**
- It needs a direct or session-pooled Postgres connection. A transaction-pooling PgBouncer would break session-level locks.
- Each tick briefly uses a second pool connection.
- This makes the **sweeper** multi-replica safe, but not the realtime hub: a broadcast still reaches only the WebSocket clients of the replica that produced it, sweeper broadcasts included. Running several replicas still needs hub fan-out (Redis/NATS, ARCHITECTURE §10).
**Date:** 2026-11-06

## 025 — Realtime fan-out across replicas: Redis pub/sub, local-first delivery, gap detection → resync
**Status:** Accepted (2026-11-06). Partly supersedes #009.
**Decision:**
- **Local first.** `Hub.publish` delivers to local subscribers and then calls `hub.forward`. When `OPENFLEET_REDIS_URL` is set, `forward` is `app/realtime/bus.py :: RedisBus`. Messages from other replicas arrive through `Hub.deliver`, which is local only, so nothing bounces back out.
- **Transport.** One Redis pub/sub channel (`openfleet:realtime`) carries `{o: instance id, s: seq, c: channels, m: message}`. A single sender task drains a bounded outbound queue, so the publisher never blocks and messages from one replica stay in order. Each replica ignores its own echoes.
- **Loss detection, not prevention.**
  - `s` counts every message the instance produced, including ones dropped on queue overflow or a failed publish.
  - A heartbeat repeats the current `s` every 5 s, so losses at the tail of a burst are caught too.
  - A receiver that sees a gap from any origin, or that has just (re)subscribed, calls `Hub.resync()`.
  - Every local WebSocket then closes with **1012**. The dashboard already reconnects and reloads state over REST on every reconnect, so no frontend change was needed.
- **No `REDIS_URL`** means an in-process hub only, which is correct for one backend process (tests, local `uvicorn`). Docker Compose runs Redis 7 (host port 6380) and sets the URL.
- **Redis down** costs only cross-replica delivery: requests, local delivery and startup keep working, and the loops retry with backoff.
**Why:**
- WS messages were already hints with REST as the source of truth (ARCHITECTURE §8). Fan-out doesn't need durable delivery, just *knowing* when something was lost; the client already repairs a lost delta by reloading.
- Local-first keeps single-replica latency and behaviour exactly as before, and a Redis outage degrades the system instead of breaking it.
- Redis is the smallest broker that does pub/sub. It also covers the next likely needs (rate limiting, liveness caching, ARCHITECTURE §10), so it's a well-used piece of infrastructure rather than a one-off.
**Alternatives considered:**
- Postgres `LISTEN/NOTIFY`: no new service, but there's an 8 KB payload limit (frames are larger), notifications are delivered inside the commit path, and every replica would hold a dedicated listening connection.
- NATS: also a good fit, but less widely known and with nothing else here to use it for.
- Redis Streams (durable, replayable): more machinery (consumer positions, trimming) to guarantee deliveries that the client doesn't need.
- Publishing only through Redis and delivering only from the echo: one code path, but every local message pays a round trip and Redis becomes a hard dependency for live updates.
**Tradeoffs:**
- A resync closes *all* of a replica's sockets, not only the affected channels. That's rare (Redis restarts or subscription losses) and cheap for clients.
- The first publish after a Redis restart can fail on a stale pooled connection. It's counted as lost, and the gap check repairs it.
- Every replica receives every message, whether or not it has subscribers for those channels. At fleet scale this would become per-channel Redis subscriptions.
**Date:** 2026-11-06

## 026 — Triage audit log: its own append-only table, not derived from events
**Status:** Accepted (2026-11-06).
**Decision:**
- Every manual incident status change (`PATCH /incidents/{id}`) inserts a `triage_actions` row: `incident_id`, `robot_id`, `actor` (username), `from_status`, `to_status`, `at` (server time). It's in the same transaction as the change. No-ops (already in that status) and rejected changes (409) record nothing.
- `GET /api/v1/triage?actor=&robot_id=&incident_id=&limit=&cursor=` (viewer role) lists actions newest first, with each incident's title. The cursor is the last row's id. The dashboard's **Audit** page (`/triage`) shows it, and each incident page links to its own history.
- Migration `0005` backfills manual resolutions already recorded as `incident_resolved` events (`reason = manual`). Their `from_status` is NULL (never recorded), and so is `actor` when the resolve predates auth.
- The `incident_resolved` event still carries the actor, so the robot timeline is unchanged.
**Why:**
- Before this, only manual *resolves* recorded the actor, and only inside an event payload. INVESTIGATING left no trace at all.
- Events are robot telemetry and will be subject to retention (the planned data-retention work). An audit trail must outlive that, so it gets a small table of its own that retention doesn't touch.
- One row per change, written in the same transaction, can't drift from the incident's status.
**Alternatives considered:**
- Emitting an event for INVESTIGATING and querying `events` by type: no new table, but the audit would then depend on telemetry retention, and querying JSONB payloads by actor needs an expression index.
- A generic audit table for every write (users, keys, triage): more general than anything needs today. User and key management happen through the CLI on the server.
**Tradeoffs:**
- The actor is a username, not a user id. Usernames are unique, users are disabled rather than deleted, and there's no rename. If renames are ever added, store the user id as well.
- Auto-resolves (hysteresis, run end) aren't triage and aren't in this log. They remain events.
**Date:** 2026-11-06

## 027 — Login throttling: per-username and per-IP attempt windows, in Redis with an in-memory fallback
**Status:** Accepted (2026-11-06). Replaces #022's "rate-limit `/auth/login` at the proxy".
**Decision:**
- `POST /auth/login` reserves a slot in two fixed-window counters before checking the password: one for the username (lowercased; unknown usernames are counted too) and one for the client IP. If either is over its limit (`LOGIN_MAX_FAILURES_PER_USER` = 5, `LOGIN_MAX_FAILURES_PER_IP` = 20 per `LOGIN_WINDOW_S` = 300 s), the response is **429** with `Retry-After` (seconds to the end of the window), and the password isn't checked. A limit of 0 disables that scope.
- A successful login clears its username's counter and gives back its IP slot, so only failures use up the limits.
- Counters are `INCR`+`EXPIRE` in Redis (`openfleet:login:{user|ip}:{window}:{id}`) when `REDIS_URL` is set, so every replica shares them. If Redis is unset or unreachable (0.5 s timeout), they're counted in process memory, with one warning logged per outage.
- The window index comes from the injected server clock, so tests control it.
- The client IP is `request.client.host`. Behind the compose nginx it comes from `X-Forwarded-For`: uvicorn trusts it via `FORWARDED_ALLOW_IPS`, and both nginx configs set it to `$remote_addr` (overwrite, never append), because uvicorn uses the first entry.
- The dashboard shows "Too many sign-in attempts: try again in N s."
**Why:**
- Brute force has two shapes: many passwords against one account (stopped by the username limit, from any number of IPs) and one password against many accounts (stopped by the IP limit).
- Reserving before verifying makes the check atomic. With check-then-record, a burst of parallel requests would all pass while scrypt runs; `tests/test_login_throttle.py` fires 20 at once and exactly 5 get through.
- In the app rather than nginx `limit_req`: nginx can only limit per IP and per request (not per username, not failures only), and it would have to be configured in every deployment's proxy. Redis is already there for the bus (#025).
- Counting unknown usernames the same way means a 429 doesn't reveal which accounts exist, matching the indistinguishable 401s from #022.
- Not clearing the IP counter on success stops an attacker from resetting it by signing in to their own account between guesses.
**Alternatives considered:**
- Lockout until an admin unlocks the account: stronger, but turns every guessing attempt into a denial of service against the user and needs an unlock flow.
- Sliding windows or exponential backoff: smoother, but more state per key. A fixed window allows at most twice the limit across a window boundary, which is fine at these numbers.
- Failing open when Redis is down: simpler, but it would turn a Redis outage into "no throttling at all". Per-replica counting is weaker but nonzero.
**Tradeoffs:**
- Anyone can lock a known username out for up to one window by making 5 wrong attempts. That's the usual cost of per-account limits; the window is short and configurable. API tokens (CLI/CI) aren't affected.
- Users behind one NAT share the per-IP limit (20 failures per 5 min).
- `FORWARDED_ALLOW_IPS="*"` in compose means a client calling the published backend port `:8000` directly can choose its own IP and dodge the per-IP limit (not the per-username one). A real deployment shouldn't expose the backend and should set it to the proxy's address.
- With the memory fallback and N replicas behind round-robin, the effective limit during a Redis outage is up to N times higher.
**Date:** 2026-11-06

## 028 — Heartbeats stored per run as merged spans, so re-detection can tell a stall from silence
**Status:** Accepted (2026-11-07). Removes #023's first tradeoff. Refines ARCHITECTURE §3 "Heartbeats are not stored".
**Decision:**
- A heartbeat received while the robot has a current run is recorded in `heartbeat_spans` (`run_id`, `robot_id`, `started_at`, `ended_at`, `heartbeats`; migration `0006`). If it falls within `HEARTBEAT_SPAN_GAP_S` (5 s) of the run's latest span, it extends that span; otherwise it starts a new one. A robot heartbeating steadily through a run costs one row.
- Times are the heartbeat's `ts` (robot clock), falling back to server time when it has none, because the replay works in robot time like the batches it lines them up with. Liveness itself still uses server time only (#010).
- Heartbeats outside a run aren't stored: re-detection is per run, and `heartbeat_missing` only fires during one (#019).
- `replay_run(contacts=...)` treats batches and spans together as contact. A stretch longer than the timeout with neither is silence, as before, and opens `heartbeat_missing` at the last contact plus the timeout. Where only heartbeats arrived, the robot is live and its data goes stale. Live detection re-evaluates on every heartbeat, so the replay evaluates at every instant that gives the same result: the last fresh instant and the first stale one for each topic, each pending hysteresis deadline, and the end of the contact.
- The simulator gained a `telemetry_stall` failure (batches stop, heartbeats with `ts` continue) and a `telemetry_stall` scenario. CI runs it in `--fast` mode and asserts that `openfleet redetect` finds the degraded `/scan` and **no new** incidents. Without spans the stall comes out as a `replay_only` `heartbeat_missing`.
**Why:**
- A hung data pipeline under a live uplink is a real failure mode (the ROS graph stops, the agent doesn't). Live detection treats it as stale data, and the replay called it an outage. That made re-detection disagree with live on exactly the runs worth investigating.
- Spans instead of one row per heartbeat: the replay only needs to know when contact existed, and merging keeps that at almost no storage (the 1 Hz batches already dominate).
- Evaluating at change points instead of a fixed cadence: exact, and the cost doesn't grow with how long a stall lasts.
**Alternatives considered:**
- One row per heartbeat: simplest, but it duplicates the batch row rate for robots that heartbeat during runs, for no extra information.
- Storing heartbeats in `events`: events are for changes; this is continuous contact, and the payload would be read by range.
- Replaying with a 1 s tick through stalls: approximates live heartbeats, but it's inexact and grows with stall length.
**Tradeoffs:**
- A replay with a `heartbeat_timeout_s` override below `HEARTBEAT_SPAN_GAP_S` can't see heartbeat gaps shorter than the merge gap; they're inside one span.
- Heartbeats cost one indexed SELECT and an UPDATE (or INSERT) while the robot row is already locked.
- Clients that heartbeat without `ts` get server-time spans next to robot-time batches; they line up only when the robot clock is roughly right, as with BUG-003.
- Silence before the first batch isn't replayed, as before.
- Neither of the bundled clients heartbeats during a run except in the simulator's `telemetry_stall`; the benefit is for clients that do (e.g. a separate uplink agent).
**Date:** 2026-11-07

## 029 — Sliding sessions with an absolute maximum
**Status:** Accepted (2026-11-07). Replaces #022's fixed 12 h session.
**Decision:**
- A dashboard session now expires after `SESSION_TTL_S` (12 h) **without use**, and in any case `SESSION_MAX_S` (7 days) after sign-in. At login the session row gets `expires_at = now + ttl` and a new `absolute_expires_at = now + max` (migration `0007`, `user_tokens.absolute_expires_at`).
- Each authenticated HTTP request with a session moves `expires_at` to `min(now + ttl, absolute_expires_at)`, but only when that moves it at least `SESSION_REFRESH_S` (300 s), so a busy dashboard writes at most once per 5 minutes per session. The write is a conditional `UPDATE … WHERE expires_at < new`, so concurrent requests can't move it backwards. It's committed in `current_user` before the endpoint runs.
- WebSocket re-checks (`WS_REAUTH_INTERVAL_S`) are read-only: an open socket alone doesn't keep a session alive. Requests do, and every reconnect reloads over REST.
- API tokens never slide; their expiry is the one chosen at creation.
- The cookie's `Max-Age` is now `SESSION_MAX_S`, the most a session can live. The server decides whether it's still live; with a 12 h cookie, the browser would have dropped a session the server had extended.
- Settings validation rejects `SESSION_MAX_S < SESSION_TTL_S`.
**Why:**
- With a fixed 12 h lifetime, someone working all day was signed out mid-task, while a session left on a shared machine lived the same 12 h. An idle timeout plus a hard cap gives both: active users stay signed in, abandoned sessions die early, and a stolen cookie is useless after at most 7 days, however much it's used.
- Throttling the extension keeps the read path essentially read-only; a request writes only when the expiry would move by more than 5 minutes.
- Making WebSockets read-only means "in use" means the person did something. Otherwise a forgotten tab would keep a session alive up to the cap.
**Alternatives considered:**
- Reissuing a new token on each extension (rotation): limits the life of a stolen cookie further, but it needs grace windows for concurrent requests that still carry the old token. Not worth it at this threat level.
- Recording `last_used_at` and computing expiry on read: the same write volume, plus a column to keep in sync.
- Refreshing the cookie on every response: unnecessary once the cookie lives as long as the session can.
**Tradeoffs:**
- A session's real idle lifetime is up to `SESSION_REFRESH_S` shorter than `SESSION_TTL_S`, because the last extension may have been skipped.
- Someone watching only the live dashboard (WebSocket, no clicks) is signed out after 12 h idle, as before.
- Sessions created before `0007` have no absolute expiry: they keep their fixed expiry and don't slide.
**Date:** 2026-11-07

## 030 — Data retention: compact old runs (freeze the summary, drop the raw rows), off by default
**Status:** Accepted (2026-11-07).
**Decision:**
- With `RETENTION_DAYS` set, finished runs whose `ended_at` is older than that are **compacted**. In one transaction, holding the run row lock:
  1. The summary aggregates (the same metric, topic and event-count queries the summary endpoint runs) are frozen into `runs.summary` (JSONB, versioned).
  2. The run's `telemetry_samples`, `topic_samples`, `heartbeat_spans` and `log` events are deleted.
  3. `runs.compacted_at` is set (migration `0008`; partial index `ix_runs_retention` on `ended_at WHERE compacted_at IS NULL`).
- **Kept:** the run, its incidents (with their evidence), its state-change and incident events, and `triage_actions` (#026).
- **Readers:**
  - `GET /runs/{id}/summary`, `/compare` and therefore `openfleet check` read the frozen aggregates for a compacted run, so the numbers are identical to before compaction.
  - `POST /runs/{id}/redetect` returns 422 ("compacted by data retention"), not a misleading replay.
  - `RunRead.compacted_at` is exposed, and the run page shows a notice and hides re-detection.
  - The replay stream returns the remaining events.
- **Where it runs:**
  - In the sweeper tick, so under its advisory lock (#024), at most once per `RETENTION_INTERVAL_S` (1 h) per process and at most `RETENTION_BATCH_RUNS` (20) runs per pass.
  - `openfleet retention --days N [--dry-run] [--json] [--limit]` (direct database access, like `openfleet keys`) for previews and one-off cleanups.
- **Off by default** (`RETENTION_DAYS` unset), including in compose: deleting data must be an explicit choice.
**Why:**
- Per-second samples are almost all of the data (ARCHITECTURE §10), and their value decays: after a few weeks a run matters for its outcome, its incidents and as a regression baseline, not for second-by-second charts.
- Freezing the aggregates before deleting keeps the CI story working. `openfleet check --baseline OLD_RUN` gives exactly the same verdict after compaction, which plain deletion (empty summaries) or downsampling (shifted percentiles and minimums) would not.
- The incident record and the audit trail are small and are the long-term memory; they're never touched.
- Per-run transactions keep each lock short and make compaction all-or-nothing and idempotent (a compacted run is skipped).
**Alternatives considered:**
- Downsampling (keep 1 sample per N s): charts survive, but `check` results drift, and the tables still grow linearly.
- Time partitioning or TimescaleDB with drop-chunk policies: the right tool at scale (still the evolution path in §10), but new infrastructure and per-time rather than per-run semantics, which would cut runs in half.
- Deleting whole runs: loses incidents and baselines.
- A separate cron container: one more moving part; the sweeper already runs exactly once per interval across replicas.
**Tradeoffs:**
- A compacted run's replay and incident charts have no samples, only events. That's stated on the page.
- `ended_at` is the robot's clock (like every run time); a robot with a badly wrong clock is compacted early or late.
- Retention frees rows, but Postgres only returns the disk space after (auto)vacuum. That's fine for steady state; a big one-off cleanup may want a manual `VACUUM`.
- Frozen summaries don't change if summary queries are later improved; `summary.version` exists to tell old freezes apart.
**Date:** 2026-11-07

## 031 — Embedded toolchain: one Linux dev container, CMake presets, cross builds run under qemu-user, Renode native on arm64
**Status:** Accepted (2026-11-07). Phase 0 of [EMBEDDED_ROADMAP](EMBEDDED_ROADMAP.md).
**Decision:**
- All C/C++ and emulator work happens in one Ubuntu 24.04 container (`edge/docker/Dockerfile.dev`, run with `scripts/edge-dev.sh`). It holds gcc 13, clang 18, gdb 15, valgrind 3.22, CMake 3.28, Ninja, a cross g++ for the *other* architecture, qemu-user 8.2 and Renode 1.17.0 (the portable build for the image's architecture).
- `edge/` is a CMake project with presets: `native`, `release`, `asan` (ASan+UBSan), `tsan`, `aarch64`, `x86_64`. The cross toolchain files set `CMAKE_CROSSCOMPILING_EMULATOR` to `qemu-<arch> -L /usr/<arch>-linux-gnu`, so `ctest` runs the cross-built tests through user-mode emulation. GoogleTest is fetched by CMake, pinned by SHA-256. Warnings are errors, for our own targets only.
- CI job `edge` on the x86_64 runner: native, ASan+UBSan, TSan (with `vm.mmap_rnd_bits=28`, which Ubuntu 24.04 needs for TSan), then a real **x86_64 → aarch64 cross-compile**, checked with `file`, whose tests run under `qemu-aarch64`.
- `firmware/renode/smoke.sh` boots an emulated STM32F4 Discovery and checks that its UART4 output arrives over TCP. This is the transport Phase 3 will use between the firmware and the agent.
**Why:**
- The dev Mac is arm64, and Docker on it runs linux/arm64 natively, so "ARM64 under QEMU" would be meaningless locally. The meaningful cross-compile is in CI, from x86_64. Locally the mirror image (arm64 → x86_64 under `qemu-x86_64`) exercises the same toolchain mechanism.
- gdb and valgrind don't run on Apple Silicon macOS; Linux-only APIs (`/proc`, `epoll`, ptys) are what the agent targets anyway. One container gives the same tools locally and in CI.
- Renode spike result (the roadmap's main risk): 1.17.0 ships `linux-arm64-portable`. It ran natively in the arm64 container, booted Antmicro's STM32F4 Contiki demo, and its UART4 came out on a TCP socket. The STM32F4 platform also models the independent watchdog (`STM32_IndependentWatchdog`), which Phase 3 needs. No fallback needed.
- The first real component, `BoundedQueue`, replaces a hello-world placeholder. It's what the agent's threads share, so TSan has a real target from day one.
**Verified:**
- In the arm64 container: native, asan, tsan and x86_64 cross presets all pass (7 tests each).
- Seeded bugs: a data race reported by TSan, a heap overflow reported by ASan, so passing sanitizer runs mean something here.
- The CI job's exact commands, replayed in an emulated linux/amd64 container: native passes; the aarch64 cross build produces an `ARM aarch64` ELF; its tests pass under `qemu-aarch64`.
**Alternatives considered:**
- Building on macOS directly with Homebrew: no gdb, no valgrind, no Linux APIs, and a different toolchain from CI.
- A full-system QEMU ARM64 VM for development: slower and heavier, with nothing user-mode emulation doesn't already cover for unit tests. It stays an optional Phase 4 item, for systemd deployment.
- Wokwi: its CLI needs a cloud token; Renode is local and open source.
**Tradeoffs:**
- `renode`'s console exits at the end of its input, so scripts hold stdin open (`sleep infinity | renode …`).
- The smoke test downloads Antmicro's prebuilt demo ELF, so it's not in CI (an external dependency). Phase 3 replaces it with our own firmware and a CI job.
- qemu-user results say nothing about performance on real ARM hardware.
**Date:** 2026-11-07

## 032 — C++ edge agent: three owned threads around a drop-oldest queue, the collector's uplink contract, libcurl only at the edge
**Status:** Accepted (2026-11-07). Phase 1 of [EMBEDDED_ROADMAP](EMBEDDED_ROADMAP.md).
**Decision:**
- `openfleet-edge-agent` (`edge/`) runs on a Linux robot computer as its own robot (e.g. `edge-01`), and reports:
  - CPU (`/proc/stat` deltas), memory (`MemAvailable`, falling back to `MemFree + Buffers + Cached` on old kernels).
  - Link latency: the full time of the last successful telemetry request, as `net_latency_ms`.
  - Watched processes as nodes: RUNNING; DEGRADED if stopped by a signal (`T`); ERROR if only zombies are left; STOPPED if none is found. Matching is on `comm`, which the kernel cuts to 15 characters.
  - Its own health as node `edge_agent`: DEGRADED when the sampler missed its period or batches were lost since the last batch.
  - Host uptime goes into a startup log line, and an outage summary into a log line when the API comes back. The backend is unchanged.
- **Threads:** sampler → `BoundedQueue<Sample>` (64, drop-oldest, #031) → uplink, plus a signal thread. SIGTERM and SIGINT are blocked in every thread and taken with `sigtimedwait`. The uplink thread's backoff sleep is the queue's timed pop, so it keeps taking samples while it waits.
- **Uplink:** `Uplink` follows `collector/openfleet_collector/uplink.py` exactly:
  - An ordered outbox (600 batches), and a retry re-sends the same seq.
  - Network errors and 5xx: retry. 401/403: keep the batch buffered.
  - 404/409: drop the dead run's batches and open a fresh run (404 re-registers). Other 4xx: drop the batch.
  - Backoff doubles from 0.5 s to 10 s with equal jitter (each delay is in [base/2, base]).
  - SIGTERM: flush for at most 5 s, then end the run **ABORTED**. `--duration S` ends it SUCCESS.
- **Layering:** `edge_core` (parsers, sampler, batch JSON, uplink, loops) has no network code and takes an `HttpClient` interface. libcurl lives in `curl_client.cpp` and the binary only.
  - nlohmann/json 3.11.3 is fetched and pinned by SHA-256, like GoogleTest.
  - The API key comes only from `OPENFLEET_API_KEY`, never a flag: argv is readable by every user through `/proc`.
- Compose profile `edge`: the agent container (`init: true`, unprivileged user) plus `edge-worker`, a stand-in robot process in the agent's PID namespace. Also `edge/e2e/e2e.py`, CI job `edge-agent-e2e`, and a hardened systemd unit (`edge/packaging/`).
**Why:**
- Same contract as the collector: the server's dedup and run semantics are already proven against that behaviour (#013, #016), and one rule for every uplink is easier to reason about than two.
- ABORTED on SIGTERM, unlike the collector's SUCCESS: the roadmap asked for it, and an agent stopped from outside means the run was interrupted, not completed. A deliberate end uses `--duration`.
- Keeping the network at the edge means every line of logic is built and tested under every preset, including the aarch64 cross build under qemu, which has no aarch64 libcurl. Only the ~80-line transport is not cross-tested.
- Owned threads plus one queue are few enough to verify with TSan. That is easier to show correct than an event loop for this little concurrency.
**Verified:**
- **Tests:** 61 GoogleTest tests (parsers against `/proc` files saved from a real container, sampler, batch contract, backoff bounds, every uplink case, the loops with a racing producer through an outage, config).
  - In the arm64 dev container they pass on native, ASan+UBSan, TSan, clang 18, and the x86_64 cross build under `qemu-x86_64`.
  - Valgrind on the real binary against the live backend: no errors, no leaks.
- **Live on the compose stack, `edge/e2e/e2e.py`, all passing:**
  - `edge-01` registered with CPU, memory and latency.
  - `stress-ng` on every CPU opened `high_cpu`, which resolved afterwards.
  - Stopping `edge-worker` opened `node_health:demo-worker`, which resolved after restart.
  - A 30 s backend outage: the agent logged 35 s unreachable and buffered 35 batches. The run ended with 104 batches, seq 0–103 contiguous, 0 dropped, one run.
  - SIGTERM ended the run ABORTED.
- `systemd-analyze verify` passes on the unit. It has not been run under a real systemd boot.
**Alternatives considered:**
- Reusing `BoundedQueue` as the outbox: the outbox needs peek-retry-pop and "drop one run's batches", so it's a plain deque owned by one thread.
- A hand-written HTTP/1.1 client: the roadmap rules it out; libcurl gives TLS, proxies and timeouts.
- A YAML config file: flags plus one environment variable are enough here, and work as they are in systemd and compose.
- Heartbeats: telemetry already counts as contact, as for the collector.
**Tradeoffs:**
- **No batch is lost only while the robot's silence stays under the server's `RUN_ABANDON_AFTER_S` (60 s).** Past that, the sweeper aborts the run and the agent opens a fresh one, dropping the old run's buffered batches. That's the collector's contract too. So the roadmap's "60 s outage" criterion is met at 30 s in the e2e, with the longer path covered by unit tests. Keeping a dead run's data would need a server change (re-opening or appending to an ABORTED run).
- In a container, CPU and memory are the Docker VM's, not a cgroup's; the process list is the container's PID namespace. On a real robot computer it is the whole machine.
- Samples taken before the first run opens are counted, not sent.
- Under ASan in the arm64 Docker VM, each allocation costs about 11 µs, so the loop test paces its producer on queue depth. None of these timings are benchmarks (Phase 4).
**Date:** 2026-11-07

## 033 — Device protocol: COBS + CRC-16 frames from one C11 library, a gateway that maps devices onto topics/nodes/logs, and a seeded fault-injecting simulator
**Status:** Accepted (2026-11-07). Phase 2 of [EMBEDDED_ROADMAP](EMBEDDED_ROADMAP.md). Specification: [PROTOCOL.md](PROTOCOL.md).
**Decision:**
- **Protocol (`protocol/`, C11, no allocation, no I/O):**
  - Frames are `COBS(header | payload | CRC-16/CCITT-FALSE) 0x00`. The 14-byte header holds version, type, device id, seq, boot id, uptime ms and length; payloads are at most 240 bytes.
  - Messages: HELLO (reset reason, firmware), SENSOR, HEALTH (task states, stack high-water marks), LOG and DESCRIBE (names and expected rates, repeated every 5 s).
  - A byte-at-a-time decoder with a fixed 258-byte buffer judges each frame at its delimiter. Payload parsers validate every field.
- **Gateway (`edge/src/gateway.cpp`):** maps devices onto the existing model:
  - sensor → topic `/mcu<id>/<name>`, with the rate measured per window;
  - task → node `mcu<id>/<task>`;
  - device → node `mcu<id>` (STOPPED when silent for 3 s, DEGRADED when ≥ 25 % of frames are bad);
  - resets, device logs and link-error summaries → log events.
  - It detects resets by HELLO, a new boot id, or uptime going backwards.
  - It drops duplicates, counts gaps and drops stale frames with a 64-frame window per boot.
- **Transport (`device_link.cpp`):** `--device tcp://HOST:PORT | serial:/dev/PATH[@BAUD]`. Everything is an fd read with `poll`; a dropped link is reopened with backoff. One device thread and one `Gateway` per link; the sampler thread takes each gateway's window into its sample (the agent's threads, #032).
- **Simulator (`openfleet-device-sim`, `DeviceSim`):** encodes with the same C library. Faults (corrupt, truncate, drop, duplicate, fragment, garbage, oversize, silence, resets with or without HELLO, task states, a stopped sensor) are driven by commands, and the fault RNG is seeded.
- Compose `edge` profile: a `device-sim` service on TCP :4000; the agent has `--device=tcp://device-sim:4000`. E2E: `edge/e2e/device_e2e.py`. CI: a libFuzzer step in `edge`, and the device e2e in `edge-agent-e2e`.
**Why:**
- **COBS + CRC-16:**
  - Bounded overhead and trivial on a Cortex-M.
  - Resynchronises at the next delimiter after any fault, so one bad frame never costs the next.
  - The CRC catches every single- and double-bit error (tested exhaustively for single bits) at 2 bytes per frame.
  - A JSON or line protocol would cost more on the MCU and still need framing and checksums.
- **One C library for both ends** means the encoder the firmware ships is the one the tests and fuzzers exercise. C11 with no allocation is what the firmware can use.
- **No new backend concepts.** The backend already judges topic rates, node states and logs, so a stopped sensor, a hung task and a silent MCU become incidents by existing, tested rules. The robot aggregates; the backend judges.
- **DESCRIBE is periodic, not only at boot**, because the gateway may start after the device. Rates are measured gateway-side, so a sensor's frames need no timestamps of their own.
- **Three reset signals**, because HELLO can be lost on a noisy line and not every MCU keeps a boot counter.
- **Fuzzing:** the decoder fuzzer sees raw bytes. A second, structured fuzzer builds valid frames from the fuzz input, because random bytes almost never pass a CRC and would leave the gateway's logic unexplored.
**Verified:**
- **Tests:** 96 C/C++ tests (35 new for the protocol, the gateway and the transports) pass in the arm64 dev container on native, ASan+UBSan, TSan, clang 18, and the x86_64 cross build under qemu. Valgrind on the agent with a live faulty device link: no errors, no leaks.
- **Fuzzing (60 s each, locally):** `fuzz_decoder` 0.73 M runs, `fuzz_gateway` 0.26 M runs, no findings. The first run showed that coverage was not reaching the libraries; `-fsanitize=fuzzer-no-link` on our targets fixed that (decoder coverage went from 16 to 99 edges).
- **Live, `edge/e2e/device_e2e.py` PASS (about 2 min):**
  - `/mcu1/imu` ~50 Hz and `/mcu1/range` ~10 Hz HEALTHY, with expected rates from DESCRIBE; four nodes RUNNING.
  - Stopping sensor 2 opened CRITICAL `topic_rate:/mcu1/range`, which resolved after restart.
  - The watchdog reset and the HELLO-less reset were both logged as events.
  - Under corrupt 0.1, truncate 0.05, duplicate 0.2, fragmentation, 3000 garbage bytes and an oversized frame, the agent kept running, imu stayed delivered, and the errors were reported. Over the run: 3347 good frames, 81 bad (36 CRC, 9 short, 3 overflow), 68 duplicates, 52 lost.
  - 10 s of silence opened and resolved `node_health:mcu1`; a task error opened and resolved `node_health:mcu1/comms`.
  - A device-sim restart was logged, reconnected, and stayed on one run.
  - The Phase 1 e2e still passes.
**Alternatives considered:**
- SLIP: unbounded worst-case doubling, and escape handling spread through the code.
- Length-prefixed frames: a corrupted length desynchronises the stream until a heuristic recovers.
- CRC-32: twice the overhead, without a meaningful gain for ≤ 256-byte frames.
- Protobuf/nanopb: a schema compiler and more code on the MCU, for five small messages.
- Retransmission or ACKs: telemetry is periodic, so the next reading replaces a lost one. Loss is counted instead.
- Python for the simulator: it would not exercise the C encoder the firmware uses.
**Tradeoffs:**
- **Not exercised yet:** the device link has no authentication (it's a wire inside the robot), and `serial:` has been tested on ptys only, not a real UART adapter.
- Link errors can't be attributed to a device id (the frame is unreadable), so they degrade every device on that link.
- With 1 s windows, a sensor's rate is measured over what arrived in the window. Short bursts of loss show as rate dips, which is the intended signal.
- A bug in my simulator, found by the e2e: a restarted simulator replayed old control commands (the file survives a container restart), producing a spurious reset. The gateway reported it correctly. The simulator now only runs lines appended after it starts.
**Date:** 2026-11-07

## 034 — FreeRTOS firmware on Renode's STM32F4: a supervisor-gated watchdog, a boot record for the reset cause, and the gateway holding a reset device DEGRADED
**Status:** Accepted (2026-11-07). Phase 3 of [EMBEDDED_ROADMAP](EMBEDDED_ROADMAP.md). Details: [FIRMWARE.md](FIRMWARE.md).
**Decision:**
- **The firmware** (`firmware/`): FreeRTOS V11.1.0 (pinned by hash) on an STM32F407 at its 16 MHz reset clock, with board support written against registers.
  - Four static tasks: sensor (50 Hz imu, 10 Hz range), comms (sole UART owner, control queue first), health (task states and stack high-water marks), watchdog.
  - No heap. It speaks the Phase 2 protocol as device 2 through the same `protocol/` C library.
- **Watchdog:** the IWDG is kicked only while `sup_may_kick` says every supervised task checked in on time. The first late task latches the supervisor; the watchdog task notes "watchdog, task N" in the boot record, logs the stall, and stops kicking, and the IWDG resets the MCU.
- **Boot record:** no-init RAM at `0x2001FF00`, with a magic and a seal. It gives a boot id (random after power-on, then +1) and the reset reason: the supervisor's note first, `RCC_CSR` flags as the fallback.
- **Gateway** (#033) extended:
  - After an unexpected reset (watchdog, brownout, unknown, or one detected without HELLO), node `mcu<id>` is DEGRADED for 20 s (`reset_hold`).
  - Sensors and tasks seen before their DESCRIBE wait 6 s (`describe_grace`) before a placeholder name is reported.
- **Emulation:**
  - `firmware/renode/openfleet.resc`: a SysTick overlay at 16 MHz, a `reset` macro that reloads the image (SRAM kept), USART2 on TCP 3456, and a 1 ms time quantum.
  - Faults are injected by writing `fault_ctl` (`0x2001FE00`) through Renode's monitor: stall a task, slow the link, stop a sensor.
- **Tests and CI:**
  - The portable logic (`firmware/app`) is host-tested in every edge preset.
  - `firmware/tests/renode_test.py` uses an independent Python protocol reader.
  - `firmware/renode/gdb_check.sh` checks GDB.
  - Compose profile `firmware` (a Renode container) with `firmware/tests/firmware_e2e.py`.
  - CI job `firmware`, plus the e2e in `edge-agent-e2e`.
**Why:**
- **The supervisor gates the watchdog** instead of each task kicking it, so a single stalled task resets the MCU even while others run. That's the failure the roadmap asked for. Latching makes the reset, not a lucky late check-in, the recovery.
- **Why the boot record:** Renode doesn't model the reset flags (`RCC_CSR` reads `0x0E000000` regardless; RMVF is ignored), found with a bare-metal probe. A note written by the supervisor works on the emulator and on real silicon, and names the task, which no hardware flag can.
- **Why the reset hold:** a watchdog reset recovers in about 1 s. As a bare event, it would be a log line nobody sees. Holding the device DEGRADED briefly turns it into a `node_health` incident through the existing rule, with no backend change. It's a measurement of the device's recent stability, like `edge_agent` going DEGRADED after lost batches; the backend still decides.
- **Two queues:** with one, a slow link filled the queue with readings and the HEALTH and "readings dropped" reports timed out. The Renode test caught this. Readings are the droppable traffic; reports about them are not.
- **16 MHz without PLL setup:** the honest default for a register-level demo; the emulator is configured to match, not the other way round.
**Verified:**
- **ARM build:** about 12 KB flash, 21 KB RAM, warning-free with `-Werror` on our sources.
- **Host tests:** 107 C/C++ tests (9 new for the supervisor and boot record, 2 for the reset hold and DESCRIBE grace) pass on native, ASan+UBSan, TSan, clang, and x86_64 under qemu. The gateway fuzzer was rerun on the changed code.
- **`renode_test.py` (in the dev container, arm64 Renode) PASS:**
  - HELLO power-on; DESCRIBE; imu 50.00 Hz and range 10.00 Hz in device time; every task RUNNING (lowest free stack 219 words); contiguous seq; 0 bad frames.
  - A stalled sensor task gave the stall log, then HELLO watchdog with boot id +1, then "previous boot ended by the watchdog: task sensor stopped checking in after 7000 ms up", and sampling resumed.
  - A 60 ms/frame link dropped readings (reported), the sensor task went DEGRADED, and nothing reset.
- **`gdb_check.sh`:** gdb-multiarch attached through Renode's GDB server and read the live boot record, the supervisor and the tick count.
- **Live, `firmware_e2e.py` PASS:**
  - `/mcu2/imu` and `/mcu2/range` HEALTHY; four nodes RUNNING.
  - The stall produced, in order: the firmware's stall log, `mcu2 reset (watchdog)`, the next boot's cause, `node_health:mcu2` opened, and resolved after the hold.
  - The slow link gave `mcu2/sensor` DEGRADED, `/mcu2/imu` DEGRADED, the drop logs, no reset, and recovery.
  - The Phase 1 and Phase 2 e2e still pass.
**Alternatives considered:**
- The FreeRTOS POSIX port for the task logic: the decision logic is pure C and host-tested directly, and Renode runs the real binary, so a third execution environment would add little.
- Vendor HAL/CubeMX: a lot of generated code for four peripherals, and it hides what the firmware does to the hardware.
- Each task kicking the watchdog: a stalled task goes unnoticed while another kicks.
- A backend rule for reset log events: it would be a new backend concept for something the existing node rule already expresses.
- Wokwi: its CLI needs a cloud token (#031).
**Tradeoffs:**
- **Emulation only.** Peripheral behaviour is Renode's model. Polled UART TX is fine there; real hardware at higher rates would want DMA. The board support follows the reference manual, but nothing has run on silicon.
- **Host cost:** Renode needs about 1 host core for this firmware on the dev Mac, measured with `docker stats` after the WFI idle hook and the 1 ms quantum; it was about 2.6 cores before. Because the agent measures the Docker VM, emulator load can itself raise edge-01's CPU. That happened once (a `high_cpu` incident) before these fixes.
- Emulation ran at about 1.01–1.06× real time here. Rates are asserted in device time; wall-clock rates depend on the host.
- No device-side receive path. Faults go in through the emulator's monitor, not the protocol.
- **Bugs found while building this:**
  - A NOLOAD linker section for the boot record sat in an ELF LOAD segment, and Renode's loader zero-filled it on every reset. The record is now addressed directly and only reserved in the linker script.
  - `nc -q 1` never exited on Renode's monitor socket and hung the e2e; it now uses `timeout` + `nc -N`, and every compose call has a timeout.
**Date:** 2026-11-07

## 035 — Regression tier and benchmarks: YAML scenarios that measure, Robot Framework on Renode in emulated time, a nightly report that labels every figure
**Status:** Accepted (2026-11-07). Phase 4 of [EMBEDDED_ROADMAP](EMBEDDED_ROADMAP.md). Details: [BENCHMARKS.md](BENCHMARKS.md).
**Decision:**
- **Scenario runner** (`edge/e2e/run_scenarios.py`, `edge/e2e/scenarios/*.yaml`): each scenario starts compose services, injects faults (device-sim commands, Renode `fault_ctl` writes, stopping containers, `stress-ng`), and waits for what OpenFleet shows.
  - A `mark` records the server clock; a `measure` is the server-side timestamp of the effect (incident `started_at`/`resolved_at`, event `received_at`) minus the mark, with a budget (`max_s`).
  - Each scenario ends its run (SIGTERM → ABORTED) and gates on `redetect` over it (caught/missed/new).
  - Five scenarios: `device_sensor_stops`, `device_link_reconnect`, `backend_outage`, `agent_footprint`, `firmware_watchdog`.
- **Robot Framework on Renode** (`firmware/renode/tests/firmware.robot`, run with Renode's `renode-test`): 8 tests that advance the emulation with `emulation RunFor` and read USART2 from a file backend, decoded by `ofp.py` (`OfpKeywords.py`). Includes a same-seed-same-bytes determinism test and Renode-labelled metrics (instructions per second and per frame, watchdog recovery time, stack headroom). `openfleet.resc` was split so the tests reuse the machine (`openfleet-machine.resc`) without the TCP UART.
- **Benchmarks** (`edge/bench/bench.cpp`, target `edge_bench`): CRC, encode, streaming decode, the gateway's per-frame receive path, and the per-batch JSON. Median of 5 samples of ≥ 300 ms. Every report carries the caller's `--env` label plus arch, compiler and build type. ctest runs it once with `--quick` on every preset so it can't rot.
- **Nightly workflow** (`.github/workflows/nightly.yml`, 03:17 UTC and on demand): benchmarks (native x86_64; aarch64 under qemu-user), the Robot suite, all scenarios, 10 min of fuzzing per target on a corpus cached across nights, then `scripts/nightly_report.py` writes one Markdown report (job summary + artifact).
- The Robot suite also runs in the regular CI `firmware` job (~70 s).
**Why:**
- **Measure in the existing tests' terms.** The scenarios assert the same things as the e2e scripts, but also record how long each took on the server's clock, so a regression in latency shows up as a number, not as a flaky timeout.
- **Emulated time for the Robot tier.** The Phase 3 test reads a free-running emulator against the wall clock, so a slow host changes what it sees. Driving Renode with `RunFor` makes counts exact (499–501 imu readings in 10 s), makes the byte stream reproducible (tested), and lets a slow CI runner take longer without failing.
- **Labels over comparisons.** A qemu-user figure says how fast the emulator is (the gateway is ~5× slower there, JSON ~14×), not the CPU. Figures are compared only with the same environment and architecture, and only against the previous nightly on the same kind of runner, because a committed baseline from the dev Mac (arm64) would be meaningless on an x86_64 runner.
- **Gate on correctness, report performance.** Shared runners vary by tens of percent; a benchmark gate would be flaky. Slowdowns ≥ 1.5× are flagged in the report. Latency budgets in scenarios (`max_s`) are generous bounds on behaviour (e.g. detection < 10 s), not performance targets.
- **Redetect on every scenario run** turns each recorded edge run into a regression check on the detection rules. It found BUG-007 on its first run.
**Verified (dev Mac, arm64 Docker VM):**
- `edge_bench` native (RelWithDebInfo): gateway 287 ns/frame, decode 229 ns per SENSOR frame, batch JSON 8.2 µs; x86_64 under qemu-user: 1.43 µs and 112 µs.
- Robot suite 8/8 PASS in 70 s: 322,599 instructions per emulated second (about 2 % of 16 MHz at 1 IPC), 5,186 per frame, watchdog recovery 1,250 ms of emulated time, lowest free stack 217 words.
- Scenarios 5/5 PASS: sensor stop → incident 0.7 s; device gone → `node_health:mcu1` 3.8 s; gateway reconnect 1.3 s, data HEALTHY again 2.3 s; backend restart → first batch 3.2 s, no batch lost, no `heartbeat_missing`; agent 0.65 % of a core and 11.8 MiB RSS with a 60 frame/s device; watchdog stall → reset seen 1.8 s, incident resolved 30.8 s. Redetect: missed 0 everywhere.
- `nightly_report.py` on those results, with the baseline path exercised.
**Alternatives considered:**
- Renode's line-oriented UART keywords: the link is binary COBS frames, so a small keyword library over the existing independent decoder fits better.
- Google Benchmark: one more fetched dependency for six cases; a short harness gives exactly the JSON the report needs.
- A committed benchmark baseline: wrong for any runner other than the machine that made it.
- Folding the e2e scripts into scenarios: the e2e scripts stay as the per-push CI checks; the scenarios are the measured, slower tier.
- Full-system QEMU (Debian arm64 with the agent as a systemd service): optional in the roadmap and not done here. On an 8 GB Mac it competes with Renode and compose, and the systemd unit is already verified with `systemd-analyze`. (Done later: #036.)
**Tradeoffs:**
- The nightly workflow has not run on GitHub yet (nor have Phases 1–3's CI jobs). (Since run, 2026-11-08: green after fixing GCC 15 false positives.)
- Scenario latencies include the agent's 1 s sampling period and the backend's 2 s sweeper; they are end-to-end figures for this stack, not component costs.
- Agent CPU is read from its own `/proc` (utime + stime over wall time); RSS is VmRSS. Under `stress-ng` the agent's share fell (0.26 %), because it competes for the same CPUs; that is a figure for this VM, not a property of the agent.
- Renode instruction counts are not cycles; the "duty share" assumes 1 instruction per cycle.
**Date:** 2026-11-07

## 036 — The edge agent under a real systemd: Debian 13 arm64 in qemu-system-aarch64, and a hardened unit
**Status:** Accepted (2026-11-08). The optional item of [EMBEDDED_ROADMAP](EMBEDDED_ROADMAP.md) Phase 4.
**Decision:**
- **Full-system e2e** (`edge/qemu/qemu_e2e.py`, nightly job `qemu-system`): boots a pinned Debian 13 genericcloud arm64 image (by date and SHA-512) in `qemu-system-aarch64`. HVF or KVM on an arm64 host, TCG otherwise (CI's x86_64 runners; `--accel tcg` forces it).
  - cloud-init's NoCloud seed comes over HTTP from the script (SMBIOS `ds=nocloud;s=http://10.0.2.2:PORT/`), so no ISO tooling is needed. It installs the agent, the unit **byte for byte as shipped** (checked by hash), and a drop-in that only overrides `ExecStart`, as an operator would.
  - Checks, through SSH and the API: start at boot as a DynamicUser; memory matches the guest's `/proc/meminfo`; a root-owned watched process RUNNING → STOPPED → RUNNING; no capabilities, `NoNewPrivs`, `MemoryMax`; `systemd-analyze security` ≤ 1.5; SIGKILL → `Restart=on-failure` → a new run; `systemctl stop` → ABORTED; reboot → ABORTED during shutdown and a new run after boot; poweroff → ABORTED; no EPERM in the journal.
  - Writes `qemu-system.json`; the nightly report shows its timings with the environment label.
- **Guest build** (`edge/qemu/Dockerfile.build`): Debian trixie, so the binary matches the guest's glibc and libcurl. Native on arm64; on x86_64 it cross-compiles with the repo's aarch64 toolchain against multiarch `libcurl4-openssl-dev:arm64` (explicit `CURL_INCLUDE_DIR`/`CURL_LIBRARY`: the toolchain's find root doesn't hold multiarch packages).
- **Unit hardening** (`edge/packaging/openfleet-edge-agent.service`), found by the first run (exposure 3.4): `PrivateUsers`, `ProtectClock`, `ProtectHostname`, `ProtectKernelLogs`, `RestrictRealtime`, `RestrictSUIDSGID`, `UMask=0077`, `SystemCallFilter=@system-service ~@privileged @resources` with `EPERM`. Exposure 1.2.
  - Not set, on purpose: `ProtectProc=invisible` (would hide watched processes owned by others), `ProcSubset=pid` (would hide `/proc/stat` and `/proc/meminfo`), `PrivateNetwork`/`IPAddressDeny` (the uplink; addresses are per site).
  - The unit now documents the drop-in a serial device needs (`PrivateDevices=no`, `DeviceAllow=`, `dialout`): `PrivateDevices=yes` hides `/dev/tty*` from the gateway.
**Why:**
- `systemd-analyze verify` only checks syntax. Ordering at shutdown (does the agent flush before the network goes?), restart policy, DynamicUser with `/proc` access, and the syscall filter only show under a booted systemd.
- A cloud image + cloud-init is how a fleet computer is actually provisioned, and needs no image building (Yocto/Buildroot stay out, per the roadmap).
- Serving the seed over HTTP keeps the host side to Python + QEMU on both macOS and Linux.
**Verified (dev Mac, arm64):**
- HVF: 7/7 steps PASS. Boot to first run 25.1 s, SIGKILL to new run 6.2 s (`RestartSec=5`), `systemctl stop` 0.1 s, reboot to new run 23.7 s, 5.1 MiB in the service's cgroup.
- TCG (forced, still on the arm64 host): 7/7 PASS in 3 min. Boot to first run 67.7 s, reboot to new run 52.7 s. x86_64 runners emulate a foreign ISA and will be slower; timeouts scale ×4 under TCG.
- The cross build's exact commands in an emulated amd64 trixie container; the report section and its failure path with `nightly_report.py`.
**Alternatives considered:**
- An ISO seed (`cloud-localds`/`genisoimage`, `hdiutil` on macOS): two code paths and a tool to install, for what an HTTP seed does with the standard library.
- Building the agent inside the guest: needs a compiler in the image and minutes of TCG compile time.
- Raising the exposure threshold to 3.4: the score pointed at real, cheap hardening.
**Tradeoffs:**
- Not yet run on GitHub's x86_64 runners; the first nightly is the real TCG timing.
- The EPERM check reads the agent's journal; a syscall that failed silently without logging wouldn't show. The full lifecycle passing is the stronger evidence.
- The guest is a QEMU `virt` machine, not robot hardware; timings describe that machine.
**Date:** 2026-11-08
