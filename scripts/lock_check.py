#!/usr/bin/env python3
# Copyright (c) 2026, the FlipperEmu contributors
# SPDX-License-Identifier: BSD-3-Clause
"""Check that a module on the expansion port cannot unlock a PIN-locked
Flipper, with a Flipper's firmware (Flipware#33).

    scripts/lock_check.py path/to/flipper-z-f7-update-local.tgz

Starts a throwaway device.  Unlocked, `expansion --rpc` has its session and
IsLocked says it is not locked.  Then the device is stopped and a PIN is set
the way the firmware keeps one: in the RTC's backup registers, which the
emulator saves in the device's nvram.bin (the "coin cell") when it stops.
Register 7 gets the PIN (Up Down Right Left: two bits a button, the length
in the top four bits), register 1 the Lock flag (FuriHalRtcFlagLock, bit 2),
register 5 no failed tries.  Started again, the firmware's desktop finds a
PIN and the flag and locks itself, as after a reboot of a locked Flipper.
Then `expansion --rpc --unlock --try storage-list --try input`: IsLocked
says locked, the unlock is refused (ERROR), IsLocked still says locked, a
list of /ext and a press of Back are refused, exit status 0.  The Lock flag
is still set in the register, a second module still finds the Flipper
locked, and nothing crashed.

Firmware that lets a module unlock it (stock firmware, or Flipware with
Allow Module RPC While Locked on) fails here: `expansion` exits 1 and says
"a module on pins 13 and 14 unlocked a PIN-locked Flipper".

The same by hand, on a device of your own (`-d NAME` throughout):

    ./flipperemu stop                       # QEMU writes nvram.bin
    scripts/lock_check.py --set-pin devices/NAME/nvram.bin
    ./flipperemu run ...                    # boots locked, PIN Up Down Right Left
    ./flipperemu expansion --rpc --unlock --try storage-list --try input

(or on the emulated Flipper: Settings > Desktop > PIN Setup, then lock it with
the PIN from the desktop's Up menu).  Exit status 0 = pass.
"""
import argparse
import os
import pathlib
import re
import shutil
import struct
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import check_limit  # noqa: E402
from fe_monitor import Monitor  # noqa: E402
from files_check import Bench, CRASH, check, results  # noqa: E402

# nvram.bin, as qemu/hw/arm/flipper/flipper_zero.c writes it (FlipperNvram):
# magic, version, four bytes of the gauge, its 4 KiB of data memory, then
# WB55RtcBackup: the calendar's offset (8 bytes, aligned), ten registers,
# BKP0R..BKP19R
NVRAM_MAGIC, NVRAM_VERSION = 0x564E4546, 2
NVRAM_SIZE = 0x1090
BKP_AT = 0x1040
BKP = 0x40002850                # BKP0R on the bus
SYSTEM, PIN_FAILS, PIN_VALUE = 1, 5, 7  # FuriHalRtcRegister
FLAG_LOCK = 1 << 2                      # FuriHalRtcFlagLock
PIN = [0, 1, 2, 3]              # Up Down Right Left (InputKey)


def pin_value(keys):
    """The PIN as desktop_pin_code_pack() keeps it"""
    value = len(keys) << 28
    for i, key in enumerate(keys):
        value |= key << (2 * i)
    return value


def set_pin(nvram, keys=PIN):
    """A PIN and the Lock flag in a stopped device's nvram.bin: (the
    registers before, after)"""
    data = bytearray(nvram.read_bytes())
    magic, version = struct.unpack_from("<II", data) if len(data) >= 8 \
        else (0, 0)
    if (len(data), magic, version) != (NVRAM_SIZE, NVRAM_MAGIC, NVRAM_VERSION):
        raise ValueError(f"{nvram}: not an nvram.bin of this emulator "
                         f"({len(data)} bytes, magic {magic:#x}, "
                         f"version {version})")
    before = list(struct.unpack_from("<20I", data, BKP_AT))
    after = list(before)
    after[SYSTEM] |= FLAG_LOCK
    after[PIN_FAILS] = 0
    after[PIN_VALUE] = pin_value(keys)
    struct.pack_into("<20I", data, BKP_AT, *after)
    nvram.write_bytes(bytes(data))
    return before, after


def registers(b, *numbers):
    """Backup registers of the running device (None: the monitor did not
    say)"""
    m = Monitor(str(b.dir / "monitor.sock"))
    try:
        out = []
        for n in numbers:
            word = re.search(r":\s*0x([0-9a-f]{8})\b",
                             m.cmd(f"xp/1wx {BKP + 4 * n:#x}"))
            out.append(int(word.group(1), 16) if word else None)
        return out
    finally:
        m.close()


