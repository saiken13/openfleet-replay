#include "boot_record.h"

#include "openfleet/protocol.h"

#define NO_REASON 0xFFu

static uint32_t seal_of(const boot_record *r) {
  return ~(r->magic ^ r->boot_count ^ r->last_uptime_ms ^ (uint32_t)r->boot_id ^
           ((uint32_t)r->pending_reason << 16) ^ ((uint32_t)r->stalled_task << 24));
}

static void reseal(boot_record *r) { r->seal = seal_of(r); }

static bool valid(const boot_record *r) {
  return r->magic == BOOT_RECORD_MAGIC && r->seal == seal_of(r);
}

boot_info boot_record_on_boot(boot_record *r, uint32_t rcc_csr, uint32_t random) {
  boot_info info = {0};
  if (!valid(r)) { /* power-on: RAM holds noise */
    r->magic = BOOT_RECORD_MAGIC;
    r->boot_count = 0;
    r->boot_id = (uint16_t)(random & 0xFFFFu);
    r->last_uptime_ms = 0;
    info.reason = OFP_RESET_POWER_ON;
  } else {
    r->boot_id = (uint16_t)(r->boot_id + 1u);
    info.prev_uptime_ms = r->last_uptime_ms;
    if (r->pending_reason != NO_REASON) {
      info.reason = r->pending_reason;
      info.stalled_task = r->stalled_task;
      info.stall_known = r->pending_reason == OFP_RESET_WATCHDOG;
    } else if (rcc_csr & (RCC_CSR_IWDGRSTF | RCC_CSR_WWDGRSTF)) {
      info.reason = OFP_RESET_WATCHDOG; /* a watchdog the supervisor didn't arm (a hang) */
    } else if (rcc_csr & RCC_CSR_SFTRSTF) {
      info.reason = OFP_RESET_SOFTWARE;
    } else if (rcc_csr & RCC_CSR_PORRSTF) {
      info.reason = OFP_RESET_POWER_ON;
    } else if (rcc_csr & RCC_CSR_PINRSTF) {
      info.reason = OFP_RESET_PIN;
    } else {
      info.reason = OFP_RESET_UNKNOWN;
    }
  }
  if (r->boot_id == 0) {
    r->boot_id = 1; /* 0 means "no boot id" in the protocol */
  }
  r->boot_count++;
  r->pending_reason = NO_REASON;
  r->stalled_task = 0;
  r->last_uptime_ms = 0;
  reseal(r);
  info.boot_id = r->boot_id;
  info.boot_count = r->boot_count;
  return info;
}

void boot_record_note_watchdog(boot_record *r, uint8_t stalled_task, uint32_t uptime_ms) {
  r->pending_reason = OFP_RESET_WATCHDOG;
  r->stalled_task = stalled_task;
  r->last_uptime_ms = uptime_ms;
  reseal(r);
}

void boot_record_note_uptime(boot_record *r, uint32_t uptime_ms) {
  r->last_uptime_ms = uptime_ms;
  reseal(r);
}
