# Handoff

## Context
- **AI assistant/model:** Claude Code, Opus 5.5 (`claude-opus-5-5`)
- **Date:** 2026-11-08
- **Branch:** `main` (remote: https://github.com/saiken13/openfleet-replay)
- **Latest commit:** see `git log -1`

## Current state
**Owner's decisions (2026-11-08): Google sign-in; multi-tenancy as low-scale "sites" (shared DB); scale infrastructure skipped (documented evolution path only). Goal: finish quickly for the owner's resume.**

**Sign in with Google done (2026-11-08, DECISIONS #038).** OIDC code flow + PKCE run by the backend (`app/core/oidc.py`), `GET /auth/oidc/login|callback`, migration 0009 (`users.oidc_subject`). Access: linked identity → invite (`openfleet users create EMAIL --sso`) → `OIDC_ALLOWED_DOMAINS` (viewer) → refused. Login page shows "Sign in with Google" when configured and the refusal message. Compose reads `OPENFLEET_OIDC_*` from `.env` (`.env.example`). Tests: `tests/test_oidc.py` (16, fake provider with real RS256); backend 269 pass. Live: redirect to accounts.google.com verified with a placeholder client; **a real Google sign-in needs the owner's OAuth client id/secret.**
- **QEMU on GitHub fixed:** `-cpu neoverse-n1` (Ubuntu's edk2 hangs under `-cpu max`); `qemu-system` passes on the x86 runner (boot → first run 74 s).

**Sites (multi-tenancy) done (2026-11-08, DECISIONS #039).** Migration 0010: `sites`, `robots.site_id` (default `default`), `users.site_id` (NULL = all). `app/api/scope.py` scope dependencies (404 across sites) on every `{robot_id}`/`{run_id}`/`{incident_id}` route + compare; list routes and triage filtered; fleet WS filtered, robot WS refused. CLI `openfleet sites create|list|assign`, `users create --site`, `users set-site`. `OIDC_DEFAULT_SITE`. UI: user's site in the header, site filter + badges on Fleet when >1 site. Tests: `tests/test_sites.py` (10, incl. a route guard verified to catch a missing check); backend 279. Live via nginx with a scoped viewer. The local dev DB now has site `warehouse-a` (amr-01, amr-02) and user `wa-viewer` from that check.
- **Next:** final README/resume polish (owner wants to finish quickly). Scale infra intentionally skipped (ARCHITECTURE §10 is the evolution path).

**Earlier: ROS 2 integration done (2026-11-08, DECISIONS #037): `openfleet-ros-bridge` republishes microcontrollers into ROS 2; the collector reads `/diagnostics`.** The item the embedded roadmap had postponed.
- **Bridge** (`edge/ros/`, ament_cmake, image `edge/ros/Dockerfile` on Jazzy; `-p devices:=[URI,...]`): the edge gateway per link. Readings → `std_msgs/Int32` on `/mcu<id>/<sensor>`; device/task health → `/diagnostics`; resets and device logs → `/rosout`. It never talks to OpenFleet (the collector is the robot's one sender).
  - Gateway additions: `GatewayConfig::on_reading` (each new reading, once its name is settled) and `self_source` (who the link events are logged as). 1 new test; C/C++ 109.
- **Collector:** optional `diagnostics_topic` (`openfleet_collector/diagnostics.py`): OK/WARN/ERROR/STALE → RUNNING/DEGRADED/ERROR/STOPPED, silent 5 s → STOPPED, ≤ 256 names, graph nodes win clashes. 4 new tests (28).
- **E2E:** `python edge/ros/e2e.py` (compose profile `ros`: `ros-device-sim`, `ros-bridge`, `ros-mcu-collector`, ROS domain 43, robot `ros-mcu-01` + demo key). 6 steps PASS locally (stopped sensor, watchdog reset, failed task, silent device, bridge down/up; one run). The existing `collector/ros_test/e2e.py` still passes. CI: `ros2-collector-e2e` is now a 2-script matrix. **Not yet run on GitHub.**
- **QEMU follow-up:** the first GitHub `qemu-system` run failed (QEMU had no `efi-virtio.rom`; fixed in 41f7f6e with `romfile=` + fail-fast). The rerun (Nightly 42514767161) was still in progress at handoff, past boot. Check it.
- **Next:** nothing planned; the remaining options (OIDC, multi-tenancy, scale infrastructure) need the owner's decisions.

**Earlier: full-system QEMU (2026-11-08, DECISIONS #036).** The edge agent as a systemd service on Debian 13 arm64 in `qemu-system-aarch64`. The last open item of the embedded roadmap.
- **E2E:** `python edge/qemu/qemu_e2e.py` (`--accel tcg` emulates as CI does; `--report FILE`). A pinned cloud image (SHA-512), cloud-init seeded over HTTP from the script (no ISO tools), the shipped unit installed unchanged plus an `ExecStart` drop-in.
  - 7 steps: boot as a DynamicUser with the guest's /proc; a root-owned watched process (`demo-worker`) RUNNING → STOPPED → RUNNING; hardening (no caps, NoNewPrivs, MemoryMax, exposure ≤ 1.5); SIGKILL → restart → new run; stop → ABORTED; reboot → ABORTED during shutdown + new run at boot; poweroff → ABORTED; no EPERM.
  - Guest binary: `edge/qemu/Dockerfile.build` (trixie; native on arm64, cross with multiarch libcurl on x86_64).
- **Unit hardened** (found by the first run, exposure 3.4 → 1.2): PrivateUsers, ProtectClock/Hostname/KernelLogs, RestrictRealtime/SUIDSGID, UMask, a `@system-service` syscall filter. The unit's header documents the drop-in a serial device needs (`PrivateDevices=yes` hides `/dev/tty*`).
- **Nightly:** new job `qemu-system` (TCG on the x86_64 runner, image cached); the report has a section for it and fails if it fails. New demo key `edge-vm-01`.
- **Verified locally (M2):** HVF 7/7 (boot → first run 25 s, reboot → new run 24 s); TCG 7/7 in 3 min; the cross build's commands in an emulated amd64 container; the report and its failure path. **Not yet run on GitHub**: trigger a manual Nightly and watch `qemu-system` (TCG on x86 is the slow path).
- ROS 2 integration followed (above).

**Earlier: pushed and verified on GitHub (2026-11-08).** Commits f9881da (BUG-006/007), fc3dfd5 (embedded Phases 0–4), 63062cb (CI fixes), 432131c (parallel edge e2e).
- **CI: all green on GitHub** (63062cb and 432131c), every embedded job included.
- **Nightly (manual runs):** on fc3dfd5, firmware, scenarios and fuzz passed; bench failed (below), so `report` failed by design. On 63062cb, every job passed, report included (run 42510262373).
- **Runner differences fixed:**
  - `ubuntu-latest` ships **GCC 15** (dev container: GCC 13). At `-O2 -Werror` it false-positives `-Wuninitialized` in `BoundedQueue::pop_for` (moving `optional<Sample>`) and `-Warray-bounds` on `push_back` after a sized vector in `DeviceSim` (`oversize`). Code restructured, not silenced. Reproduce locally with the `gcc:15` Docker image.
  - `scripts/multi_replica_e2e.py` assumed amr-02..04 existed; it now registers them (idempotent PUT), so it works on CI's fresh DB.
- **CI time 13 → 5.5 min:** `edge-agent-e2e` is a 3-script matrix needing only `backend` (it waited for `edge` + `firmware`, then ran ~7.3 min serially). The longest job is now `edge` (~5.5 min).
- Full-system QEMU followed (above).

**Earlier: Embedded Phase 4 done (2026-11-07, DECISIONS #035; [docs/BENCHMARKS.md](docs/BENCHMARKS.md)): regression tier + benchmarks. Every embedded phase is complete.**
- **Scenario runner:** `edge/e2e/run_scenarios.py` runs the YAML files in `edge/e2e/scenarios/`: `device_sensor_stops`, `device_link_reconnect`, `backend_outage`, `agent_footprint` and `firmware_watchdog`.
  - `mark` takes the server clock; `measure` is the effect's server-side timestamp minus the mark, with a `max_s` budget.
  - Each scenario ends its run and gates on `redetect`.
  - Reports go to `edge/build/scenarios/` (JSON + `scenarios.md`).
- **Robot Framework:** `firmware/renode/tests/firmware.robot` + `OfpKeywords.py`, run with `firmware/renode/tests/run.sh` (Renode's `renode-test`).
  - It drives the emulation with `emulation RunFor` and reads USART2 from a file, so results are deterministic: the metrics were byte-identical across runs, even with the host under load.
  - 8 tests. Also in the CI `firmware` job.
  - `openfleet.resc` was split: `openfleet-machine.resc` is the machine only, and callers pass `$machine_resc`.
- **Benchmarks:** `edge/bench/bench.cpp` → `edge_bench --env LABEL [--json]`. ctest runs `edge_bench_smoke` (`--quick`) on every preset (now 108 C/C++ tests).
- **Nightly:** `.github/workflows/nightly.yml` (03:17 UTC + manual). It runs the benchmarks (native x86_64, aarch64 under qemu-user), the Robot suite, every scenario, and 10 min of fuzzing per target on a cached corpus. Then `scripts/nightly_report.py` writes one report: the job summary plus the `nightly-report` artifact.
  - Benchmarks are compared with the previous nightly (Actions cache) and only flagged, never gated.
- **BUG-007 fixed (found by `backend_outage`):** a restarted backend opened `heartbeat_missing` on robots that had been buffering through its outage. It would also have aborted their runs after an outage over 60 s. The sweeper now waits until it has listened one `HEARTBEAT_TIMEOUT_S` before marking robots OFFLINE, and one `RUN_ABANDON_AFTER_S` before aborting runs (`Sweeper(listening_since=...)`, set in `main.py`). 2 tests; backend 253 pass.
- **Verified locally:** all of the above; the C/C++ presets native, asan, tsan and x86_64; `renode_test.py`, `gdb_check.sh`, and the three e2e scripts. Since run on GitHub (2026-11-08, above).
- Not done: the optional full-system QEMU (Debian arm64 + systemd).
- **Next:** nothing planned. The options below need the owner's decision.

**Earlier (kept for context):**

**Embedded Phase 3 done (2026-11-07, DECISIONS #034; [docs/FIRMWARE.md](docs/FIRMWARE.md)): FreeRTOS firmware on Renode's emulated STM32F4.**
- **`firmware/`:** FreeRTOS V11.1.0 (pinned), register-level board support, 16 MHz, static allocation only (about 12 KB flash, 21 KB RAM).
  - Tasks: sensor (imu 50 Hz, range 10 Hz → data queue), comms (control queue first), health (HEALTH with stack high-water marks, DESCRIBE), watchdog.
  - The IWDG is kicked only while every task checks in (`app/supervisor.c`). The reset cause is in a boot record at `0x2001FF00` (`app/boot_record.c`), because Renode doesn't model `RCC_CSR`.
  - Device id 2, so OpenFleet shows `/mcu2/*` and `mcu2*`.
- **Gateway changes:**
  - Node `mcu<id>` is DEGRADED for 20 s after an unexpected reset (`reset_hold`), so a 1 s watchdog reset becomes a `node_health` incident.
  - Undescribed sensors and tasks wait 6 s for their names (`describe_grace`).
- **Renode** (`firmware/renode/openfleet.resc`): 16 MHz SysTick overlay, `reset` macro (reload the ELF; SRAM kept), USART2 → TCP 3456, 1 ms quantum.
  - Faults go through the monitor (port 1234) into `fault_ctl` at `0x2001FE00`: +0 stall task, +4 comms delay ms, +8 stop sensors.
- **Tests:**
  - 107 C/C++ tests, all presets; 9 of them are firmware logic on the host.
  - `firmware/tests/renode_test.py`: Renode, with an independent Python decoder `ofp.py`.
  - `firmware/renode/gdb_check.sh`: GDB through Renode.
  - `firmware/tests/firmware_e2e.py`: compose profile `firmware`, Renode container → agent → backend.
  - All pass, and the Phase 1 and 2 e2e still pass.
- **CI:** new job `firmware` (ARM build, Renode test, GDB check). `edge-agent-e2e` also runs the firmware e2e. **None of this has run on GitHub yet.**
- Phase 4 followed (above).

**Embedded Phase 2 done (2026-11-07, DECISIONS #033; spec [docs/PROTOCOL.md](docs/PROTOCOL.md)): the device protocol and the gateway.**
- **`protocol/`:** a C11 library with no allocation: COBS + CRC-16/CCITT-FALSE frames (14-byte header, ≤ 240-byte payload) and HELLO / SENSOR / HEALTH / LOG / DESCRIBE messages. It has a streaming decoder and validating parsers, and is built as part of `edge/`.
- **Gateway** (`edge/src/gateway.cpp`), via `--device tcp://H:P | serial:/dev/X[@BAUD]` (`device_link.cpp`, one thread per link):
  - sensors → topics `/mcu<id>/<name>`; tasks → nodes `mcu<id>/<task>`; the device → node `mcu<id>` (STOPPED when silent 3 s, DEGRADED at ≥ 25 % bad frames);
  - resets (HELLO, boot id, uptime) → log events; dedup and loss with a 64-frame window; link-error summaries every ≤ 10 s.
- **Simulator:** `openfleet-device-sim` (`DeviceSim`). Faults come from `--command` or lines appended to `--control-file`. Compose service `device-sim` (TCP :4000); the agent's command has `--device=tcp://device-sim:4000`.
- **Tests:** 96 C/C++ tests. They pass on native, ASan+UBSan, TSan, clang and x86_64 under qemu. Valgrind is clean with a faulty device link.
- **Fuzzing:** `cmake --preset fuzz` builds `fuzz_decoder`, `fuzz_gateway` and `make_seeds`. 60 s each locally found nothing. CI fuzzes 45 s each.
- **Live, `edge/e2e/device_e2e.py` PASS:** a stopped sensor opened and resolved a CRITICAL topic incident; both reset kinds became events; a corrupted link was counted while the agent kept running; silence and a failed task opened and resolved node incidents; it reconnected on one run. The Phase 1 e2e still passes.
- **CI:** the `edge` job adds clang and libclang-rt and a fuzz step; `edge-agent-e2e` also runs the device e2e. **None of this has run on GitHub yet.**
- **Next: Phase 3 (FreeRTOS firmware on Renode STM32F4). It needs the owner's go.**

**BUG-006 fixed (2026-11-07):** replicas starting together on a fresh database raced in `alembic upgrade head` (the cause of CI's red `e2e-multi-replica` on d71efc4). `alembic/env.py` now serializes migrators with an advisory lock; there's a 6-process race test in `tests/test_migrations.py`. Also fixed a flaky test, `test_retention.py::test_compacting_keeps_summary...`: it started two runs on one robot at the same `started_at`, so the #016 tie-break by random id sometimes aborted the new one (0 failures in 20 runs since).

**Embedded Phase 1 done (2026-11-07, DECISIONS #032): the C++ edge agent `openfleet-edge-agent`.**
- **What it reports, as its own robot `edge-01`:** CPU, memory, link latency, watched processes as nodes, its own health as node `edge_agent`, and startup and outage log lines.
- **Threads:** sampler → `BoundedQueue` → uplink, plus a signal thread.
- **Uplink:** the collector's contract (ordered outbox of 600, same-seq retry, jittered backoff 0.5–10 s, 404/409 → fresh run, other 4xx → drop). SIGTERM flushes ≤ 5 s and ends the run ABORTED; `--duration` ends it SUCCESS.
- **Layout:** `edge_core` has no network code (an `HttpClient` interface); libcurl is only in `src/curl_client.cpp` and the binary. nlohmann/json is pinned by hash.
- **Compose profile `edge`:**
  - `edge-agent`: tini, unprivileged user, key `ofk_demo_edge-01_public-not-secret` in `examples/demo-robot-keys.json`.
  - `edge-worker`: a `demo-worker` process in the agent's PID namespace.
  - E2E script: `edge/e2e/e2e.py`.
- **systemd unit:** `edge/packaging/` (verified with `systemd-analyze verify` only).
- **CI:**
  - The `edge` job now installs libcurl and also builds the binary.
  - The aarch64 cross job builds and tests `edge_core` and skips the binary (no aarch64 libcurl on the runner).
  - New job `edge-agent-e2e`.
- **Tests:** 61 GoogleTest tests. They pass in the arm64 dev container on native, ASan+UBSan, TSan, clang 18 and x86_64 under qemu. Valgrind on the real binary is clean.
- **Verified live (`edge/e2e/e2e.py` PASS, about 2 min):**
  - `stress-ng` opened and resolved `high_cpu`.
  - Stopping `edge-worker` opened and resolved `node_health:demo-worker`.
  - A 30 s backend outage lost nothing (104 batches, seq 0–103 contiguous, one run, and a "35 s unreachable" log).
  - SIGTERM ended the run ABORTED.
- **Adjusted criterion:** the no-loss outage is 30 s, not 60 s, because the server aborts runs after 60 s of silence (`RUN_ABANDON_AFTER_S`).
- **Next: Phase 2 (protocol + gateway). It needs the owner's go.**

**Data retention done (2026-11-07, DECISIONS #030). The 7-item batch is complete.** With `OPENFLEET_RETENTION_DAYS` set (off by default, including in compose), the sweeper compacts finished runs older than that, at most once per hour, 20 runs per pass. Compacting freezes the summary aggregates into `runs.summary`, deletes samples, topic samples, heartbeat spans and log events, and sets `runs.compacted_at` (migration 0008). Summary, compare and check read the frozen data; redetect returns 422; the run page shows a notice. CLI: `openfleet retention --days N [--dry-run] [--json]`. Code: `app/services/retention_service.py`, `Sweeper._retention`, `openfleet_cli/retention.py`. Tests: `tests/test_retention.py` (5, incl. a real-commit CLI test). Verified live: a dry run over the dev DB listed 103 runs and deleted nothing; compacting one throwaway run left its summary byte-identical, redetect refused it, and the run page showed the notice (screenshot).

**Embedded Phase 0 done (2026-11-07, DECISIONS #031); roadmap: [docs/EMBEDDED_ROADMAP.md](docs/EMBEDDED_ROADMAP.md).**
- **Dev container:** `edge/docker/Dockerfile.dev`, run with `scripts/edge-dev.sh`. It has gcc 13, clang 18, gdb, valgrind, cmake, the cross g++ for the other architecture, qemu-user and Renode 1.17.
- **`edge/`:** a CMake C++20 project with presets native/release/asan/tsan/aarch64/x86_64. The first component is `include/openfleet/edge/bounded_queue.hpp`, with 7 GoogleTest tests.
- **CI:** job `edge` runs native, ASan+UBSan and TSan, then the x86_64 → aarch64 cross build with tests under qemu-aarch64.
- **Renode spike passed:** it runs natively on arm64 and `firmware/renode/smoke.sh` boots an STM32F4 with UART4 over TCP. The IWDG is modelled.
- **Verified:** all presets locally; seeded TSan/ASan bugs are caught; the CI job's commands replayed in an emulated amd64 container.
- **Next: Phase 1, the C++ edge agent** (`/proc` sampling, uplink with the Python collector's contract, SIGTERM, e2e against compose).

**Sliding sessions done (2026-11-07, DECISIONS #029).** Sessions get `absolute_expires_at = login + SESSION_MAX_S` (7 d; migration 0007). Each request extends `expires_at` to `min(now + SESSION_TTL_S, absolute)` when that moves it ≥ `SESSION_REFRESH_S` (300 s). The extension happens in `current_user` via `UserService.authenticate(..., Sliding)` + `UserRepository.extend_token` (conditional UPDATE). WebSocket re-checks don't slide; API tokens never do. Cookie `Max-Age` = `SESSION_MAX_S`. Tests: `tests/test_sessions.py` (7); `test_session_expires` became `test_session_expires_when_idle`. Verified live: through :8080 the cookie has `Max-Age=604800`. A throwaway backend container with TTL 20 s / MAX 60 s / refresh 0 showed an active session alive at 30 s and 45 s, then 401 at 60 s (cap); an idle session was 401 at 30 s.

**Heartbeats stored per run (2026-11-07, DECISIONS #028).** A heartbeat during a run extends or starts a `heartbeat_spans` row (migration 0006; `app/repositories/heartbeat_repository.py`, hooked in `LivenessService.heartbeat`). The row is in robot time (`ts`, else server now) and merges heartbeats up to `HEARTBEAT_SPAN_GAP_S` = 5 s apart. `replay_run(contacts=...)` now treats spans as contact: uncovered gaps over the timeout are silence; covered ones are a live robot whose data goes stale, evaluated at change points (last-fresh / first-stale instants, hysteresis deadlines, contact end). Simulator: new `telemetry_stall` failure kind and scenario; CI asserts `redetect` on it gives `new == 0`. Tests: `tests/test_heartbeat_spans.py` (9: storage, pure replay, and two live-vs-replay parity runs with the sweeper ticking) + 1 simulator test. Verified live: a fast stall run made one 25-heartbeat span, and the replay caught `/scan` with no new incidents; with the spans deleted, it reported a `replay_only` heartbeat_missing. A real-time run (60 s) matched live: the robot never went OFFLINE during the stall; live and replay both opened and cleared `topic_rate:/scan` (caught 1, missed 0, new 0); one span, 25 heartbeats over 24 s.

**Login throttling done (2026-11-06, DECISIONS #027).** `app/services/login_throttle.py :: LoginThrottle` reserves a slot per username and per client IP before the password is checked. Over the limit (5 / 20 per 300 s), the response is 429 + `Retry-After`. Counters are kept in Redis (shared by replicas), falling back to memory. Success clears the username counter and gives back the IP slot. Both nginx configs now overwrite `X-Forwarded-For` with `$remote_addr`, and compose sets `FORWARDED_ALLOW_IPS: "*"` so uvicorn uses it. The login page shows the wait. Also fixed `alembic/env.py`: `fileConfig` was disabling every already-imported app logger when migrations ran in-process. Tests: `tests/test_login_throttle.py` (10, two of them against real Redis). Verified live through :8080: five 401s then a 429; a spoofed `X-Forwarded-For` was ignored (the key held the real client IP, not nginx's); both keys were in Redis; one warning was logged; the login page showed the message (headless-Chrome screenshot).

**Triage audit log done (2026-11-06, DECISIONS #026).** Every manual incident status change writes a `triage_actions` row (actor, from → to, server time) in the same transaction (`app/repositories/triage_repository.py`, hooked in `IncidentService.update_status`). `GET /api/v1/triage` (viewer; filters actor/robot_id/incident_id, id cursor) feeds the dashboard's **Audit** page (`frontend/src/pages/TriagePage.tsx`); incident pages link to their own history. Migration `0005` backfills older manual resolves from `incident_resolved` events (actor/from may be NULL). Tests: `tests/test_triage_audit.py`. Verified live: demo operator INVESTIGATING → RESOLVED on a real incident, both rows on the page (screenshot via headless Chrome).

**Realtime fan-out across replicas done (2026-11-06, DECISIONS #025).** With `OPENFLEET_REDIS_URL` set (compose sets it), `app/realtime/bus.py :: RedisBus` forwards hub messages over Redis pub/sub. Delivery is local-first, own echoes are skipped, and per-instance seq numbers plus a heartbeat detect loss. On a gap or a (re)subscribe the replica calls `Hub.resync()`, so its WebSockets close with 1012 and clients reload over REST (no frontend change).
- Tests: `tests/test_realtime_bus.py` (5 pure seq-logic tests and 4 against real Redis, skipped locally if Redis isn't on 6380, required in CI), plus hub forward/deliver/resync and WS 1012 tests in `tests/test_realtime.py`.
- Verified live with two replicas (`:8000` and a second container on `:8001`): a heartbeat through either replica updated a fleet WebSocket on the other. `CLIENT KILL TYPE pubsub` closed both sockets with 1012 and they resubscribed. With Redis stopped, the backend kept serving, and it recovered on restart.
- **Together with #024, several backend replicas can run.** `docker compose --profile replicas up` adds `backend-2` (:8001) and a round-robin nginx `lb` (:8090, `scripts/lb.nginx.conf`); `python scripts/multi_replica_e2e.py` checks cross-replica delivery both ways, delivery through the lb, and the 1012 path after `CLIENT KILL TYPE pubsub`. CI runs it as `e2e-multi-replica`.

**Sweeper is replica-safe (2026-11-06, DECISIONS #024).** Each tick takes a session-level `pg_try_advisory_lock` on its own connection (`app/workers/lock.py`); replicas that miss it skip the tick. Tests: `tests/test_sweeper_lock.py` (exclusion, release on error and on cancel, sweeper skip). Verified live with a second backend replica on the compose DB: while psql held the sweeper's key, neither replica marked a silent robot offline (the replica logged 12 skips), and the robot went OFFLINE within one interval of release.

**Re-detection replay done (2026-11-06, DECISIONS #023).** `POST /runs/{id}/redetect` runs the current rules (or overridden thresholds) over a recorded run and compares with the recorded incidents: caught / missed / new.
- Pure engine `app/detection/replay.py`, service `app/services/redetect_service.py`, run-page panel `frontend/src/components/RedetectPanel.tsx`, CLI `openfleet redetect RUN [--set k=v] [--fail-on-missed] [--json]`.
- Verified with all suites and on the live compose stack through nginx (viewer cookie, CSRF 403), on `lidar_regression` and all `demo` robots. On a `--fast` demo run it found the amr-04 offline window that live detection missed (fast mode compresses server time; replay uses robot time).
- Committed (68ca0cc / a201405). The panel was built and typechecked but not looked at in a browser.

**Auth done (2026-11-05), steps 1–4.**
- Per-robot API keys on robot writes (DECISIONS #021).
- Users with viewer/operator roles, a dashboard login (HttpOnly session cookie), API tokens for the CLI/CI, auth on every read, and authenticated WebSockets (DECISIONS #022).
- Verified with all suites, the live compose stack through nginx (cookie, CSRF header, roles, WS Origin, logout), the CI regression gate run locally with `OPENFLEET_TOKEN`, and the ROS 2 Jazzy e2e.
- Committed (68ca0cc / a201405).

**ROS 2 collector done (2026-11-06).** `collector/` runs on real ROS 2 and is verified end to end on Jazzy in CI. Earlier summary below.

**The MVP is complete.** Everything in the README's "What it does" section is implemented, tested, and checked end to end with the real stack:
- Docker Compose: Postgres + backend + nginx frontend + simulator
- Screenshots taken of the fleet, incident and replay pages

CI jobs: backend, simulator, frontend, edge (C++: sanitizers + aarch64 cross under qemu), edge-agent-e2e (3 parallel matrix jobs: agent, device gateway, firmware; needs only `backend`), collector, ros2-collector-e2e, `e2e-regression-check` (threshold check, the re-detection gate, and the telemetry-stall replay check) and `e2e-multi-replica`.

## Completed
- **Backend** (`backend/app/`):
  - Registration, runs (#013, #016), telemetry ingest, heartbeat.
  - Sweeper.
  - Pure detection rules plus the reconciler (dedup #007, escalation, hysteresis, offline semantics #019, run-data freshness #017).
  - Incidents API with triage and timeline.
  - Realtime hub and WebSockets.
  - Replay stream, run summary, and compare.
- **CLI** (`backend/openfleet_cli/`): `openfleet check RUN -c regression.yaml [--baseline RUN] [--json]`, with exit code 0, 1 or 2. `openfleet keys create|list|revoke` (direct DB).
- **Robot auth** (#021): `robot_api_keys` (sha256, migration 0003), `app/api/auth.py` (`RobotAuth`, `require_robot`), ownership via `caller_robot_id` in RunService.end / TelemetryService.ingest. 401 before validation, 403 for another robot's id/run. Startup seeding from `OPENFLEET_ROBOT_KEYS_FILE`.
- **User auth** (#022):
  - `users` and `user_tokens` (migration 0004), `app/core/passwords.py` (scrypt), `app/services/user_service.py`, `app/api/v1/auth.py` (login/logout/me/config).
  - `current_user` / `require_viewer` / `require_operator` in `app/api/auth.py`; WS auth, Origin check and re-auth in `app/api/v1/ws.py`.
  - CLI: `openfleet users …`; `check --token` / `OPENFLEET_TOKEN`.
  - Frontend: `src/auth/AuthContext.tsx`, `src/pages/LoginPage.tsx`, `src/lib/auth.ts`.
- **Simulator** (`simulator/`):
  - Seeded YAML scenarios: `demo`, `healthy`, `lidar_regression`, `telemetry_stall`.
  - Failure kinds: topic_rate, node_state, cpu, memory, battery_drain, offline, telemetry_stall, log.
  - Modes: real time, `--fast`, and `--loop`.
- **Frontend** (`frontend/`): fleet, robot (live), incidents, incident detail (charts, timeline, first signal, triage), runs, run replay, and compare.
- **Tests:**
  - Backend: 253 (incl. 2 restart-grace tests for BUG-007, a concurrent-migration race test, incl. 5 retention tests, 7 sliding-session tests, 9 heartbeat-span tests, 10 login-throttle tests, 4 triage-audit tests, 9 bus tests, 4 sweeper-lock tests, 15 pure replay-engine tests and 8 redetect API tests), against real Postgres, including concurrency races and mutation-checked guards. Auth tests: `tests/test_auth.py` (robots), `tests/test_user_auth.py` (users, with a route-coverage test), the WS auth in `tests/test_realtime.py`, and the CLI in `tests/test_cli.py`.
  - Simulator: 15.
  - Collector: 24.
  - Edge (C++): 108 ctest tests (GoogleTest incl. the C protocol library and the firmware's portable logic, plus the bench smoke), 2 libFuzzer targets, plus `edge/e2e/e2e.py`, `edge/e2e/device_e2e.py` and the 5 scenarios (`edge/e2e/run_scenarios.py`).
  - Firmware: `firmware/tests/renode_test.py` (Renode), `firmware/renode/tests/firmware.robot` (8, Robot Framework, emulated time), `firmware/renode/gdb_check.sh`, `firmware/tests/firmware_e2e.py` (compose).
  - Frontend: 17 (reducer, ReplayClock, auth helpers, redetect overrides).
- **Docs:** README, ARCHITECTURE, FLOW (real file:function paths), PROTOCOL, FIRMWARE, BENCHMARKS, DECISIONS 001–035, BUGS 001–007, CONSTRAINTS.

## Currently working
Nothing in progress.

## Not built yet
- **Auth follow-ups:** SSO/OIDC, multi-tenancy.
- **rosbag2/MCAP storage:** object storage.
- **Scaling work** (ARCHITECTURE §10): TimescaleDB, retention, detection behind a queue. (Sweeper lock #024 and Redis fan-out #025 are done.)

## Known bugs
None open. Fixed bugs are recorded in docs/BUGS.md.

## Gotchas
- **Ports:**
  - Postgres is on host port **5433**, because the owner's Mac runs Homebrew Postgres on 5432.
  - Dashboard on **8080** (nginx); dev server on 5173.
  - Redis on host port **6380** (compose). Bus integration tests use `redis://localhost:6380/15` (`OPENFLEET_TEST_REDIS_URL`) and skip if it's down, so run `docker compose up -d redis` before the tests.
- **Second replica:** `docker compose --profile replicas up -d --build --wait` (backend-2 on :8001, lb on :8090), then `python scripts/multi_replica_e2e.py`. Stop with `docker compose --profile replicas down` (a plain `docker compose down` leaves the profile's containers running). The script fails, as it should, if Redis is stopped.
- **Bus semantics:** never call `hub.deliver` for locally produced messages (that skips forwarding), and never `hub.publish` something received from the bus (that re-forwards it).
- **Lock order:** robot → runs → incidents, everywhere.
- **Realtime publishing:** services must publish only through `Service.commit()` / `publish_after_commit()`.
- **Test fixtures:**
  - The `clock` fixture controls server time for API tests.
  - The `sweeper` fixture shares the test transaction; call `await sweeper.tick(now)`.
  - That fixture has `lock=None` (no advisory lock); set `sweeper.lock = AdvisoryLock(engine, KEY)` to test locking, with a key other than the real sweeper's so a running dev backend can't interfere.
- **Two runs on one robot in a test** need different `started_at`: with equal times, the "latest run" (#016) is decided by the random UUID, and the new run may be aborted as superseded.
- **Real-commit tests** go in `tests/test_concurrency.py`, using ids prefixed `concurrency-`. `purge_robots()` cleans them up.
- **Alembic in tests:** runs through `run_in_fresh_thread()`.
- **Enum changes:** changing a `StrEnum` needs a migration that updates the CHECK constraint (#015).
- **Clocks:** incident times are server time and frame times are robot time. They're aligned when robot clocks are roughly correct, which the simulator ensures in real-time mode (BUG-003).
- **Fast mode:** in `openfleet-sim --fast`, incidents don't auto-resolve *by hysteresis* within the run (it uses server time), but they still resolve when the run ends (#017), so a finished fast run leaves nothing open. To triage a live incident by hand, run `simulator/.venv/bin/openfleet-sim -s simulator/scenarios/lidar_regression.yaml --keys examples/demo-robot-keys.json` in real time (60 s).
- **Embedded dev:**
  - Run everything C/C++ through `scripts/edge-dev.sh` (it builds the image on first use); the Mac has no gdb or valgrind.
  - Locally the container is arm64, so `native` = aarch64 and the cross preset to try is `x86_64`. The `aarch64` preset is the CI path (from x86_64).
  - Renode's console quits at the end of its input: run it as `sleep infinity | renode --disable-gui --console ...`.
  - The container is `--rm`: files written outside `/src` (e.g. `/tmp`) are gone after each `edge-dev.sh` call. Put scratch files under `edge/build/` (git-ignored).
  - ASan in the arm64 Docker VM: about 11 µs per allocation (roughly 200× native), so the ASan ctest takes ~20 s. Tests mustn't assume native speed: pace producers on queue depth, not the clock.
  - TSan locally: run `ctest --preset tsan` as is; `setarch -R` isn't permitted in the container.
  - The agent container runs as an unprivileged user in `/`: `stress-ng` there needs `--temp-path /tmp`.
  - `docker compose --profile edge down` stops the edge services; a plain `docker compose down` leaves them running.
  - Outage tests longer than `RUN_ABANDON_AFTER_S` (60 s) of robot silence end in a fresh run, by design (#032).
  - **Device simulator control:** `docker compose --profile edge exec device-sim sh -c 'echo "CMD" >> /tmp/devsim.ctl'`. Only lines appended after it starts are run (the file survives restarts). The commands are in `openfleet-device-sim --help` and docs/PROTOCOL.md.
  - The agent image's build context is the repo root (it needs `protocol/`); `edge/docker/Dockerfile.agent.dockerignore` keeps the context to `edge/` and `protocol/`.
  - **libFuzzer needs `-fsanitize=fuzzer-no-link` on the code under test** (set by `EDGE_FUZZ`); without it coverage stays near zero and fuzzing is blind.
  - **Scenarios / Robot (Phase 4):**
    - `run_scenarios.py` measures from `mark`s on the *server* clock (offset measured once via psql). Mark *before* an action whose effect can happen before the action returns (e.g. `compose start backend`: batches arrive before `up --wait` returns).
    - An incident's `started_at` is its first opening (maybe WARNING), even if the scenario then expects CRITICAL.
    - Restarting device-sim is an unexplained reset to the gateway (HELLO sent before the link came up), so `mcu1` stays DEGRADED for the 20 s reset hold. That's by design (#034).
    - `/proc/<pid>/status` lists VmHWM before VmRSS.
    - Renode `include @x` paths aren't relative to the including script; pass absolute paths (`$machine_resc`).
    - The dev image now has Robot Framework (pip, with `python3-dev` for psutil on arm64). Rebuild an older image (command below).
  - **Firmware/Renode:**
    - Build with `scripts/edge-dev.sh bash -c 'cd /src && cmake -S firmware -B firmware/build -G Ninja && cmake --build firmware/build'`.
    - Renode needs a `reset` macro, or an IWDG reset jumps to address 0 and crashes.
    - Never put the boot record in a linker section: Renode's ELF loader zero-fills LOAD segments on every reset.
    - The monitor socket never closes: send with `timeout 3 nc -N localhost 1234`. `nc -q 1` hangs forever.
    - Renode costs about 1 host core. Because the agent measures the whole Docker VM, Renode can push edge-01's CPU up (it did, before the WFI idle hook and the 1 ms quantum).
    - The firmware's `RCC_CSR` read always gives `0x0E000000` under Renode.
    - Device-time rates are exact; Renode ran at about 1.01–1.06× real time on the Mac.
    - For Renode tests, use `-P <port>` (monitor over TCP) rather than `--console` with a held-open stdin.
  - The dev image now includes `libclang-rt-18-dev` (clang's sanitizer and fuzzer runtimes). Rebuild an older image: `docker build -t openfleet-edge-dev -f edge/docker/Dockerfile.dev edge/docker`.
  - macOS ships bash 3.2: empty arrays under `set -u` need `${a[@]+"${a[@]}"}`.
- **Simulator binary:** `openfleet-sim` lives in `simulator/.venv/bin/`, not the backend venv.
- **Logging in from a script:** `POST /api/v1/auth/login` needs the `X-OpenFleet-Client` header (CSRF defense), as do cookie-authenticated writes.
- **Login throttle (#027):** scripts that log in repeatedly with a wrong password get 429s for up to 5 min. To unlock the dev stack: `docker compose exec -T redis redis-cli --scan --pattern 'openfleet:login:*' | xargs docker compose exec -T redis redis-cli DEL` (or restart the backend if Redis isn't in use). Through :8080 the per-IP key is Docker Desktop's gateway (192.168.65.1), so every host browser shares one IP limit.
- **Credentials in tests:** the `client` fixture signs every request (`RobotKeyAuth` in conftest).
  - Robot writes use the acting robot's key (`test-robot-key-<id>`). Everything else uses an OPERATOR user token (`ofu_test-operator-token`).
  - Both are issued inside the test transaction. `/auth/*` requests are left unsigned.
  - To test auth itself, pass `auth=None` plus explicit headers.
  - `passwords.SCRYPT_N` is lowered in conftest for speed.
  - WS tests need a really committed user (TestClient runs its own loop), so they use the `concurrency-ws-` prefix; `purge_robots()` also deletes users by that prefix.
- **New endpoints:** `test_every_api_route_requires_robot_or_user_auth` fails until a route has `dependencies=[VIEWER]`, an `Operator`, or `RobotAuth`. Its route count (25) also needs updating.
- **Demo users:** `examples/demo-users.json` (operator/operator-demo, viewer/viewer-demo, and the viewer token `ofu_demo_viewer_public-not-secret`, which CI and `collector/ros_test/e2e.py` use) is public and seeded by compose. A test keeps CI and the e2e in sync with it.
- **Cookies:** the session cookie is `Path=/api`, SameSite=Strict. Every dashboard request sends `X-OpenFleet-Client`; cookie writes without it get 403. nginx forwards `$http_host` (the WS same-origin check needs the port).
- **Demo keys:** `examples/demo-robot-keys.json` is public and seeded by compose (backend and simulator mount it). Add any new scenario robot or collector test robot there; `test_demo_keys_file_covers_every_bundled_robot` enforces it. The simulator needs `--keys` / `OPENFLEET_ROBOT_KEYS_FILE`; the collector needs `OPENFLEET_API_KEY` or `api_key`.
- **Re-detection parity:** `_Reconciler` in `app/detection/replay.py` mirrors `Detector.reconcile`. Change one, change the other; `test_replay_with_current_rules_matches_live_detection` catches drift.
- **Wrong Vite version:** running `npx vite` may pick up a different global Vite. Use `npm run dev`.

## How to start the system
```bash
docker compose --profile demo up --build      # dashboard http://localhost:8080
```

## Diagnosing a live system
```bash
docker compose exec -T postgres psql -U openfleet -d openfleet -q < scripts/integrity_checks.sql   # 24 invariants, 0 = OK
docker compose logs backend | grep -iE "error|traceback|sweeper"
```

## How to run tests
```bash
docker compose up -d postgres
(cd backend && .venv/bin/pytest)
(cd simulator && .venv/bin/pytest)
(cd frontend && npm test && npm run typecheck)
scripts/edge-dev.sh bash -c 'cmake --preset native && cmake --build --preset native && ctest --preset native'   # also asan, tsan, x86_64
python edge/e2e/e2e.py                                   # C++ agent against the compose stack
python edge/e2e/device_e2e.py                            # device gateway + simulated MCU
scripts/edge-dev.sh bash -c 'cd /src && python3 firmware/tests/renode_test.py'   # firmware on Renode
python firmware/tests/firmware_e2e.py                    # firmware -> agent -> backend (compose)
scripts/edge-dev.sh bash -c 'cd /src && firmware/renode/tests/run.sh'   # Robot Framework on Renode (emulated time)
python edge/e2e/run_scenarios.py [NAME ...]              # measured scenarios + redetect (pip install httpx pyyaml)
python scripts/nightly_report.py edge/build -o report.md # one report from local results
scripts/edge-dev.sh bash -c 'cmake --preset fuzz && cmake --build --preset fuzz'   # then see edge/README.md
```

## Collector notes
- **Code:** `collector/openfleet_collector/`. `rates.py`, `batch.py` and `uplink.py` are pure and unit-tested without ROS. `node.py` is the thin rclpy glue.
- **ROS 2 e2e:** `python collector/ros_test/e2e.py` (Docker, `--profile ros`, ROS_DOMAIN_ID 42). It needs the backend on localhost:8000.
- **Startup grace:** 5 s (BUG-005). Topics are reported only after one full window since they were subscribed.
- **Known limitation:** `/rosout` lines logged right after a node starts can be missed before discovery completes.

## Recommended next task
**Agreed with the owner (2026-11-06): do this batch of 7, one feature at a time.** For each one: build, test, verify on the live compose stack, update the docs (DECISIONS / ARCHITECTURE / FLOW / HANDOFF), then move on. Don't commit unless the owner asks; offer a commit point between features.
1. ~~**CI: re-detection gate.**~~ Done: `e2e-regression-check` runs `openfleet redetect <lidar_regression run> --fail-on-missed --json` and asserts `caught >= 1 and missed == 0`, then checks that the gate exits 1 with `--set topic_degraded_ratio=0.4` (7/15 Hz = 0.47 is no longer below the threshold).
2. ~~**CI: multi-replica e2e.**~~ Done: compose profile `replicas` (backend-2 + nginx lb), `scripts/multi_replica_e2e.py`, CI job `e2e-multi-replica`. Verified live, including that it fails with Redis stopped.
3. ~~**Triage audit view.**~~ Done (#026): `triage_actions` table + `GET /triage` + Audit page. Note: only resolves had the actor before; INVESTIGATING was unrecorded, hence a table rather than an events query.
4. ~~**Login rate limiting.**~~ Done (#027): in-app per-username/per-IP windows in Redis (memory fallback), 429 + Retry-After; nginx overwrites `X-Forwarded-For`.
5. ~~**Store heartbeats per run.**~~ Done (#028): merged `heartbeat_spans`; replay treats them as contact; `telemetry_stall` scenario gated in CI.
6. ~~**Sliding sessions.**~~ Done (#029): idle 12 h, cap 7 d, extension throttled to once per 5 min; WebSockets don't slide.
7. ~~**Data retention.**~~ Done (#030): compact runs (frozen summary, raw rows deleted) via the sweeper or `openfleet retention`; off by default.

**Embedded expansion** ([docs/EMBEDDED_ROADMAP.md](docs/EMBEDDED_ROADMAP.md)): Phases 0–4 are done. Left over, each needing the owner's go:
- ~~Push and watch `CI` and a manual `Nightly` on GitHub.~~ Done 2026-11-08 (see Current state).
- ~~The optional full-system QEMU (Debian arm64 + the agent as a systemd service).~~ Done 2026-11-08 (#036).
- ~~Postponed from the roadmap: ROS 2 integration.~~ Done 2026-11-08 (#037), as `openfleet-ros-bridge`.

**Not in the batch: these need the owner's decisions first.**
- OIDC: which provider; auto-create users or invite only?
- Multi-tenancy: what a tenant is; shared database or isolated?
- TimescaleDB, detection behind a queue, MCAP/object storage: new infrastructure, only worth it at real scale.
