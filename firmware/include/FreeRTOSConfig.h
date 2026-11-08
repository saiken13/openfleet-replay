/* FreeRTOS configuration for the OpenFleet demo firmware (STM32F407, Cortex-M4F at 16 MHz).
 * Static allocation only: no heap, so memory use is fixed at link time. */
#ifndef FREERTOS_CONFIG_H
#define FREERTOS_CONFIG_H

#define configCPU_CLOCK_HZ 16000000u
#define configTICK_RATE_HZ 1000u
#define configUSE_PREEMPTION 1
#define configUSE_TIME_SLICING 1
#define configMAX_PRIORITIES 6
#define configMINIMAL_STACK_SIZE 128
#define configMAX_TASK_NAME_LEN 12
#define configTICK_TYPE_WIDTH_IN_BITS TICK_TYPE_WIDTH_32_BITS
#define configIDLE_SHOULD_YIELD 1
#define configUSE_IDLE_HOOK 1  /* WFI: sleep until the next interrupt (main.c) */
#define configUSE_TICK_HOOK 0
#define configUSE_MUTEXES 0
#define configUSE_COUNTING_SEMAPHORES 0
#define configUSE_TIMERS 0
#define configTIMER_TASK_STACK_DEPTH configMINIMAL_STACK_SIZE  /* required by V11 even unused */
#define configQUEUE_REGISTRY_SIZE 0

#define configSUPPORT_STATIC_ALLOCATION 1
#define configSUPPORT_DYNAMIC_ALLOCATION 0
#define configKERNEL_PROVIDED_STATIC_MEMORY 1  /* idle task memory from the kernel */

#define configCHECK_FOR_STACK_OVERFLOW 2      /* calls vApplicationStackOverflowHook */
#define configUSE_MALLOC_FAILED_HOOK 0

#define INCLUDE_vTaskDelay 1
#define INCLUDE_xTaskDelayUntil 1
#define INCLUDE_uxTaskGetStackHighWaterMark 1
#define INCLUDE_xTaskGetCurrentTaskHandle 1
#define INCLUDE_vTaskSuspend 1

/* Cortex-M4: 4 priority bits. */
#define configPRIO_BITS 4
#define configKERNEL_INTERRUPT_PRIORITY (15 << (8 - configPRIO_BITS))
#define configMAX_SYSCALL_INTERRUPT_PRIORITY (5 << (8 - configPRIO_BITS))

/* A failed assertion stops the CPU; the watchdog then resets it. */
#define configASSERT(x)                \
  if ((x) == 0) {                      \
    __asm volatile("cpsid i");         \
    for (;;) {                         \
    }                                  \
  }

/* The port's handlers are the vector table's. */
#define vPortSVCHandler SVC_Handler
#define xPortPendSVHandler PendSV_Handler
#define xPortSysTickHandler SysTick_Handler

#endif
