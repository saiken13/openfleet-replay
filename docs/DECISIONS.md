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
- Session expiry is absolute, not sliding. A user is signed out after 12 h even while active.
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
