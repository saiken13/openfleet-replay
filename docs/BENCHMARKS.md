# Regression tier and benchmarks

> **Status: Implemented (Phase 4 of [EMBEDDED_ROADMAP](EMBEDDED_ROADMAP.md), DECISIONS #035).**
> Every figure here says where it was measured. Nothing ran on robot hardware.

## What runs where

| Tier | What | Where | Gate |
|---|---|---|---|
| Every push (`ci.yml`) | unit tests on every preset (incl. `edge_bench --quick`), sanitizers, 45 s fuzzing, the e2e scripts, the Robot suite | GitHub runner | any failure |
| Nightly (`nightly.yml`, 03:17 UTC, or *Run workflow*) | benchmarks, the Robot suite, every scenario, 10 min fuzzing per target, one report | GitHub runner | tests, scenarios and fuzzing; benchmarks are reported, not gated |

The nightly report is the job summary of the run and the artifact `nightly-report` (Markdown + JSON). Raw results stay as artifacts: `bench`, `robot` (Robot `report.html`/`log.html`), `scenarios`.

## Environments (the labels)

| Label | Meaning |
|---|---|
| `native` | The binary runs on the machine's own CPU. On CI an x86_64 runner VM; locally the arm64 Docker VM (`native-container`). |
| `qemu-user` | A cross-compiled binary under user-mode qemu. The figure describes the emulator. |
| `renode` | Renode's STM32F4 model. Not cycle-accurate: instruction counts, not cycles; emulated time, not wall time. |
| compose | The real backend, Postgres and agent in Docker on one VM, with a simulated (device-sim) or emulated (Renode) device. |

Figures are compared only within one label and architecture.

## Running it locally

```bash
# Micro-benchmarks (Release build; label the environment yourself)
scripts/edge-dev.sh bash -c 'cmake --preset release && cmake --build --preset release && build/release/edge_bench --env native-container'
# ... and the x86_64 build under qemu-user
scripts/edge-dev.sh bash -c 'cmake --preset x86_64 -B build/bench-x86_64 -DCMAKE_BUILD_TYPE=Release -DEDGE_BUILD_TESTS=OFF \
  && cmake --build build/bench-x86_64 --target edge_bench \
  && qemu-x86_64 -L /usr/x86_64-linux-gnu build/bench-x86_64/edge_bench --env qemu-user'

# Robot Framework on Renode (needs the firmware built)
scripts/edge-dev.sh bash -c 'cd /src && cmake -S firmware -B firmware/build -G Ninja && cmake --build firmware/build && firmware/renode/tests/run.sh'

# Scenarios against compose (pip install httpx pyyaml)
python edge/e2e/run_scenarios.py --list
python edge/e2e/run_scenarios.py                 # all; reports in edge/build/scenarios/

# One report from any of the above
python scripts/nightly_report.py edge/build -o report.md
```

## Micro-benchmarks (`edge/bench/bench.cpp`)

Each case runs a calibrated batch repeatedly for ≥ 300 ms, five times, and reports the median.

| Case | One operation |
|---|---|
| `crc16_256B` | CRC-16/CCITT over a maximum-size raw frame (bitwise, as on the MCU) |
| `encode_sensor` | build + encode a SENSOR frame |
| `decode_sensor` / `decode_max` | streaming decode, byte by byte, of a SENSOR / maximum-size frame |
| `gateway_frame` | the gateway's receive path per frame for a firmware-like mix (decode, duplicate window, payload parse, rate counting, mutex) |
| `batch_json` | one telemetry batch (4 topics, 9 nodes) to JSON text, as the agent sends once a second |

Dev Mac (Apple M2), 2026-11-07:

| Case | native-container, aarch64, gcc 13.3, RelWithDebInfo | qemu-user, x86_64, gcc 13.3, Release |
|---|---:|---:|
| crc16_256B | 2,572 ns (99.6 MB/s) | 3,909 ns |
| encode_sensor | 194 ns | 383 ns |
| decode_sensor | 229 ns | 705 ns |
| decode_max | 3,208 ns (80.4 MB/s) | 8,357 ns |
| gateway_frame | 287 ns (~3.5 M frames/s) | 1,431 ns |
| batch_json | 8.2 µs | 111.7 µs |

Reading it: a device at 60 frames/s costs the gateway about 17 µs of CPU per second natively. The JSON build is the agent's biggest per-batch cost and still about 8 µs a second. Under qemu-user the gap varies by code (2× for the CRC loop, 14× for JSON), which is why emulated figures are not CPU figures.

## Firmware on Renode (`firmware/renode/tests/firmware.robot`)

Eight tests drive the emulation with `emulation RunFor` and read USART2 from a file (`OfpKeywords.py`, decoding with the independent `firmware/tests/ofp.py`): boot and DESCRIBE, exact rates in emulated time, same seed → same bytes, watchdog reset on a stalled task (boot count read from the boot record in emulated RAM), recovery time, slow link without a reset, stopped sensor, CPU cost.

| Metric (renode) | Value | What |
|---|---:|---|
| instructions_per_s | 322,599 | executed per second of device time (WFI idle excluded) |
| instructions_per_frame | 5,186 | all firmware work divided by frames sent |
| duty_share_pct | 2.0 % | of 16 MHz, assuming 1 instruction per cycle |
| watchdog_recovery_ms | 1,250 | stalled task → watchdog reset → first reading of the next boot (50 ms steps) |
| lowest_free_stack_words | 217 | lowest headroom any task reported |

## Scenarios (`edge/e2e/scenarios/*.yaml`)

The format is documented at the top of `edge/e2e/run_scenarios.py`. A `measure` is the server-side timestamp of the effect minus a `mark` on the server clock. Every scenario ends its run and gates on `redetect`.

Dev Mac, compose, agent native aarch64 in a container, 2026-11-07:

| Scenario | Device | Measured |
|---|---|---|
| device_sensor_stops | simulated | stop → incident 0.7 s (WARNING, then CRITICAL); topic FAILED 1.7 s; restart → HEALTHY 1.6 s; resolved 11.6 s (10 s hysteresis) |
| device_link_reconnect | simulated | link loss logged 0.8 s; device STOPPED and `node_health:mcu1` 3.8 s (3 s silence timeout); reconnect 1.3 s; data HEALTHY 2.3 s; mcu1 RUNNING 21.3 s (20 s reset hold, #034) |
| backend_outage | simulated | 15 s outage; start issued → first buffered batch 3.2 s; 34/34 batches, no gap; no `heartbeat_missing` (BUG-007) |
| agent_footprint | simulated | agent 0.65 % of one core, 11.8 MiB RSS, 6 threads (60 frames/s device); `high_cpu` 1.9 s after `stress-ng` |
| firmware_watchdog | emulated (Renode) | stall → watchdog reset seen 1.8 s; `node_health:mcu2` 1.8 s; resolved 30.8 s (20 s hold + 10 s hysteresis) |

These are end-to-end latencies for this stack: they include the agent's 1 s sampling period, the backend's 2 s sweeper and the incident hysteresis. They are what an operator sees, not the cost of one component.

## Not done
- **Full-system QEMU** (Debian arm64 with the agent as a systemd service) was optional in the roadmap and is not built; the unit file is verified with `systemd-analyze verify` only.
- **Neither workflow has run on GitHub yet.**
