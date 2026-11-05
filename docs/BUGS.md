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