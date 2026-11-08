# Embedded & Embedded Linux expansion — roadmap

**Status:** Approved by the owner (2026-11-07). **Phase 0 done** (DECISIONS #031). **Phase 1 done** (#032). **Phase 2 done** (#033). **Phase 3 done** (#034). **Phase 4 done** (#035). Every phase is complete; the optional full-system QEMU item was not built.
**Hard constraint:** no physical hardware. Everything is native, cross-compiled + emulated, or simulated, and every claim and measurement says which.

## Facts that shaped the plan
- The dev machine is an Apple M2 (arm64, 8 GB); Docker runs `linux/aarch64` natively. Locally, the agent therefore runs **natively on ARM64 Linux** (container). The real **x86_64 → aarch64 cross-compile, run under `qemu-aarch64`**, happens in CI (GitHub runners are x86_64).
- gdb and valgrind don't run on Apple Silicon macOS: they live in the Linux dev container.
- The backend needs no changes: topic, node and log names are free strings (≤ 256 chars), so an MCU maps onto the existing model. Each sensor stream becomes a topic (`TopicRateRule` → "sensor stopped"). Each firmware task becomes a node (`NodeHealthRule` → "task stalled"). Resets, watchdog and CRC errors become log events.
- Two senders on one run would collide on `UNIQUE(run_id, seq)`, so the C++ agent is the uplink for **its own robot** (e.g. `edge-01`), not a second sender beside the ROS collector.

## What was cut, merged or postponed
| Brief feature | Verdict |
|---|---|
| 1. C++ edge agent | Phase 1 (top priority) |
| 3. Device protocol | Phase 2: a C11 library shared by firmware and agent |
| 2 + 4. MCU firmware + FreeRTOS | **Merged**, Phase 3: one FreeRTOS firmware on a Renode STM32F4 |
| 6. Fault injection | Inside each phase; only embedded-specific faults (CPU/mem/network are already in the Python simulator) |
| 7. Regression workflow | Inside phases + a slow nightly CI tier; reuses `openfleet check` / `redetect` |
| 8. Deployment/debugging | Sanitizers + systemd unit (Phase 1), firmware GDB via Renode (Phase 3); full-system QEMU optional |
| 9. Performance | Phase 4, measured and labelled by environment |
| 5. ROS 2 integration | **Postponed.** Later: the agent republishes MCU data as ROS topics, so the existing collector monitors them |

**Avoid:** Wokwi as the primary simulator (the CLI needs a cloud token), Yocto/Buildroot, a hand-written HTTP stack, a C++ copy of the ROS collector, MCU-specific backend tables or rules, and real-time claims.

## Phases (effort in focused days)
**Phase 0: toolchain (½). Done (#031):** Renode 1.17 runs natively on arm64; the IWDG is modelled; the CI cross path was verified in an emulated amd64 container.
- Ubuntu 24.04 dev container: cmake, ninja, gcc, clang, gdb, valgrind, `gcc-aarch64-linux-gnu`, `qemu-user`, libcurl.
- CI skeleton.
- **Renode spike:** does it run on the M2 (native or Docker)? Is the STM32F4 UART reachable over TCP? Fallback: the FreeRTOS POSIX port in a Linux container.

**Phase 1: C++20 edge agent, `edge/` (5–7). Done (#032):** all criteria met, with one adjustment. The no-loss outage is 30 s in the e2e, because the server aborts a run after 60 s of silence (`RUN_ABANDON_AFTER_S`); longer outages take the fresh-run path, as in the collector.
- Monitors CPU (`/proc/stat` deltas), memory, uptime, configured process health and link latency.
- Threads: sampler, uplink, signals, joined by a bounded queue.
- Uplink follows `collector/openfleet_collector/uplink.py`'s contract: ordered bounded buffer, same-seq retry, backoff with jitter, 409/404 → fresh run, other 4xx → drop. SIGTERM → ABORTED.
- Its own health is reported as node `edge_agent`. Uses libcurl + nlohmann/json; GoogleTest via FetchContent.
- **Tests:** parsers against saved `/proc` fixtures; queue and backoff; an e2e against the compose stack (like `collector/ros_test/e2e.py`).
- **CI:** native + ASan/UBSan, TSan, and an aarch64 cross build with tests under `qemu-aarch64`.
- **Done when:** `edge-01` is on the dashboard; `stress` opens `high_cpu`; a 60 s backend outage loses no batch; SIGTERM ends the run.

**Phase 2: protocol + gateway, `protocol/` + `docs/PROTOCOL.md` (4–5). Done (#033):** all criteria met. Every fault in the brief is covered; there are two libFuzzer targets; the compose e2e passes.
- Frames: COBS + CRC-16/CCITT; header holds version, device id, type, seq, timestamp, length. The decoder streams into fixed buffers with no malloc.
- Gateway in the agent, over a transport abstraction (TCP or pty): drops duplicates, detects resets (boot counter or seq restart), maps data to topics, nodes and logs.
- A host device simulator with fault injection (corrupt, truncate, fragment, drop, reset, silence).
- **Tests:** every case in the brief, plus a libFuzzer target.
- **Done when:** "sensor stops" opens a topic-rate incident; a reset becomes an event; bad frames are counted and never crash anything.

**Phase 3: FreeRTOS firmware on a Renode STM32F4, `firmware/` (8–10, highest risk). Done (#034):**
- Done when met: a stalled task leads to a watchdog reset, then a `node_health:mcu2` incident that resolves, scripted (`firmware/tests/firmware_e2e.py`).
- Deviations:
  - The reset cause comes from a boot record, because Renode doesn't model `RCC_CSR`.
  - The gateway holds a reset device DEGRADED for 20 s, so the brief reset becomes an incident.
  - The task logic is host-tested directly instead of under the POSIX port.
- Tasks: sensor (periodic sampling into a bounded queue); comms (a slow link can't block sampling); health (stack high-water marks); watchdog (a stalled task leads to a reset).
- Same protocol library built for Cortex-M4; the UART goes to the agent over TCP. The task logic also builds against the POSIX port for unit tests. GDB via Renode.
- **Done when:** a stalled task causes a watchdog reset, which OpenFleet shows as an incident that then recovers. Scripted and repeatable.

**Phase 4: regression tier + benchmarks (4–5). Done (#035; [BENCHMARKS.md](BENCHMARKS.md)):**
- Done as planned: a YAML scenario runner that measures on the server clock and gates on `redetect` (5 scenarios); an 8-test Robot Framework suite on Renode in emulated time; `edge_bench`; a nightly workflow with one labelled report.
- Found on the first scenario run: BUG-007 (a restarted backend blamed robots for its own outage), fixed.
- Not done: the optional full-system QEMU (Debian arm64 + systemd).
- Scenario runner; Renode Robot Framework tests; a nightly CI job that writes a report.
- Benchmarks: parser throughput and per-frame cost, agent RSS/CPU, end-to-end detection latency, reconnect time. Each figure is tagged native / QEMU / Renode.
- Optional: Debian arm64 in `qemu-system-aarch64` with the agent as a systemd service.

## Key choices
- **C11** for the protocol and firmware (it runs on the MCU); **C++20** for the agent (GCC 13 cross).
- **Renode over Wokwi:** open source, local, STM32F4, UART over TCP, GDB server. Apple Silicon support is unverified, hence the Phase 0 spike.
- **Binary framing over JSON** on the device link: bounded, cheap, checksummed. The agent still speaks JSON to the API.
- **A few owned threads over an event loop** in the agent, verified with TSan. Serial reads use `poll` with a timeout.
- **Memory buffering first**, then a disk spool that survives crashes as an option.
