/*
 * OpenFleet demo firmware (docs/FIRMWARE.md): FreeRTOS on an STM32F407, speaking the
 * OpenFleet device protocol (docs/PROTOCOL.md) on USART2. Developed and tested only on
 * Renode's emulated STM32F4: no physical board, no real-time claims.
 *
 * Tasks (all statically allocated):
 *   sensor   (prio 4) samples "imu" at 50 Hz and "range" at 10 Hz into the data queue. It
 *                     never blocks on the link: a full queue drops the reading and counts it.
 *   comms    (prio 2) the only user of the UART. Serves the control queue (HEALTH, LOG,
 *                     DESCRIBE) before the data queue, so on a slow link readings are what
 *                     gets dropped, never the reports about it.
 *   health   (prio 3) every second: task states and stack high-water marks (HEALTH); every
 *                     5 s, names and rates (DESCRIBE).
 *   watchdog (prio 5) kicks the independent watchdog only while every supervised task has
 *                     checked in on time. A stalled task stops the kicks; the IWDG resets the
 *                     MCU about 1 s later, and the next boot reports why.
 */
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "FreeRTOS.h"
#include "board.h"
#include "boot_record.h"
#include "openfleet/protocol.h"
#include "queue.h"
#include "supervisor.h"
#include "task.h"

#define FW_VERSION "openfleet-fw 0.1"
#define DEVICE_ID 2u

enum { TASK_SENSOR = 1, TASK_COMMS = 2, TASK_HEALTH = 3, TASK_COUNT = 4 };
static const char *const kTaskNames[TASK_COUNT] = {"", "sensor", "comms", "health"};

enum { SENSOR_IMU = 1, SENSOR_RANGE = 2 };

#define IMU_PERIOD_MS 20u   /* 50 Hz */
#define RANGE_EVERY 5u      /* every 5th imu period: 10 Hz */
#define WATCHDOG_TIMEOUT_MS 1000u
#define DATA_QUEUE_DEPTH 32u /* sensor readings: droppable */
#define CTL_QUEUE_DEPTH 8u   /* health, logs, describes: served first */

/* A message for the comms task: an OFP payload and its type. */
typedef struct {
  uint8_t type;
  uint8_t len;
  uint8_t payload[OFP_MAX_PAYLOAD];
} tx_msg;

static StaticQueue_t data_queue_storage;
static uint8_t data_queue_buffer[DATA_QUEUE_DEPTH * sizeof(tx_msg)];
static QueueHandle_t data_queue;
static StaticQueue_t ctl_queue_storage;
static uint8_t ctl_queue_buffer[CTL_QUEUE_DEPTH * sizeof(tx_msg)];
static QueueHandle_t ctl_queue;

static supervisor g_sup;
static boot_info g_boot;
static TaskHandle_t g_tasks[TASK_COUNT];
static volatile uint32_t g_sensor_dropped; /* readings lost to a full data queue */

static uint32_t now_ms(void) { return (uint32_t)xTaskGetTickCount(); } /* 1 kHz tick */

/* Queue a message; from a task that must not block, timeout 0. False if the queue is full. */
static bool post(uint8_t type, const uint8_t *payload, size_t len, TickType_t wait) {
  tx_msg m;
  m.type = type;
  m.len = (uint8_t)len;
  memcpy(m.payload, payload, len);
  return xQueueSend(type == OFP_SENSOR ? data_queue : ctl_queue, &m, wait) == pdTRUE;
}

static void post_log(uint8_t level, const char *text, TickType_t wait) {
  uint8_t p[OFP_MAX_PAYLOAD];
  const size_t n = ofp_build_log(p, sizeof p, level, text);
  if (n > 0) {
    (void)post(OFP_LOG, p, n, wait);
  }
}

/* --- sensor ------------------------------------------------------------------------- */

static void sensor_task(void *arg) {
  (void)arg;
  TickType_t wake = xTaskGetTickCount();
  uint32_t tick = 0;
  int32_t imu = 0;
  int32_t range = 1500;
  for (;;) {
    while (g_fault.stall_task == TASK_SENSOR) {  /* injected: alive, but never checks in */
      vTaskDelay(pdMS_TO_TICKS(10));
    }
    sup_checkin(&g_sup, TASK_SENSOR, now_ms());
    uint8_t p[8];
    imu = (imu + 37) % 2000; /* a synthetic signal: values don't matter, rates do */
    if ((g_fault.stop_sensors & (1u << SENSOR_IMU)) == 0 &&
        !post(OFP_SENSOR, p, ofp_build_sensor(p, sizeof p, SENSOR_IMU, imu - 1000), 0)) {
      g_sensor_dropped++;
    }
    if (++tick % RANGE_EVERY == 0) {
      range = 1000 + (range + 13) % 1000;
      if ((g_fault.stop_sensors & (1u << SENSOR_RANGE)) == 0 &&
          !post(OFP_SENSOR, p, ofp_build_sensor(p, sizeof p, SENSOR_RANGE, range), 0)) {
        g_sensor_dropped++;
      }
    }
    xTaskDelayUntil(&wake, pdMS_TO_TICKS(IMU_PERIOD_MS));
  }
}

