# OpenFleet demo firmware (FreeRTOS, STM32F407 on Renode)

`firmware/` holds a small FreeRTOS application for an STM32F407. It reports to OpenFleet over the [device protocol](PROTOCOL.md), through the edge agent's gateway. Design rationale: [DECISIONS #034](DECISIONS.md).

**It has only ever run on Renode's emulated STM32F4.** No physical board has been used. Nothing here is a real-time guarantee or a hardware validation, and every timing below is emulated time.

## What it does

| Task | Priority | Job |
|---|---|---|
| `sensor` | 4 | Samples `imu` at 50 Hz and `range` at 10 Hz into the **data queue** (32). It never waits on the link: a full queue drops the reading and counts it. |
| `comms` | 2 | The only user of USART2. It serves the **control queue** (8: HEALTH, LOG, DESCRIBE) before the data queue, so on a slow link readings are what gets dropped, never the reports about it. |
| `health` | 3 | Every second: a HEALTH frame with each task's state and stack high-water mark (`sensor` is DEGRADED if readings were dropped), plus a log line when they were. Every 5 s: DESCRIBE. It also notes uptime in the boot record. |
| `watchdog` | 5 | Every 100 ms: kicks the independent watchdog (IWDG, 1 s timeout) only while every supervised task has checked in on time (`sensor` 200 ms, `comms` 500 ms, `health` 3 s). |

- **Device id:** 2. So in OpenFleet it appears as topics `/mcu2/imu` and `/mcu2/range`, and nodes `mcu2`, `mcu2/sensor`, `mcu2/comms` and `mcu2/health`.
- **Memory:** static allocation only, no heap (`configSUPPORT_DYNAMIC_ALLOCATION 0`; `_sbrk` refuses). About 12 KB of flash and 21 KB of RAM; the linker prints the usage on every build.
- **Clock:** the 16 MHz internal oscillator, which is the reset default. No PLL is set up, and the Renode platform overlay sets SysTick to match.
- **Board support:** direct register access (`board/stm32f4/`), no vendor HAL.

### Watchdog path

1. A task stops checking in. In the demo this is injected; in real life it would be a deadlock, a busy loop or a missed deadline.
2. `sup_may_kick` latches false, and the watchdog task stops kicking.
3. Before that, it writes "watchdog, task N" into the **boot record** and sends a LOG: `watchdog: task sensor stopped checking in; resetting`.
4. About 1 s later the IWDG resets the MCU.
5. The next boot reads the boot record. Its HELLO says **watchdog**, the boot id is +1, and a LOG names the task and the previous uptime.
6. The gateway logs `mcu2 reset (watchdog)` and holds node `mcu2` DEGRADED for 20 s. The backend opens `node_health:mcu2` and resolves it once the device has run cleanly.

A failed `configASSERT`, a stack overflow (`configCHECK_FOR_STACK_OVERFLOW 2`) or a fault handler stops the CPU, and the IWDG resets it the same way.

### Boot record and reset reason

- The record is 6 words at `0x2001FF00` that the startup code doesn't initialise, with a magic and a seal. It holds the boot count, the boot id (random after power-on via the RNG, then +1 per boot), the last noted uptime, and the pending reset reason and stalled task.
- **Why not rely on the reset flags:** Renode doesn't model the reset flags in `RCC_CSR` (it always reads `0x0E000000`, and RMVF is ignored). So the supervisor's own note is the primary source, and the hardware flags are the fallback.
- **Fixed addresses, not linker sections:** the record and the fault block are addressed directly, and the linker script only reserves the range. As a NOLOAD section they sat in an ELF `LOAD` segment, and Renode's loader zero-filled it on every reset, which erased the record. The Renode test caught this.

## Build and run

