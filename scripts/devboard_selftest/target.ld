/* scripts/devboard_selftest.py's stand-in: flash at 0x08000000, RAM at
 * 0x20000000, as on the STM32WB55 (and QEMU's netduinoplus2) */
MEMORY {
    FLASH (rx) : ORIGIN = 0x08000000, LENGTH = 1024K
    RAM (rwx)  : ORIGIN = 0x20000000, LENGTH = 128K
}
ENTRY(reset_handler)
_estack = ORIGIN(RAM) + LENGTH(RAM);
SECTIONS {
    .text : { KEEP(*(.vectors)) *(.text*) *(.rodata*) } > FLASH
    .bss (NOLOAD) : { *(.bss*) *(COMMON) } > RAM
}
