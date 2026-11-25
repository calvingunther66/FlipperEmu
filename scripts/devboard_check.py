#!/usr/bin/env python3
# Copyright (c) 2026, the FlipperEmu contributors
# SPDX-License-Identifier: BSD-3-Clause
"""Check the Wi-Fi Developer Board (`devboard`) on the emulator, with a
Flipper's firmware.

    scripts/devboard_check.py firmware/official-1.5.1/flipper-z-f7-update-1.5.1.tgz

Starts a throwaway device and puts the board on its header with its stock
firmware, Black Magic Probe.  A gdb on its GDB server: with Debug off (as
the firmware starts) `monitor swdp_scan` fails, as on a Flipper whose SWD
pins are analog; after `sysctl debug 1` it finds the STM32WBxx, `attach 1`
halts the processor in the firmware's code and `detach` lets it go on (its
CLI answers).  With the firmware's symbols (the device's firmware.elf): the
debugger flag set as the firmware's own gdb script sets it, an app started
from the card halts at the loader's BKPT (SIGTRAP, the PC on it), the log
says "Triggering BP for debugger" on the UART bridge's TCP port, and
`continue` from there starts the app.  `load` of a page of flash after the
firmware (the probe's vFlash), `compare-sections` matches, and the page is
still there after the device is stopped and started again.  Then the board
with ESP32 Marauder, and examples/devboard/marauder.js on the Flipper drives
it as the WiFi Marauder app does: a scan finds the scene's access points,
`list -a` lists them.  Exit status 0 = pass.
"""
import argparse
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import check_limit  # noqa: E402
import fe_devboard  # noqa: E402
from files_check import ROOT, Bench, CRASH, check, results  # noqa: E402

APP = "/ext/apps/Games/snake_game.fap"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def gdb_tool():
    for name in ("arm-none-eabi-gdb-py3", "arm-none-eabi-gdb", "gdb-multiarch"):
        found = shutil.which(name)
        if found:
            return found
    return None


