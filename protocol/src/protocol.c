#include "openfleet/protocol.h"

#include <string.h>

/* --- little-endian helpers ---------------------------------------------------------- */

static void put_u16(uint8_t *p, uint16_t v) {
  p[0] = (uint8_t)(v & 0xFFu);
  p[1] = (uint8_t)(v >> 8);
}

static void put_u32(uint8_t *p, uint32_t v) {
  for (unsigned i = 0; i < 4u; ++i) {
    p[i] = (uint8_t)((v >> (8u * i)) & 0xFFu);
  }
}

static uint16_t get_u16(const uint8_t *p) { return (uint16_t)(p[0] | (uint16_t)(p[1] << 8)); }

static uint32_t get_u32(const uint8_t *p) {
  return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) |
         ((uint32_t)p[3] << 24);
}

/* strnlen is POSIX, not C11. */
static size_t bounded_len(const char *s, size_t max) {
  size_t n = 0;
  while (n < max && s[n] != '\0') {
    ++n;
  }
  return n;
}

/* --- primitives --------------------------------------------------------------------- */

uint16_t ofp_crc16(const uint8_t *data, size_t len) {
  unsigned crc = 0xFFFFu; /* unsigned arithmetic, masked to 16 bits: no implicit narrowing */
  for (size_t i = 0; i < len; ++i) {
    crc ^= (unsigned)data[i] << 8;
    for (unsigned bit = 0; bit < 8u; ++bit) {
      crc = (crc & 0x8000u) ? ((crc << 1) ^ 0x1021u) : (crc << 1);
      crc &= 0xFFFFu;
    }
  }
  return (uint16_t)crc;
}

size_t ofp_cobs_encode(const uint8_t *in, size_t len, uint8_t *out, size_t out_cap) {
  if (out_cap < len + len / 254u + 1u) {
    return 0;
  }
  size_t code_pos = 0;
  size_t o = 1;
  uint8_t code = 1;
  for (size_t i = 0; i < len; ++i) {
    if (in[i] == 0) {
      out[code_pos] = code;
      code_pos = o++;
      code = 1;
    } else {
      out[o++] = in[i];
      ++code;
      if (code == 0xFFu) { /* a full block of 254 non-zero bytes */
        out[code_pos] = code;
        code_pos = o++;
        code = 1;
      }
    }
  }
  out[code_pos] = code;
  return o;
}

bool ofp_cobs_decode(const uint8_t *in, size_t len, uint8_t *out, size_t out_cap,
                     size_t *out_len) {
  size_t i = 0;
  size_t o = 0;
  while (i < len) {
    const uint8_t code = in[i++];
    if (code == 0) {
      return false;
    }
    for (uint8_t k = 1; k < code; ++k) {
      if (i >= len || in[i] == 0 || o >= out_cap) {
        return false;
      }
      out[o++] = in[i++];
    }
    /* A block shorter than 254 bytes stood for a zero, except at the very end. */
    if (code != 0xFFu && i < len) {
      if (o >= out_cap) {
        return false;
      }
      out[o++] = 0;
    }
  }
  *out_len = o;
  return true;
}

/* --- frames ------------------------------------------------------------------------- */

size_t ofp_encode(const ofp_header *h, const uint8_t *payload, uint8_t *out, size_t out_cap) {
  if (h->length > OFP_MAX_PAYLOAD || (h->length > 0 && payload == NULL)) {
    return 0;
  }
  uint8_t raw[OFP_MAX_RAW];
  raw[0] = (uint8_t)OFP_VERSION;
  raw[1] = h->type;
  put_u16(raw + 2, h->device_id);
  put_u16(raw + 4, h->seq);
  put_u16(raw + 6, h->boot_id);
  put_u32(raw + 8, h->uptime_ms);
  put_u16(raw + 12, h->length);
  if (h->length > 0) {
    memcpy(raw + OFP_HEADER_SIZE, payload, h->length);
  }
  const size_t body = OFP_HEADER_SIZE + h->length;
  put_u16(raw + body, ofp_crc16(raw, body));
  const size_t n = ofp_cobs_encode(raw, body + OFP_CRC_SIZE, out, out_cap);
  if (n == 0 || n >= out_cap) {
    return 0; /* no room for the delimiter */
  }
  out[n] = 0;
  return n + 1;
}

