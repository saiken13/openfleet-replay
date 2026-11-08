/* Writes a seed corpus for fuzz_decoder: one valid wire frame of each type, and a stream of
 * several. Seeds let the fuzzer start from frames that pass the CRC instead of having to
 * find a valid checksum by chance.   make_seeds DIR */
#include <stdio.h>
#include <string.h>

#include "openfleet/protocol.h"

static int write_file(const char *dir, const char *name, const uint8_t *data, size_t len) {
  char path[512];
  snprintf(path, sizeof path, "%s/%s", dir, name);
  FILE *f = fopen(path, "wb");
  if (f == NULL || fwrite(data, 1, len, f) != len) {
    perror(path);
    return 1;
  }
  return fclose(f) != 0;
}

int main(int argc, char **argv) {
  if (argc != 2) {
    fprintf(stderr, "usage: make_seeds DIR\n");
    return 2;
  }
  uint8_t p[OFP_MAX_PAYLOAD];
  uint8_t wire[OFP_MAX_WIRE];
  uint8_t stream[OFP_MAX_WIRE * 5];
  size_t stream_len = 0;
  ofp_header h = {0};
  h.device_id = 1;
  h.boot_id = 2;
  h.uptime_ms = 1000;

  ofp_describe d = {OFP_KIND_SENSOR, 1, 500, "imu"};
  ofp_health health = {2, {{1, OFP_TASK_RUNNING, 100}, {2, OFP_TASK_ERROR, 3}}};
  struct {
    const char *name;
    uint8_t type;
    size_t len;
  } seeds[5];
  seeds[0].name = "hello"; seeds[0].type = OFP_HELLO;
  seeds[1].name = "sensor"; seeds[1].type = OFP_SENSOR;
  seeds[2].name = "health"; seeds[2].type = OFP_HEALTH;
  seeds[3].name = "log"; seeds[3].type = OFP_LOG;
  seeds[4].name = "describe"; seeds[4].type = OFP_DESCRIBE;
  int rc = 0;
  for (unsigned i = 0; i < 5; ++i) {
    switch (seeds[i].type) {
      case OFP_HELLO: seeds[i].len = ofp_build_hello(p, sizeof p, OFP_RESET_WATCHDOG, "fw 1"); break;
      case OFP_SENSOR: seeds[i].len = ofp_build_sensor(p, sizeof p, 1, -5); break;
      case OFP_HEALTH: seeds[i].len = ofp_build_health(p, sizeof p, &health); break;
      case OFP_LOG: seeds[i].len = ofp_build_log(p, sizeof p, OFP_LOG_WARN, "low battery"); break;
      default: seeds[i].len = ofp_build_describe(p, sizeof p, &d); break;
    }
    h.type = seeds[i].type;
    h.seq = (uint16_t)i;
    h.length = (uint16_t)seeds[i].len;
    const size_t n = ofp_encode(&h, p, wire, sizeof wire);
    rc |= write_file(argv[1], seeds[i].name, wire, n);
    memcpy(stream + stream_len, wire, n);
    stream_len += n;
  }
  rc |= write_file(argv[1], "stream", stream, stream_len);
  return rc;
}
