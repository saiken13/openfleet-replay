"""Robot Framework keywords for the firmware's device-protocol UART (docs/FIRMWARE.md).

Renode's own UART keywords are line-oriented; this link is binary (COBS frames), so the
suite captures USART2 to a file (`CreateFileBackend`) and these keywords decode it with the
independent Python reader `firmware/tests/ofp.py`. Because the suite advances the emulation
with `emulation RunFor`, everything here is a function of emulated time: the same firmware
gives the same frames on every run, on any host.
"""

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
import ofp  # noqa: E402


class OfpKeywords:
    ROBOT_LIBRARY_SCOPE = "SUITE"

    def __init__(self) -> None:
        self._path: Path | None = None
        self._offset = 0
        self._reader = ofp.Reader()
        self._metrics: dict[str, dict] = {}

    # --- capture ------------------------------------------------------------------------

    def new_uart_capture(self) -> str:
        """A fresh, empty capture file; pass it to `sysbus.usart2 CreateFileBackend`."""
        fd, name = tempfile.mkstemp(prefix="ofp-uart-", suffix=".bin")
        os.close(fd)
        self._path = Path(name)
        self._offset = 0
        self._reader = ofp.Reader()
        return name

    def read_new_frames(self) -> list:
        """Frames completed since the last call (bytes of a frame still in flight stay
        buffered for the next call)."""
        assert self._path is not None, "call New Uart Capture first"
        data = self._path.read_bytes()[self._offset :]
        self._offset += len(data)
        start = len(self._reader.frames)
        self._reader.feed(data)
        return self._reader.frames[start:]

    def capture_digest(self) -> str:
        """SHA-256 of everything captured so far (for determinism checks)."""
        assert self._path is not None
        return hashlib.sha256(self._path.read_bytes()).hexdigest()

    def bad_frames_should_be_zero(self) -> None:
        if self._reader.errors:
            raise AssertionError(f"{self._reader.errors} frames failed to decode")

    # --- queries ------------------------------------------------------------------------

    def frames_of_kind(self, frames: list, kind: str) -> list:
        return [f for f in frames if f.kind == kind]

    def count_sensor_readings(self, frames: list, sensor_id: int) -> int:
        sensor_id = int(sensor_id)
        return sum(1 for f in frames if f.kind == "SENSOR" and f.payload[0] == sensor_id)

    def reset_reasons(self, frames: list) -> list:
        """The reset reason of each HELLO, in order (e.g. ['power-on'])."""
        return [ofp.hello(f)[0] for f in frames if f.kind == "HELLO"]

    def firmware_version(self, frames: list) -> str:
        return next(ofp.hello(f)[1] for f in frames if f.kind == "HELLO")

    def boot_ids(self, frames: list) -> list:
        """Distinct boot ids, in order of appearance."""
        seen: list[int] = []
        for f in frames:
            if not seen or seen[-1] != f.boot_id:
                seen.append(f.boot_id)
        return seen

    def described_names(self, frames: list) -> dict:
        """{name: expected Hz} from DESCRIBE frames (tasks have 0)."""
        return {ofp.describe(f)[3]: ofp.describe(f)[2] for f in frames if f.kind == "DESCRIBE"}

    def task_states(self, frames: list, task_id: int) -> list:
        """Every reported state of one task (0 RUNNING, 1 DEGRADED, 2 STOPPED, 3 ERROR)."""
        task_id = int(task_id)
        return [
            s for f in frames if f.kind == "HEALTH" for t, s, _ in ofp.health(f) if t == task_id
        ]

    def lowest_free_stack(self, frames: list) -> int:
        return min(free for f in frames if f.kind == "HEALTH" for _, _, free in ofp.health(f))

    def logs_should_contain(self, frames: list, fragment: str) -> None:
        texts = [ofp.log_text(f) for f in frames if f.kind == "LOG"]
        if not any(fragment in t for t in texts):
            raise AssertionError(f"no LOG contains {fragment!r}; logs: {texts}")

    def seq_should_be_contiguous(self, frames: list) -> None:
        """Within each boot, seq rises by exactly 1 per frame (mod 2^16)."""
        prev: dict[int, int] = {}
        for f in frames:
            if f.boot_id in prev and f.seq != (prev[f.boot_id] + 1) & 0xFFFF:
                raise AssertionError(
                    f"boot {f.boot_id}: seq {prev[f.boot_id]} then {f.seq} (lost or reordered)"
                )
            prev[f.boot_id] = f.seq

    def new_boot_sensor_readings(self, frames: list, old_boot_id: int) -> int:
        """SENSOR frames from a boot other than `old_boot_id`."""
        old = int(old_boot_id)
        return sum(1 for f in frames if f.kind == "SENSOR" and f.boot_id != old)

    def device_uptime_span_ms(self, frames: list) -> int:
        return frames[-1].uptime_ms - frames[0].uptime_ms

    # --- metrics ------------------------------------------------------------------------

    def record_metric(self, name: str, value, unit: str, what: str = "") -> None:
        """Kept for the nightly report; written by `Write Metrics`."""
        self._metrics[name] = {"value": float(value), "unit": unit, "what": what}

    def write_metrics(self, path: str = "") -> str:
        """Writes the recorded metrics as JSON, labelled as emulated (Renode)."""
        target = Path(path or os.environ.get("OFP_METRICS", "firmware-metrics.json"))
        target.write_text(
            json.dumps(
                {
                    "suite": "firmware_robot",
                    "env": "renode",
                    "note": "Renode STM32F4 model; not cycle-accurate. Instruction counts "
                    "are executed instructions, not cycles.",
                    "metrics": self._metrics,
                },
                indent=2,
            )
            + "\n"
        )
        return str(target)