const char *ofp_result_name(ofp_result r) {
  switch (r) {
    case OFP_NONE: return "none";
    case OFP_FRAME: return "frame";
    case OFP_ERR_OVERFLOW: return "overflow";
    case OFP_ERR_COBS: return "cobs";
    case OFP_ERR_SHORT: return "short";
    case OFP_ERR_CRC: return "crc";
    case OFP_ERR_VERSION: return "version";
    case OFP_ERR_LENGTH: return "length";
  }
  return "unknown";
}

ofp_result ofp_decode_frame(const uint8_t *encoded, size_t len, ofp_frame *out) {
  uint8_t raw[OFP_MAX_RAW];
  size_t n = 0;
  if (!ofp_cobs_decode(encoded, len, raw, sizeof raw, &n)) {
    return OFP_ERR_COBS;
  }
  if (n < OFP_HEADER_SIZE + OFP_CRC_SIZE) {
    return OFP_ERR_SHORT;
  }
  const size_t body = n - OFP_CRC_SIZE;
  if (ofp_crc16(raw, body) != get_u16(raw + body)) {
    return OFP_ERR_CRC;
  }
  if (raw[0] != OFP_VERSION) {
    return OFP_ERR_VERSION;
  }
  const uint16_t length = get_u16(raw + 12);
  if (length > OFP_MAX_PAYLOAD || (size_t)length != body - OFP_HEADER_SIZE) {
    return OFP_ERR_LENGTH;
  }
  out->header.version = raw[0];
  out->header.type = raw[1];
  out->header.device_id = get_u16(raw + 2);
  out->header.seq = get_u16(raw + 4);
  out->header.boot_id = get_u16(raw + 6);
  out->header.uptime_ms = get_u32(raw + 8);
  out->header.length = length;
  if (length > 0) {
    memcpy(out->payload, raw + OFP_HEADER_SIZE, length);
  }
  return OFP_FRAME;
}

void ofp_decoder_init(ofp_decoder *d) {
  d->len = 0;
  d->overflow = false;
}

ofp_result ofp_decoder_push(ofp_decoder *d, uint8_t byte, ofp_frame *out) {
  if (byte != 0) {
    if (d->len < sizeof d->buf) {
      d->buf[d->len++] = byte;
    } else {
      d->overflow = true; /* keep discarding until the delimiter */
    }
    return OFP_NONE;
  }
  ofp_result r = OFP_NONE; /* an empty frame (back-to-back delimiters) is just idle */
  if (d->overflow) {
    r = OFP_ERR_OVERFLOW;
  } else if (d->len > 0) {
    r = ofp_decode_frame(d->buf, d->len, out);
  }
  d->len = 0;
  d->overflow = false;
  return r;
}

/* --- payloads ----------------------------------------------------------------------- */

bool ofp_valid_name(const char *s, size_t len) {
  if (len == 0 || len > OFP_MAX_NAME) {
    return false;
  }
  for (size_t i = 0; i < len; ++i) {
    const char c = s[i];
    const bool ok = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') ||
                    c == '_' || c == '-';
    if (!ok) {
      return false;
    }
  }
  return true;
}

/* Text on the wire: any bytes except NUL, so the parsed copy is a safe C string. */
static bool valid_text(const uint8_t *s, size_t len) {
  if (len > OFP_MAX_TEXT) {
    return false;
  }
  for (size_t i = 0; i < len; ++i) {
    if (s[i] == 0) {
      return false;
    }
  }
  return true;
}

size_t ofp_build_hello(uint8_t *p, size_t cap, uint8_t reset_reason, const char *firmware) {
  const size_t n = bounded_len(firmware, OFP_MAX_TEXT + 1u);
  if (n > OFP_MAX_TEXT || cap < 1u + n) {
    return 0;
  }
  p[0] = reset_reason;
  memcpy(p + 1, firmware, n);
  return 1u + n;
}

size_t ofp_build_describe(uint8_t *p, size_t cap, const ofp_describe *d) {
  const size_t n = bounded_len(d->name, OFP_MAX_NAME + 1u);
  if ((d->kind != OFP_KIND_SENSOR && d->kind != OFP_KIND_TASK) || !ofp_valid_name(d->name, n) ||
      cap < 4u + n) {
    return 0;
  }
  p[0] = d->kind;
  p[1] = d->id;
  put_u16(p + 2, d->expected_dhz);
  memcpy(p + 4, d->name, n);
  return 4u + n;
}

