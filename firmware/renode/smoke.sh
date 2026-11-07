#!/usr/bin/env bash
# Boot the emulated STM32F4 headless and check its UART output arrives over TCP.
# Runs inside the dev container:  scripts/edge-dev.sh ../firmware/renode/smoke.sh
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
log="$(mktemp)"
# Renode's console exits at end of input, so hold stdin open; stop it when we're done.
sleep infinity | renode --disable-gui --console --plain "$here/smoke.resc" >"$log" 2>&1 &
renode_pid=$!  # the last process of the pipeline
trap 'kill "$renode_pid" 2>/dev/null || true; rm -f "$log"' EXIT
python3 - <<'PY'
import socket, sys, time
deadline = time.time() + 90
while True:
    try:
        s = socket.create_connection(("127.0.0.1", 3456), timeout=2)
        break
    except OSError:
        if time.time() > deadline:
            sys.exit("FAIL: Renode never opened the UART socket on :3456")
        time.sleep(1)
s.settimeout(30)
buf = b""
while b"Contiki" not in buf:
    try:
        chunk = s.recv(4096)
    except socket.timeout:
        sys.exit(f"FAIL: no boot banner on UART4 within 30 s (got {len(buf)} bytes)")
    if not chunk:
        sys.exit("FAIL: UART socket closed")
    buf += chunk
print(f"OK: emulated STM32F4 booted; {len(buf)} bytes on UART4 over TCP, banner found")
PY