def gdb(tool, port, commands, elf=None, timeout=120, background=False):
    argv = [tool, "-batch", "-nx", "-ex", "set pagination off",
            "-ex", "set confirm off",
            "-ex", f"target extended-remote localhost:{port}"]
    for c in commands:
        argv += ["-ex", c]
    if elf:
        argv.append(str(elf))
    if background:
        return subprocess.Popen(argv, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
    try:
        r = subprocess.run(argv, capture_output=True, text=True,
                           timeout=timeout)
        return r.stdout + r.stderr
    except subprocess.TimeoutExpired as e:
        return str(e.stdout or "") + "\n(gdb timed out)"


def board_up(b, *more):
    """`devboard` in the background, once it says it is there"""
    p = b.bg("devboard", *more)
    said = ""
    end = time.time() + 20
    while time.time() < end:
        line = p.stdout.readline()
        if not line:
            break
        said += line
        if "Ctrl-C pulls it out" in line:
            return p, said
    return None, said


def board_down(p):
    p.terminate()
    try:
        out, _ = p.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()
        out, _ = p.communicate()
    return out


def probe_checks(b, tool, args):
    gport, uport = free_port(), free_port()
    p, said = board_up(b, "--gdb-port", gport, "--uart-port", uport)
    if not check("devboard: the board is on the header, its ports said",
                 p and f"TCP port {gport}" in said and f"TCP port {uport}" in said,
                 said):
        return
    try:
        out = gdb(tool, gport, ["monitor version", "monitor swdp_scan"])
        check("monitor version is the board's probe",
              fe_devboard.BMP_IDENT in out, out)
        check("with Debug off, swdp_scan fails (the SWD pins are analog)",
              "SW-DP scan failed!" in out, out)
        code, text = b.fe("cli", "sysctl debug 1")
        out = gdb(tool, gport, ["monitor swdp_scan", "attach 1",
                                "info registers pc", "detach"])
        check("after sysctl debug 1 it finds the STM32WBxx",
              " 1      STM32WBxx M4" in out, text + out)
        check("attach 1 halts it in the firmware's code, detach lets it go",
              "pc             0x80" in out and "detached" in out, out)
        ok, text = b.answers()
        check("the firmware runs on after detach (its CLI answers)", ok, text)

        elf = b.dir / "firmware.elf"
        if elf.exists():
            bridge = socket.create_connection(("127.0.0.1", uport))
            bridge.settimeout(0.5)
            g = gdb(tool, gport, [
                "monitor swdp_scan", "attach 1",
                "set var furi_hal_debug_gdb_session_active = 1", "continue",
                "x/i $pc", "set var furi_hal_debug_gdb_session_active = 0",
                # past the BKPT (the probe steps over it), then let it run:
                # a detach on it would run it with no debugger, a HardFault
                "stepi", "detach"], elf, background=True)
            time.sleep(8)
            code, text = b.fe("cli", f"loader open {APP}", timeout=60)
            try:
                out, _ = g.communicate(timeout=90)
            except subprocess.TimeoutExpired:
                g.kill()
                out, _ = g.communicate()
            logged = b""
            end = time.time() + 3
            while time.time() < end:
                try:
                    chunk = bridge.recv(65536)
                    if not chunk:
                        break
                    logged += chunk
                except socket.timeout:
                    pass
            bridge.close()
            check("an app's start halts at the loader's BKPT (SIGTRAP)",
                  "SIGTRAP" in out and "bkpt" in out, out + text)
            check("the log says so on the UART bridge's TCP port",
                  b"Triggering BP for debugger" in logged, logged[-2000:])
            code, text = b.fe("cli", "loader info")
            check("on from the BKPT, the app starts",
                  "snake" in text.lower() or "Snake" in text, text + out)
            b.fe("cli", "loader close")
        else:
            print("  (no firmware.elf in the device: the BKPT checks are "
                  "skipped)")

        objcopy = shutil.which("arm-none-eabi-objcopy")
        if objcopy:
            with tempfile.TemporaryDirectory() as work:
                raw = pathlib.Path(work) / "page.bin"
                page = pathlib.Path(work) / "page.elf"
                raw.write_bytes(b"FlipperEmu devboard " * 200)
                subprocess.run([objcopy, "-I", "binary", "-O",
                                "elf32-littlearm", "-B", "arm",
                                "--rename-section", ".data=.text,alloc,load,"
                                "readonly,code,contents",
                                "--change-section-address", ".text=0x080C0000",
                                str(raw), str(page)], check=True)
                out = gdb(tool, gport, ["monitor swdp_scan", "attach 1",
                                        "load", "compare-sections",
                                        "x/s 0x080C0000", "detach"], page)
                check("load writes a page of the flash (vFlash), "
                      "compare-sections matches",
                      "Loading section .text" in out and "matched" in out
                      and "MIS-MATCHED" not in out, out)
        else:
            print("  (no arm-none-eabi-objcopy: the flash check is skipped)")
    finally:
        out = board_down(p)
        check("the board leaves the header at Ctrl-C",
              "the Wi-Fi Developer Board is out" in out, out)

    if objcopy:
        b.stop()
        ok = b.start()
        code, text = b.fe("wait", "--timeout", args.timeout)
        check("the device starts again", ok and code == 0, text)
        p, said = board_up(b, "--gdb-port", gport, "--uart-port", "-1")
        if p:
            b.fe("cli", "sysctl debug 1")
            out = gdb(tool, gport, ["monitor swdp_scan", "attach 1",
                                    "x/s 0x080C0000", "detach"])
            board_down(p)
            check("the page written by the probe is still in the flash",
                  "FlipperEmu devboard" in out, out)


def marauder_checks(b):
    p, said = board_up(b, "--firmware", "marauder")
    if not check("devboard --firmware marauder: Marauder on pins 13 and 14",
                 p and "ESP32 Marauder" in said, said):
        return
    try:
        code, text = b.fe("push", ROOT / "examples/devboard/marauder.js", "/ext")
        code, text = b.fe("cli", "js /ext/marauder.js", timeout=120)
        names = [ap["essid"] for ap in fe_devboard.DEFAULT_SCENE["aps"]
                 if ap["essid"]]
        check("a scan from the Flipper finds the scene's access points",
              "Scanning for APs and Stations" in text
              and all(f"ESSID: {n}" in text for n in names), text)
        check("list -a lists them",
              "#list -a" in text and "[0][CH:" in text, text)
    finally:
        board_down(p)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("firmware")
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="seconds to wait for the firmware to start")
    ap.add_argument("--keep", action="store_true",
                    help="keep the throwaway device")
    args = ap.parse_args()
    check_limit.arm(900)
    tool = gdb_tool()
    b = Bench(f"devboardcheck-{int(time.time())}-{os.getpid()}", args.firmware,
              args.timeout)
    try:
        ok = b.start(first=True)
        said = ""
        if ok:
            code, text = b.fe("wait", "--timeout", args.timeout)
            ok, said = b.answers() if code == 0 else (False, text)
        if check("the firmware starts, and its CLI answers", ok,
                 said + b.uart_text()[-2000:]):
            if tool:
                probe_checks(b, tool, args)
            else:
                print("  (no arm-none-eabi-gdb or gdb-multiarch: the probe's "
                      "checks are skipped)")
            marauder_checks(b)
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


if __name__ == "__main__":
    sys.exit(main())
