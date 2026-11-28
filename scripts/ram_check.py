#!/usr/bin/env python3
# Copyright (c) 2026, the FlipperEmu contributors
# SPDX-License-Identifier: BSD-3-Clause
"""Free RAM and survival in the Sub-GHz app (Flipware roadmap 2.5).

    scripts/ram_check.py path/to/flipper-z-f7-update-local.tgz [--min-block N]

Boots a throwaway device, reads the firmware's `free` (free heap and the
largest free block), opens the Sub-GHz app, enters Read (its first item) and
reads `free` again.  The firmware must not restart on the way in, must still
answer on the CLI with the app still running, and in Read the largest free block must be at
least --min-block bytes (default 0: it only prints the numbers).  The numbers
are printed as `RAM: ...` lines so that a CI run shows them.

Exit status 0 = pass.
"""
import argparse
import os
import pathlib
import re
import shutil
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import check_limit  # noqa: E402
from files_check import Bench, CRASH, check, results  # noqa: E402


def heap(b):
    """The firmware's `free`: (free heap, total heap, largest free block)"""
    code, text = b.fe("cli", "--timeout", "10", "--", "free")
    got = {}
    for key, label in (("free", "Free heap size"), ("total", "Total heap size"),
                       ("block", "Maximum heap block")):
        m = re.search(rf"{label}: (\d+)", text)
        got[key] = int(m.group(1)) if m else None
    return got, text


def runs(b):
    """The Sub-GHz app still runs and the CLI says so: (it does, what it said).
    (`uptime`, which Bench.answers() asks for, is refused while an app is open.)
    """
    code, text = b.fe("cli", "--timeout", "10", "--", "loader info")
    return code == 0 and 'Application "Sub-GHz" is running' in text, text


def show(when, got):
    print(f"RAM: {when}: free {got['free']} B, largest block {got['block']} B "
          f"(heap {got['total']} B)", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("firmware")
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="seconds to wait for the firmware to start")
    ap.add_argument("--min-block", type=int, default=0,
                    help="the largest free block that Sub-GHz Read must leave")
    ap.add_argument("--keep", action="store_true",
                    help="keep the throwaway device")
    args = ap.parse_args()
    check_limit.arm(600)

    b = Bench(f"ram-{int(time.time())}-{os.getpid()}", args.firmware,
              args.timeout)
    try:
        if check("boot: 'Startup complete'",
                 b.start(first=True), b.uart_text()[-2000:]):
            b.fe("wait", "--timeout", args.timeout)
            before, text = heap(b)
            check("`free` answers", before["block"] is not None, text)
            show("idle", before)

            code, text = b.fe("app", "Sub-GHz")
            check("the Sub-GHz app opens", code == 0, text)
            time.sleep(2)
            home, text = heap(b)
            show("Sub-GHz menu", home)

            b.fe("press", "ok")                 # its first item: Read
            time.sleep(4)
            log = b.uart_text()
            check("entering Read does not restart the firmware",
                  log.count("Startup complete") == 1 and not CRASH.search(log),
                  log[-2000:])
            ok, said = runs(b)
            check("the app still runs and the CLI answers in Read", ok, said)
            read, text = heap(b)
            show("Sub-GHz Read", read)
            b.fe("shot", "--text", b.dir / "read.txt")  # (a look, with --keep)
            if read["block"] is not None and args.min_block:
                check(f"Sub-GHz Read leaves a free block of {args.min_block} B "
                      "or more", read["block"] >= args.min_block,
                      f"largest free block: {read['block']} B")

            b.fe("press", "back")
            time.sleep(1)
            b.fe("app", "--close")
            time.sleep(1)
            log = b.uart_text()
            check("no restart and no crash after closing it",
                  log.count("Startup complete") == 1 and not CRASH.search(log),
                  log[-2000:])
    finally:
        b.stop()
        if not args.keep:
            shutil.rmtree(b.dir, ignore_errors=True)
    bad = [label for label, ok in results if not ok]
    print(f"{len(results) - len(bad)} of {len(results)} checks passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
