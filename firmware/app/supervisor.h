/*
 * Task supervisor (docs/FIRMWARE.md): every supervised task checks in; the watchdog task
 * kicks the hardware watchdog only while every task has checked in within its deadline.
 * Once a task is late the supervisor latches: the hardware watchdog then resets the MCU,
 * which is the recovery. Pure C, no RTOS: unit-tested on the host.
 *
 * Concurrency on the target: each task writes only its own 32-bit check-in word and the
 * watchdog task only reads them; aligned 32-bit accesses are atomic on Cortex-M4.
 */
#ifndef OPENFLEET_FW_SUPERVISOR_H
#define OPENFLEET_FW_SUPERVISOR_H

#include <stdbool.h>
#include <stdint.h>

#define SUP_MAX_TASKS 8u

typedef struct {
  volatile uint32_t last_checkin_ms;
  uint32_t deadline_ms;
  bool registered;
} sup_task;

typedef struct {
  sup_task tasks[SUP_MAX_TASKS];
  bool tripped;
  uint8_t stalled; /* the first task found late */
} supervisor;

void sup_init(supervisor *s);
/* Registers task `id` (< SUP_MAX_TASKS), which must check in at least every deadline_ms. */
bool sup_register(supervisor *s, uint8_t id, uint32_t deadline_ms, uint32_t now_ms);
void sup_checkin(supervisor *s, uint8_t id, uint32_t now_ms);
/* True while the watchdog may be kicked. False from the first late task on (latched); then
 * *stalled is that task. Safe across the 32-bit millisecond wrap. */
bool sup_may_kick(supervisor *s, uint32_t now_ms, uint8_t *stalled);

#endif
