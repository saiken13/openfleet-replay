# Execution Flows

> **Status: planned.** The file and function names below are the *intended* locations.
> When each flow is implemented, replace "(planned)" with the real `file:function` references and keep this document accurate.

## 0. Health check (implemented)

```
GET /api/v1/health
→ backend/app/api/v1/health.py :: health()
→ backend/app/db/engine.py :: ping(engine, DB_CONNECT_TIMEOUT_S)   SELECT 1, bounded by asyncio.timeout
    ├─ ok      → 200 {"status": "ok", "database": "ok"}
    └─ failure → 503 {"status": "unavailable", "database": "unavailable"}  (cause logged as WARNING)
```

The engine is created in the app lifespan (`backend/app/main.py :: create_app()`) and disposed on shutdown. Tests build a fresh app per test with `create_app(settings)`.

## 0b. Robot registration (implemented)

```
Robot / collector (on boot, safe to retry)
→ PUT /api/v1/robots/{robot_id}                    body: RobotRegister {name, model?}; robot_id ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$
→ backend/app/api/v1/robots.py :: register_robot()  Pydantic validation (extra fields such as status → 422)
→ backend/app/services/robot_service.py :: RobotService.register()
    ├─ RobotRepository.insert_identity_if_absent()   INSERT … ON CONFLICT (id) DO NOTHING RETURNING
    ├─ if it conflicted → RobotRepository.update_identity()   name, model, updated_at only; state untouched
    └─ COMMIT
→ 201 (created) | 200 (updated), body: RobotRead

GET /api/v1/robots/{robot_id} → RobotService.get() → 200 | 404
```

A racing first registration blocks on the other's uncommitted insert, then falls through to the update (`tests/test_robot_registration_concurrency.py`).

## 0c. Run lifecycle (implemented)

```
Start (client-generated UUID, retry-safe)
→ PUT /api/v1/runs/{run_id}                        body: RunStart {robot_id, software_version?, started_at (tz-aware)}
→ backend/app/api/v1/runs.py :: start_run()
→ backend/app/services/run_service.py :: RunService.start()
    ├─ RobotRepository.get(for_update)               lock robot row → serializes run starts per robot; 404 if unknown
    ├─ RunRepository.insert_if_absent()              INSERT … ON CONFLICT (id) DO NOTHING
    │    └─ conflict → compare robot_id / version / started_at → 200 (same) | 409 (different)
    ├─ _reconcile_current_run()                      DECISIONS #016
    │    ├─ RunRepository.latest_for_robot()         ORDER BY started_at DESC, id DESC
    │    ├─ RunRepository.abort_running_except()     older RUNNING runs → ABORTED, ended_at = latest.started_at
    │    └─ RobotRepository.set_current_run()        current_run_id (+ software_version) or NULL
    └─ COMMIT → 201 (run may already be ABORTED if it was a late-synced older run)

End
→ POST /api/v1/runs/{run_id}/end                   body: RunEnd {status: SUCCESS|FAILED|ABORTED, ended_at}
→ RunService.end()
    ├─ lock robot row, then run row                  same order as start() → no deadlock (tests/test_concurrency.py)
    ├─ already terminal: same status → 200 (no change) | different → 409
    ├─ ended_at < started_at → 422
    └─ set status/ended_at; clear robots.current_run_id if it pointed here → COMMIT → 200

Read
→ GET /api/v1/runs/{run_id}                         200 | 404
→ GET /api/v1/robots/{robot_id}/runs?limit=&cursor= newest first; keyset on (started_at, id); opaque cursor
```

Errors: services raise `app/domain/errors.py` exceptions. `app/api/errors.py` maps them in one place (NotFound → 404, Conflict → 409, InvalidRequest → 422) to `{"detail": "..."}`.

## 1. Telemetry ingest (planned — Phase 6–9)

```
Simulator / collector (1 batch per robot per ~1s)
→ POST /api/v1/runs/{run_id}/telemetry            body: TelemetryBatch {seq, ts, metrics, pose, topics[], nodes[], logs[]}
→ app/api/v1/telemetry.py :: ingest_batch()        Pydantic validation → 422 on malformed input
→ app/services/telemetry_service.py :: ingest()
    ├─ load run; 404 if unknown, 409 if run is not RUNNING
    ├─ BEGIN
    │   ├─ TelemetryRepository.insert_sample()      ON CONFLICT (run_id, seq) DO NOTHING
    │   │     └─ if duplicate → return early (idempotent retry, 200)
    │   ├─ TelemetryRepository.insert_topic_samples()
    │   ├─ StateRepository.upsert_topic_states()   classify() each topic → detect status changes
    │   ├─ StateRepository.upsert_node_states()    detect state changes
    │   ├─ EventRepository.insert_many()           change-events + forwarded logs
    │   └─ RobotRepository.touch()                 last_seen_at = server now, latest metrics
    ├─ COMMIT
    ├─ DetectionService.evaluate_robot(robot_id)    → see flow 3
    └─ RealtimeHub.publish(...)                     after commit only
→ 200 {accepted: true, duplicate: false}
```

## 2. Heartbeat and offline detection (planned — Phase 3–4)

```
Robot
→ POST /api/v1/robots/{robot_id}/heartbeat        {ts, software_version, state}
→ app/api/v1/robots.py :: heartbeat()
→ app/services/robot_service.py :: record_heartbeat()
    ├─ last_seen_at = server now   (never robot ts — clocks drift)
    ├─ if version changed → software_version_changed event
    └─ if status was OFFLINE → re-derive status, emit robot_status_changed
→ 204

Sweeper (every SWEEP_INTERVAL_S)
→ app/workers/sweeper.py :: tick()
→ RobotService.mark_stale_offline(now)  last_seen_at < now - HEARTBEAT_TIMEOUT_S
→ DetectionService.evaluate_robot() for each affected robot
→ RunService.abort_abandoned(now)       RUNNING runs on robots offline > RUN_ABANDON_AFTER_S
```

## 3. Detection → incident (planned — Phase 9)

```
DetectionService.evaluate_robot(robot_id, now)
→ SnapshotBuilder.build(robot_id)          reads robots + topic_states + node_states (no history scans)
→ for rule in RULES: rule.evaluate(snapshot, now) → findings   (pure, app/detection/rules/*.py)
→ IncidentService.reconcile(robot_id, findings, now)
    ├─ new dedup_key        → INSERT … ON CONFLICT DO NOTHING (partial unique index) + incident_opened event
    ├─ higher severity      → UPDATE severity + incident_escalated event
    └─ key absent ≥ RESOLVE_AFTER_S → status=RESOLVED, resolved_at + incident_resolved event
→ RobotService.derive_status()             ERROR / DEGRADED / ONLINE
→ RealtimeHub.publish(incident + status changes)
```

## 4. Incident timeline (planned — Phase 10)

```
GET /api/v1/incidents/{id}/timeline
→ IncidentService.timeline(id)
→ EventRepository.window(robot_id, started_at - TIMELINE_LOOKBACK_S, resolved_at or now)
  + TelemetryRepository.downsampled(...)  (context points)
→ ordered by ts → TimelineEntry[]
```

## 5. Replay (planned — Phase 13)

```
Frontend ReplayPage
→ GET /api/v1/runs/{id}/stream?cursor=…   (paged)
→ app/replay/stream.py :: merged_stream()  samples ∪ topic_samples ∪ events ORDER BY ts, kind, id
→ frontend/src/replay/ReplayClock.ts        play/pause/seek/speed; emits frames at (ts - t0) / speed
→ dispatch(frame) → fleetReducer            same reducer the live WebSocket feeds
```
