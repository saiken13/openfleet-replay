/*
 * libFuzzer target for the device protocol decoder (docs/PROTOCOL.md). Built with
 * `cmake --preset fuzz` (clang, ASan + UBSan); CI runs it for a fixed time.
 *
 * Properties checked on arbitrary input fed in arbitrary fragments:
 * - no crash, no out-of-bounds access, no undefined behaviour (the sanitizers);
 * - every frame the decoder accepts re-encodes to a frame that decodes identically;
 * - every payload parser either rejects the frame or yields bounded, NUL-terminated data.
 */
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "openfleet/protocol.h"

static void check_frame(const ofp_frame *f) {
  if (f->header.length > OFP_MAX_PAYLOAD) {
    abort();
  }
  uint8_t wire[OFP_MAX_WIRE];
  const size_t n = ofp_encode(&f->header, f->payload, wire, sizeof wire);
  if (n == 0 || wire[n - 1] != 0) {
    abort();
  }
  ofp_frame again;
  if (ofp_decode_frame(wire, n - 1, &again) != OFP_FRAME ||
      memcmp(&again.header, &f->header, sizeof f->header) != 0 ||
      memcmp(again.payload, f->payload, f->header.length) != 0) {
    abort();
  }

  ofp_hello hello;
  ofp_describe desc;
  ofp_sensor sensor;
  ofp_health health;
  ofp_log log;
  if (ofp_parse_hello(f, &hello) && strlen(hello.firmware) > OFP_MAX_TEXT) {
    abort();
  }
  if (ofp_parse_describe(f, &desc) && !ofp_valid_name(desc.name, strlen(desc.name))) {
    abort();
  }
  (void)ofp_parse_sensor(f, &sensor);
  if (ofp_parse_health(f, &health) && health.count > OFP_MAX_HEALTH) {
    abort();
  }
  if (ofp_parse_log(f, &log) && (strlen(log.text) > OFP_MAX_TEXT || log.level > OFP_LOG_FATAL)) {
    abort();
  }
}

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size);

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size) {
  ofp_decoder d;
  ofp_decoder_init(&d);
  ofp_frame f;
  for (size_t i = 0; i < size; ++i) {
    if (ofp_decoder_push(&d, data[i], &f) == OFP_FRAME) {
      check_frame(&f);
    }
  }
  /* The same bytes as one frame, ignoring delimiters' meaning. */
  if (size > 0 && size <= OFP_MAX_ENCODED && ofp_decode_frame(data, size, &f) == OFP_FRAME) {
    check_frame(&f);
  }
  return 0;
}
