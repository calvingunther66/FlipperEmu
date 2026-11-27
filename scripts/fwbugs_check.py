#!/usr/bin/env python3
# Copyright (c) 2026, the FlipperEmu contributors
# SPDX-License-Identifier: BSD-3-Clause
"""Regression checks for the firmware bugs of docs/FIRMWARE_BUGS.md that
Flipware has fixed (Flipware roadmap 1.5, documentation/FirmwareBugTriage.md
there).

    scripts/fwbugs_check.py path/to/flipper-z-f7-update-local.tgz

Boots a throwaway device and gives it, one after the other:

- FW-01: the 11-byte RPC message with a storage list request that has no path
  (`fuzz rpc --replay`).  Fixed: no crash and no hang, the CLI answers after
  it.
- FW-02: a card that is ISO14443-3B and no more, held to the Flipper and read
  by the NFC shell's `dump` (`fuzz nfc --replay`).  Fixed: no hang.
- FW-04 and FW-19 (only with --fuzz): `fuzz nfc --runs 16`, whose cards include a Type 4 tag, an
  EMV card and an NTAG 424 DNA, which the NFC shell names (and `scanner`
  prints).  Fixed: no crash.
- FW-06: the NFC app asked to close by the loader in the middle of a read
  (`app NFC`, Read, `app --close`).  Fixed: it closes, and the firmware does
  not restart.

Each of them fails on firmware that has the bug (the official 1.5.1 and
Momentum mntm-012 do).  A check that needs something the firmware does not
have (no NFC app, no NFC shell) says so and is skipped.  FW-14 has its own
script (scripts/usb_dual_check.py); FW-23's is scripts/fw23_lost_flag.py.
Exit status 0 = pass.
"""
import argparse
import os
import pathlib
import re
import shutil
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import check_limit  # noqa: E402
from files_check import Bench, CRASH, ROOT, check, results  # noqa: E402

PLUGIN = ROOT / "build" / "plugins" / "libfe_cov.so"

# The message that `fuzz rpc` found in the first minute (FW-01): a storage
# list request (field 7) whose body has no path, framed with its length
FW01_MESSAGE = bytes.fromhex("0a08033a064a042f658874")

# A card that is ISO14443-3B and no more (FW-02)
FW02_CARD = ("# ISO14443-3B\ntype iso14443b\nuid 1a 2b 3c 4d\n"
             "appdata 00 12 34 e1\nprotinfo 00 80 80\napp none\n")


def numbers(text):
    """"99 inputs in 12 s (8 a second), 17 kept ..., 0 crashes" of the last
    such line -> (inputs, kept, crashes)"""
    hit = re.findall(r"(\d+) inputs in .*?, (\d+) kept.*?, (\d+) crashes", text)
    return tuple(map(int, hit[-1])) if hit else (0, 0, -1)


def fw01(b, work):
    msg = work / "fw01.bin"
    msg.write_bytes(FW01_MESSAGE)
    code, text = b.fe("fuzz", "rpc", "--replay", msg, "--out", work / "rpc",
                      timeout=600)
    ok, said = b.answers()
    check("FW-01: a storage list request with no path: no crash and no "
          "hang, and the firmware's CLI answers after it",
          code == 0 and "no crash and no hang" in text and ok,
          text[-800:] + said)


def fw02(b, work):
    card = work / "fw02.card"
    card.write_text(FW02_CARD)
    code, text = b.fe("fuzz", "nfc", "--replay", card, "--out",
                      work / "nfc-3b", timeout=600)
    if code == 2 and "no `nfc` shell" in text:
        print("  SKIP  FW-02, FW-04: this firmware's CLI has no NFC shell")
        return False
    ok, said = b.answers()
    check("FW-02: the NFC shell's `dump` of a card that is ISO14443-3B and "
          "no more comes back: no hang, and the CLI answers after it",
          code == 0 and "no crash and no hang" in text and ok,
          text[-800:] + said)
    return True


def fw04(b, work):
    code, text = b.fe("fuzz", "nfc", "--runs", 16, "--seed", 1, "--out",
                      work / "nfc", timeout=900)
    inputs, kept, crashes = numbers(text)
    ok, said = b.answers()
    check("FW-04, FW-19: cards of each kind (Type 4 tag, EMV, NTAG 424 DNA "
          f"among them), {inputs} inputs given to the NFC shell: no crash, "
          "and the firmware's CLI answers after it",
          code == 0 and inputs >= 16 and crashes == 0 and ok,
          text[-1500:] + said)


def fw06(b):
    before = len(b.uart_text())     # (an earlier check's crash is not ours)
    code, text = b.fe("app", "NFC")
    if code:
        print("  SKIP  FW-06: no NFC app in this firmware", flush=True)
        return
    time.sleep(2)
    b.fe("press", "ok")                     # its first item: Read
    time.sleep(3)
    code, text = b.fe("app", "--close")
    log = b.uart_text()[before:]
    check("FW-06: the NFC app asked to close in the middle of a read: "
          "closed, and the firmware does not crash",
          code == 0 and not CRASH.search(log),
          text + log[-1500:])
    code, text = b.fe("app")
    check("... and no app runs afterwards", code == 0 and "no app runs" in text,
          text)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("firmware")
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="seconds to wait for the firmware to start")
    ap.add_argument("--keep", action="store_true",
                    help="keep the throwaway device")
    ap.add_argument("--fuzz", action="store_true",
                    help="also run the NFC fuzzer for FW-04/FW-19. Off by "
                    "default: it still ends in the crash of FW-05 (an EMV "
                    "card read by `dump`), which is not fixed")
    args = ap.parse_args()
    check_limit.arm(1500)

    b = Bench(f"fwbugs-{int(time.time())}-{os.getpid()}", args.firmware,
              args.timeout)
    work = pathlib.Path(tempfile.mkdtemp(prefix="fe-fwbugs-")).resolve()
    try:
        # (coverage, where it is built: the fuzzer then keeps what is new)
        more = ("--coverage",) if PLUGIN.exists() else ()
        if check("boot: 'Startup complete'",
                 b.start(first=True, more=more), b.uart_text()[-2000:]):
            b.fe("wait", "--timeout", args.timeout)
            fw01(b, work)
            if fw02(b, work) and args.fuzz:
                fw04(b, work)
            fw06(b)
            check("no crash in the firmware's log",
                  not CRASH.search(b.uart_text(every=True)),
                  b.uart_text()[-2000:])
    finally:
        b.stop()
        shutil.rmtree(work, ignore_errors=True)
        if not args.keep:
            shutil.rmtree(b.dir, ignore_errors=True)
    bad = [label for label, ok in results if not ok]
    print(f"{len(results) - len(bad)} of {len(results)} checks passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
