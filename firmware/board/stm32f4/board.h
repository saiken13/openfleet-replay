/* STM32F407 board support, direct register access (no vendor HAL): USART2 for the device
 * protocol, the independent watchdog, the RNG, reset flags, and the two RAM areas that
 * startup leaves alone. Runs at the 16 MHz internal oscillator (the reset default). */
#ifndef OPENFLEET_FW_BOARD_H
#define OPENFLEET_FW_BOARD_H

#include <stddef.h>
#include <stdint.h>

#include "boot_record.h"

#define BOARD_CPU_HZ 16000000u

/* Faults the emulator can inject by writing these words (Renode monitor:
 * `sysbus WriteDoubleWord 0x2001FE00 1`). Zeroed at every boot, so a fault never survives
 * the reset it causes. Fixed address: see stm32f4.ld and docs/FIRMWARE.md. */
typedef struct {
  volatile uint32_t stall_task;      /* +0x00: task id that stops checking in (0 none) */
  volatile uint32_t comms_delay_ms;  /* +0x04: extra delay per frame sent (a slow link) */
  volatile uint32_t stop_sensors;    /* +0x08: bitmask of sensor ids (1 << id) not sampled */
  volatile uint32_t spare[61];
} fault_ctl;

#define FAULT_CTL_ADDR 0x2001FE00u
#define BOOT_RECORD_ADDR 0x2001FF00u

/* Fixed addresses, not linker sections: an ELF loader zero-fills even NOLOAD sections that
 * sit in a LOAD segment (Renode's does, on every reset), which would erase the boot record.
 * The linker script only reserves the range (region FIXED). */
#define g_fault (*(fault_ctl *)FAULT_CTL_ADDR)
#define g_boot_record (*(boot_record *)BOOT_RECORD_ADDR)

void board_init(void);
void board_uart_write(const uint8_t *data, size_t len);
uint32_t board_reset_flags(void); /* RCC_CSR, read once; the flags are cleared afterwards */
uint32_t board_random(void);
void board_watchdog_start(uint32_t timeout_ms);
void board_watchdog_kick(void);

#endif