/* --- comms -------------------------------------------------------------------------- */

static void send_frame(uint16_t *seq, uint8_t type, const uint8_t *payload, uint16_t len) {
  ofp_header h = {0};
  h.type = type;
  h.device_id = DEVICE_ID;
  h.seq = (*seq)++;
  h.boot_id = g_boot.boot_id;
  h.uptime_ms = now_ms();
  h.length = len;
  uint8_t wire[OFP_MAX_WIRE];
  const size_t n = ofp_encode(&h, payload, wire, sizeof wire);
  if (n > 0) {
    board_uart_write(wire, n);
  }
}

static void comms_task(void *arg) {
  (void)arg;
  uint16_t seq = 0;
  uint8_t p[OFP_MAX_PAYLOAD];
  send_frame(&seq, OFP_HELLO, p,
             (uint16_t)ofp_build_hello(p, sizeof p, g_boot.reason, FW_VERSION));
  tx_msg m;
  for (;;) {
    sup_checkin(&g_sup, TASK_COMMS, now_ms());
    if (xQueueReceive(ctl_queue, &m, 0) == pdTRUE ||
        xQueueReceive(data_queue, &m, pdMS_TO_TICKS(10)) == pdTRUE) {
      send_frame(&seq, m.type, m.payload, m.len);
      const uint32_t delay = g_fault.comms_delay_ms; /* injected: a slow link */
      if (delay > 0) {
        vTaskDelay(pdMS_TO_TICKS(delay > 400u ? 400u : delay));
      }
    }
  }
}

/* --- health ------------------------------------------------------------------------- */

static void post_describes(void) {
  uint8_t p[OFP_MAX_PAYLOAD];
  const ofp_describe sensors[] = {{OFP_KIND_SENSOR, SENSOR_IMU, 500, "imu"},
                                  {OFP_KIND_SENSOR, SENSOR_RANGE, 100, "range"}};
  for (unsigned i = 0; i < 2; ++i) {
    (void)post(OFP_DESCRIBE, p, ofp_build_describe(p, sizeof p, &sensors[i]), pdMS_TO_TICKS(50));
  }
  for (uint8_t t = 1; t < TASK_COUNT; ++t) {
    ofp_describe d = {OFP_KIND_TASK, t, 0, ""};
    strncpy(d.name, kTaskNames[t], OFP_MAX_NAME);
    (void)post(OFP_DESCRIBE, p, ofp_build_describe(p, sizeof p, &d), pdMS_TO_TICKS(50));
  }
}

static void health_task(void *arg) {
  (void)arg;
  char text[OFP_MAX_TEXT + 1];
  if (g_boot.stall_known) {
    snprintf(text, sizeof text,
             "previous boot ended by the watchdog: task %s stopped checking in after %lu ms up",
             g_boot.stalled_task < TASK_COUNT ? kTaskNames[g_boot.stalled_task] : "?",
             (unsigned long)g_boot.prev_uptime_ms);
    post_log(OFP_LOG_ERROR, text, pdMS_TO_TICKS(100));
  }
  snprintf(text, sizeof text, "boot %lu since power-on (boot id %u)",
           (unsigned long)g_boot.boot_count, (unsigned)g_boot.boot_id);
  post_log(OFP_LOG_INFO, text, pdMS_TO_TICKS(100));

  uint32_t dropped_seen = 0;
  uint32_t seconds = 0;
  TickType_t wake = xTaskGetTickCount();
  for (;;) {
    sup_checkin(&g_sup, TASK_HEALTH, now_ms());
    if (seconds % 5u == 0) {
      post_describes();
    }
    const uint32_t dropped = g_sensor_dropped;
    ofp_health h = {0};
    for (uint8_t t = 1; t < TASK_COUNT; ++t) {
      uint8_t state = OFP_TASK_RUNNING;
      if (t == TASK_SENSOR && dropped != dropped_seen) {
        state = OFP_TASK_DEGRADED; /* sampling fine, but its readings didn't all get out */
      }
      h.tasks[h.count].task_id = t;
      h.tasks[h.count].state = state;
      h.tasks[h.count].stack_free_words = (uint16_t)uxTaskGetStackHighWaterMark(g_tasks[t]);
      h.count++;
    }
    uint8_t p[OFP_MAX_PAYLOAD];
    (void)post(OFP_HEALTH, p, ofp_build_health(p, sizeof p, &h), pdMS_TO_TICKS(50));
    if (dropped != dropped_seen) {
      snprintf(text, sizeof text, "sensor: %lu readings dropped (data queue full: slow link)",
               (unsigned long)(dropped - dropped_seen));
      post_log(OFP_LOG_WARN, text, pdMS_TO_TICKS(50));
      dropped_seen = dropped;
    }
    boot_record_note_uptime(&g_boot_record, now_ms());
    seconds++;
    xTaskDelayUntil(&wake, pdMS_TO_TICKS(1000));
  }
}