```bash
scripts/edge-dev.sh bash -c 'cd /src && cmake -S firmware -B firmware/build -G Ninja && cmake --build firmware/build'
scripts/edge-dev.sh bash -c 'cd /src && python3 firmware/tests/renode_test.py'   # on Renode, in the dev container
scripts/edge-dev.sh bash -c 'cd /src && firmware/renode/gdb_check.sh'             # GDB through Renode
scripts/edge-dev.sh bash -c 'cd /src && firmware/renode/tests/run.sh'             # Robot Framework, emulated time
docker compose --profile edge --profile firmware up -d --build                   # firmware -> agent -> OpenFleet
python firmware/tests/firmware_e2e.py                                            # the end-to-end check
```

- FreeRTOS-Kernel V11.1.0 is fetched by CMake and pinned by SHA-256. Only its sources are compiled: `tasks.c`, `queue.c`, `list.c` and the `GCC/ARM_CM4F` port.
- `firmware/renode/openfleet-machine.resc` loads the board with the overlay and defines the `reset` macro (reload the image; SRAM is kept, as on the chip). `firmware/renode/openfleet.resc` includes it (pass `$machine_resc`), serves USART2 on TCP 3456 and starts the emulation; the Robot suite includes the machine alone and drives it in emulated time.

### Fault injection (through Renode's monitor)

`fault_ctl` at `0x2001FE00` is zeroed on every boot, so a fault never survives the reset it causes:

| Address | Field | Effect |
|---|---|---|
| `0x2001FE00` | `stall_task` | That task stays alive but stops checking in (1 = sensor) |
| `0x2001FE04` | `comms_delay_ms` | Extra delay per frame sent (≤ 400): a slow link |
| `0x2001FE08` | `stop_sensors` | Bitmask `1 << sensor_id`: those sensors aren't sampled |

```bash
docker compose --profile firmware exec renode sh -c 'echo "sysbus WriteDoubleWord 0x2001FE00 1" | timeout 3 nc -N localhost 1234'
```

## Tests

| Test | Where it runs | What it shows |
|---|---|---|
| `firmware/tests/fw_app_test.cpp` (9) | host, every edge preset (native, ASan/UBSan, TSan, qemu cross) | Supervisor: deadlines, latching, the 32-bit wrap. Boot record: power-on noise, a magic without the seal, the watchdog note, the fallback to hardware flags, boot id never 0. |
| `firmware/tests/renode_test.py` | Renode, in the dev container (CI job `firmware`) | Read with an **independent Python decoder** (`ofp.py`, written from PROTOCOL.md): HELLO power-on, DESCRIBE, imu 50.00 Hz and range 10.00 Hz in device time, healthy tasks, contiguous seq, 0 bad frames. Then the stall → watchdog → reboot path, and a slow link (drops reported, sensor DEGRADED, no reset). |
| `firmware/renode/gdb_check.sh` | Renode's GDB server + `gdb-multiarch` | Attach to the running firmware and read the boot record, the supervisor and the tick count |
| `firmware/renode/tests/firmware.robot` (8) | Renode via `renode-test` (CI job `firmware`, and nightly) | The same behaviour **in emulated time** (`emulation RunFor`, UART to a file): exact counts (499–501 imu readings in 10 s), same seed → same bytes, the boot count read from emulated RAM after a watchdog reset, recovery time, a stopped sensor, and Renode-labelled cost figures ([BENCHMARKS.md](BENCHMARKS.md)) |
| `firmware/tests/firmware_e2e.py` | compose: Renode container → agent → backend | Topics and nodes for mcu2; a stall leads to a reset event with its cause and an incident that resolves; a slow link degrades the topic without a reset |

Measured on the dev Mac (Docker, arm64): Renode ran the firmware at about 1.05× real time. That's an observation about this host, not a property of the firmware.

## Limits and what's not done
- No physical board. The board support follows the STM32F407 reference manual (USART2 on PA2/PA3 AF7, IWDG, RNG, RCC_CSR), but only Renode's models of those peripherals have run it.
- The task logic is pure C and host-tested directly. It isn't run under the FreeRTOS POSIX port (the roadmap's option): host unit tests cover the decision logic, and Renode runs the real binary.
- No receive path: the device only transmits. Commands to the device (e.g. clearing a fault) would need a host → device message type.
- The UART is written by polling (TXE), which is fine at these rates under emulation. A real board at higher rates would want DMA.
