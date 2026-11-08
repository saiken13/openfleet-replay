# OpenFleet device protocol (OFP) v1

The binary link between a microcontroller and the edge agent's gateway, over a UART, a pty or TCP. It's implemented once, in C11, in [`protocol/`](../protocol). The agent (`edge/`) and the firmware (Phase 3) use the same code. Design rationale: [DECISIONS #033](DECISIONS.md).

Status: implemented and tested on the host only, against a simulated device (`openfleet-device-sim`). No physical hardware has run it.

## Framing

```
wire  = COBS( header | payload | crc16 ) 0x00
```

- **COBS** (Consistent Overhead Byte Stuffing) removes every `0x00` from the frame, so `0x00` delimits frames.
  - A receiver that joins mid-stream, or loses bytes, resynchronises at the next delimiter, and one bad frame never costs the next.
  - Overhead is 1 byte per 254, plus 1.
- **crc16** is CRC-16/CCITT-FALSE (poly `0x1021`, init `0xFFFF`, no reflection, no final XOR; check value `"123456789"` → `0x29B1`). It covers header and payload and is sent little-endian. It detects all 1- and 2-bit errors and all bursts up to 16 bits.
- **Sizes:** header 14 B, payload ≤ 240 B, raw frame ≤ 256 B, encoded ≤ 258 B, plus the delimiter.

### Header (14 bytes, little-endian)

| Offset | Size | Field | |
|---|---|---|---|
| 0 | 1 | `version` | `1`. Anything else is rejected. |
| 1 | 1 | `type` | See below. Unknown types are counted, not treated as errors (forward compatible). |
| 2 | 2 | `device_id` | Several devices may share one link. |
| 4 | 2 | `seq` | Per boot: +1 per frame, wraps at 65536, starts again after a reset. |
| 6 | 2 | `boot_id` | Changes on every boot (a counter in backup RAM, or random). `0` means not supported. |
| 8 | 4 | `uptime_ms` | Device clock since boot. Wraps after about 49 days. |
| 12 | 2 | `length` | Payload bytes. Must equal what was received. |

## Messages

Strings are not NUL-terminated on the wire and may not contain NUL. Names are 1–32 characters of `[A-Za-z0-9_-]`.

| Type | Payload | Meaning |
|---|---|---|
| `1` HELLO | `reset_reason u8`, `firmware text` (≤ 200) | First frame after boot. Reasons: 0 power-on, 1 watchdog, 2 software, 3 brownout, 4 reset pin, 255 unknown. |
| `2` SENSOR | `sensor_id u8`, `value i32` | One reading. The rate is what OpenFleet monitors; the value's unit is sensor-specific. |
| `3` HEALTH | `count u8` (≤ 16), then `count` × (`task_id u8`, `state u8`, `stack_free_words u16`) | Task states: 0 running, 1 degraded, 2 stopped, 3 error. `stack_free_words` is the lowest free stack seen (high-water mark). Sent periodically (1 s). |
| `4` LOG | `level u8` (0 debug … 4 fatal), `text` (≤ 200) | A log line. |
| `5` DESCRIBE | `kind u8` (1 sensor, 2 task), `id u8`, `expected_dhz u16`, `name` | Names a sensor or task. `expected_dhz` is the expected rate in 0.1 Hz (0 = none). Sent after boot and repeated (5 s), so a gateway that starts later learns the names. |

## What the gateway does with it

The gateway lives in the agent: `edge/src/gateway.cpp`. It measures and maps onto OpenFleet's existing model; the backend judges (no backend change):

| Device | OpenFleet |
|---|---|
| SENSOR frames of sensor *n* | topic `/mcu<device>/<name>`: actual rate per window, expected rate from DESCRIBE, age of the last reading. A sensor that stops reports 0 Hz, so `TopicRateRule` opens "stopped". |
| HEALTH task states | node `mcu<device>/<task>`, so `NodeHealthRule` covers a failed task |
| the device itself | node `mcu<device>`. RUNNING; DEGRADED when ≥ 25 % of a window's frames were bad or HEALTH stopped arriving; STOPPED when silent for 3 s (`--device-silence`). A silent device's tasks are left out (unknown), not reported as still running. |
| HELLO, or a new `boot_id`, or `uptime_ms` going back > 1 s | a **reset** log event, with the reason when HELLO arrived. WARN for watchdog, brownout and unknown. |
| LOG | a log event, source `mcu<device>`. At most 50 per window; the rest are counted. |
| HEALTH `stack_free_words` < 16 | one WARN log per task per boot |
| bad frames | counted by kind. A WARN summary at most every 10 s gives counts per kind, plus lost, duplicate and good frames. |

**Duplicates and loss:**
- Per device and boot, the gateway keeps the highest `seq` and a 64-frame bitmap of the frames before it.
- A `seq` already seen is a **duplicate** and is dropped. One more than 64 behind is **stale** and is dropped.
- A jump forward counts the skipped frames as **lost**. A late frame inside the window is accepted and un-counts itself.
- A reset starts a fresh window, so a `seq` restart is never mistaken for 65 000 lost frames.

## The decoder

`ofp_decoder_push()` takes one byte at a time, into a fixed 258-byte buffer, with no allocation. A frame is judged when its delimiter arrives:

| Result | Cause |
|---|---|
| `OFP_FRAME` | Valid; written to the caller's `ofp_frame` |
| `OFP_ERR_OVERFLOW` | More than 258 bytes without a delimiter. The rest is discarded up to the next delimiter. |
| `OFP_ERR_COBS` | Not valid COBS |
| `OFP_ERR_SHORT` | Shorter than header + CRC (typically truncated) |
| `OFP_ERR_CRC` | Checksum mismatch (corrupted, or truncated and merged with the next frame) |
| `OFP_ERR_VERSION` / `OFP_ERR_LENGTH` | Valid CRC but an unknown version, or a length that disagrees with the bytes received |

Empty frames (back-to-back delimiters) are idle line, not errors. Payload parsers (`ofp_parse_*`) validate type, exact lengths, counts, enum ranges, names and text before anything is copied out.

## Transports

`--device` on the agent:
- `tcp://HOST:PORT`: Renode's UART server socket (Phase 3) and the simulator.
- `serial:/dev/PATH[@BAUD]`: a UART adapter or a pty. Raw 8N1, default 115200.

Reads use `poll()` with a 200 ms timeout. A dropped link is reopened with backoff (0.5–5 s) and logged as an event.

## Testing (all simulated)

- **`protocol/tests/protocol_test.cpp`:**
  - The CRC check value and COBS reference vectors.
  - Round trips for every length 0–600 at several zero densities.
  - Every single-bit flip of a frame is rejected, and the next frame survives.
  - Every truncation length, oversize input, 200 kB of random noise, bad version or length, and payload validation.
- **`edge/tests/gateway_test.cpp`:** the gateway against `DeviceSim` on simulated time.
  - Mapping and expected rates; a stopped sensor; task states and low stack.
  - Resets with HELLO, without HELLO, and without boot ids (uptime).
  - Duplicates, and the accounting identity received + lost = sent.
  - Corruption and truncation counted and never accepted; garbage; oversize; fragmentation; silence; log caps; concurrent use under TSan.
- **`edge/tests/device_link_test.cpp`:** URI parsing, a socketpair, TCP with reconnect, and a pty in raw mode.
- **Fuzzing (libFuzzer + ASan + UBSan, `cmake --preset fuzz`):**
  - `protocol/fuzz/fuzz_decoder.c`: arbitrary bytes. Every accepted frame must re-encode identically, and every parser must reject or return bounded data.
  - `edge/tests/fuzz_gateway.cpp`: a structured script of well-formed frames with fuzzed fields, junk and windows.
  - CI runs each for 45 s from seeds made by `make_seeds`.
- **`edge/e2e/device_e2e.py`:** simulator → TCP → agent → backend on the compose stack. It covers a stopped sensor (incident), resets (events), a bad link (counted; the agent keeps running), silence and a failed task (incidents), and a reconnect.

### Device simulator

`openfleet-device-sim --listen PORT | --pty PATH`: see `--help`. It runs control commands given with `--command` at start, or appended to `--control-file`:

```
stop-sensor ID | start-sensor ID | task ID running|degraded|stopped|error | stack ID WORDS
reset [power|watchdog|software|brownout|pin] | reset-quiet (its HELLO is lost) | silence SECONDS
corrupt P | truncate P | drop P | duplicate P | fragment on|off | faults off
garbage N | oversize | log LEVEL TEXT
```

Faults use a seeded RNG, so a given seed and command sequence gives the same bytes.

## Limits
- There is no authentication or encryption on the device link. It's a local wire inside the robot; the agent → backend leg is where keys and TLS apply.
- No retransmission: lost device frames are counted, not recovered. Rates are measured over what arrives.
- The protocol's timing assumptions (1 s windows, 3 s silence) have been exercised only in simulation. Nothing here is a real-time guarantee.
