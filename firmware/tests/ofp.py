"""An independent Python reader for the OpenFleet device protocol, written from
docs/PROTOCOL.md rather than from the C code, so the firmware test also checks that the
specification and the implementation agree. Test tooling only."""

import struct
from dataclasses import dataclass

TYPES = {1: "HELLO", 2: "SENSOR", 3: "HEALTH", 4: "LOG", 5: "DESCRIBE"}
RESETS = {0: "power-on", 1: "watchdog", 2: "software", 3: "brownout", 4: "pin", 255: "unknown"}


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def cobs_decode(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        code = data[i]
        block = data[i + 1 : i + code]
        if code == 0 or len(block) != code - 1 or 0 in block:
            raise ValueError("bad COBS")
        out += block
        i += code
        if code != 0xFF and i < len(data):
            out.append(0)  # a block shorter than 254 bytes stood for a zero
    return bytes(out)


@dataclass
class Frame:
    type: int
    device_id: int
    seq: int
    boot_id: int
    uptime_ms: int
    payload: bytes

    @property
    def kind(self) -> str:
        return TYPES.get(self.type, f"type{self.type}")


def decode(encoded: bytes) -> Frame:
    raw = cobs_decode(encoded)
    if len(raw) < 16:
        raise ValueError("short")
    body, crc = raw[:-2], struct.unpack("<H", raw[-2:])[0]
    if crc16(body) != crc:
        raise ValueError("crc")
    version, ftype, dev, seq, boot, uptime, length = struct.unpack("<BBHHHIH", body[:14])
    if version != 1 or length != len(body) - 14:
        raise ValueError("header")
    return Frame(ftype, dev, seq, boot, uptime, body[14:])


class Reader:
    """Feed bytes; collect frames and count errors."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.frames: list[Frame] = []
        self.errors = 0

    def feed(self, data: bytes) -> None:
        for b in data:
            if b != 0:
                self.buf.append(b)
                continue
            if self.buf:
                try:
                    self.frames.append(decode(bytes(self.buf)))
                except ValueError:
                    self.errors += 1
            self.buf.clear()


def hello(f: Frame) -> tuple[str, str]:
    return RESETS.get(f.payload[0], "?"), f.payload[1:].decode()


def log_text(f: Frame) -> str:
    return f.payload[1:].decode()


def describe(f: Frame) -> tuple[int, int, float, str]:
    kind, ident, dhz = struct.unpack("<BBH", f.payload[:4])
    return kind, ident, dhz / 10, f.payload[4:].decode()


def health(f: Frame) -> list[tuple[int, int, int]]:
    n = f.payload[0]
    return [struct.unpack("<BBH", f.payload[1 + 4 * i : 5 + 4 * i]) for i in range(n)]
