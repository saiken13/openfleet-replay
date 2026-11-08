#include "board.h"

#define REG(addr) (*(volatile uint32_t *)(addr))

#define RCC_BASE 0x40023800u
#define RCC_AHB2ENR REG(RCC_BASE + 0x34u)
#define RCC_APB1ENR REG(RCC_BASE + 0x40u)
#define RCC_AHB1ENR REG(RCC_BASE + 0x30u)
#define RCC_CSR REG(RCC_BASE + 0x74u)

#define GPIOA_BASE 0x40020000u
#define GPIOA_MODER REG(GPIOA_BASE + 0x00u)
#define GPIOA_AFRL REG(GPIOA_BASE + 0x20u)

#define USART2_BASE 0x40004400u
#define USART_SR REG(USART2_BASE + 0x00u)
#define USART_DR REG(USART2_BASE + 0x04u)
#define USART_BRR REG(USART2_BASE + 0x08u)
#define USART_CR1 REG(USART2_BASE + 0x0Cu)
#define USART_SR_TXE (1u << 7)

#define IWDG_BASE 0x40003000u
#define IWDG_KR REG(IWDG_BASE + 0x00u)
#define IWDG_PR REG(IWDG_BASE + 0x04u)
#define IWDG_RLR REG(IWDG_BASE + 0x08u)

#define RNG_BASE 0x50060800u
#define RNG_CR REG(RNG_BASE + 0x00u)
#define RNG_SR REG(RNG_BASE + 0x04u)
#define RNG_DR REG(RNG_BASE + 0x08u)

_Static_assert(sizeof(fault_ctl) == 256, "fault_ctl fills its 256-byte area");
_Static_assert(sizeof(boot_record) <= 256, "boot_record fits its 256-byte area");

static uint32_t reset_flags;

void board_init(void) {
  reset_flags = RCC_CSR;
  RCC_CSR |= (1u << 24); /* RMVF: clear the flags for the next boot */

  /* USART2 on PA2 (TX) / PA3 (RX), AF7, 115200 8N1 at 16 MHz. */
  RCC_AHB1ENR |= 1u << 0;  /* GPIOA */
  RCC_APB1ENR |= 1u << 17; /* USART2 */
  GPIOA_MODER = (GPIOA_MODER & ~(0xFu << 4)) | (0xAu << 4);
  GPIOA_AFRL = (GPIOA_AFRL & ~(0xFFu << 8)) | (0x77u << 8);
  USART_BRR = BOARD_CPU_HZ / 115200u;
  USART_CR1 = (1u << 13) | (1u << 3) | (1u << 2); /* UE, TE, RE */

  RCC_AHB2ENR |= 1u << 6; /* RNG */
  RNG_CR = 1u << 2;       /* RNGEN */
}

void board_uart_write(const uint8_t *data, size_t len) {
  for (size_t i = 0; i < len; ++i) {
    while ((USART_SR & USART_SR_TXE) == 0) {
    }
    USART_DR = data[i];
  }
}

uint32_t board_reset_flags(void) { return reset_flags; }

uint32_t board_random(void) {
  for (unsigned tries = 0; tries < 1000u; ++tries) {
    if (RNG_SR & 1u) { /* DRDY */
      return RNG_DR;
    }
  }
  return 0x5EED0000u ^ REG(0xE000E018u); /* fallback: SysTick current value */
}

void board_watchdog_start(uint32_t timeout_ms) {
  /* LSI 32 kHz / 32 = 1 kHz: one count per millisecond, up to 4095 ms. */
  IWDG_KR = 0x5555u; /* unlock PR and RLR */
  IWDG_PR = 3u;      /* /32 */
  IWDG_RLR = timeout_ms > 4095u ? 4095u : timeout_ms;
  IWDG_KR = 0xAAAAu;
  IWDG_KR = 0xCCCCu; /* start; it can't be stopped again */
}

void board_watchdog_kick(void) { IWDG_KR = 0xAAAAu; }

/* No heap: anything in the C library that tries to allocate fails instead of growing into
 * the stacks. (Nothing in this firmware allocates; snprintf of integers doesn't.) */
#include <errno.h>
void *_sbrk(ptrdiff_t increment);
void *_sbrk(ptrdiff_t increment) {
  (void)increment;
  errno = ENOMEM;
  return (void *)-1;
}