/* --- watchdog ----------------------------------------------------------------------- */

static void watchdog_task(void *arg) {
  (void)arg;
  board_watchdog_start(WATCHDOG_TIMEOUT_MS);
  bool reported = false;
  for (;;) {
    uint8_t stalled = 0;
    if (sup_may_kick(&g_sup, now_ms(), &stalled)) {
      board_watchdog_kick();
    } else if (!reported) {
      /* Stop kicking: the IWDG resets us. Leave a note for the next boot, and tell the
       * gateway while the link still works. */
      reported = true;
      boot_record_note_watchdog(&g_boot_record, stalled, now_ms());
      char text[OFP_MAX_TEXT + 1];
      snprintf(text, sizeof text, "watchdog: task %s stopped checking in; resetting",
               stalled < TASK_COUNT ? kTaskNames[stalled] : "?");
      post_log(OFP_LOG_ERROR, text, 0);
    }
    vTaskDelay(pdMS_TO_TICKS(100));
  }
}

/* --- startup ------------------------------------------------------------------------ */

#define STACK_WORDS 512u
static StackType_t stacks[TASK_COUNT + 1][STACK_WORDS];
static StaticTask_t tcbs[TASK_COUNT + 1];

/* Nothing to do: sleep until the next interrupt (the tick at the latest). On a board this
 * saves power; under Renode it lets the emulator skip idle time instead of executing a busy
 * loop at full host speed. */
void vApplicationIdleHook(void);
void vApplicationIdleHook(void) { __asm volatile("wfi"); }

void vApplicationStackOverflowHook(TaskHandle_t task, char *name);
void vApplicationStackOverflowHook(TaskHandle_t task, char *name) {
  (void)task;
  (void)name;
  __asm volatile("cpsid i"); /* stop; the watchdog resets the MCU */
  for (;;) {
  }
}

int main(void) {
  board_init();
  memset((void *)&g_fault, 0, sizeof g_fault); /* injected faults never survive a reset */
  g_boot = boot_record_on_boot(&g_boot_record, board_reset_flags(), board_random());

  data_queue = xQueueCreateStatic(DATA_QUEUE_DEPTH, sizeof(tx_msg), data_queue_buffer,
                                  &data_queue_storage);
  ctl_queue = xQueueCreateStatic(CTL_QUEUE_DEPTH, sizeof(tx_msg), ctl_queue_buffer,
                                 &ctl_queue_storage);
  sup_init(&g_sup);
  const uint32_t t0 = 0;
  sup_register(&g_sup, TASK_SENSOR, 200, t0);
  sup_register(&g_sup, TASK_COMMS, 500, t0);
  sup_register(&g_sup, TASK_HEALTH, 3000, t0);

  g_tasks[TASK_SENSOR] = xTaskCreateStatic(sensor_task, "sensor", STACK_WORDS, NULL, 4,
                                           stacks[TASK_SENSOR], &tcbs[TASK_SENSOR]);
  g_tasks[TASK_COMMS] = xTaskCreateStatic(comms_task, "comms", STACK_WORDS, NULL, 2,
                                          stacks[TASK_COMMS], &tcbs[TASK_COMMS]);
  g_tasks[TASK_HEALTH] = xTaskCreateStatic(health_task, "health", STACK_WORDS, NULL, 3,
                                           stacks[TASK_HEALTH], &tcbs[TASK_HEALTH]);
  (void)xTaskCreateStatic(watchdog_task, "watchdog", STACK_WORDS, NULL, 5, stacks[0], &tcbs[0]);
  vTaskStartScheduler();
  for (;;) { /* not reached */
  }
}