def expansion(b, *more, tries=5):
    """`expansion --rpc ...`, again while the firmware does not wait on the
    port yet (it may still be starting): (exit status, what it said)"""
    for _ in range(tries):
        code, text = b.fe("expansion", "--rpc", *more, timeout=60)
        if "waits for no module" not in text and "did not answer the call" \
                not in text:
            return code, text
        time.sleep(2)
    return code, text


def started(b, timeout):
    end = time.time() + timeout
    while time.time() < end and "Startup complete" not in b.uart_text():
        time.sleep(0.2)
    return "Startup complete" in b.uart_text()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("firmware", nargs="?")
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="seconds to wait for the firmware to start")
    ap.add_argument("--keep", action="store_true",
                    help="keep the throwaway device")
    ap.add_argument("--set-pin", metavar="NVRAM", type=pathlib.Path,
                    help="only set the PIN (Up Down Right Left) and the Lock "
                         "flag in a stopped device's nvram.bin, and exit")
    args = ap.parse_args()
    if args.set_pin:
        try:
            before, after = set_pin(args.set_pin)
        except (OSError, ValueError) as e:
            sys.exit(str(e))
        print(f"{args.set_pin}: PIN register {before[PIN_VALUE]:#010x} -> "
              f"{after[PIN_VALUE]:#010x} (Up Down Right Left), system "
              f"register {before[SYSTEM]:#010x} -> {after[SYSTEM]:#010x} "
              "(Lock): the device starts locked")
        return 0
    if not args.firmware:
        ap.error("the firmware to check (or --set-pin NVRAM)")
    check_limit.arm(600)
    # (a short name: a socket's path on macOS has 103 bytes at most)
    b = Bench(f"lck-{os.getpid()}", args.firmware, args.timeout)
    try:
        ok = b.start(first=True)
        said = ""
        if ok:
            code, text = b.fe("wait", "--timeout", args.timeout)
            ok, said = b.answers() if code == 0 else (False, text)
        if not check("the firmware starts, and its CLI answers", ok,
                     said + b.uart_text()[-2000:]):
            return 1
        code, text = expansion(b)
        check("unlocked: a module has its RPC session, and IsLocked says "
              "not locked (exit 0)",
              code == 0 and "the RPC session is there" in text
              and "says it is not locked" in text, text)
        header = registers(b, 0, SYSTEM)
        b.stop()
        nvram = b.dir / "nvram.bin"
        try:
            before, after = set_pin(nvram)
            why = ""
        except (OSError, ValueError) as e:
            before, why = None, str(e)
        if not check("the device's nvram.bin has the backup registers as "
                     "the running firmware had them",
                     before and before[0] == header[0]
                     and before[SYSTEM] == header[SYSTEM],
                     why or f"monitor {header}, nvram.bin {before}"):
            return 1
        if not check("started again with a PIN and the Lock flag, the "
                     "firmware starts", started_again(b),
                     b.uart_text()[-2000:]):
            return 1
        system, pin = registers(b, SYSTEM, PIN_VALUE)
        check("the PIN is still set, and the Lock flag (the desktop locked "
              "itself)", pin == after[PIN_VALUE] and system is not None
              and system & FLAG_LOCK,
              f"system {system}, PIN {pin} (want {after[PIN_VALUE]:#x})")
        code, text = expansion(b, "--unlock", "--try", "storage-list",
                               "--try", "input")
        check("locked: a module still has its session (a ping), and "
              "IsLocked says locked",
              "the RPC session is there" in text
              and "IsLocked: the Flipper says it is locked" in text, text)
        check("--unlock is refused (ERROR), and IsLocked still says locked",
              "refused the unlock (ERROR)" in text
              and "it is still locked" in text, text)
        check("a list of /ext and a press of Back are refused",
              "--try storage-list: a list of /ext" in text
              and "--try input: a press of Back" in text
              and text.count("was refused (ERROR)") == 2, text)
        check("expansion --rpc --unlock exits 0 (nothing got through)",
              code == 0 and "unlocked a PIN-locked Flipper" not in text, text)
        system, = registers(b, SYSTEM)
        check("the Lock flag is still set afterwards",
              system is not None and system & FLAG_LOCK, f"system {system}")
        code, text = expansion(b)
        check("a second module finds the Flipper still locked",
              code == 0 and "says it is locked" in text, text)
        check("no crash in the firmware's log",
              not CRASH.search(b.uart_text(every=True)),
              b.uart_text()[-2000:])
    finally:
        b.stop()
        if not args.keep:
            shutil.rmtree(b.dir, ignore_errors=True)
    failed = [label for label, ok in results if not ok]
    print(f"{len(results) - len(failed)} of {len(results)} passed")
    return 1 if failed else 0


def started_again(b):
    # (not `wait`, nor the CLI: a Flipper locked with a PIN has its USB CLI
    # off; the log on pins 13 and 14 says when it has started)
    b.start(wait=False)
    return started(b, b.timeout)


if __name__ == "__main__":
    sys.exit(main())
