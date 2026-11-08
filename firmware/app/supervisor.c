#include "supervisor.h"

#include <string.h>

void sup_init(supervisor *s) { memset(s, 0, sizeof *s); }

bool sup_register(supervisor *s, uint8_t id, uint32_t deadline_ms, uint32_t now_ms) {
  if (id >= SUP_MAX_TASKS || deadline_ms == 0) {
    return false;
  }
  s->tasks[id].deadline_ms = deadline_ms;
  s->tasks[id].last_checkin_ms = now_ms;
  s->tasks[id].registered = true;
  return true;
}

void sup_checkin(supervisor *s, uint8_t id, uint32_t now_ms) {
  if (id < SUP_MAX_TASKS) {
    s->tasks[id].last_checkin_ms = now_ms;
  }
}

bool sup_may_kick(supervisor *s, uint32_t now_ms, uint8_t *stalled) {
  if (!s->tripped) {
    for (uint8_t i = 0; i < SUP_MAX_TASKS; ++i) {
      const sup_task *t = &s->tasks[i];
      /* Unsigned difference: correct across the wrap at 2^32 ms. */
      if (t->registered && (uint32_t)(now_ms - t->last_checkin_ms) > t->deadline_ms) {
        s->tripped = true;
        s->stalled = i;
        break;
      }
    }
  }
  if (s->tripped && stalled != 0) {
    *stalled = s->stalled;
  }
  return !s->tripped;
}
