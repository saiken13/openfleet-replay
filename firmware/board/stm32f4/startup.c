/* Vector table and reset handler for the STM32F407 (Cortex-M4F). */
#include <stdint.h>
#include <string.h>

extern uint32_t _estack, _sidata, _sdata, _edata, _sbss, _ebss;
extern int main(void);

void Reset_Handler(void);
void Default_Handler(void);
void HardFault_Handler(void);
void SVC_Handler(void);
void PendSV_Handler(void);
void SysTick_Handler(void);

void Reset_Handler(void) {
  memcpy(&_sdata, &_sidata, (size_t)((uintptr_t)&_edata - (uintptr_t)&_sdata));
  memset(&_sbss, 0, (size_t)((uintptr_t)&_ebss - (uintptr_t)&_sbss));
  *(volatile uint32_t *)0xE000ED08u = 0x08000000u;       /* VTOR: our table in flash */
  *(volatile uint32_t *)0xE000ED88u |= (0xFu << 20);      /* CPACR: enable the FPU */
  __asm volatile("dsb\n isb");
  main();
  for (;;) {
  }
}

/* A fault, or an unexpected interrupt: stop. The watchdog resets the MCU. */
void Default_Handler(void) {
  __asm volatile("cpsid i");
  for (;;) {
  }
}

void HardFault_Handler(void) { Default_Handler(); }

#define WEAK_ALIAS __attribute__((weak, alias("Default_Handler")))
void NMI_Handler(void) WEAK_ALIAS;
void MemManage_Handler(void) WEAK_ALIAS;
void BusFault_Handler(void) WEAK_ALIAS;
void UsageFault_Handler(void) WEAK_ALIAS;
void DebugMon_Handler(void) WEAK_ALIAS;

typedef void (*vector)(void);

__attribute__((section(".isr_vector"), used)) const vector g_vectors[16 + 82] = {
    (vector)(uintptr_t)&_estack,
    Reset_Handler,
    NMI_Handler,
    HardFault_Handler,
    MemManage_Handler,
    BusFault_Handler,
    UsageFault_Handler,
    0, 0, 0, 0,
    SVC_Handler,
    DebugMon_Handler,
    0,
    PendSV_Handler,
    SysTick_Handler,
    [16 ... 16 + 81] = Default_Handler, /* peripheral IRQs: none used */
};
