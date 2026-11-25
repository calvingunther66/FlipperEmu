#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Check the Video Game Module (docs/VGM.md): `vgm` on pins 13 and 14, and
the IMU of `run --spi imu@7`.

    scripts/vgm_check.py FIRMWARE [--coproc]

Starts a throwaway device with the IMU on the header's SPI bus.  The module
calls, has 2000000 baud, starts an RPC session and puts its picture on the
Flipper's screen; a button released there ends it, and the screen stream
comes: the HDMI picture is 640 by 480, orange around the mirror, and the
mirror is the screen (`shot --text`), also after a button moved the menu.
The firmware's RUN line (pin 15, PC1) held low silences the module, the
firmware gives the link up, and RUN high again brings it back with its
picture first, as at power-up.  The IMU answers WHO_AM_I, and `vgm --imu`
sets and says its motion.

--coproc (Flipware, and other firmwares with its `coproc` service): the
firmware's own checks offload to the module: `storage crc32` (the CRC the
file has), `storage heatshrink` (compress and decompress offloaded, the
block byte for byte the firmware's own, the round trip whole), and `vgm
gpio` on the breakout (an output read back, a voltage held by the module's
--pin read by its ADC, a pin that is not the breakout's refused).  The JS
`vgm` module finds the IMU and has its pitch move when the IMU is tilted.
Exit status 0 = pass.
"""
import argparse
import pathlib
import re
import subprocess
import sys
import tempfile
import time
import zlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from files_check import ROOT, Bench, CRASH, check, results  # noqa: E402
import check_limit  # noqa: E402

REF = bytes((i * 7 + i // 13) & 0xFF for i in range(2000))
IMU_JS = """let vgm = require("vgm");
let math = require("math");
delay(1500);
print("imu", vgm.present(), math.floor(vgm.getPitch()), math.floor(vgm.getRoll()));
"""


def wait_for(what, seconds, step=0.2):
    end = time.time() + seconds
    while time.time() < end:
        got = what()
        if got:
            return got
        time.sleep(step)
    return what()


class Module:
    """`vgm` in the background, and what it printed"""

    def __init__(self, bench, *args):
        self.out = ""
        self.proc = bench.bg("vgm", *args)
        import threading

        def read():
            for line in self.proc.stdout:
                self.out += line
        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()

    def said(self, text, seconds=20):
        return wait_for(lambda: text in self.out, seconds)

    def stop(self):
        if self.proc.poll() is None:
            self.proc.send_signal(2)        # Ctrl-C: it says what it did
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.reader.join(timeout=5)
        return self.out


def png_pixel(path, x, y):
    """(r, g, b) of a pixel of a PNG that fe_vgm.png() wrote"""
    data = pathlib.Path(path).read_bytes()
    w = int.from_bytes(data[16:20], "big")
    at, idat = 8, b""
    while at < len(data):
        size = int.from_bytes(data[at:at + 4], "big")
        kind = data[at + 4:at + 8]
        if kind == b"IDAT":
            idat += data[at + 8:at + 8 + size]
        at += 12 + size
    raw = zlib.decompress(idat)
    row = raw[y * (w * 3 + 1) + 1:(y + 1) * (w * 3 + 1)]
    return tuple(row[x * 3:x * 3 + 3]), w, int.from_bytes(data[20:24], "big")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("firmware")
    ap.add_argument("--coproc", action="store_true",
                    help="the firmware has Flipware's coprocessor service")
    ap.add_argument("--timeout", type=float, default=120)
    args = ap.parse_args()
    check_limit.arm(900)
    bench = Bench("vgm-check", args.firmware, args.timeout)
    tmp = pathlib.Path(tempfile.mkdtemp())
    text = tmp / "mirror.txt"
    hdmi = tmp / "hdmi.png"
    module = None
    try:
        check("the device starts with the IMU on the header's SPI bus",
              bench.start(first=True, more=["--spi", "imu@7"]))
        rc, out = bench.fe("vgm", "--imu")
        check("`vgm --imu` finds the IMU, flat and at rest",
              rc == 0 and "accel-z 1000 mg" in out, out)

        # ---- the link, the picture, the stream
        module = Module(bench, "--text", text, "--hdmi", hdmi, "--pin",
                        "26=1.65V")
        check("the module links at 2000000 baud and starts an RPC session",
              module.said("linked at 2000000 baud"), module.out)
        check("its picture is on the Flipper's screen",
              module.said("virtual display:"), module.out)
        shown = wait_for(lambda: (lambda r: r[1].startswith("█" * 128)
                                  and r)(bench.fe("shot", "--text")), 10)
        check("the screen has the module's frame", shown,
              bench.fe("shot", "--text")[1])
        bench.fe("press", "ok")
        check("a button released there ends it: the screen stream",
              module.said("screen stream: on HDMI"), module.out)
        check("frames come to HDMI", wait_for(text.exists, 10))
        time.sleep(1.0)
        bench.fe("press", "ok")             # the menu: a screen that stays
        time.sleep(2.0)

        def same():
            rc, screen = bench.fe("shot", "--text")
            try:
                mirror = text.read_text()
            except OSError:
                return None
            return rc == 0 and screen.strip("\n") == mirror.strip("\n")
        check("the mirror is the screen", wait_for(same, 5),
              f"screen:\n{bench.fe('shot', '--text')[1]}\nmirror:\n"
              f"{text.read_text() if text.exists() else '(none)'}")
        try:
            corner, w, h = png_pixel(hdmi, 0, 0)
            inside, _, _ = png_pixel(hdmi, 64 + 4 * 2, 48 + 6 * 1)
        except (OSError, ValueError, zlib.error) as e:
            corner, w, h, inside = None, 0, 0, str(e)
        check("HDMI is 640 by 480, orange around the mirror",
              (w, h) == (640, 480) and corner == (255, 130, 0),
              f"{w}x{h}, corner {corner}, inside {inside}")

        # ---- the RUN line
        bench.fe("cli", "gpio mode PC1 1")
        bench.fe("cli", "gpio set PC1 0")
        check("RUN low: the module's RP2040 is in reset",
              module.said("RUN held low"), module.out)
        bench.fe("cli", "gpio set PC1 1")
        check("RUN high again: it starts as at power-up, picture first",
              wait_for(lambda: module.out.count("picture is on") >= 2,
                       20), module.out)
        bench.fe("press", "ok")
        check("... and streams again",
              wait_for(lambda: module.out.count("screen stream:") >= 2, 20),
              module.out)
        bench.fe("cli", "gpio mode PC1 0")

        if args.coproc:
            src = tmp / "vgm_ref.bin"
            src.write_bytes(REF)
            bench.fe("push", src, "/ext/vgm_ref.bin")
            rc, out = bench.fe("cli", "storage crc32 /ext/vgm_ref.bin")
            want = f"{zlib.crc32(REF):08x}"
            check(f"`storage crc32` is offloaded, and is {want}",
                  f"{want} (offloaded" in out, out)
            rc, out = bench.fe("cli", "storage heatshrink /ext/vgm_ref.bin")
            check("`storage heatshrink`: both ways offloaded, the firmware's "
                  "own block, the round trip whole",
                  "offloaded" in out.split("decompress")[0]
                  and re.search(r"decompress:.*offloaded", out)
                  and "matches local compress_encode: yes" in out
                  and "round trip: ok" in out, out)
            outs = [bench.fe("cli", c)[1] for c in (
                "vgm gpio 16 mode out", "vgm gpio 16 write 1",
                "vgm gpio 16 read", "vgm gpio 26 adc", "vgm gpio 5 read",
                "vgm gpio 17 write 1")]
            check("`vgm gpio`: an output reads back what it drives",
                  "GPIO 16: 1" in outs[2], "\n".join(outs))
            check("... the ADC reads what --pin holds (1.65 V)",
                  re.search(r"GPIO 26: adc 20[34]\d", outs[3]), outs[3])
            check("... pin 5 is not the breakout's, an input is no output",
                  "not a VGM breakout pin" in outs[4]
                  and "not an output" in outs[5], "\n".join(outs[4:]))
            check("the module did the jobs",
                  module.said("jobs done", 0) or "coproc: job" in module.out,
                  module.out)

            js = tmp / "imu.js"
            js.write_text(IMU_JS)
            bench.fe("push", js, "/ext/imu.js")
            rc, out = bench.fe("cli", "js /ext/imu.js", timeout=60)
            flat = re.search(r"imu true (-?\d+) (-?\d+)", out)
            check("the JS `vgm` module finds the IMU; flat, its pitch and "
                  "roll are near 0",
                  flat and abs(int(flat[1])) < 10 and abs(int(flat[2])) < 10,
                  out)
            bench.fe("vgm", "--imu", "accel-x=-1000", "accel-z=0")
            rc, out = bench.fe("cli", "js /ext/imu.js", timeout=60)
            tilt = re.search(r"imu true (-?\d+) (-?\d+)", out)
            check("tilted on its side, the pitch moves",
                  tilt and abs(int(tilt[1])) > 30, out)

        out = module.stop()
        module = None
        check("the module ends with what it did",
              "the VGM is gone:" in out and "frames on HDMI" in out, out)
        check("no crash", not CRASH.search(bench.uart_text(every=True)),
              "\n".join(line for line in bench.uart_text(every=True)
                        .splitlines() if CRASH.search(line)))
    finally:
        if module:
            module.stop()
        bench.stop()
    failed = [label for label, ok in results if not ok]
    print(f"{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
