#!/usr/bin/env python3
# Copyright (c) 2026, the FlipperEmu contributors
# SPDX-License-Identifier: BSD-3-Clause
"""Test the Wi-Fi Developer Board (scripts/fe_devboard.py) without the
emulator: its debug probe against a real gdb and QEMU's own gdb stub, and
its Marauder firmware against a Flipper's side played here.

    scripts/devboard_selftest.py

Needs what a distribution has: qemu-system-arm (any recent one: the test
runs QEMU's netduinoplus2, a Cortex-M4 with its flash at 0x08000000 as the
STM32WB55 has it), arm-none-eabi-gcc and gdb-multiarch (or
arm-none-eabi-gdb).  None of FlipperEmu's QEMU is needed: this is the board
alone.  scripts/devboard_check.py tests it on the emulator, with a Flipper's
firmware.

The program it builds (scripts/devboard_selftest/target.c) counts in a loop
and runs a BKPT where a flag is set, as the Flipper's loader does for its
debugger.  Through the board's GDB server: `monitor version` and `help`,
`monitor swdp_scan` and the target's line, `attach 1` halts it, the
probe's memory map, `load` of another build of it into the flash (erased
and written in pages, kept in the flash's image, and `compare-sections`
matches), a plain write to the flash refused, the BKPT halts it with
SIGTRAP and `continue` goes on after it, breakpoints in the flash are the
processor's six (a seventh is refused), a watchpoint, `detach` lets it run,
and a second gdb waits for the first.  With the SWD pins off the scan
fails, as on the Flipper with Debug off.  The UART bridge carries bytes
both ways, from TCP and from the USB serial port.  Marauder: its banner, a
scan that finds the scene's access points and stations in a few seconds,
`list`, `select`, `info`, `ssid`, an attack that needs a target and one
with it, `stopscan`.  Exit status 0 = pass.
"""
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fe_devboard as fd  # noqa: E402

FAILS = []


def check(cond, what, detail=""):
    print(("  ok   " if cond else "  FAIL ") + what, flush=True)
    if not cond:
        FAILS.append(what)
        if detail:
            print("       " + str(detail).replace("\n", "\n       ")[:3000])


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def tool(*names):
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


class QemuTarget(fd.Target):
    """QEMU's netduinoplus2 as the Flipper"""

    def __init__(self, gdb_port, monitor):
        self.gdb_port = gdb_port
        self.monitor = monitor
        self.swd_off = None
        self.kept = []
        self.bauds = []
        self.resets = 0

    def stub(self):
        return socket.create_connection(("127.0.0.1", self.gdb_port), 5)

    def swd(self):
        return self.swd_off

    def hmp(self, command):
        s = socket.socket(socket.AF_UNIX)
        s.connect(self.monitor)
        s.settimeout(2)
        time.sleep(0.05)
        s.recv(4096)
        s.sendall(command.encode() + b"\n")
        time.sleep(0.2)
        try:
            out = s.recv(65536)
        except socket.timeout:
            out = b""
        s.close()
        return out.decode(errors="replace")

    def reset(self):
        self.resets += 1
        self.hmp("system_reset")

    def persist(self, addr, data):
        self.kept.append((addr, len(data)))
        return None

    def line_baud(self, rate):
        self.bauds.append(rate)


def gdb_run(gdb, port, commands, elf=None, timeout=30):
    argv = [gdb, "-batch", "-nx", "-ex", "set pagination off",
            "-ex", "set confirm off",
            "-ex", f"target extended-remote 127.0.0.1:{port}"]
    for c in commands:
        argv += ["-ex", c]
    if elf:
        argv.append(str(elf))
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=timeout)
        return p.stdout + p.stderr
    except subprocess.TimeoutExpired as e:
        return (e.stdout or b"").decode() + "\n(gdb timed out)"


