# Bugs

Record *meaningful* bugs here: ones where the root cause taught us something about the system. Typos don't belong here.

## Template

```
### BUG-NNN — <short title>
Bug:
Symptoms:
Root cause:
How we diagnosed it:
Fix:
Files changed:
Tests added:
How regression is prevented:
Date:
```

---

### BUG-001 — Run start and run end could deadlock
Bug: `RunService.end()` locked the run row, then the robot row; `start()` locked robot, then runs.
Symptoms: none observed in production; found in review before merge.
Root cause: inconsistent lock order across two code paths that touch the same rows.
How we diagnosed it: reasoning about lock order, then a mutation test — reversing the order (with a 50 ms widening sleep) made `test_run_start_racing_run_end_does_not_deadlock` fail with `DeadlockDetectedError`.
Fix: every path locks robot → runs → incidents (DECISIONS #016).
Files changed: backend/app/services/run_service.py
Tests added: backend/tests/test_concurrency.py
How regression is prevented: the race test; lock order is written in FLOW.md and HANDOFF gotchas.
Date: 2026-11-05

### BUG-002 — A finished run's last values kept incidents open forever
Bug: `/scan degraded` on `ci-01` stayed OPEN long after its run ended normally.
Symptoms: seen on the dashboard during the first end-to-end demo.
Root cause: topic/node state rows outlive the run; detection read them regardless of age or run. The robot then went offline, and incidents are (deliberately) never auto-resolved while offline — so nothing could close it.
How we diagnosed it: the incident's `last_detected_at` matched the final batch of a run that had ended; the robot had no current run.
Fix: detection only uses run data while in a run and refreshed within the heartbeat timeout; a robot-reported run end resolves that run's data incidents (DECISIONS #017).
Files changed: backend/app/services/detection_service.py, backend/app/services/run_service.py
Tests added: `test_ending_a_run_resolves_its_data_incidents_and_stale_state_does_not_refire`, `test_stale_topic_state_is_ignored_during_a_run`
How regression is prevented: those tests.
Date: 2026-11-05

### BUG-003 — Simulator robot clock ran faster than real time with --speed
Bug: incident timelines showed the cause (topic change) *after* the incident it caused.
Root cause: in real-time mode with `--speed 2`, batches were stamped with simulated time (t0 + scenario seconds), so the robot clock ran 2× faster than the server clock that stamps OpenFleet's own events.
How we diagnosed it: incident page screenshot — `incident_opened` (server time) preceded `topic_status_changed` (robot time) by 15 s.
Fix: real-time mode stamps batches with wall-clock time like a real robot; only `--fast` (CI) uses synthetic time.
Files changed: simulator/openfleet_sim/main.py, simulator/openfleet_sim/robot.py
Tests added: simulator tests now pass timestamps explicitly.
How regression is prevented: `SimRobot.step(t, ts)` makes the clock an explicit input.
Date: 2026-11-05


### BUG-004 — Stopping the simulator left runs dangling (false "robot stopped reporting" incidents)
Bug: every run started by the simulator container ended ABORTED/"abandoned", with a CRITICAL `heartbeat_missing` incident on every robot.
Symptoms: found by a live-data diagnostic — 8 runs aborted by the sweeper; simulator container `Exited (137)`.
Root cause: the simulator runs as PID 1 in its container without a SIGTERM handler, so it ignored `docker compose stop`; Docker SIGKILLed it and its runs were never ended. The backend behaved correctly: robots went silent mid-run, so it raised `heartbeat_missing` and the sweeper aborted the runs after `RUN_ABANDON_AFTER_S`.
How we diagnosed it: integrity checks all passed (backend consistent) → runs list showed only container-started runs were abandoned → container exit code 137 (not OOM) → reproduced with `docker compose stop simulator` (exit 137, 4 runs left RUNNING).
Fix: SIGTERM/SIGINT cancel the main task; the scenario ends its open runs as ABORTED (reported by the robot) before exiting 0.
Files changed: simulator/openfleet_sim/main.py
Tests added: `test_cancellation_ends_open_runs_as_aborted`; verified in Docker (exit 0 in ~1s, runs ABORTED "reported by robot", no incidents).
How regression is prevented: the test; `scripts/integrity_checks.sql` for live diagnostics.
Date: 2026-11-05

### BUG-005 — Collector raised a false CRITICAL incident on every start
Bug: on every collector start, each monitored topic was reported at 0 Hz for the first batch, so OpenFleet opened a CRITICAL "stopped" incident. Because incidents only escalate (never de-escalate), a real degradation seconds later was then hidden inside that CRITICAL incident.
Symptoms: the ROS 2 end-to-end test expected a WARNING incident when /scan dropped to 4 Hz and found only an (already open) CRITICAL one.
Root cause: the first window after subscribing was partial (and before discovery there was no subscription at all), so the measured rate was 0. The same race applies to nodes not yet visible in the ROS graph.
How we diagnosed it: the event trail showed `incident_opened: /scan stopped` at the run's first second, followed one second later by `/scan FAILED → HEALTHY`.
Fix: a topic is reported only after one full window since it was subscribed; undiscovered topics and nodes are reported missing only after `startup_grace_s` (default 5 s).
Files changed: collector/openfleet_collector/rates.py, batch.py, node.py, config.py
Tests added: `test_startup_grace_no_false_failures`, `test_node_states_hides_missing_nodes_during_grace`; the ROS 2 e2e test asserts the WARNING → CRITICAL sequence.
How regression is prevented: those tests (unit + ros2-collector-e2e in CI).
Date: 2026-11-06
### BUG-006 — Backend replicas starting together on a fresh database: one crashed in migrations
Bug: every replica runs `alembic upgrade head` at startup (backend Dockerfile CMD). On an empty database, two replicas both tried to create `alembic_version` at the same moment.
Symptoms: CI job `e2e-multi-replica` failed on commit d71efc4: `backend-1 exited (1)` with `UniqueViolationError ... pg_type_typname_nsp_index` on `CREATE TABLE alembic_version`. Earlier runs passed by timing luck.
Root cause: nothing serialized concurrent migrators; transactional DDL doesn't help, since both transactions start before either commits.
How we diagnosed it: the CI log for backend-1, then a test starting 6 real `alembic upgrade head` processes on a scratch database, which reproduced the same error.
Fix: `alembic/env.py` takes `pg_advisory_xact_lock(MIGRATION_LOCK_KEY)` inside alembic's transaction, before it reads the current version. Later migrators wait, see the committed head, and do nothing. The lock is released at commit or rollback.
Files changed: backend/alembic/env.py
Tests added: backend/tests/test_migrations.py::test_concurrent_upgrades_on_a_fresh_database_all_succeed (fails without the lock, passes 3/3 with it)
How regression is prevented: that test. Also verified live: three backend containers started together on an empty database; one applied 0001→0008, the other two waited, and all three came up.
Date: 2026-11-07

### BUG-007 — A restarted backend blamed robots for its own outage
Bug: after the backend was down for 15 s, edge-01 got a CRITICAL `heartbeat_missing` incident ("edge-01 stopped reporting during a run"), although the robot had been buffering the whole time and lost nothing.
Symptoms: found by the Phase 4 scenario `backend_outage`. Re-detection over the same run reported the incident as `missed`: in robot time the batches were contiguous, so there was no silence to detect.
Root cause: the sweeper's first tick after startup compared every robot's `last_seen_at` with `now - HEARTBEAT_TIMEOUT_S`. The silence it saw was the backend's own downtime, during which no robot could have reached it. The agent retries with a 0.5–10 s backoff, so the sweeper usually won the race. A long enough outage (> `RUN_ABANDON_AFTER_S`) would also have aborted every run on restart; the Phase 1 e2e worked around that by keeping its outage at 30 s.
How we diagnosed it: the scenario's redetect gate failed (caught 0, missed 1); the incident's `started_at` was 1 s after the backend came up, and its evidence showed `last_seen_at` just before the outage.
Fix: the sweeper only judges silence it was there to hear. With `listening_since` (the process start), it marks robots OFFLINE only once it has listened for `HEARTBEAT_TIMEOUT_S`, and abandons runs only after `RUN_ABANDON_AFTER_S`. A robot that really died during the outage is still caught, at most one timeout later.
Files changed: backend/app/workers/sweeper.py, backend/app/main.py
Tests added: `test_restarted_backend_does_not_blame_robots_for_its_own_outage`, `test_robot_that_died_during_the_outage_is_caught_after_the_grace` (both fail with the guard removed)
How regression is prevented: those tests, and the `backend_outage` scenario in the nightly tier (`no_incident: heartbeat_missing`, redetect `missed_max: 0`).
Date: 2026-11-07
