# edge/: OpenFleet C++ edge agent

`openfleet-edge-agent` is a C++20 agent for a Linux robot computer that has no ROS (an embedded Linux board, a gateway, an industrial PC). It samples `/proc` and streams the result to OpenFleet as its own robot. OpenFleet judges health from that (high CPU, a process stopped, the robot gone silent). The agent only measures and delivers. It also acts as a **gateway** for microcontrollers speaking the [OpenFleet device protocol](../docs/PROTOCOL.md): their sensors become topics, their tasks nodes, and their resets events. Design: [DECISIONS #032, #033](../docs/DECISIONS.md); roadmap: [EMBEDDED_ROADMAP](../docs/EMBEDDED_ROADMAP.md) (Phases 1–2 done).

No hardware is involved anywhere. Each result below says where it ran.

## What it reports

| OpenFleet field | Source |
|---|---|
| `cpu_pct` | `/proc/stat`: busy share of the time since the previous sample |
| `mem_pct` | `/proc/meminfo`: `(MemTotal − MemAvailable) / MemTotal` |
| `net_latency_ms` | Full time of the last successful telemetry request to the API |
| node `<process>` | Each `--process NAME`: RUNNING, DEGRADED (stopped by a signal), ERROR (only zombies), STOPPED (not found) |
| node `edge_agent` | The agent itself: DEGRADED if it missed a sampling period or lost batches |
| log events | Startup (with host uptime); "API was unreachable for N s" after an outage; a run that was rejected |
| topic `/mcu<id>/<sensor>` | Each `--device`: the rate of each sensor's readings, expected rate from the device's DESCRIBE |
| node `mcu<id>`, `mcu<id>/<task>` | The device (STOPPED when silent; DEGRADED on a bad link or for 20 s after an unexpected reset) and its tasks' states |
| log events from devices | Resets (with reason), device log lines, low stack, link-error summaries, link up/down |

Delivery uses the ROS collector's contract:
- Batches go in order and a retry re-sends the same seq, so the server deduplicates.
- Up to 600 batches (10 min at 1 Hz) are buffered while the API is unreachable.
- Backoff with jitter, 0.5 s to 10 s.
- 404/409 open a fresh run; other 4xx drop the batch.

**SIGTERM/SIGINT** flushes for at most 5 s, then ends the run **ABORTED**. `--duration S` ends it **SUCCESS**.

## Run it

```bash
docker compose --profile edge up -d --build     # edge-01 + a watched "demo-worker" process
python edge/e2e/e2e.py                          # the end-to-end check (also in CI)
docker compose --profile edge exec edge-agent stress-ng --cpu 0 --timeout 30s --temp-path /tmp   # opens high_cpu
python edge/e2e/device_e2e.py                   # the device gateway against the simulated MCU
python edge/e2e/run_scenarios.py                # measured scenarios + redetect (docs/BENCHMARKS.md)
docker compose --profile edge exec device-sim sh -c 'echo "stop-sensor 2" >> /tmp/devsim.ctl'   # opens topic_rate
```

On a Linux machine:

```bash
export OPENFLEET_API_KEY=...        # `openfleet keys create edge-01`; never a flag (argv is public)
openfleet-edge-agent --api=https://openfleet.example.com --robot-id=edge-01 --process=my_driver \
    --device=serial:/dev/ttyUSB0@115200      # or tcp://HOST:PORT (e.g. Renode's UART socket)
openfleet-device-sim --listen 4000           # a simulated MCU to point --device at (see --help)
# The FreeRTOS firmware on Renode is mcu2: compose profile `firmware`, see docs/FIRMWARE.md
openfleet-edge-agent --help         # every option
```

`packaging/openfleet-edge-agent.service` is a hardened systemd unit (`systemd-analyze security` exposure 1.2). Customise it with a drop-in, not by editing it: `systemctl edit openfleet-edge-agent`, then override `ExecStart` (robot id, API URL, `--process`). A serial device also needs `PrivateDevices=no`, `DeviceAllow=` and the `dialout` group (see the unit's header).

`qemu/qemu_e2e.py` runs it under a real systemd boot: Debian 13 arm64 in `qemu-system-aarch64`, provisioned by cloud-init, through start at boot, a crash restart, stop, reboot and poweroff (DECISIONS #036). Needs QEMU with an aarch64 UEFI firmware (`brew install qemu`, or `qemu-system-arm qemu-efi-aarch64`), Docker and `pip install httpx`:

```bash
python edge/qemu/qemu_e2e.py                # HVF/KVM on an arm64 host, ~1.5 min
python edge/qemu/qemu_e2e.py --accel tcg    # emulated, as on CI's x86_64 runners
```

## Build and test

Everything runs in one Linux dev container (`docker/Dockerfile.dev`: gcc 13, clang 18, gdb, valgrind, CMake 3.28, a cross compiler, qemu-user, Renode 1.17):

```bash
scripts/edge-dev.sh bash -c 'cmake --preset native && cmake --build --preset native && ctest --preset native'
scripts/edge-dev.sh            # interactive shell in /src/edge
```

| Preset | What it is |
|---|---|
| `native` / `release` | The container's own architecture (arm64 on an Apple Silicon Mac, x86_64 on CI) |
| `asan` | AddressSanitizer + UndefinedBehaviorSanitizer |
| `tsan` | ThreadSanitizer |
| `aarch64` | Cross-compiled for aarch64 Linux; ctest runs the tests under `qemu-aarch64` (CI, from x86_64) |
| `x86_64` | Cross-compiled for x86_64 Linux; ctest runs the tests under `qemu-x86_64` (locally, from arm64) |
| `release` + `edge_bench` | Micro-benchmarks (`bench/bench.cpp`): `build/release/edge_bench --env native [--json out.json]`; ctest runs it once with `--quick` on every preset |
| `fuzz` | clang + libFuzzer + ASan/UBSan: `fuzz_decoder` (protocol) and `fuzz_gateway`; seeds from `make_seeds` (see `docs/PROTOCOL.md`) |

- **libcurl:** the binary needs libcurl for its target. A cross build without one (CI's aarch64 job) builds and tests `edge_core` and skips the binary (`-DEDGE_BUILD_AGENT=AUTO|ON|OFF`).
- **ASan:** in the arm64 Docker VM each allocation costs about 11 µs, so the ASan test run takes about 20 s instead of 1 s.

Execution environments, kept distinct:
- **Native:** the binary runs on the CPU it was built for (the container on an M2 is native arm64 Linux).
- **Cross + qemu-user:** another architecture's binary, run by user-mode emulation. This proves the cross build and the code are correct, not anything about timing.
- **Container on the compose stack:** the e2e. The "robot computer" is a container; its CPU and memory figures are the Docker VM's.
- **Full system (QEMU):** a whole emulated arm64 computer booting Debian with systemd (`qemu/`). Accelerated (HVF/KVM) on an arm64 host, emulated (TCG) elsewhere.
- **Renode:** a whole emulated microcontroller (`../firmware/renode/smoke.sh` boots an STM32F4 and reads its UART over TCP).

## Layout
- `include/openfleet/edge/`, `src/`:
  - `proc`: pure `/proc` parsers.
  - `sampler`: reads `/proc`, scans processes.
  - `telemetry`: the batch JSON.
  - `backoff`.
  - `uplink`: the delivery state machine, behind the `http.hpp` interface.
  - `agent`: config, the three threads, signals.
  - `bounded_queue.hpp`.
  - `gateway`: device frames → topics, nodes and logs. `device_link`: TCP and serial transports.
  - `device_sim` and `device_sim_main.cpp`: the simulated MCU (`openfleet-device-sim`).
  - `curl_client`: the only network code.
  - `main.cpp`.
- `tests/`: GoogleTest, fetched by CMake and pinned by hash. Real `/proc` fixtures are in `tests/fixtures/proc`; `fake_http.hpp` is a scripted API.
- `../protocol/`: the device protocol, a C11 library shared with the firmware, with its tests and fuzz target. It's built as part of this project.
- `e2e/e2e.py`, `e2e/device_e2e.py`: drive the compose `edge` profile. `e2e/run_scenarios.py` + `e2e/scenarios/*.yaml`: the measured regression tier.
- `bench/`: micro-benchmarks (`edge_bench`).
- `docker/`: `Dockerfile.dev` (toolchain) and `Dockerfile.agent` (the agent image).
- `packaging/`: the systemd unit. `qemu/`: the full-system e2e and the guest's build. `cmake/toolchains/`: the cross toolchain files.