def probe_tests(work, qemu, gcc, gdb):
    elfs = []
    for mark in (1, 2):
        elf = work / f"target{mark}.elf"
        subprocess.run([gcc, "-mcpu=cortex-m4", "-mthumb", "-O1", "-g",
                        "-nostdlib", "-ffreestanding",
                        f"-DMARK=0x1111111{mark}",
                        "-T", str(HERE / "devboard_selftest" / "target.ld"),
                        str(HERE / "devboard_selftest" / "target.c"),
                        "-o", str(elf)], check=True)
        elfs.append(elf)
    stub_port = free_port()
    monitor = str(work / "monitor.sock")
    q = subprocess.Popen([qemu, "-M", "netduinoplus2", "-kernel", str(elfs[0]),
                          "-nographic", "-serial", "null", "-monitor",
                          f"unix:{monitor},server=on,wait=off",
                          "-gdb", f"tcp:127.0.0.1:{stub_port}"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    for _ in range(100):
        if os.path.exists(monitor):
            break
        time.sleep(0.05)
    time.sleep(0.3)
    target = QemuTarget(stub_port, monitor)
    flipper, board_end = socket.socketpair()
    logs = []
    gdb_port, uart_port = free_port(), free_port()
    board = fd.Board(target, board_end, "blackmagic", gdb_port=gdb_port,
                     uart_port=uart_port,
                     links={"gdb": str(work / "gdb.tty"),
                            "uart": str(work / "uart.tty")},
                     log=logs.append)
    t = threading.Thread(target=board.run, daemon=True)
    t.start()
    try:
        check(target.bauds == [230400], "the board's UART is at 230400 baud",
              target.bauds)
        out = gdb_run(gdb, gdb_port, ["monitor version", "monitor help"])
        check("Black Magic Probe ESP32-S2v1.9.0-1-g1cdc914, Hardware Version 0"
              in out, "monitor version: the probe's own words", out)
        check("\tswdp_scan -- Scan SW-DP for devices" in out
              and "vector_catch" not in out,
              "monitor help: the general commands (no target's yet)", out)

        out = gdb_run(gdb, gdb_port, ["attach 1"])
        check("E01" in out or "fail" in out.lower() or "Couldn't" in out
              or "No such" in out, "attach before a scan is refused", out)

        out = gdb_run(gdb, gdb_port, [
            "monitor swdp_scan", "attach 1", "info registers pc",
            "monitor help", "info mem", "p/x VERSION"], elfs[0])
        check("Available Targets:\nNo. Att Driver\n 1      STM32WBxx M4" in out,
              "swdp_scan lists the target as the probe does", out)
        check("pc " in out and "0x80000" in out,
              "attach 1 halts it, in its code in the flash", out)
        check("STM32WBxx specific commands:" in out
              and "\tvector_catch -- Catch exception vectors" in out,
              "attached, help has the target's commands", out)
        check("flash blocksize 0x1000" in out and "0x20000000 0x20040000 rw" in out,
              "the memory map is the probe's: flash in 4 KiB pages, the RAM",
              out)
        check("0x11111111" in out, "it reads the program's memory", out)

        target.kept.clear()
        out = gdb_run(gdb, gdb_port, [
            "monitor swdp_scan", "attach 1", "load", "compare-sections",
            "p/x VERSION", "set var *(int*)0x08000100 = 5"], elfs[1], 60)
        check("Loading section .text" in out and "Transfer rate" in out,
              "load writes the other build into the flash", out)
        check("matched" in out and "MIS-MATCHED" not in out,
              "compare-sections: the probe's CRC of it matches", out)
        check("0x11111112" in out, "the flash has the new build", out)
        check(target.kept and target.kept[0][0] == 0x08000000,
              "what was written is kept in the flash's image", target.kept)
        check("Cannot access memory" in out or "Writing to flash memory forbidden"
              in out, "a plain write to the flash is refused", out)

        out = gdb_run(gdb, gdb_port, [
            "monitor swdp_scan", "attach 1", "set var after_bkpt = 0",
            "set var debugger = 1", "continue", "x/i $pc",
            "set var debugger = 0", "hbreak tick", "continue",
            "p after_bkpt"], elfs[1])
        check("SIGTRAP" in out and "bkpt" in out,
              "the BKPT halts it (SIGTRAP), the PC on the BKPT", out)
        check("$1 = 1" in out or "= 1\n" in out.split("p after_bkpt")[-1]
              or out.rstrip().endswith("= 1"),
              "continue goes on after the BKPT (the code after it ran)", out)

        out = gdb_run(gdb, gdb_port, [
            "monitor swdp_scan", "attach 1", "watch watched", "continue",
            "delete", "hbreak tick", "hbreak app_start", "hbreak reset_handler",
            "hbreak default_handler", "hbreak *0x08000060", "hbreak *0x08000064",
            "hbreak *0x08000068", "continue"], elfs[1])
        check("atchpoint" in out and "Old value" in out,
              "a watchpoint stops it where the value changed", out)
        check("Could not insert hardware breakpoint" in out
              or "Cannot insert hardware breakpoint" in out
              or "hardware breakpoint" in out.lower() and "E01" not in out
              and "Could not insert" in out,
              "a seventh breakpoint in hardware is refused", out)

        out = gdb_run(gdb, gdb_port, ["monitor swdp_scan", "attach 1",
                                      "p counter", "detach"], elfs[1])
        first = out
        time.sleep(0.3)
        out = gdb_run(gdb, gdb_port, ["monitor swdp_scan", "attach 1",
                                      "p counter", "detach"], elfs[1])
        try:
            a = int(first.split("$1 = ")[1].split()[0])
            b = int(out.split("$1 = ")[1].split()[0])
        except (IndexError, ValueError):
            a = b = 0
        check(b != a, "detach lets it run (the counter moved between two "
              "attaches)", first + out)

        # a second gdb while one is connected waits for it
        hold = socket.create_connection(("127.0.0.1", gdb_port))
        time.sleep(0.2)
        second = socket.create_connection(("127.0.0.1", gdb_port))
        second.settimeout(1)
        try:
            gone = second.recv(10) == b""
        except socket.timeout:
            gone = False
        check(gone, "a second gdb is turned away while one is connected")
        second.close()
        hold.close()
        time.sleep(0.2)

        target.swd_off = "the SWD pins are analog (Debug is off)"
        out = gdb_run(gdb, gdb_port, ["monitor swdp_scan", "attach 1"])
        check("SW-DP scan failed!" in out, "with the SWD pins off the scan "
              "fails", out)
        target.swd_off = None

        # the UART bridge: TCP 3456, and the USB serial port
        c = socket.create_connection(("127.0.0.1", uart_port))
        time.sleep(0.2)
        flipper.sendall(b"[I][Flipper] log line\r\n")
        c.settimeout(2)
        got = b""
        try:
            while b"\n" not in got:
                got += c.recv(100)
        except socket.timeout:
            pass
        check(got == b"[I][Flipper] log line\r\n", "the Flipper's USART comes "
              "out of the UART bridge's TCP port", got)
        c.sendall(b"help\r\n")
        flipper.settimeout(2)
        got = b""
        try:
            while b"\n" not in got:
                got += flipper.recv(100)
        except socket.timeout:
            pass
        check(got == b"help\r\n", "and what is sent there goes to the "
              "Flipper's RX pin", got)
        c.close()
        fdu = os.open(str(work / "uart.tty"), os.O_RDWR | os.O_NOCTTY)
        import tty
        tty.setraw(fdu)
        flipper.sendall(b"via usb\n")
        time.sleep(0.3)
        os.set_blocking(fdu, False)
        try:
            got = os.read(fdu, 100)
        except BlockingIOError:
            got = b""
        os.close(fdu)
        check(got == b"via usb\n", "and out of its USB serial port", got)

        # gdb on the USB serial port
        argv = [gdb, "-batch", "-nx", "-ex", "set pagination off", "-ex",
                f"target extended-remote {work / 'gdb.tty'}",
                "-ex", "monitor swdp_scan", "-ex", "attach 1", "-ex",
                "p/x VERSION", "-ex", "detach", str(elfs[1])]
        out = subprocess.run(argv, capture_output=True, text=True,
                             timeout=30).stdout
        check("0x11111112" in out, "gdb on the board's USB serial port", out)
    finally:
        board.stop()
        t.join(3)
        board.close()
        q.terminate()
        q.wait(5)


def marauder_tests():
    clock = [1000.0]
    out = bytearray()
    m = fd.Marauder(out.extend, now=lambda: clock[0], seed=7)

    def run(line, seconds=0.0):
        del out[:]
        m.take(line.encode() + b"\n")
        end = clock[0] + seconds
        while clock[0] < end:
            clock[0] += 0.05
            m.tick()
        return out.decode()

    m.boot()
    text = out.decode()
    check("ESP32 Marauder" in text and fd.MARAUDER_VERSION in text
          and text.endswith("> "), "Marauder's banner and its prompt", text)
    text = run("list -a")
    check(text == "#list -a\r\n0 selected\r\n> ", "an empty list before a "
          "scan", text)
    text = run("attack -t deauth")
    check("You don't have any targets selected. Use select" in text,
          "an attack needs a target", text)
    text = run("scanall", 8)
    aps = fd.DEFAULT_SCENE["aps"]
    check(text.startswith("#scanall\r\nScanning for APs and Stations. Stop "
                          "with stopscan\r\n"), "scanall starts as Marauder's",
          text)
    found = [ap for ap in aps if f"{ap['bssid']} ESSID: {ap['essid']} " in text]
    check(len(found) == len(aps), "the scan finds every access point of the "
          "scene", text)
    stations = sum(len(ap["stations"]) for ap in aps)
    check(text.count(" -> ") == stations, "and every station", text)
    text = run("stopscan")
    check("Stopping WiFi tran/recv" in text, "stopscan", text)
    text = run("list -a")
    check("[0][CH:" in text and f"{len(aps)} " not in text.split("\r\n")[0]
          and "0 selected" in text, "list -a: what the scan found", text)
    text = run("select -a 0,2")
    check("2 selected, 0 unselected" in text, "select -a 0,2", text)
    text = run("list -a")
    check(text.count("(selected)") == 2, "list -a marks them", text)
    text = run("info -a 0")
    check("   ESSID: " in text and "Security: " in text, "info -a 0", text)
    text = run("attack -t deauth", 2)
    check("Starting Deauthentication attack. Stop with stopscan" in text,
          "with a target the attack starts", text)
    run("stopscan")
    text = run("ssid -a -n Free WiFi")
    check("Free WiFi" in text, "ssid -a -n", text)
    text = run("list -s")
    check("[0] Free WiFi" in text, "list -s", text)
    text = run("attack -t beacon -l")
    check("Starting Beacon list spam" in text, "beacon list spam", text)
    run("stopscan")
    text = run("list -c")
    check(text.count("  [") == stations, "list -c: the stations under their "
          "access points", text)
    text = run("help")
    check("============ Commands ============" in text, "help", text)
    text = run("sniffprobe", 4)
    # (Marauder prints "Starting" and the scan's name with no space between)
    check("StartingProbe sniff. Stop with stopscan" in text
          and "Requesting: " in text, "sniffprobe: probe requests", text)
    run("stopscan")
    text = run("channel -s 11")
    check("Set channel: 11" in text, "channel -s 11", text)


def main():
    qemu = tool("qemu-system-arm")
    gcc = tool("arm-none-eabi-gcc")
    gdb = tool("arm-none-eabi-gdb", "gdb-multiarch")
    print("Marauder:")
    marauder_tests()
    if not (qemu and gcc and gdb):
        print("the probe: skipped (needs qemu-system-arm, arm-none-eabi-gcc "
              "and gdb-multiarch)")
    else:
        print("the probe (Black Magic), with QEMU's netduinoplus2:")
        with tempfile.TemporaryDirectory() as work:
            probe_tests(pathlib.Path(work), qemu, gcc, gdb)
    print(f"{len(FAILS)} failed" if FAILS else "all passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
