/*
 * OpenFleet device protocol (OFP): the binary link between a microcontroller and the edge
 * agent's gateway. Specification: docs/PROTOCOL.md.
 *
 * C11, no dynamic allocation, no I/O, no global state: the same code builds for the
 * firmware (Cortex-M4, Phase 3) and for the agent (Linux). Every function is bounded by its
 * buffer sizes.
 *
 * Wire: COBS(header | payload | crc16) 0x00
 *   header (14 bytes, little-endian): version u8, type u8, device_id u16, seq u16,
 *                                     boot_id u16, uptime_ms u32, length u16
 *   crc16: CRC-16/CCITT-FALSE over header and payload, little-endian
 */
#ifndef OPENFLEET_PROTOCOL_H
#define OPENFLEET_PROTOCOL_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define OFP_VERSION 1u
#define OFP_HEADER_SIZE 14u
#define OFP_CRC_SIZE 2u
#define OFP_MAX_PAYLOAD 240u
#define OFP_MAX_RAW (OFP_HEADER_SIZE + OFP_MAX_PAYLOAD + OFP_CRC_SIZE) /* 256 */
/* COBS adds one byte per 254 bytes of input, plus one. */
#define OFP_MAX_ENCODED (OFP_MAX_RAW + OFP_MAX_RAW / 254u + 1u) /* 258 */
#define OFP_MAX_WIRE (OFP_MAX_ENCODED + 1u)                     /* + the 0x00 delimiter */

#define OFP_MAX_NAME 32u      /* sensor and task names */
#define OFP_MAX_HEALTH 16u    /* task entries per HEALTH frame */
#define OFP_MAX_TEXT 200u     /* LOG text and HELLO firmware version */

typedef enum {
  OFP_HELLO = 1,    /* first frame after boot: why the device reset, firmware version */
  OFP_SENSOR = 2,   /* one reading of one sensor */
  OFP_HEALTH = 3,   /* state and stack headroom of each task */
  OFP_LOG = 4,      /* a log line */
  OFP_DESCRIBE = 5  /* name and expected rate of a sensor or task; repeated periodically */
} ofp_type;

typedef enum {
  OFP_RESET_POWER_ON = 0,
  OFP_RESET_WATCHDOG = 1,
  OFP_RESET_SOFTWARE = 2,
  OFP_RESET_BROWNOUT = 3,
  OFP_RESET_PIN = 4,
  OFP_RESET_UNKNOWN = 255
} ofp_reset_reason;

typedef enum { OFP_KIND_SENSOR = 1, OFP_KIND_TASK = 2 } ofp_kind;

typedef enum {
  OFP_TASK_RUNNING = 0,
  OFP_TASK_DEGRADED = 1,
  OFP_TASK_STOPPED = 2,
  OFP_TASK_ERROR = 3
} ofp_task_state;

typedef enum {
  OFP_LOG_DEBUG = 0,
  OFP_LOG_INFO = 1,
  OFP_LOG_WARN = 2,
  OFP_LOG_ERROR = 3,
  OFP_LOG_FATAL = 4
} ofp_log_level;

typedef struct {
  uint8_t version;
  uint8_t type;
  uint16_t device_id;
  uint16_t seq;        /* per boot, +1 per frame, wraps */
  uint16_t boot_id;    /* changes on every boot (counter or random); 0 = not supported */
  uint32_t uptime_ms;  /* device clock since boot */
  uint16_t length;     /* payload bytes */
} ofp_header;

typedef struct {
  ofp_header header;
  uint8_t payload[OFP_MAX_PAYLOAD];
} ofp_frame;

/* --- primitives --------------------------------------------------------------------- */

/* CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection, no final xor.
 * Check value: "123456789" -> 0x29B1. Detects every 1- and 2-bit error and every burst of
 * up to 16 bits in frames this size. */
uint16_t ofp_crc16(const uint8_t *data, size_t len);

/* Consistent Overhead Byte Stuffing: the output never contains 0x00, so 0x00 can delimit
 * frames and a receiver resynchronises at the next delimiter. Returns the bytes written, or
 * 0 if `out_cap` is too small. */
size_t ofp_cobs_encode(const uint8_t *in, size_t len, uint8_t *out, size_t out_cap);

/* Decodes into `out` and stores the size in *out_len. Returns false if the input is not
 * valid COBS (a 0x00 byte, or a code that runs past the end) or `out_cap` is too small. */