size_t ofp_build_sensor(uint8_t *p, size_t cap, uint8_t sensor_id, int32_t value) {
  if (cap < 5u) {
    return 0;
  }
  p[0] = sensor_id;
  put_u32(p + 1, (uint32_t)value);
  return 5u;
}

size_t ofp_build_health(uint8_t *p, size_t cap, const ofp_health *h) {
  const size_t n = 1u + 4u * (size_t)h->count;
  if (h->count > OFP_MAX_HEALTH || cap < n) {
    return 0;
  }
  p[0] = h->count;
  for (size_t i = 0; i < h->count; ++i) {
    const ofp_task_health *t = &h->tasks[i];
    if (t->state > OFP_TASK_ERROR) {
      return 0;
    }
    p[1 + 4 * i] = t->task_id;
    p[2 + 4 * i] = t->state;
    put_u16(p + 3 + 4 * i, t->stack_free_words);
  }
  return n;
}

size_t ofp_build_log(uint8_t *p, size_t cap, uint8_t level, const char *text) {
  const size_t n = bounded_len(text, OFP_MAX_TEXT + 1u);
  if (level > OFP_LOG_FATAL || n > OFP_MAX_TEXT || cap < 1u + n) {
    return 0;
  }
  p[0] = level;
  memcpy(p + 1, text, n);
  return 1u + n;
}

bool ofp_parse_hello(const ofp_frame *f, ofp_hello *out) {
  const size_t len = f->header.length;
  if (f->header.type != OFP_HELLO || len < 1u || !valid_text(f->payload + 1, len - 1u)) {
    return false;
  }
  out->reset_reason = f->payload[0];
  memcpy(out->firmware, f->payload + 1, len - 1u);
  out->firmware[len - 1u] = '\0';
  return true;
}

bool ofp_parse_describe(const ofp_frame *f, ofp_describe *out) {
  const size_t len = f->header.length;
  if (f->header.type != OFP_DESCRIBE || len < 5u) {
    return false;
  }
  const uint8_t kind = f->payload[0];
  const char *name = (const char *)(f->payload + 4);
  if ((kind != OFP_KIND_SENSOR && kind != OFP_KIND_TASK) || !ofp_valid_name(name, len - 4u)) {
    return false;
  }
  out->kind = kind;
  out->id = f->payload[1];
  out->expected_dhz = get_u16(f->payload + 2);
  memcpy(out->name, name, len - 4u);
  out->name[len - 4u] = '\0';
  return true;
}

bool ofp_parse_sensor(const ofp_frame *f, ofp_sensor *out) {
  if (f->header.type != OFP_SENSOR || f->header.length != 5u) {
    return false;
  }
  out->sensor_id = f->payload[0];
  out->value = (int32_t)get_u32(f->payload + 1);
  return true;
}

bool ofp_parse_health(const ofp_frame *f, ofp_health *out) {
  const size_t len = f->header.length;
  if (f->header.type != OFP_HEALTH || len < 1u) {
    return false;
  }
  const uint8_t count = f->payload[0];
  if (count > OFP_MAX_HEALTH || len != 1u + 4u * (size_t)count) {
    return false;
  }
  for (size_t i = 0; i < count; ++i) {
    const uint8_t state = f->payload[2 + 4 * i];
    if (state > OFP_TASK_ERROR) {
      return false;
    }
    out->tasks[i].task_id = f->payload[1 + 4 * i];
    out->tasks[i].state = state;
    out->tasks[i].stack_free_words = get_u16(f->payload + 3 + 4 * i);
  }
  out->count = count;
  return true;
}

bool ofp_parse_log(const ofp_frame *f, ofp_log *out) {
  const size_t len = f->header.length;
  if (f->header.type != OFP_LOG || len < 1u || f->payload[0] > OFP_LOG_FATAL ||
      !valid_text(f->payload + 1, len - 1u)) {
    return false;
  }
  out->level = f->payload[0];
  memcpy(out->text, f->payload + 1, len - 1u);
  out->text[len - 1u] = '\0';
  return true;
}
