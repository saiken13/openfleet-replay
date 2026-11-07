# edge/: OpenFleet C++ edge agent

The robot-side C++20 agent from the [embedded roadmap](../docs/EMBEDDED_ROADMAP.md). **Status: Phase 0.** The toolchain, presets, CI and the bounded queue the agent's threads will share are in place. The agent itself is Phase 1.

No hardware is involved anywhere. Each result below says where it ran.

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

Execution environments, kept distinct:
- **Native:** the binary runs on the CPU it was built for (the container on an M2 is native arm64 Linux).
- **Cross + qemu-user:** another architecture's binary, run by user-mode emulation. This proves the cross build and the code are correct, not anything about timing.
- **Renode:** a whole emulated microcontroller (`../firmware/renode/smoke.sh` boots an STM32F4 and reads its UART over TCP).

## Layout
- `include/openfleet/edge/bounded_queue.hpp`: a fixed-capacity ring. Producers never block; when it's full, the oldest item is dropped and counted.
- `tests/`: GoogleTest, fetched by CMake (pinned by hash).
- `cmake/toolchains/`: cross toolchain files.
- `docker/Dockerfile.dev`: the dev container.