bool ofp_cobs_decode(const uint8_t *in, size_t len, uint8_t *out, size_t out_cap,
                     size_t *out_len);

/* --- frames ------------------------------------------------------------------------- */

/* Encodes a frame to wire bytes, delimiter included. `h->length` is the payload size and
 * `h->version` is ignored (always OFP_VERSION). Returns the bytes written, or 0 if the
 * payload is too long or `out_cap` < the encoded size (OFP_MAX_WIRE is always enough). */
size_t ofp_encode(const ofp_header *h, const uint8_t *payload, uint8_t *out, size_t out_cap);

typedef enum {
  OFP_NONE = 0,        /* need more bytes */
  OFP_FRAME,           /* a valid frame was written to *out */
  OFP_ERR_OVERFLOW,    /* longer than OFP_MAX_ENCODED before a delimiter: discarded */
  OFP_ERR_COBS,        /* not valid COBS */
  OFP_ERR_SHORT,       /* shorter than a header + CRC (e.g. truncated) */
  OFP_ERR_CRC,         /* checksum mismatch (corrupted or truncated) */
  OFP_ERR_VERSION,     /* unknown protocol version */
  OFP_ERR_LENGTH       /* header length disagrees with the bytes received */
} ofp_result;

const char *ofp_result_name(ofp_result r);

/* Streaming decoder. Fixed buffer; feed it bytes in any fragmentation. A frame is judged
 * when its delimiter arrives, so one bad frame never costs the next one. */
typedef struct {
  uint8_t buf[OFP_MAX_ENCODED];
  size_t len;
  bool overflow;
} ofp_decoder;

void ofp_decoder_init(ofp_decoder *d);
ofp_result ofp_decoder_push(ofp_decoder *d, uint8_t byte, ofp_frame *out);

/* Decodes one complete COBS frame (without its delimiter). */
ofp_result ofp_decode_frame(const uint8_t *encoded, size_t len, ofp_frame *out);

/* --- payloads ----------------------------------------------------------------------- */
/* Builders return the payload length (0 if it doesn't fit). Parsers validate everything
 * and return false for anything malformed. Strings are NOT NUL-terminated on the wire;
 * parsers NUL-terminate their copies. */

typedef struct {
  uint8_t reset_reason;
  char firmware[OFP_MAX_TEXT + 1];
} ofp_hello;

typedef struct {
  uint8_t kind;           /* ofp_kind */
  uint8_t id;
  uint16_t expected_dhz;  /* expected rate in 0.1 Hz (sensors); 0 = no expectation */
  char name[OFP_MAX_NAME + 1];  /* [A-Za-z0-9_-], 1..32 chars */
} ofp_describe;

typedef struct {
  uint8_t sensor_id;
  int32_t value;  /* sensor-specific units (e.g. milli-g, mm) */
} ofp_sensor;

typedef struct {
  uint8_t task_id;
  uint8_t state;             /* ofp_task_state */
  uint16_t stack_free_words; /* lowest free stack seen (high-water mark) */
} ofp_task_health;

typedef struct {
  uint8_t count;
  ofp_task_health tasks[OFP_MAX_HEALTH];
} ofp_health;

typedef struct {
  uint8_t level;  /* ofp_log_level */
  char text[OFP_MAX_TEXT + 1];
} ofp_log;

size_t ofp_build_hello(uint8_t *p, size_t cap, uint8_t reset_reason, const char *firmware);
size_t ofp_build_describe(uint8_t *p, size_t cap, const ofp_describe *d);
size_t ofp_build_sensor(uint8_t *p, size_t cap, uint8_t sensor_id, int32_t value);
size_t ofp_build_health(uint8_t *p, size_t cap, const ofp_health *h);
size_t ofp_build_log(uint8_t *p, size_t cap, uint8_t level, const char *text);

bool ofp_parse_hello(const ofp_frame *f, ofp_hello *out);
bool ofp_parse_describe(const ofp_frame *f, ofp_describe *out);
bool ofp_parse_sensor(const ofp_frame *f, ofp_sensor *out);
bool ofp_parse_health(const ofp_frame *f, ofp_health *out);
bool ofp_parse_log(const ofp_frame *f, ofp_log *out);

/* A valid sensor or task name: 1..32 of [A-Za-z0-9_-]. */
bool ofp_valid_name(const char *s, size_t len);

#ifdef __cplusplus
}
#endif

#endif /* OPENFLEET_PROTOCOL_H */
