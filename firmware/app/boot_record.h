/*
 * The boot record: a few words in RAM that the startup code does not initialise, so they
 * survive a reset but not a power cycle (docs/FIRMWARE.md). It gives:
 * - a boot id that changes on every boot (random after power-on, then +1), for the
 *   protocol header;
 * - the reason for the last reset. The supervisor records "watchdog, task N stalled" just
 *   before it lets the watchdog fire, so the next boot knows why even where the reset flags
 *   in RCC_CSR aren't available (Renode does not model them). Where they are, they're used
 *   too.
 * A seal word rejects the random contents RAM has after power-on. Pure C, host-tested.
 */
#ifndef OPENFLEET_FW_BOOT_RECORD_H
#define OPENFLEET_FW_BOOT_RECORD_H

#include <stdbool.h>
#include <stdint.h>

#define BOOT_RECORD_MAGIC 0x0F1EB007u

typedef struct {
  uint32_t magic;
  uint32_t boot_count;
  uint32_t last_uptime_ms;
  uint16_t boot_id;
  uint8_t pending_reason; /* ofp_reset_reason to report next boot; 0xFF none */
  uint8_t stalled_task;   /* with OFP_RESET_WATCHDOG: which task stopped checking in */
  uint32_t seal;
} boot_record;

typedef struct {
  uint8_t reason;           /* ofp_reset_reason */
  uint16_t boot_id;
  uint32_t boot_count;      /* boots since power-on, this one included */
  uint8_t stalled_task;     /* valid when reason is watchdog and noted by the supervisor */
  bool stall_known;
  uint32_t prev_uptime_ms;  /* how long the previous boot ran (last noted) */
} boot_info;

/* RCC_CSR reset flags (STM32F4). */
#define RCC_CSR_PINRSTF (1u << 26)
#define RCC_CSR_PORRSTF (1u << 27)
#define RCC_CSR_SFTRSTF (1u << 28)
#define RCC_CSR_IWDGRSTF (1u << 29)
#define RCC_CSR_WWDGRSTF (1u << 30)

/* Call once, early in boot. `random` seeds the boot id after power-on. */
boot_info boot_record_on_boot(boot_record *r, uint32_t rcc_csr, uint32_t random);
/* The supervisor is about to let the watchdog reset the MCU. */
void boot_record_note_watchdog(boot_record *r, uint8_t stalled_task, uint32_t uptime_ms);
void boot_record_note_uptime(boot_record *r, uint32_t uptime_ms);

#endif
