"""Firmware test on Renode's emulated STM32F4 (docs/FIRMWARE.md). Emulated, not a board.

    python firmware/tests/renode_test.py            # in the dev container (scripts/edge-dev.sh)

Boots the firmware, reads its UART over TCP with an independent protocol reader (ofp.py),
and drives faults through Renode's monitor:
1. boot: HELLO (power-on), DESCRIBE, sensors at 50/10 Hz in device time, healthy tasks,
   contiguous seq, no bad frames;
2. a stalled sensor task: the supervisor stops kicking, the IWDG resets the MCU, and the
   next boot reports a watchdog reset naming the task, with a new boot id;
3. a slow link: readings are dropped and reported, the sensor task is DEGRADED, and nothing
   resets (sampling never waits on the link).
"""

import argparse
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import ofp  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
FAULT_CTL = 0x2001FE00  # board.h: stall_task +0, comms_delay_ms +4, stop_sensors +8


class Renode:
    def __init__(self, elf: Path, uart_port: int, monitor_port: int) -> None:
        cmd = (
            f"$bin=@{elf}; $repl=@{ROOT / 'firmware/renode/openfleet-f4.repl'}; "
            f"$machine_resc=@{ROOT / 'firmware/renode/openfleet-machine.resc'}; "
            f"$uart_port={uart_port}; include @{ROOT / 'firmware/renode/openfleet.resc'}"
        )
        self.proc = subprocess.Popen(
            [
                shutil.which("renode") or "renode",
                "--disable-gui",
                "-P",
                str(monitor_port),
                "-e",
                cmd,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.uart = _connect(uart_port)
        self.monitor = _connect(monitor_port)
        self.reader = ofp.Reader()

    def command(self, line: str) -> None:
        self.monitor.sendall(line.encode() + b"\n")

    def poke(self, offset: int, value: int) -> None:
        self.command(f"sysbus WriteDoubleWord {FAULT_CTL + offset:#x} {value}")

    def read_for(self, seconds: float) -> list[ofp.Frame]:
        start = len(self.reader.frames)
        end = time.monotonic() + seconds
        self.uart.settimeout(0.2)
        while time.monotonic() < end:
            try:
                data = self.uart.recv(4096)
            except TimeoutError:
                continue
            if not data:
                raise RuntimeError("UART connection closed")
            self.reader.feed(data)
        return self.reader.frames[start:]

    def read_until(self, pred, timeout: float) -> list[ofp.Frame]:
        got: list[ofp.Frame] = []
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            got += self.read_for(0.5)
            if pred(got):
                return got
        raise AssertionError(f"timed out; last frames: {[f.kind for f in got[-10:]]}")

    def close(self) -> None:
        try:
            self.command("quit")
        except OSError:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def _connect(port: int) -> socket.socket:
    end = time.monotonic() + 60  # Renode takes a few seconds to start
    while True:
        try:
            return socket.create_connection(("127.0.0.1", port), timeout=2)
        except OSError:
            if time.monotonic() > end:
                raise
            time.sleep(0.5)


def check(ok: bool, what: str) -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        sys.exit(1)


def rate(frames: list[ofp.Frame], sensor: int) -> float:
    """Readings per second of DEVICE time (uptime_ms), within one boot."""
    xs = [f for f in frames if f.kind == "SENSOR" and f.payload[0] == sensor]
    if len(xs) < 2:
        return 0.0
    return (len(xs) - 1) / ((xs[-1].uptime_ms - xs[0].uptime_ms) / 1000)


def logs(frames: list[ofp.Frame]) -> list[str]:
    return [ofp.log_text(f) for f in frames if f.kind == "LOG"]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--elf", default=str(ROOT / "firmware/build/openfleet-fw.elf"))
    p.add_argument("--uart-port", type=int, default=3456)
    p.add_argument("--monitor-port", type=int, default=1234)
    args = p.parse_args()

    r = Renode(Path(args.elf), args.uart_port, args.monitor_port)
    try:
        print("1. boot")
        wall0 = time.monotonic()
        frames = r.read_for(6)
        wall = time.monotonic() - wall0
        hellos = [f for f in frames if f.kind == "HELLO"]
        check(
            len(hellos) == 1 and ofp.hello(hellos[0]) == ("power-on", "openfleet-fw 0.1"),
            f"HELLO power-on, firmware openfleet-fw 0.1 ({len(hellos)} HELLO)",
        )
        boot_id = hellos[0].boot_id
        check(
            all(f.device_id == 2 and f.boot_id == boot_id for f in frames),
            f"device 2, boot id {boot_id} on every frame",
        )
        names = {ofp.describe(f)[3]: ofp.describe(f)[2] for f in frames if f.kind == "DESCRIBE"}
        check(
            {"imu": 50.0, "range": 10.0}.items() <= names.items()
            and {"sensor", "comms", "health"} <= names.keys(),
            f"DESCRIBE {names}",
        )
        imu, rng = rate(frames, 1), rate(frames, 2)
        check(
            abs(imu - 50) < 1 and abs(rng - 10) < 0.5,
            f"rates in device time: imu {imu:.2f} Hz, range {rng:.2f} Hz",
        )
        uptime_s = (frames[-1].uptime_ms - frames[0].uptime_ms) / 1000
        print(
            f"  info: {uptime_s:.1f} s of device time in {wall:.1f} s wall "
            f"(emulation speed {uptime_s / wall:.2f}x real time on this host)"
        )
        tasks = [h for f in frames if f.kind == "HEALTH" for h in ofp.health(f)]
        check(tasks and all(state == 0 for _, state, _ in tasks), "HEALTH: every task RUNNING")
        print(f"  info: lowest free stack seen {min(s for _, _, s in tasks)} words")
        seqs = [f.seq for f in frames]
        check(
            seqs == list(range(seqs[0], seqs[0] + len(seqs))) and r.reader.errors == 0,
            f"{len(frames)} frames, seq contiguous, {r.reader.errors} bad",
        )

        print("2. the sensor task stalls")
        r.poke(0, 1)
        got = r.read_until(lambda fs: any(f.kind == "HELLO" for f in fs), timeout=15)
        check(
            any("watchdog: task sensor stopped checking in" in t for t in logs(got)),
            "the watchdog task reported the stall before the reset",
        )
        reboot = next(f for f in got if f.kind == "HELLO")
        check(
            ofp.hello(reboot)[0] == "watchdog" and reboot.boot_id == (boot_id + 1) & 0xFFFF,
            f"rebooted: HELLO watchdog, boot id {reboot.boot_id}",
        )
        after = got[got.index(reboot) :] + r.read_for(4)  # the new boot's first frames on
        got = after
        check(
            any("previous boot ended by the watchdog: task sensor" in t for t in logs(got)),
            "the new boot names the stalled task",
        )
        check(abs(rate(got, 1) - 50) < 1, f"sampling again at {rate(got, 1):.2f} Hz")
        msg = next(t for t in logs(got) if "previous boot" in t)
        print(f"  info: {msg}")

        print("3. a slow link")
        r.poke(4, 60)  # 60 ms per frame: ~17 frames/s against 60 readings/s
        got = r.read_for(6)
        check(not any(f.kind == "HELLO" for f in got), "no reset: sampling never waited")
        check(any("readings dropped" in t for t in logs(got)), "dropped readings reported")
        degraded = [s for f in got if f.kind == "HEALTH" for (t, s, _) in ofp.health(f) if t == 1]
        check(1 in degraded, "sensor task reported DEGRADED")
        r.poke(4, 0)
        got = r.read_for(4)
        check(abs(rate(got[len(got) // 2 :], 1) - 50) < 2, "recovered when the link is fast again")
        print("PASS: firmware on Renode")
    finally:
        r.close()


if __name__ == "__main__":
    main()
