#!/usr/bin/env bash
# Debugging the firmware with GDB through Renode's GDB server (docs/FIRMWARE.md).
# Emulated target: this shows the workflow, not debugging on a physical board.
#   scripts/edge-dev.sh bash -c 'cd /src && firmware/renode/gdb_check.sh'
set -euo pipefail
cd "$(dirname "$0")/../.."
elf=firmware/build/openfleet-fw.elf
renode --disable-gui -P 3334 \
  -e "\$bin=@$PWD/$elf; \$repl=@$PWD/firmware/renode/openfleet-f4.repl; \$uart_port=3457" \
  -e "\$machine_resc=@$PWD/firmware/renode/openfleet-machine.resc" \
  -e "include @$PWD/firmware/renode/openfleet.resc" \
  -e "machine StartGdbServer 3333 true" </dev/null >/tmp/renode-gdb.log 2>&1 &
renode_pid=$!
trap 'kill $renode_pid 2>/dev/null || true' EXIT
for _ in $(seq 60); do nc -z 127.0.0.1 3333 2>/dev/null && break; sleep 0.5; done
sleep 3  # let the firmware boot and run for a while

gdb-multiarch -q -batch "$elf" \
  -ex 'set confirm off' \
  -ex 'target remote :3333' \
  -ex 'print/x *(boot_record *)0x2001FF00' \
  -ex 'print g_boot' \
  -ex 'print g_sup.tripped' \
  -ex 'print g_sensor_dropped' \
  -ex 'print xTickCount' \
  -ex 'info symbol $pc' \
  -ex 'detach' 2>&1 | tee /tmp/gdb.out
grep -q 'magic = 0xf1eb007' /tmp/gdb.out && grep -q 'reason = 0' /tmp/gdb.out \
  && echo "PASS: GDB attached to the emulated MCU and read the live boot record"
