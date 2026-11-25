/* A stand-in for the Flipper's firmware, for scripts/devboard_selftest.py:
 * the probe's side of the board (fe_devboard.BlackMagic) is tested with it
 * against QEMU's own netduinoplus2 (a Cortex-M4 with its flash at
 * 0x08000000, as the STM32WB55's) and a real gdb.
 *
 * It counts in a loop, and where `debugger` is set it runs a BKPT, as the
 * Flipper's loader does where an app starts when its debugger flag is set
 * (furi_hal_debug_gdb_session_active).
 *
 * Copyright (c) 2026, the FlipperEmu contributors
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include <stdint.h>

volatile uint32_t counter;
volatile uint32_t debugger;
volatile uint32_t after_bkpt;
volatile uint32_t watched;
const uint32_t VERSION = MARK;

extern uint32_t _estack;

__attribute__((noinline)) void app_start(void) {
    if(debugger) {
        __asm volatile("bkpt 0");
        after_bkpt++;
    }
}

__attribute__((noinline)) void tick(void) {
    counter++;
    if((counter & 0xFFF) == 0) watched++;
}

void reset_handler(void) {
    for(;;) {
        tick();
        if((counter & 0xFFFF) == 0) app_start();
    }
}

void default_handler(void) {
    for(;;) {
    }
}

__attribute__((section(".vectors"), used)) const void* vectors[16] = {
    &_estack, reset_handler, default_handler, default_handler,
    default_handler, default_handler, default_handler, 0, 0, 0, 0,
    default_handler, default_handler, 0, default_handler, default_handler,
};
