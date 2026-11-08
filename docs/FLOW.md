# Execution Flows

> **Status: implemented.** File and function references below are real. Keep them accurate when code moves.
> Lock order everywhere: **robot row → run rows → incident rows** (DECISIONS #016).
> Realtime: services queue messages and `Service.commit()` (`backend/app/services/base.py`) publishes them only after COMMIT.

## 0. Health check

```
GET /api/v1/health
→ backend/app/api/v1/health.py :: health()
→ backend/app/db/engine.py :: ping(engine, DB_CONNECT_TIMEOUT_S)   SELECT 1, bounded by asyncio.timeout
    ├─ ok      → 200 {"status": "ok", "database": "ok"}
    └─ failure → 503 {"status": "unavailable", "database": "unavailable"}  (cause logged as WARNING)
```

The engine is created in the app lifespan (`backend/app/main.py :: create_app()`) and disposed on shutdown. Tests build a fresh app per test with `create_app(settings)`.

## 0a. Robot authentication (every robot write: 0b, 0c, 1, 2)

```
Authorization: Bearer <key>
→ backend/app/api/auth.py :: authenticated_robot()      FastAPI dependency, runs before body validation
    ├─ no/other-scheme header → 401 (WWW-Authenticate: Bearer)
    ├─ ApiKeyService.authenticate()  backend/app/services/api_key_service.py
    │     ApiKeyRepository.active_by_hash(sha256(key))   unique index; revoked_at IS NULL; read-only
    └─ unknown/revoked → 401, else → caller robot_id
→ ownership
    ├─ register / heartbeat: require_robot(caller, path robot_id)          → 403
    ├─ run start:            require_robot(caller, body.robot_id)          → 403
    └─ run end / telemetry:  RunService.end / TelemetryService.ingest(caller_robot_id=…)
                             after the run lookup: 404 if missing, ForbiddenError → 403 if not owned

Keys: openfleet keys create|list|revoke → backend/openfleet_cli/keys.py → ApiKeyService.issue/list/revoke (direct DB)
Seed: app/main.py :: seed_robot_keys() at startup if OPENFLEET_ROBOT_KEYS_FILE → ApiKeyService.seed()
```

## 0a′. User authentication (every read, triage, WebSockets)

```
Dashboard: POST /api/v1/auth/login {username, password} + X-OpenFleet-Client header
→ backend/app/api/v1/auth.py :: login()
→ LoginThrottle.attempt(username, client IP)  backend/app/services/login_throttle.py (#027)
    ├─ INCR openfleet:login:user:{window}:{name} and …:ip:{window}:{ip}  (Redis; memory fallback)
    └─ either over its limit → 429 + Retry-After; the password is not checked
→ UserService.login()  backend/app/services/user_service.py
    ├─ UserRepository.by_username(lowercase)        unknown → verify against DUMMY_HASH (same timing)
    ├─ passwords.verify_password() in a thread      scrypt, constant-time compare
    └─ insert SESSION token (sha256), expires now + SESSION_TTL_S, absolute_expires_at now + SESSION_MAX_S; COMMIT
→ wrong credentials → 401 (the reserved slots stay used)
→ LoginThrottle.succeeded(): clear the username counter, give back the IP slot
→ 200 UserRead + Set-Cookie openfleet_session (HttpOnly, SameSite=Strict, Path=/api)

Any read: app/api/auth.py :: current_user()   (via require_viewer / require_operator)
    ├─ Bearer header (API token) or else the session cookie   → none: 401
    ├─ UserRepository.active_token(sha256, now)    not revoked, not expired, user not disabled → else 401
    ├─ session? slide (#029): expires_at = min(now + SESSION_TTL_S, absolute_expires_at)
    │     only if that moves it ≥ SESSION_REFRESH_S; conditional UPDATE + COMMIT
    ├─ cookie + unsafe method without X-OpenFleet-Client → 403 (CSRF)
    └─ require_operator: role < OPERATOR → 403      (PATCH /incidents/{id}; actor recorded in triage_actions and on the event)

WebSocket: app/api/v1/ws.py :: _pump()
    ├─ Bearer or cookie; cookie needs an allowed Origin (CORS_ORIGINS or same host); valid token
    │     else close 1008 before accept (HTTP 403)
    ├─ reauth task: every WS_REAUTH_INTERVAL_S re-check the token → close 1008 when revoked/expired
    └─ sender: hub RESYNC (bus lost messages, #025) → close 1012; the client reconnects + reloads REST

Cross-replica (if OPENFLEET_REDIS_URL): app/realtime/bus.py :: RedisBus
    Hub.publish → local subscribers → hub.forward → outbound queue → PUBLISH openfleet:realtime {o, s, c, m}
    listener: other origin → seq check (gap → Hub.resync) → Hub.deliver (local only, never re-forwarded)

POST /auth/logout → UserService.logout(): revoke if SESSION (API tokens untouched); clear cookie
Users/tokens: openfleet users … → backend/openfleet_cli/users.py → UserService (direct DB)
Seed: app/main.py :: seed_users() if OPENFLEET_USERS_FILE
```

## 0b. Robot registration

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

## 0c. Run lifecycle

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
    ├─ run_started event (+ run_ended for superseded runs, software_version_changed)
    ├─ record_contact() + Detector.evaluate()      a run start is contact from the robot
    └─ commit() → 201 (run may already be ABORTED if it was a late-synced older run)

End
→ POST /api/v1/runs/{run_id}/end                   body: RunEnd {status: SUCCESS|FAILED|ABORTED, ended_at}
→ RunService.end()
    ├─ lock robot row, then run row                  same order as start() → no deadlock (tests/test_concurrency.py)
    ├─ already terminal: same status → 200 (no change) | different → 409
    ├─ ended_at < started_at → 422
    ├─ finish_run(): status/ended_at, clear robots.current_run_id, run_ended event
    ├─ Detector.resolve_run_incidents()            that run's data incidents → RESOLVED (#017)
    └─ record_contact() + Detector.evaluate() → commit() → 200

Read
→ GET /api/v1/runs/{run_id}                         200 | 404
→ GET /api/v1/robots/{robot_id}/runs?limit=&cursor= newest first; keyset on (started_at, id); opaque cursor
```

Errors: services raise `app/domain/errors.py` exceptions. `app/api/errors.py` maps them in one place (NotFound → 404, Conflict → 409, InvalidRequest → 422) to `{"detail": "..."}`.

## 1. Telemetry ingest

```
Simulator / collector (1 batch per robot per ~1s)
→ POST /api/v1/runs/{run_id}/telemetry           body: TelemetryBatch {seq, ts, metrics, pose, topics[], nodes[], logs[]}
→ backend/app/api/v1/runs.py :: ingest_telemetry()   Pydantic validation → 422 (tz-aware ts, 0–100 %, ≤200 topics…)
→ backend/app/services/telemetry_service.py :: TelemetryService.ingest()      ONE transaction (DECISIONS #018)
    ├─ run unknown → 404; lock robot row; run not RUNNING → 409
    ├─ TelemetryRepository.insert_sample()      ON CONFLICT (run_id, seq) DO NOTHING
    │     └─ duplicate → rollback, 200 {duplicate: true}   (idempotent retry)
    ├─ _ingest_topics(): classify_topic() each → topic_samples + topic_states; topic_status_changed on change
    ├─ _ingest_nodes(): node_states; node_state_changed on change
    ├─ _ingest_logs(): each log → events(type=log)
    ├─ robot metrics + record_contact()         last_seen_at = server now
    ├─ Detector.evaluate(robot, now)            → flow 3
    └─ commit() → publish frames (sample/topic/event), robot, incidents
→ 200 {accepted: true, duplicate: false}
```

## 1a. C++ edge agent → telemetry ingest (DECISIONS #032)
Robot side, in `edge/` (a separate process; it calls the same endpoints as the collector):
1. `main` (`src/main.cpp`) → `parse_args` (`src/agent.cpp`; key only from `OPENFLEET_API_KEY`) → `run_agent`. It blocks SIGTERM/SIGINT in every thread, then starts three threads.
2. **Sampler thread:** `sampler_loop` → `Sampler::sample` (`src/sampler.cpp`), which reads `/proc/stat`, `/proc/meminfo` and `/proc/<pid>/stat` through the pure parsers in `src/proc.cpp` (`parse_cpu_times`/`cpu_pct`, `mem_pct`, `parse_pid_stat`/`process_state`). It then calls `BoundedQueue<Sample>::push` (drop-oldest, never blocks).
3. **Uplink thread:** `uplink_loop`. The queue's `pop_for(backoff)` is also its sleep.
   - `Uplink::start` (`src/uplink.cpp`): `PUT /api/v1/robots/{id}`, then `PUT /api/v1/runs/{uuid}` (§0b, §0c).
   - `Uplink::enqueue`: adds node `edge_agent`, then `batch_json` (`src/telemetry.cpp`) into the outbox with the next seq.
   - `Uplink::pump`: `POST /api/v1/runs/{id}/telemetry` (§1) through `CurlClient::send` (`src/curl_client.cpp`).
     - 2xx: pop the batch and record the latency.
     - Transport error, 5xx, 401/403: keep it and `Backoff::next`.
     - 404/409: `drop_run`, then `start` again.
     - Other 4xx: drop the batch.
4. **Signal thread:** `sigtimedwait` → `StopSignal::request`. Then the sampler is joined and the queue closed. The uplink drains it, flushes for ≤ 5 s, and calls `Uplink::end(ABORTED)` (`POST /runs/{id}/end`, §0c).

Server side, nothing is specific to the agent: processes are nodes (`NodeHealthRule`), CPU and memory are metrics (`HighCpuRule`, `HighMemoryRule`), and silence is liveness (§2).

## 1b. Device → gateway → telemetry (DECISIONS #033, [PROTOCOL.md](PROTOCOL.md))
1. **Device:** `DeviceSim::tick` (`edge/src/device_sim.cpp`; Phase 3: the firmware) → `ofp_build_*` + `ofp_encode` (`protocol/src/protocol.c`) → bytes on TCP or a UART.
2. **Device thread:** `device_loop` (`edge/src/device_link.cpp`): `open_device`, then `pump_fd` (`poll` + `read`), then `Gateway::on_bytes` (`edge/src/gateway.cpp`).
   - `ofp_decoder_push` judges each frame at its `0x00`: errors go to `count_error`, frames to `handle`.
   - `handle` detects resets (HELLO, `boot_id`, uptime), runs `check_seq` (duplicate, stale or lost), then parses: SENSOR counts per sensor; DESCRIBE sets names and expected rates; HEALTH sets task states (low stack → log); LOG → log.
   - A dropped link goes to `Gateway::on_link`, and the link is reopened with backoff.
3. **Sampler thread:** each period `sampler_loop` calls `enrich`, which calls `Gateway::contribute`. That adds topics `/mcu<id>/<sensor>` (rate over the window), nodes `mcu<id>` (STOPPED / DEGRADED / RUNNING) and `mcu<id>/<task>`, and the window's logs (plus a rate-limited link-error summary) to the `Sample`.
4. From there it's §1a: `Uplink::enqueue` → `batch_json` (topics included) → `POST /runs/{id}/telemetry`. The backend's `TopicRateRule` and `NodeHealthRule` judge it like any robot's data.

## 1c. Firmware → watchdog reset → incident (DECISIONS #034, [FIRMWARE.md](FIRMWARE.md))
1. **Boot:** `Reset_Handler` (`firmware/board/stm32f4/startup.c`) → `main` (`firmware/src/main.c`) → `board_init` → `boot_record_on_boot` (`firmware/app/boot_record.c`), which gives the reason and boot id → four static tasks → `vTaskStartScheduler`.
2. **Normal running:**
   - `sensor_task`: `sup_checkin` → `post` (data queue; dropped if full).
   - `comms_task`: control queue, then data queue → `send_frame` → `ofp_encode` → `board_uart_write` (USART2 → Renode → TCP 3456).
   - `health_task`: HEALTH (with `uxTaskGetStackHighWaterMark`) and DESCRIBE.
   - `watchdog_task`: `sup_may_kick` (`firmware/app/supervisor.c`) → `board_watchdog_kick`.
3. **A task stalls:** `sup_may_kick` latches false → `boot_record_note_watchdog` + `post_log` ("stopped checking in") → no more kicks → the IWDG resets the MCU → Renode's `reset` macro reloads the image, keeping SRAM → step 1, where `boot_record_on_boot` reports a watchdog reset and the stalled task.
4. **Gateway** (§1b), `Gateway::handle`: HELLO from a known device → `reset()`, which logs `mcu2 reset (watchdog)` and sets `degraded_until = now + reset_hold`. Then `contribute` reports node `mcu2` DEGRADED until then, `NodeHealthRule` opens `node_health:mcu2`, and the reconciler resolves it after the hold.

## 2. Heartbeat and liveness

```
Robot
→ POST /api/v1/robots/{robot_id}/heartbeat      {ts?, software_version?}
→ backend/app/api/v1/robots.py :: heartbeat()
→ backend/app/services/liveness_service.py :: LivenessService.heartbeat()
    ├─ lock robot; last_seen_at = server now   (never robot ts — DECISIONS #010)
    ├─ in a run? HeartbeatRepository.record()    extend the latest heartbeat_spans row if ts (or now)
    │                                            is within HEARTBEAT_SPAN_GAP_S, else insert (#028)
    ├─ record_software_version()                software_version_changed event if it changed
    ├─ Detector.evaluate()                       e.g. OFFLINE → ONLINE (robot_status_changed)
    └─ commit()
→ 204

Sweeper (every SWEEP_INTERVAL_S; started in app lifespan, backend/app/main.py)
→ backend/app/workers/sweeper.py :: Sweeper.tick(now)      (now injectable for tests)
→ backend/app/workers/lock.py :: AdvisoryLock.try_hold()   pg_try_advisory_lock on its own connection;
                                                          not acquired → TickResult(skipped=True) (#024)
   Sweeper._heard_for(): skipped until this process has listened HEARTBEAT_TIMEOUT_S
                         (RUN_ABANDON_AFTER_S for aborts) since it started (BUG-007)
→ LivenessService.mark_stale_offline(now)      robots not OFFLINE with last_seen_at < now - HEARTBEAT_TIMEOUT_S
     └─ per robot: lock, Detector.evaluate()  → OFFLINE; heartbeat_missing incident if in a run (#019)
→ LivenessService.abort_abandoned_runs(now)    OFFLINE robots with a current run, silent > RUN_ABANDON_AFTER_S
     └─ per robot: RunService.finish_run(ABORTED, reason="abandoned"); run_ended event
→ Sweeper._retention(now)   RETENTION_DAYS set and RETENTION_INTERVAL_S since the last pass (#030)
     └─ RetentionService.run(): due() finished runs, ended < now - days, oldest first, ≤ RETENTION_BATCH_RUNS
        per run: lock row → freeze metrics/topics/event counts into runs.summary
                 → DELETE samples, topic samples, heartbeat spans, log events → compacted_at; COMMIT
   (also: openfleet retention --days N [--dry-run]   backend/openfleet_cli/retention.py)
```

## 3. Detection → incident

```
backend/app/services/detection_service.py :: Detector.evaluate(robot, now)     (caller holds robot lock)
→ snapshot(robot, now)          robots + topic_states + node_states; run data only if in a run & fresh (#017)
→ backend/app/detection/rules.py :: evaluate_all(snapshot, now, cfg)   pure; one finding per dedup_key
→ reconcile(robot, findings, now, live)
    ├─ new dedup_key      → IncidentRepository.open_if_absent()  INSERT … ON CONFLICT DO NOTHING (partial unique index)
    │                        + incident_opened event
    ├─ same key           → last_detected_at = now; higher severity → escalate + incident_escalated
    └─ key absent ≥ INCIDENT_RESOLVE_AFTER_S and robot live → RESOLVED + incident_resolved   (#019)
→ _apply_status()               derive_robot_status() (backend/app/domain/status.py); robot_status_changed on change

Run ended by the robot → Detector.resolve_run_incidents()  (reason "run ended", #017)
Manual triage → PATCH /api/v1/incidents/{id} → IncidentService.update_status()
    → TriageRepository.record() (triage_actions row: actor, from → to; #026) → re-evaluate robot status
Audit log → GET /api/v1/triage (backend/app/api/v1/triage.py) → frontend/src/pages/TriagePage.tsx
```

## 4. Incident timeline

```
GET /api/v1/incidents/{id}/timeline
→ backend/app/services/incident_service.py :: IncidentService.timeline()
→ window = [started_at - TIMELINE_LOOKBACK_S, resolved_at or now]   (server receive time)
→ EventRepository.window_for_robot()                   ordered by robot ts
  + TelemetryRepository.downsampled_window()            ≤300 samples (every n-th row)
  + TelemetryRepository.downsampled_topic_window()      ≤150 points per topic
→ IncidentTimeline {incident, events, samples, topic_samples}
```

## 5. Replay

```
frontend/src/pages/RunPage.tsx
→ fetchWholeStream(): GET /api/v1/runs/{id}/stream?cursor=…   (paged)
   → backend/app/services/analysis_service.py :: AnalysisService.stream()
     samples ∪ topic_samples ∪ events, keyset (ts, kind_rank, id) per source, merged, opaque cursor
→ frontend/src/replay/ReplayClock.ts      play/pause/seek/speed; emits frames by robot-time offset
→ dispatch({type: "frames"}) → frontend/src/state/robotReducer.ts     the same reducer live WS frames feed
→ frontend/src/components/RobotView.tsx   the same component the live robot page renders
```

## 5a. Re-detection replay (DECISIONS #023)

```
POST /api/v1/runs/{id}/redetect  {optional threshold overrides}      VIEWER, read-only
→ backend/app/api/v1/runs.py :: redetect_run()
→ backend/app/services/redetect_service.py :: RedetectService.redetect()
   size check (OPENFLEET_REDETECT_MAX_SAMPLES) → effective DetectionConfig (settings + overrides)
   analysis_repository.py: samples(), topic_samples() (grouped by seq), node_events()
   heartbeat_repository.py: for_run() → RecordedContact spans (#028)
→ backend/app/detection/replay.py :: replay_run()                    pure
   between batches (and after the last, to run end): split the gap by heartbeat spans
      uncovered > timeout → silent snapshot → heartbeat_missing
      covered → live snapshot, data going stale; evaluated at its change points only
   per batch (ts order):
                         apply node changes ≤ ts, carry metrics, classify_topic() again
                         → RobotSnapshot → rules.evaluate_all() → _Reconciler.step()
   run end → _Reconciler.end_run()
→ _pair() / _keys(): per dedup_key vs recorded incidents → both | recorded_only | replay_only

frontend/src/components/RedetectPanel.tsx (run page)   openfleet redetect RUN [--set k=v] [--fail-on-missed]
```

## 6. Compare and regression check

```
GET /api/v1/runs/{id}/summary  → AnalysisService.summary()   SQL aggregates (analysis_repository.py):
                                 avg/min/p05 Hz, % time per topic status, cpu/mem/battery, incidents by severity
GET /api/v1/runs/compare?base&candidate → AnalysisService.compare()   per-topic Δ%, metric Δ, incident Δ

openfleet check RUN -c regression.yaml [--baseline RUN]
→ backend/openfleet_cli/main.py   GET summary (and baseline summary)
→ backend/openfleet_cli/checks.py :: evaluate()    pure assertions on the server's numbers (no re-detection)
→ exit 0 pass | 1 fail | 2 error
```
