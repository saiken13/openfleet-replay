/* STM32F407VG: 1 MB flash, 128 KB SRAM. The top 512 bytes of SRAM (FIXED) hold the
 * fault-injection block (0x2001FE00) and the boot record (0x2001FF00). Nothing is linked
 * there on purpose, so no ELF segment covers it and no loader clears it (board.h). */
ENTRY(Reset_Handler)

MEMORY {
  FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 1024K
  RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 0x1FE00
  FIXED (rw)  : ORIGIN = 0x2001FE00, LENGTH = 0x200
}

_estack = ORIGIN(RAM) + LENGTH(RAM);

SECTIONS {
  .isr_vector : { KEEP(*(.isr_vector)) } > FLASH
  .text : { *(.text*) *(.rodata*) . = ALIGN(4); } > FLASH
  .ARM.exidx : { *(.ARM.exidx*) } > FLASH
  _sidata = LOADADDR(.data);
  .data : { _sdata = .; *(.data*) . = ALIGN(4); _edata = .; } > RAM AT > FLASH
  .bss (NOLOAD) : { _sbss = .; *(.bss*) *(COMMON) . = ALIGN(4); _ebss = .; } > RAM
}
