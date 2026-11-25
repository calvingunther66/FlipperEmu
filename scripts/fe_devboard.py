"""The Wi-Fi Developer Board: the ESP32-S2 board that plugs into the Flipper's
GPIO header, as a program on this computer at the other end of its pins.

The board is wired to the header so:

    pins 13 and 14   the Flipper's USART1 (TX, RX) to the ESP32-S2's UART0
    pins 10 and 12   the Flipper's SWD (SWCLK, SWDIO) to two GPIOs of the
                     ESP32-S2, which the debug probe in its firmware drives
    pin 9 (3V3)      its power: it is on while the Flipper is

What it does is what its firmware does, and two are modelled:

`blackmagic`, the firmware it ships with (flipperdevices/blackmagic-esp32-s2,
Black Magic Probe at 1cdc914, "v1.9.0-1-g1cdc914").  The board is a debug
probe for the Flipper's STM32WB55: a GDB server on TCP port 2345 (over Wi-Fi
on the real board) and on a USB serial port, and a bridge from the USB
serial port's second interface and TCP port 3456 to the Flipper's USART1 at
230400 baud, which is where the firmware logs.  The GDB server is the
probe's: `monitor swdp_scan` finds the target (and does not when the
firmware has its SWD pins off: Settings > System > Debug), `attach 1` halts
it, the memory map is the probe's (flash in 4 KiB pages: gdb's `load` erases
and writes it, and `compare-sections` takes a CRC of it on the probe), six
breakpoints and four watchpoints in hardware, and a BKPT instruction halts
the processor as it does with a probe on SWD (the firmware has one where an
app starts and where it crashes, for a debugger that it knows is there).
Behind it is QEMU's own gdb stub: a packet that a probe answers itself is
answered here, one that reads or writes the core is passed on.

`marauder`, ESP32 Marauder (justcallmekoko/ESP32Marauder, v1.21.0, built for
"Flipper Zero Dev Board"): a command line on UART0 at 115200 baud, which the
WiFi Marauder app on the Flipper drives.  The air around the board is a
scene (a JSON file, or one built in): access points with their channel,
signal and stations, and probe requests.  `scanall`, `sniffbeacon` and the
other sniffs find them over a few seconds, as a scan does, and print them as
Marauder does; `list`, `select`, `info`, `ssid`, `clearlist`, `channel` and
the attacks work on what was found.  Nothing is sent on any air: an attack
says what Marauder says, and counts.

The board is in this module without the emulator: `Board` takes a `Target`,
which is what it needs of the Flipper (its serial port, its gdb stub, a
reset, its flash image, the state of its SWD pins).  The launcher's
`devboard` gives it the emulator's; scripts/devboard_selftest.py gives it a
QEMU of the distribution's own with a small program, to test the probe
against a real gdb and a real stub.

Copyright (c) 2026, the FlipperEmu contributors
SPDX-License-Identifier: BSD-3-Clause
"""
import json
import os
import random
import re
import select
import socket
import time

# --------------------------------------------------------------------------
# GDB's remote serial protocol
# --------------------------------------------------------------------------


def checksum(data):
    return sum(data) & 0xFF


def frame(payload):
    """A packet as it goes on the wire: $payload#cs, with the bytes that the
    protocol reserves escaped"""
    if isinstance(payload, str):
        payload = payload.encode()
    out = bytearray()
    for b in payload:
        if b in b"$#}*":
            out += bytes([0x7D, b ^ 0x20])
        else:
            out.append(b)
    return b"$" + bytes(out) + b"#%02x" % checksum(out)


def unescape(data):
    """The bytes of a binary packet (X, vFlashWrite): }c is c ^ 0x20"""
    out = bytearray()
    it = iter(data)
    for b in it:
        if b == 0x7D:
            out.append(next(it, 0x20) ^ 0x20)
        else:
            out.append(b)
    return bytes(out)


class Reader:
    """Cuts a byte stream into what is in it: ("ack", b"+"), ("nak", b"-"),
    ("break", b"\\x03"), ("eot", b"\\x04"), ("packet", payload) and ("bad",
    payload) for a packet whose checksum is wrong.  A payload is as it came,
    escapes and all."""

    def __init__(self):
        self.buf = b""

    def feed(self, data):
        self.buf += data
        events = []
        while self.buf:
            c = self.buf[:1]
            if c == b"$":
                end = self.buf.find(b"#")
                if end < 0 or len(self.buf) < end + 3:
                    break
                payload = self.buf[1:end]
                try:
                    good = int(self.buf[end + 1:end + 3], 16) == checksum(payload)
                except ValueError:
                    good = False
                events.append(("packet" if good else "bad", payload))
                self.buf = self.buf[end + 3:]
                continue
            self.buf = self.buf[1:]
            if c == b"+":
                events.append(("ack", c))
            elif c == b"-":
                events.append(("nak", c))
            elif c == b"\x03":
                events.append(("break", c))
            elif c == b"\x04":
                events.append(("eot", c))
            # anything else between packets is noise, as for a probe
        return events


def hexs(text):
    return text.encode().hex() if isinstance(text, str) else text.hex()


def crc32(data, crc=0xFFFFFFFF):
    """The CRC of gdb's qCRC (libiberty's xcrc32): polynomial 0x04C11DB7,
    most significant bit first, no final inversion"""
    for b in data:
        crc ^= b << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) if crc & 0x80000000 else crc << 1
            crc &= 0xFFFFFFFF
    return crc


class StubError(Exception):
    """The stub behind the probe is gone, or did not answer"""


class Stub:
    """QEMU's gdb stub, which is the probe's way to the processor.  It is
    talked to without acks.  When it connects QEMU stops the machine, as a
    probe's attach halts the core; its `D` lets it go on."""

    PC = 15                 # the program counter's number in its registers
    CHUNK = 0x600           # bytes in one m or M packet

    def __init__(self, sock, timeout=5.0):
        self.sock = sock
        self.timeout = timeout
        self.reader = Reader()
        self.pending = []
        self.acks = True
        self.sock.setblocking(False)
        self.request("QStartNoAckMode")
        self.acks = False
        self.features = self.request("qSupported:swbreak+;hwbreak+;"
                                     "xmlRegisters=arm")

    def fileno(self):
        return self.sock.fileno()

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def send(self, payload):
        try:
            self.sock.sendall(frame(payload))
        except OSError as e:
            raise StubError(f"the gdb stub is gone ({e})") from None

    def _read(self, seconds):
        if not select.select([self.sock], [], [], seconds)[0]:
            return False
        try:
            data = self.sock.recv(65536)
        except BlockingIOError:
            return True
        except OSError as e:
            raise StubError(f"the gdb stub is gone ({e})") from None
        if not data:
            raise StubError("the gdb stub closed its end")
        for kind, payload in self.reader.feed(data):
            if kind == "packet":
                if self.acks:
                    self.sock.sendall(b"+")
                self.pending.append(payload)
        return True

    def reply(self, seconds=None):
        """The stub's next packet (console output, O..., is passed over)"""
        end = time.monotonic() + (self.timeout if seconds is None else seconds)
        while True:
            while self.pending:
                p = self.pending.pop(0)
                if p[:1] == b"O" and p != b"OK" and re.fullmatch(rb"O[0-9a-fA-F]+", p):
                    continue
                return p
            left = end - time.monotonic()
            if left <= 0:
                raise StubError("the gdb stub did not answer in time")
            self._read(left)

    def poll(self):
        """A packet that has come, or None (for a stop while it runs)"""
        if not self.pending:
            self._read(0)
        return self.reply(0) if self.pending else None

    def request(self, payload, seconds=None):
        self.send(payload)
        return self.reply(seconds)

    def read(self, addr, size):
        """Memory, or None where the stub refused"""
        out = bytearray()
        while size > 0:
            n = min(size, self.CHUNK)
            r = self.request(f"m{addr:x},{n:x}")
            if r[:1] == b"E" or len(r) != 2 * n:
                return None
            out += bytes.fromhex(r.decode())
            addr += n
            size -= n
        return bytes(out)

    def write(self, addr, data):
        for at in range(0, len(data), self.CHUNK):
            piece = data[at:at + self.CHUNK]
            r = self.request(f"M{addr + at:x},{len(piece):x}:{piece.hex()}")
            if r != b"OK":
                return False
        return True

    def pc(self):
        r = self.request(f"p{self.PC:x}")
        if len(r) != 8:
            return None
        return int.from_bytes(bytes.fromhex(r.decode()), "little")

    def set_pc(self, value):
        return self.request(f"P{self.PC:x}={value.to_bytes(4, 'little').hex()}") == b"OK"


# --------------------------------------------------------------------------
# What the board needs of the Flipper
# --------------------------------------------------------------------------


class Target:
    """The Flipper, as the board sees it.  The launcher's `devboard` makes
    one of the emulator's; a test, one of its own."""

    name = "STM32WBxx"          # the target driver's name in the probe
    core = "M4"
    flash_base = 0x08000000
    flash_page = 0x1000
    flash_size_reg = 0x1FFF75E0     # FLASHSIZE: KiB, 16 bits
    sfr_reg = 0x58004080            # FLASH->SFR: SFSA, the secure area's page

    def stub(self):
        """A connected socket to the processor's gdb stub"""
        raise NotImplementedError

    def swd(self):
        """None when the SWD pins are as the probe needs them; else why
        they are not, in words"""
        return None

    def reset(self):
        """Pull the processor's nRST: the machine starts again"""

    def persist(self, addr, data):
        """Flash written by the probe is in the machine's memory already;
        this keeps it in the flash's image as well.  -> None, or why not"""
        return None

    def line_baud(self, rate):
        """The board's UART is at this speed from now on (0: whatever the
        Flipper's is)"""


# --------------------------------------------------------------------------
# The stock firmware: Black Magic Probe
# --------------------------------------------------------------------------

BMP_VERSION = "v1.9.0-1-g1cdc914"
BMP_IDENT = "Black Magic Probe ESP32-S2" + BMP_VERSION
BUF_SIZE = 1024

GENERAL_COMMANDS = [
    ("version", "Display firmware version info"),
    ("help", "Display help for monitor commands"),
    ("jtag_scan", "Scan JTAG chain for devices"),
    ("swdp_scan", "Scan SW-DP for devices"),
    ("auto_scan", "Automatically scan all chain types for devices"),
    ("frequency", "set minimum high and low times"),
    ("targets", "Display list of available targets"),
    ("morse", "Display morse error message"),
    ("halt_timeout", "Timeout (ms) to wait until Cortex-M is halted: (Default 2000)"),
    ("connect_rst", "Configure connect under reset: (enable|disable)"),
    ("reset", "Pulse the nRST line - disconnects target"),
    ("tdi_low_reset", "Pulse nRST with TDI set low to attempt to wake certain "
                      "targets up (eg LPC82x)"),
    ("heapinfo", "Set semihosting heapinfo"),
]
TARGET_COMMANDS = [
    ("Cortex-M", [("vector_catch", "Catch exception vectors")]),
    ("STM32WBxx", [("erase_bank1", "Erase entire bank1 flash memory"),
                   ("erase_bank2", "Erase entire bank2 flash memory"),
                   ("option", "Manipulate option bytes")]),
]
VECTORS = ["reset", None, None, None, "mm", None, None, None, "nocp", "chk",
           "stat", "bus", "int", "hard"]
HW_BREAKPOINTS = 6          # the FPB of a Cortex-M4
HW_WATCHPOINTS = 4          # its DWT


def is_bkpt(halfword):
    return (halfword & 0xFF00) == 0xBE00


class BlackMagic:
    """The GDB server of the board's probe: one gdb at a time, on TCP or on
    the USB serial port.  `log(text)` hears what the board does."""

    def __init__(self, target, log=None):
        self.target = target
        self.log = log or (lambda text: None)
        self.client = None          # (read fd, write fd, name)
        self.closer = None
        self.reader = Reader()
        self.stub = None            # attached: the stub, else None
        self.scanned = False        # a scan found the target
        self.last_target = False    # it was attached once, and may be again
        self.running = False
        self.stop = b"T05"          # why it stopped last, as the probe says
        self.morse = None
        self.connect_rst = False
        self.halt_timeout = 2000
        self.bkpts = set()          # addresses of BKPTs, each a stub breakpoint
        self.hard = {}              # (type, addr, kind) of gdb's in hardware
        self.dirty = set()          # flash pages written since vFlashDone
        self.flash_end = None
        self.secure = None          # where CPU2's secure flash begins
        self.catch = 0

    # ---- the gdb on the other end

    def connect(self, rfd, wfd, name, close=None):
        """A gdb connects (`close()` ends its connection).  -> False when one
        is there already"""
        if self.client:
            return False
        self.client = (rfd, wfd, name)
        self.closer = close
        self.reader = Reader()
        self.log(f"gdb connected ({name})")
        return True

    def disconnect(self):
        """The gdb has gone: as the probe does, the target is let go"""
        if not self.client:
            return
        name = self.client[2]
        self.client = None
        if self.closer:
            self.closer()
            self.closer = None
        self._detach()
        self.log(f"gdb disconnected ({name})")

    def _out(self, data):
        try:
            os.write(self.client[1], data)
        except (OSError, TypeError):
            pass

    def put(self, payload):
        self._out(frame(payload))

    def say(self, text):
        """What a monitor command prints, in gdb's console"""
        self.put("O" + hexs(text))

    def fds(self):
        out = []
        if self.client:
            out.append(self.client[0])
        if self.stub and self.running:
            out.append(self.stub.fileno())
        return out

    def ready(self, fd):
        """Something to read on one of fds()"""
        if self.client and fd == self.client[0]:
            try:
                data = os.read(fd, 65536)
            except BlockingIOError:
                return
            except OSError:
                data = b""
            if not data:
                self.disconnect()
                return
            self.take(data)
        elif self.stub and fd == self.stub.fileno():
            self._poll_stop()

    def take(self, data):
        """What gdb sent"""
        for kind, payload in self.reader.feed(data):
            if kind == "packet":
                self._out(b"+")
                try:
                    self.packet(payload)
                except StubError as e:
                    self._lost(str(e))
                    self.put("E01")
            elif kind == "bad":
                self._out(b"-")
            elif kind == "break":
                if self.stub and self.running:
                    try:
                        self.stub.sock.sendall(b"\x03")
                    except OSError:
                        pass
            elif kind == "eot":
                self._detach()

    def _lost(self, why):
        self.log(f"the target is lost: {why}")
        if self.stub:
            self.stub.close()
        self.stub = None
        self.running = False
        self.morse = "TARGET LOST."

    # ---- running

    def _poll_stop(self):
        try:
            p = self.stub.poll()
        except StubError as e:
            self._lost(str(e))
            self.put("X%02X" % 0x1D)        # SIGLOST
            return
        if p is None:
            return
        self.running = False
        self.stop = self._stop_reply(p)
        self.put(self.stop)

    def _stop_reply(self, p):
        """The stub's stop reply as the probe has it: no thread, and a
        watchpoint as watch:ADDR"""
        text = p.decode(errors="replace")
        if text[:1] in "TS":
            sig = int(text[1:3], 16)
            m = re.search(r"(?:r|a)?watch:([0-9a-fA-F]+);", text)
            if m:
                return "T%02Xwatch:%08X;" % (sig, int(m[1], 16))
            return "T%02X" % (5 if sig not in (2,) else 2)
        if text[:1] in "WX":
            return text
        return "T05"

    def _resume(self, packet):
        """c or s: from a BKPT, from the instruction after it (the probe
        steps past one; the core would only halt on it again)"""
        pc = self.stub.pc()
        if pc is not None and pc in self.bkpts:
            word = self.stub.read(pc, 2)
            if word and is_bkpt(int.from_bytes(word, "little")):
                self.stub.set_pc(pc + 2)
        self.stub.send(packet)
        self.running = True

    # ---- attaching

    def _attach(self):
        if self.stub:
            return True
        why = self.target.swd()
        if why:
            self.morse = "TARGET LOST."
            self.log(f"attach: {why}")
            return False
        try:
            self.stub = Stub(self.target.stub())
            self.stub.request("?")
            self._learn()
        except (OSError, StubError) as e:
            self.log(f"attach: {e}")
            if self.stub:
                self.stub.close()
            self.stub = None
            return False
        self.running = False
        self.stop = b"T05"
        self.last_target = True
        self.morse = None
        self.log("attached: the processor is halted")
        return True

    def _learn(self):
        """What the probe reads as it attaches: the flash's size and where
        CPU2's secure part begins; and every BKPT that the flash has"""
        size = self.stub.read(self.target.flash_size_reg, 2)
        kib = int.from_bytes(size, "little") if size else 1024
        if not 0 < kib <= 2048:
            kib = 1024
        self.flash_end = self.target.flash_base + kib * 1024
        sfr = self.stub.read(self.target.sfr_reg, 4)
        sfsa = (int.from_bytes(sfr, "little") & 0xFF) if sfr else 0
        self.secure = (self.target.flash_base + sfsa * self.target.flash_page
                       if sfsa else self.flash_end)
        self.bkpts = set()
        self.hard = {}
        self._scan_bkpts(self.target.flash_base,
                         self.secure - self.target.flash_base)

    def _scan_bkpts(self, addr, size, data=None):
        """Mark the BKPTs in that memory: QEMU's stub stops where each one
        is, before it runs (a BKPT that ran would be a HardFault there:
        nothing in the emulator halts the core as a probe on SWD does).
        A breakpoint of the stub's changes nothing in memory, so one where
        the halfword is data and never runs costs nothing."""
        if data is None:
            data = self.stub.read(addr, size)
            if data is None:
                return
        start = addr + (addr & 1)
        for a in range(start, addr + len(data) - 1, 2):
            hw = data[a - addr] | data[a - addr + 1] << 8
            if is_bkpt(hw):
                if a not in self.bkpts:
                    self.stub.request(f"Z0,{a:x},2")
                    self.bkpts.add(a)
            elif a in self.bkpts:
                self.stub.request(f"z0,{a:x},2")
                self.bkpts.discard(a)

    def _detach(self):
        if not self.stub:
            return
        try:
            if self.running:
                self.stub.sock.sendall(b"\x03")
                self.stub.reply()
            self.stub.request("D")
        except (OSError, StubError):
            pass
        self.stub.close()
        self.stub = None
        self.running = False
        self.log("detached: the processor runs")

    # ---- the packets

    def packet(self, payload):
        text = payload.decode("latin-1")
        c = text[:1]
        attached = self.stub is not None
        if c in "gGmMpPXzZ" and not attached:
            self.put("EFF")
        elif c == "g":
            self.put(self.stub.request("g"))
        elif c in "Gp":
            r = self.stub.request(text)
            self.put(r if c == "p" or r == b"OK" else ("EFF" if c == "p" else "E01"))
        elif c == "P":
            self.put("OK" if self.stub.request(text) == b"OK" else "EFF")
        elif c == "m":
            self._read_mem(text)
        elif c == "M":
            m = re.fullmatch(r"M([0-9a-fA-F]+),([0-9a-fA-F]+):([0-9a-fA-F]*)", text)
            if not m:
                self.put("E02")
                return
            addr, size = int(m[1], 16), int(m[2], 16)
            data = bytes.fromhex(m[3])
            if size > len(data):
                self.put("E02")
                return
            self._write_mem(addr, data[:size])
        elif c == "X":
            m = re.match(rb"X([0-9a-fA-F]+),([0-9a-fA-F]+):", payload)
            if not m:
                self.put("E02")
                return
            data = unescape(payload[m.end():])
            size = int(m[2], 16)
            if size > len(data):
                self.put("E02")
                return
            self._write_mem(int(m[1], 16), data[:size])
        elif c == "H":
            try:
                tid = int(text[2:], 16)
            except ValueError:
                tid = -1
            self.put("OK" if 0 <= tid <= 1 else "E01")
        elif c in "sc":
            if not attached:
                self.put("X1D")
                return
            self._resume(c)
        elif c == "?":
            self.put(self.stop if attached else "W00")
        elif c == "!":
            self.put("OK")
        elif c == "D":
            self._detach()
            self.put("OK")
        elif c == "k":
            self._kill()
        elif c in "rR":
            if attached or (self.last_target and self._attach()):
                self.target.reset()
        elif c in "zZ":
            self._breakwatch(text)
        elif c == "q":
            self._query(text, payload)
        elif c == "v":
            self._verbose(text, payload)
        else:
            self.put("")

    def _read_mem(self, text):
        m = re.fullmatch(r"m([0-9a-fA-F]+),([0-9a-fA-F]+)", text)
        if not m:
            self.put("E01")
            return
        addr, size = int(m[1], 16), int(m[2], 16)
        if size > (BUF_SIZE + 1) // 2:
            self.put("E02")
            return
        data = self.stub.read(addr, size) if size else b""
        self.put("E01" if data is None else data.hex())

    def _in_flash(self, addr, size):
        end = self.flash_end or self.target.flash_base + 0x100000
        return addr < end and addr + size > self.target.flash_base

    def _write_mem(self, addr, data):
        # The flash takes no plain writes: on the chip they are a bus
        # error, and the probe says E01.  gdb writes it with vFlash.
        if self._in_flash(addr, len(data)):
            self.put("E01")
            return
        if not self.stub.write(addr, data):
            self.put("E01")
            return
        # gdb's breakpoints outside the flash are BKPTs that it writes there
        # (it takes them out again when the target stops)
        lo, hi = addr & ~1, (addr + len(data) + 1) & ~1
        self._scan_bkpts(lo, hi - lo)
        self.put("OK")

    def _kill(self):
        if self.stub:
            self.target.reset()
            self._detach()
            self.last_target = True

    def _breakwatch(self, text):
        m = re.fullmatch(r"([zZ])([0-4]),([0-9a-fA-F]+),([0-9a-fA-F]+)", text)
        if not m:
            self.put("E01")
            return
        insert, kind = m[1] == "Z", int(m[2])
        key = (kind, int(m[3], 16), int(m[4], 16))
        if kind == 0:
            # breakpoints that are a write to memory: gdb does those itself
            self.put("")
            return
        if insert:
            if key in self.hard:
                self.put("OK")
                return
            used = sum(1 for k in self.hard if (k[0] == 1) == (kind == 1))
            if used >= (HW_BREAKPOINTS if kind == 1 else HW_WATCHPOINTS):
                self.put("E01")
                return
            r = self.stub.request(text)
            if r == b"OK":
                self.hard[key] = True
            self.put("OK" if r == b"OK" else "E01")
        else:
            if key not in self.hard:
                self.put("E01")
                return
            self.stub.request(text)
            del self.hard[key]
            self.put("OK")

    def _string_reply(self, reply, param):
        m = re.fullmatch(r"([0-9a-fA-F]+),([0-9a-fA-F]+)", param)
        if not m:
            self.put("E01")
            return
        off, size = int(m[1], 16), int(m[2], 16)
        if off > len(reply):
            self.put("E01")
        elif off == len(reply):
            self.put("l")
        else:
            self.put(b"m" + reply[off:off + size])

    def memory_map(self):
        # As the probe builds it for an STM32WB: its RAM list is newest
        # first, and SRAM2 at 0x10000000 is given in bytes where KiB were
        # meant (the probe's own slip, kept: gdb sees the same map)
        flash = (self.flash_end or self.target.flash_base + 0x100000) \
            - self.target.flash_base
        return ('<memory-map>'
                '<memory type="ram" start="0x20000000" length="0x40000"/>'
                '<memory type="ram" start="0x10000000" length="0x40"/>'
                f'<memory type="flash" start="0x{self.target.flash_base:08x}" '
                f'length="0x{flash:x}">'
                f'<property name="blocksize">0x{self.target.flash_page:x}'
                '</property></memory></memory-map>').encode()

    def _query(self, text, payload):
        if text.startswith("qRcmd,"):
            try:
                command = bytes.fromhex(text[6:]).decode("latin-1")
            except ValueError:
                self.put("E01")
                return
            result = self.monitor(command)
            if result is None:
                self.put("")
            elif result:
                self.put("OK")
            else:
                self.put(hexs("Failed\n"))
        elif text.startswith("qSupported"):
            self.put(f"PacketSize={BUF_SIZE:X};qXfer:memory-map:read+;"
                     "qXfer:features:read+")
        elif text.startswith("qXfer:memory-map:read::"):
            if not self.stub and not (self.last_target and self._attach()):
                self.put("E01")
                return
            self._string_reply(self.memory_map(), text[23:])
        elif text.startswith("qXfer:features:read:"):
            if not self.stub and not (self.last_target and self._attach()):
                self.put("E01")
                return
            r = self.stub.request(text)
            self.put(r)
        elif text.startswith("qCRC:"):
            m = re.fullmatch(r"qCRC:([0-9a-fA-F]+),([0-9a-fA-F]+)", text)
            if not m:
                return
            if not self.stub:
                self.put("E01")
                return
            data = self.stub.read(int(m[1], 16), int(m[2], 16))
            self.put("E03" if data is None else "C%x" % crc32(data))
        elif text.startswith("qC"):
            self.put("QC1")
        elif text.startswith("qfThreadInfo"):
            self.put("m1")
        elif text.startswith("qsThreadInfo"):
            self.put("l")
        else:
            self.put("")

    def _verbose(self, text, payload):
        if text.startswith("vAttach;"):
            try:
                n = int(text[8:], 16)
            except ValueError:
                n = 0
            if n == 1 and self.scanned and self._attach():
                self.put("T05thread:1;")
            else:
                self.put("E01")
        elif text.startswith("vKill;"):
            self._kill()
            self.put("OK")
        elif text.startswith("vRun"):
            if self.stub or (self.last_target and self._attach()):
                self.target.reset()
                self.put("T05")
            else:
                self.put("E01")
        elif text.startswith("vFlashErase:"):
            m = re.fullmatch(r"vFlashErase:([0-9a-fA-F]+),([0-9a-fA-F]+)", text)
            self.put("OK" if m and self._erase(int(m[1], 16), int(m[2], 16))
                     else "EFF")
        elif text.startswith("vFlashWrite:"):
            m = re.match(rb"vFlashWrite:([0-9a-fA-F]+):", payload)
            ok = m and self._flash_write(int(m[1], 16), unescape(payload[m.end():]))
            self.put("OK" if ok else "EFF")
        elif text == "vFlashDone":
            self.put("OK" if self._flash_done() else "EFF")
        elif text == "vStopped":
            self.put("OK")
        else:
            self.put("")

    # ---- the flash

    def _pages(self, addr, size):
        page = self.target.flash_page
        first = addr - (addr - self.target.flash_base) % page
        return range(first, addr + size, page)

    def _flash_ok(self, addr, size):
        """Within the flash, and below CPU2's secure part (which the chip
        does not let CPU1, nor a probe through it, erase or write)"""
        return (self.stub is not None and self.flash_end is not None
                and self.target.flash_base <= addr
                and addr + size <= min(self.flash_end, self.secure))

    def _erase(self, addr, size):
        if not self._flash_ok(addr, size):
            return False
        page = self.target.flash_page
        for p in self._pages(addr, size):
            if not self.stub.write(p, b"\xff" * page):
                return False
            self.dirty.add(p)
        self._scan_bkpts(self._pages(addr, size)[0],
                         len(self._pages(addr, size)) * page,
                         b"\xff" * (len(self._pages(addr, size)) * page))
        return True

    def _flash_write(self, addr, data):
        if not self._flash_ok(addr, len(data)):
            return False
        if not self.stub.write(addr, data):
            return False
        self.dirty.update(self._pages(addr, len(data)))
        lo = addr & ~1
        pad = self.stub.read(lo, (len(data) + (addr - lo) + 1) & ~1)
        if pad is not None:
            self._scan_bkpts(lo, len(pad), pad)
        return True

    def _flash_done(self):
        """What was written is kept in the flash's image too, page by page
        in runs (a probe's flash keeps it through a power cycle)"""
        pages = sorted(self.dirty)
        self.dirty = set()
        if not pages or not self.stub:
            return True
        page = self.target.flash_page
        runs, start, end = [], pages[0], pages[0] + page
        for p in pages[1:]:
            if p == end:
                end += page
            else:
                runs.append((start, end))
                start, end = p, p + page
        runs.append((start, end))
        for start, end in runs:
            data = self.stub.read(start, end - start)
            why = (self.target.persist(start, data) if data is not None
                   else "the flash could not be read back")
            if why:
                self.log(f"flash 0x{start:08x}-0x{end:08x}: not kept in the "
                         f"image ({why})")
        self.log(f"flash written: {len(pages)} page"
                 f"{'' if len(pages) == 1 else 's'} of "
                 f"{page // 1024} KiB")
        return True

    # ---- monitor commands

    def monitor(self, command):
        """`monitor ...`: True (OK), False (Failed), None (no such
        command)"""
        argv = command.split()
        name = argv[0] if argv else ""
        for cmd, _ in GENERAL_COMMANDS:
            if cmd.startswith(name):
                return getattr(self, "cmd_" + cmd)(argv)
        if not self.stub:
            return None
        for _, cmds in TARGET_COMMANDS:
            for cmd, _ in cmds:
                if cmd.startswith(name):
                    return getattr(self, "cmd_" + cmd)(argv)
        return None

    def cmd_version(self, argv):
        self.say(BMP_IDENT)
        self.say(", Hardware Version 0\n")
        self.say("Copyright (C) 2022 Black Magic Debug Project\n")
        self.say("License GPLv3+: GNU GPL version 3 or later "
                 "<http://gnu.org/licenses/gpl.html>\n\n")
        return True

    def cmd_help(self, argv):
        self.say("General commands:\n")
        for cmd, text in GENERAL_COMMANDS:
            self.say(f"\t{cmd} -- {text}\n")
        if self.stub:
            for group, cmds in TARGET_COMMANDS:
                self.say(f"{group} specific commands:\n")
                for cmd, text in cmds:
                    self.say(f"\t{cmd} -- {text}\n")
        return True

    def _scan(self):
        why = self.target.swd()
        if why:
            self.log(f"scan: {why}")
            self.scanned = False
            return False
        self.scanned = True
        return True

    def cmd_targets(self, argv):
        self.say("Available Targets:\n")
        self.say("No. Att Driver\n")
        if not self.scanned:
            self.say("No usable targets found.\n")
            return False
        self.say("%2d   %c  %s %s\n" % (1, "*" if self.stub else " ",
                                         self.target.name, self.target.core))
        return True

    def cmd_swdp_scan(self, argv):
        if self.stub:
            self._detach()
        if not self._scan():
            self.say("SW-DP scan failed!\n")
            return False
        return self.cmd_targets(argv)

    def cmd_jtag_scan(self, argv):
        # The board wires no JTAG: TMS, TDI, TDO and TCK are none of its pins
        if self.stub:
            self._detach()
        self.scanned = False
        self.say("JTAG device scan failed!\n")
        return False

    def cmd_auto_scan(self, argv):
        if self.stub:
            self._detach()
        self.say("JTAG scan found no devices, trying SWD!\n")
        if not self._scan():
            self.say("SW-DP scan found no devices.\n")
            return False
        return self.cmd_targets(argv)

    def cmd_frequency(self, argv):
        self.say("Current SWD frequency: 1000000 Hz\n")
        return True

    def cmd_morse(self, argv):
        self.say(f"{self.morse}\n" if self.morse else "No message\n")
        return True

    def cmd_halt_timeout(self, argv):
        if len(argv) > 1:
            try:
                self.halt_timeout = int(argv[1], 0)
            except ValueError:
                pass
        self.say("Cortex-M timeout to wait for device halts: "
                 f"{self.halt_timeout}\n")
        return True

    def cmd_connect_rst(self, argv):
        if len(argv) == 1:
            self.say("Assert nRST during connect: "
                     f"{'enabled' if self.connect_rst else 'disabled'}\n")
            return True
        if argv[1] in ("enable", "disable"):
            self.connect_rst = argv[1] == "enable"
            return True
        self.say("Unrecognized command format\n")
        return True

    def cmd_reset(self, argv):
        self.target.reset()
        return True

    def cmd_tdi_low_reset(self, argv):
        return self.cmd_reset(argv)

    def cmd_heapinfo(self, argv):
        if not self.stub:
            self.say("not attached\n")
        elif len(argv) == 5:
            self.say("heapinfo heap_base: %s heap_limit: %s stack_base: %s "
                     "stack_limit: %s\n" % tuple(argv[1:5]))
        else:
            self.say("heapinfo heap_base heap_limit stack_base stack_limit\n")
        return True

    def cmd_vector_catch(self, argv):
        if len(argv) >= 3 and argv[1] in ("enable", "disable"):
            for name in argv[2:]:
                if name in VECTORS:
                    bit = 1 << VECTORS.index(name)
                    self.catch = (self.catch | bit if argv[1] == "enable"
                                  else self.catch & ~bit)
        elif len(argv) > 1:
            self.say("usage: monitor vector_catch (enable|disable) "
                     "(hard|int|bus|stat|chk|nocp|mm|reset)\n")
        self.say("Catching vectors: ")
        for i, name in enumerate(VECTORS):
            if name and self.catch & (1 << i):
                self.say(f"{name} ")
        self.say("\n")
        return True

    def cmd_erase_bank1(self, argv):
        size = self.secure - self.target.flash_base
        self.say("Erasing bank 1: ")
        ok = self._erase(self.target.flash_base, size)
        self._flash_done()
        self.say("done\n" if ok else "failed\n")
        return ok

    def cmd_erase_bank2(self, argv):
        self.say("No second bank on this device\n")
        return True

    def cmd_option(self, argv):
        # read only here: the option bytes are the emulator's to set
        regs = self.stub.read(0x58004020, 0x34)
        if regs is None:
            return False
        names = {0x20: "OPTR", 0x24: "PCROP1ASR", 0x28: "PCROP1AER",
                 0x2C: "WRP1AR", 0x30: "WRP1BR", 0x34: "PCROP1BSR",
                 0x38: "PCROP1BER", 0x3C: "IPCCBR", 0x50: "C2OPT"}
        for off, name in names.items():
            at = off - 0x20
            if at + 4 <= len(regs):
                value = int.from_bytes(regs[at:at + 4], "little")
                self.say(f"0x{0x58004000 + off:08X}: 0x{value:08X} ({name})\n")
        if len(argv) > 1:
            self.say("(the emulated board reads the option bytes; it does "
                     "not write them)\n")
        return True


# --------------------------------------------------------------------------
# ESP32 Marauder
# --------------------------------------------------------------------------

MARAUDER_VERSION = "v1.21.0"
MARAUDER_HARDWARE = "Flipper Zero Dev Board"

DEFAULT_SCENE = {
    "aps": [
        {"essid": "Bramble-Home", "bssid": "a4:2b:b0:4e:11:d2", "channel": 1,
         "rssi": -41, "security": "WPA2", "brand": "TP-Link",
         "stations": ["3c:22:fb:91:0a:7e", "f0:18:98:2c:55:01"]},
        {"essid": "Bramble-Guest", "bssid": "a6:2b:b0:4e:11:d2", "channel": 1,
         "rssi": -43, "security": "Open", "brand": "TP-Link", "stations": []},
        {"essid": "NETGEAR57", "bssid": "9c:3d:cf:7a:20:4b", "channel": 6,
         "rssi": -67, "security": "WPA2", "brand": "Netgear",
         "stations": ["d8:bb:2c:10:9e:3f"]},
        {"essid": "xfinitywifi", "bssid": "2c:7e:81:c4:09:a0", "channel": 11,
         "rssi": -72, "security": "Open", "brand": "Arris", "stations": []},
        {"essid": "Pixel_8810", "bssid": "da:a1:19:6b:3e:02", "channel": 6,
         "rssi": -58, "security": "WPA3", "brand": "", "stations":
         ["a8:5e:45:07:b1:c9"]},
        {"essid": "", "bssid": "70:3a:cb:5f:88:14", "channel": 11,
         "rssi": -80, "security": "WPA2", "brand": "Google", "stations": []},
    ],
    "probes": [
        {"mac": "f0:18:98:2c:55:01", "essid": "Starbucks WiFi"},
        {"mac": "f0:18:98:2c:55:01", "essid": "Bramble-Home"},
        {"mac": "62:0d:9a:41:73:e8", "essid": "eduroam"},
    ],
}


def load_scene(path):
    """The air around the board: a JSON file as DEFAULT_SCENE, or that"""
    if not path:
        return json.loads(json.dumps(DEFAULT_SCENE))
    with open(path) as f:
        scene = json.load(f)
    for ap in scene.get("aps", []):
        ap.setdefault("essid", "")
        ap.setdefault("channel", 1)
        ap.setdefault("rssi", -60)
        ap.setdefault("security", "WPA2")
        ap.setdefault("brand", "")
        ap.setdefault("stations", [])
        if not re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", ap.get("bssid", "")):
            raise ValueError(f"an access point's bssid: {ap.get('bssid')!r}")
    scene.setdefault("probes", [])
    return scene


SCAN_NAMES = {"sniffbeacon": "Beacon sniff", "sniffprobe": "Probe sniff",
              "sniffdeauth": "Deauth sniff", "sniffraw": "Raw sniff",
              "sniffpwn": "Pwnagotchi sniff", "sniffsae": "Commit sniff"}
HELP = [
    "============ Commands ============",
    "channel [-s <channel>]",
    "settings [-s <setting> enable/disable>]/[-r]",
    "clearlist -a/-c/-s",
    "reboot",
    "scanall",
    "sniffraw",
    "sniffbeacon",
    "sniffprobe",
    "sniffdeauth",
    "sniffpmkid [-c <channel>][-d][-l]",
    "stopscan [-f]",
    "attack -t <quiet/csa/sae/beacon [-l/-r/-a]/deauth [-c]/[-s <src mac>] "
    "[-d <dst mac>]/probe/rickroll/badmsg [-c]/sleep [-c]>",
    "info [-a <index>]",
    "list -s", "list -a", "list -c", "list -p",
    "select -a/-s/-c <index (comma separated)>/-f \"equals <String> or "
    "contains <String>\"",
    "ssid -a [-g <count>/-n <name>]",
    "ssid -r <index>",
    "==================================",
]
SETTINGS = ["ForcePMKID", "ForceProbe", "SavePCAP", "EnableLED", "EPDeauth",
            "ChanHop"]
RICKROLL = ["01 Never gonna give you up", "02 Never gonna let you down",
            "03 Never gonna run around", "04 and desert you",
            "05 Never gonna make you cry", "06 Never gonna say goodbye",
            "07 Never gonna tell a lie", "08 and hurt you"]


class Marauder:
    """ESP32 Marauder's command line.  `send(bytes)` is the board's UART
    (to the Flipper), `now()` the time, `rng` the random numbers of its
    scans and of generated SSIDs."""

    def __init__(self, send, scene=None, now=time.monotonic, seed=None):
        self.send = send
        self.scene = scene or load_scene(None)
        self.now = now
        self.rng = random.Random(seed)
        self.line = b""
        self.aps = []           # what scans found: dicts, with "selected"
        self.stations = []      # {"mac", "ap" (index), "selected"}
        self.ssids = []         # {"essid", "selected"}
        self.probes = []        # essids seen in probe requests
        self.channel = 1
        self.settings = {name: name in ("SavePCAP", "EnableLED", "ChanHop")
                         for name in SETTINGS}
        self.mode = None        # a scan or an attack that runs
        self.queue = []         # (when, what) of the running mode
        self.sent = 0           # frames an attack "sent"
        self.started = None

    # ---- the UART

    def println(self, text=""):
        self.send(text.encode("utf-8", "replace") + b"\r\n")

    def print(self, text):
        self.send(text.encode("utf-8", "replace"))

    def boot(self):
        """What it prints as it starts"""
        self.println("ESP-IDF version is: v4.4.7-dirty")
        for line in ["", "", "--------------------------------", "",
                     "         ESP32 Marauder      ", "",
                     "            " + MARAUDER_VERSION, "",
                     "       By: justcallmekoko", "",
                     "--------------------------------", "", ""]:
            self.println(line)
        self.print("> ")

    def take(self, data):
        """Bytes from the Flipper: a command is a line"""
        self.line += data
        while b"\n" in self.line:
            raw, self.line = self.line.split(b"\n", 1)
            text = raw.decode("utf-8", "replace").strip()
            if not text:
                continue
            self.run(text)
            self.print("> ")

    def due(self):
        """When the running scan or attack next has something to say"""
        return self.queue[0][0] if self.queue else None

    def tick(self):
        """Say what is due"""
        now = self.now()
        while self.queue and self.queue[0][0] <= now:
            _, what = self.queue.pop(0)
            what()

    # ---- the commands

    def run(self, text):
        self.println("#" + text)
        args = self._split(text)
        cmd = args[0]
        handler = getattr(self, "c_" + cmd, None)
        if handler is None:
            if cmd in SCAN_NAMES:
                self._start_sniff(cmd)
            return
        handler(args)

    @staticmethod
    def _split(text):
        out, word, quote = [], "", None
        for ch in text:
            if quote:
                if ch == quote:
                    quote = None
                else:
                    word += ch
            elif ch in "\"'":
                quote = ch
            elif ch == " ":
                if word:
                    out.append(word)
                word = ""
            else:
                word += ch
        if word:
            out.append(word)
        return out

    @staticmethod
    def _arg(args, flag):
        return args.index(flag) if flag in args else -1

    def _counts(self, selected, unselected=None):
        self.println(f"{selected} selected"
                     + (f", {unselected} unselected" if unselected is not None
                        else ""))

    def c_help(self, args):
        for line in HELP:
            self.println(line)

    def c_reboot(self, args):
        self.c_stopscan(["stopscan"], quiet=True)
        self.__init__(self.send, self.scene, self.now)
        self.boot()

    def c_channel(self, args):
        at = self._arg(args, "-s")
        if at >= 0 and at + 1 < len(args):
            try:
                ch = int(args[at + 1])
            except ValueError:
                ch = 0
            if 1 <= ch <= 14:
                self.channel = ch
                self.println(f"Set channel: {ch}")
            else:
                self.println("Invalid channel")
            return
        self.println(f"Current channel: {self.channel}")

    def c_settings(self, args):
        at = self._arg(args, "-s")
        if "-r" in args:
            self.settings = {n: n in ("SavePCAP", "EnableLED", "ChanHop")
                             for n in SETTINGS}
            self.println("Settings reset to default")
            return
        if at >= 0 and at + 2 < len(args):
            name, value = args[at + 1], args[at + 2]
            if name in self.settings and value in ("enable", "disable"):
                self.settings[name] = value == "enable"
                self.println(f"{name}: {value}d")
            else:
                self.println("Invalid setting")
            return
        for name in SETTINGS:
            self.println(f"{name}: {'enabled' if self.settings[name] else 'disabled'}")

    def c_clearlist(self, args):
        if "-a" in args:
            self.aps, self.stations = [], []
            self.println("access_points: 0")
        elif "-c" in args:
            self.stations = []
            for ap in self.aps:
                ap["stations"] = []
            self.println("stations: 0")
        elif "-s" in args:
            self.ssids = []
            self.println("ssids: 0")
        else:
            self.println("You did not specify which list to clear")

    def c_list(self, args):
        selected = 0
        if "-a" in args:
            for i, ap in enumerate(self.aps):
                mark = " (selected)" if ap["selected"] else ""
                selected += ap["selected"]
                self.println(f"[{i}][CH:{ap['channel']}] {ap['shown']} "
                             f"{ap['rssi']}{mark}")
            self._counts(selected)
        elif "-s" in args:
            for i, s in enumerate(self.ssids):
                selected += s["selected"]
                self.println(f"[{i}] {s['essid']}"
                             + (" (selected)" if s["selected"] else ""))
            self._counts(selected)
        elif "-c" in args:
            for x, ap in enumerate(self.aps):
                self.println(f"[{x}] {ap['shown']} {ap['rssi']}:")
                for i in ap["stations"]:
                    st = self.stations[i]
                    selected += st["selected"]
                    self.println(f"  [{i}] {st['mac']}"
                                 + (" (selected)" if st["selected"] else ""))
            self._counts(selected)
        elif "-p" in args:
            for i, essid in enumerate(self.probes):
                self.println(f"[{i}] {essid}")
        # -t -i -b -f -x -m: lists this board has nothing in

    def _toggle(self, items, spec):
        on = off = 0
        if spec == "all":
            picks = range(len(items))
        else:
            picks = []
            for word in spec.split(","):
                try:
                    picks.append(int(word))
                except ValueError:
                    picks.append(-1)
        for i in picks:
            if not 0 <= i < len(items):
                self.println(f"Index not in range: {i}")
                continue
            items[i]["selected"] = not items[i]["selected"]
            if items[i]["selected"]:
                on += 1
            else:
                off += 1
        self._counts(on, off)

    def c_select(self, args):
        for flag, items in (("-a", self.aps), ("-c", self.stations),
                            ("-s", self.ssids)):
            at = self._arg(args, flag)
            if at < 0:
                continue
            f = self._arg(args, "-f")
            if flag == "-a" and f >= 0 and f + 1 < len(args):
                self._filter(" ".join(args[f + 1:]))
                return
            if at + 1 >= len(args):
                self.println("You did not specify which list to select from")
                return
            self._toggle(items, args[at + 1])
            return
        self.println("You did not specify which list to select from")

    def _filter(self, expr):
        on = off = 0
        for ap in self.aps:
            hit = False
            for part in expr.split(" or "):
                m = re.fullmatch(r"\s*(equals|contains)\s+'?(.*?)'?\s*", part)
                if m and ((m[1] == "equals" and ap["shown"] == m[2])
                          or (m[1] == "contains" and m[2] in ap["shown"])):
                    hit = True
            ap["selected"] = hit
            on += hit
            off += not hit
        self._counts(on, off)

    def c_info(self, args):
        at = self._arg(args, "-a")
        if at < 0:
            for line in ["", "Firmware: Marauder",
                         "Version: " + MARAUDER_VERSION,
                         "Hardware: " + MARAUDER_HARDWARE,
                         "FCC ID: 2AYVF-FZWDB", "App partition: app0",
                         "ESP-IDF: v4.4.7-dirty", "Station MAC: "
                         "84:f7:03:2a:91:5c", "AP MAC: 84:f7:03:2a:91:5d",
                         "SD Card: Not Connected"]:
                self.println(line)
            return
        try:
            i = int(args[at + 1])
        except (IndexError, ValueError):
            i = -1
        if not 0 <= i < len(self.aps):
            self.println("Invalid AP index")
            return
        ap = self.aps[i]
        self.println("   ESSID: " + ap["shown"])
        self.println("   BSSID: " + ap["bssid"])
        self.println(" Channel: " + str(ap["channel"]))
        self.println("    RSSI: " + str(ap["rssi"]))
        self.println("  Frames: " + str(ap["packets"]))
        self.println("Stations: " + str(len(ap["stations"])))
        self.println("   Brand: " + ap["brand"])
        self.print("Complete EAPOL: ")
        self.println("FALSE")
        self.print("Security: ")
        self.println(ap["security"])

    def c_ssid(self, args):
        a, g, n, r = (self._arg(args, x) for x in ("-a", "-g", "-n", "-r"))
        if a >= 0 and g >= 0 and g + 1 < len(args):
            try:
                count = int(args[g + 1])
            except ValueError:
                count = 0
            for _ in range(count):
                name = "".join(self.rng.choice(
                    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
                    "0123456789") for _ in range(6))
                self.ssids.append({"essid": name, "selected": False})
                self.println(name)
        elif a >= 0 and n >= 0 and n + 1 < len(args):
            name = " ".join(args[n + 1:])
            self.ssids.append({"essid": name, "selected": False})
            self.println(name)
        elif r >= 0 and r + 1 < len(args):
            try:
                i = int(args[r + 1])
            except ValueError:
                i = -1
            if 0 <= i < len(self.ssids):
                self.println("Removed: " + self.ssids.pop(i)["essid"])
            else:
                self.println("Index not in range: " + args[r + 1])
        else:
            self.println("You did not provide the proper args")

    # ---- scans

    def _found(self, ap):
        """An access point of the scene, as a scan has it"""
        for i, known in enumerate(self.aps):
            if known["bssid"] == ap["bssid"]:
                return i, False
        entry = dict(ap)
        entry["shown"] = ap["essid"] or ap["bssid"]
        entry["selected"] = False
        entry["stations"] = []
        entry["packets"] = 0
        self.aps.append(entry)
        return len(self.aps) - 1, True

    def _schedule(self, period, what):
        """`what()` every so often (with a little jitter) while the mode
        runs"""
        def again():
            what()
            self.queue.append((self.now() + period * self.rng.uniform(0.7, 1.3),
                               again))
            self.queue.sort(key=lambda q: q[0])
        self.queue.append((self.now() + period * self.rng.uniform(0.3, 1.0),
                           again))
        self.queue.sort(key=lambda q: q[0])

    def _air(self):
        """The scene in the order a scan meets it: by channel, hopping"""
        aps = list(self.scene["aps"])
        self.rng.shuffle(aps)
        return aps

    def _beacon_line(self, ap, full):
        rssi = ap["rssi"] + self.rng.randint(-3, 3)
        line = f"{rssi} Ch: {ap['channel']} {ap['bssid']} ESSID: {ap['essid']} "
        if not ap["essid"]:
            line += ap["bssid"] + " "
        if full:
            line += "64 00 "
        self.println(line)

    def c_scanall(self, args):
        self.print("Scanning for APs and Stations. Stop with ")
        self.println("stopscan")
        self.mode = "scanall"
        air = self._air()
        stations = [(ap, mac) for ap in self.scene["aps"]
                    for mac in ap["stations"]]
        self.rng.shuffle(stations)

        def step():
            # beacons first, then the stations' data frames
            for ap in air:
                i, new = self._found(ap)
                self.aps[i]["packets"] += 1
                if new:
                    self._beacon_line(ap, True)
                    return
            for ap, mac in stations:
                if any(s["mac"] == mac for s in self.stations):
                    continue
                i, _ = self._found(ap)
                if not self.aps[i]:
                    continue
                self.stations.append({"mac": mac, "ap": i, "selected": False})
                self.aps[i]["stations"].append(len(self.stations) - 1)
                if self.rng.random() < 0.5:
                    self.println(f"{len(self.stations)}: ap: {ap['bssid']} "
                                 f"-> sta: {mac}")
                else:
                    self.println(f"{len(self.stations)}: sta: {mac} "
                                 f"-> ap: {ap['bssid']}")
                return
        self._schedule(0.4, step)

    def _start_sniff(self, cmd):
        self.print("Starting")
        self.print(SCAN_NAMES[cmd])
        self.print(". Stop with ")
        self.println("stopscan")
        self.mode = cmd
        if cmd == "sniffbeacon":
            air = self._air()

            def beacon():
                for ap in air:
                    i, new = self._found(ap)
                    self.aps[i]["packets"] += 1
                    if new:
                        self._beacon_line(ap, False)
                        return
            self._schedule(0.4, beacon)
        elif cmd == "sniffprobe":
            def probe():
                if not self.scene["probes"]:
                    return
                p = self.rng.choice(self.scene["probes"])
                rssi = -50 - self.rng.randint(0, 30)
                self.println(f"{rssi} Ch: {self.channel} Client: {p['mac']} "
                             f"Requesting: {p['essid']}")
                if p["essid"] not in self.probes:
                    self.probes.append(p["essid"])
            self._schedule(0.8, probe)
        elif cmd == "sniffdeauth":
            pass                    # the scene has no deauths in its air
        elif cmd == "sniffraw":
            def raw():
                ap = self.rng.choice(self.scene["aps"]) if self.scene["aps"] else None
                if ap:
                    self.println(f"{ap['rssi']} Ch: {self.channel} BSSID: "
                                 f"{ap['bssid']}")
            self._schedule(0.25, raw)

    def c_sniffpmkid(self, args):
        at = self._arg(args, "-c")
        if at >= 0 and at + 1 < len(args):
            try:
                self.channel = int(args[at + 1])
            except ValueError:
                pass
        if "-d" in args and "-l" in args:
            if not any(ap["selected"] for ap in self.aps):
                self.println("You don't have any targets selected. Use select")
                return
            self.println(f"Starting TARGETED PMKID sniff with deauthentication "
                         f"on channel {self.channel}. Stop with stopscan")
        elif "-d" in args:
            self.println(f"Starting PMKID sniff with deauthentication on "
                         f"channel {self.channel}. Stop with stopscan")
        else:
            self.println(f"Starting PMKID sniff on channel {self.channel}. "
                         "Stop with stopscan")
        self.mode = "sniffpmkid"

    def c_stopscan(self, args, quiet=False):
        self.queue = []
        self.mode = None
        if not quiet:
            self.println("Stopping WiFi tran/recv")

    # ---- attacks: nothing goes on the air; they say what Marauder says

    def c_attack(self, args):
        t = self._arg(args, "-t")
        if t < 0 or t + 1 >= len(args):
            return
        kind = args[t + 1]
        if kind == "deauth":
            if "-d" not in args and "-c" not in args:
                self.println("Sending to broadcast...")
            elif "-d" in args:
                d = self._arg(args, "-d")
                self.println("Sending to " + (args[d + 1] if d + 1 < len(args)
                                              else "") + "...")
            else:
                self.println("Sending to Station list")
            if "-s" in args:
                self.println("Starting Manual Deauthentication attack. "
                             "Stop with stopscan")
            elif not any(ap["selected"] for ap in self.aps):
                self.println("You don't have any targets selected. Use select")
                return
            else:
                self.println("Starting Deauthentication attack. Stop with "
                             "stopscan")
        elif kind == "beacon":
            if "-l" in args:
                if not self.ssids:
                    self.println("You don't have any SSIDs in your list. "
                                 "Use ssid")
                    return
                self.println("Starting Beacon list spam. Stop with stopscan")
            elif "-r" in args:
                self.println("Starting random Beacon spam. Stop with stopscan")
            elif "-a" in args:
                if not any(ap["selected"] for ap in self.aps):
                    self.println("You don't have any targets selected. "
                                 "Use select")
                    return
                self.println("Starting Targeted AP Beacon spam. Stop with "
                             "stopscan")
            else:
                return
        elif kind == "probe":
            if not any(ap["selected"] for ap in self.aps):
                self.println("You don't have any targets selected. Use select")
                return
            self.println("Starting Probe spam. Stop with stopscan")
        elif kind == "rickroll":
            self.println("Starting Rick Roll Beacon spam. Stop with stopscan")
        elif kind in ("funny", "badmsg", "sleep", "sae", "csa", "quiet"):
            self.println(f"Starting {kind} attack. Stop with stopscan")
        else:
            return
        self.mode = "attack " + kind
        self.started = self.now()
        self.sent = 0

        def count():
            self.sent += 100
        self._schedule(1.0, count)


# --------------------------------------------------------------------------
# The board: its firmware on the UART, and its ports on this computer
# --------------------------------------------------------------------------

FIRMWARES = ("blackmagic", "marauder")
FIRMWARE_BAUD = {"blackmagic": 230400, "marauder": 115200}


class Listener:
    """A TCP port of the board's (its Wi-Fi, here on this computer)"""

    def __init__(self, host, port, name):
        self.name = name
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]

    def fileno(self):
        return self.sock.fileno()

    def accept(self):
        conn, addr = self.sock.accept()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return conn, f"{self.name}, {addr[0]}:{addr[1]}"

    def close(self):
        self.sock.close()


class Pty:
    """A USB serial port of the board's: a pty, with a link to it"""

    def __init__(self, link):
        import tty
        self.master, slave = os.openpty()
        tty.setraw(slave)
        self.path = os.ttyname(slave)
        self.slave = slave          # kept open: the pty lives while we do
        os.set_blocking(self.master, False)
        self.link = link
        if link:
            try:
                os.unlink(link)
            except FileNotFoundError:
                pass
            os.symlink(self.path, link)

    def close(self):
        if self.link:
            try:
                os.unlink(self.link)
            except OSError:
                pass
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass


class Board:
    """The board on the header, until stop() or the Flipper's side goes.

    target    what it needs of the Flipper (Target)
    uart      a connected socket: the other end of pins 13 and 14
    firmware  "blackmagic" or "marauder"
    host      where its TCP ports listen; gdb_port, uart_port: which (0: any
              free one; None: none)
    links     paths for links to its USB serial ports: {"gdb": .., "uart": ..}
    scene     marauder: the air (load_scene)
    log       hears what it does, a line at a time
    on_status hears status() each time it changes (a picture of the board)"""

    def __init__(self, target, uart, firmware="blackmagic", host="127.0.0.1",
                 gdb_port=2345, uart_port=3456, links=None, scene=None,
                 log=None, seed=None, on_status=None):
        if firmware not in FIRMWARES:
            raise ValueError(f"no firmware {firmware!r}: {', '.join(FIRMWARES)}")
        self.target = target
        self.uart = uart
        self.uart.setblocking(False)
        self.firmware = firmware
        self.log = log or (lambda text: print(f"[devboard] {text}", flush=True))
        self.out = bytearray()          # for the Flipper, not yet taken
        self.listeners = []
        self.ptys = {}
        self.bridges = []               # sockets and pty fds of the UART bridge
        self.closing = False
        self.on_status = on_status
        self.said = None                # the status on_status heard last
        links = links or {}
        self.probe = self.marauder = None
        if firmware == "blackmagic":
            self.probe = BlackMagic(target, self.log)
            if gdb_port is not None:
                self.gdb_listener = Listener(host, gdb_port, "gdb")
                self.listeners.append(self.gdb_listener)
            if uart_port is not None:
                self.uart_listener = Listener(host, uart_port, "uart")
                self.listeners.append(self.uart_listener)
            self.ptys["gdb"] = Pty(links.get("gdb"))
            self.ptys["uart"] = Pty(links.get("uart"))
            self.bridges.append(self.ptys["uart"].master)
        else:
            self.marauder = Marauder(self._to_flipper, scene, seed=seed)
        self.target.line_baud(FIRMWARE_BAUD[firmware])
        if self.marauder:
            self.marauder.boot()

    # ---- the UART to the Flipper

    def _to_flipper(self, data):
        self.out += data

    def _from_flipper(self, data):
        if self.marauder:
            self.marauder.take(data)
            return
        for b in list(self.bridges):
            try:
                if isinstance(b, int):
                    os.write(b, data)
                else:
                    b.sendall(data)
            except BlockingIOError:
                pass                     # a reader that does not keep up
            except OSError:
                self._drop_bridge(b)

    def _drop_bridge(self, b):
        if b in self.bridges:
            self.bridges.remove(b)
        if not isinstance(b, int):
            try:
                b.close()
            except OSError:
                pass

    # ---- the loop

    def where(self):
        """Its ports, in words, a line each"""
        lines = []
        if self.probe:
            g = getattr(self, "gdb_listener", None)
            u = getattr(self, "uart_listener", None)
            if g:
                lines.append(f"GDB server (Black Magic Probe): TCP port "
                             f"{g.port}; also {self.ptys['gdb'].link or self.ptys['gdb'].path}")
            if u:
                lines.append(f"the Flipper's USART (pins 13 and 14) at 230400 "
                             f"baud: TCP port {u.port}; also "
                             f"{self.ptys['uart'].link or self.ptys['uart'].path}")
        else:
            lines.append(f"ESP32 Marauder {MARAUDER_VERSION} on the Flipper's "
                         f"USART (pins 13 and 14) at 115200 baud: "
                         f"{len(self.marauder.scene['aps'])} access points "
                         f"around it")
        return lines

    def status(self):
        """What it is and does, as a dict that changes only when that does:
        module ("devboard"), firmware; blackmagic: gdb_port, uart_port (None:
        none), gdb ("", "connected" or "attached"); marauder: mode (the scan
        or attack that runs, or ""), aps (how many access points it found)"""
        s = {"module": "devboard", "firmware": self.firmware}
        if self.probe:
            g = getattr(self, "gdb_listener", None)
            u = getattr(self, "uart_listener", None)
            s["gdb_port"] = g.port if g else None
            s["uart_port"] = u.port if u else None
            s["gdb"] = ("attached" if self.probe.stub is not None else
                        "connected" if self.probe.client else "")
        else:
            s["mode"] = self.marauder.mode or ""
            s["aps"] = len(self.marauder.aps)
        return s

    def _tell(self):
        if self.on_status:
            now = self.status()
            if now != self.said:
                self.said = now
                self.on_status(dict(now))

    def step(self, seconds=0.05):
        """One turn of the loop: -> False once the Flipper's end is gone"""
        rs = [self.uart] + self.listeners
        if self.probe:
            rs += [self.ptys["gdb"].master] + self.probe.fds()
            rs += [b for b in self.bridges if b not in rs]
        ws = [self.uart] if self.out else []
        if self.marauder and self.marauder.due() is not None:
            seconds = max(0.0, min(seconds, self.marauder.due() - time.monotonic()))
        try:
            r, w, _ = select.select(rs, ws, [], seconds)
        except (OSError, ValueError):
            return not self.closing
        if self.uart in w:
            try:
                n = self.uart.send(self.out[:256])
                del self.out[:n]
            except BlockingIOError:
                pass
            except OSError:
                return False
        for fd in r:
            if fd is self.uart:
                try:
                    data = self.uart.recv(65536)
                except BlockingIOError:
                    continue
                except OSError:
                    data = b""
                if not data:
                    return False
                self._from_flipper(data)
            elif fd in self.listeners:
                self._accept(fd)
            elif self.probe and fd == self.ptys["gdb"].master:
                self._gdb_pty()
            elif self.probe and fd in self.probe.fds():
                self.probe.ready(fd if isinstance(fd, int) else fd.fileno())
            elif fd in self.bridges:
                self._bridge_in(fd)
        if self.marauder:
            self.marauder.tick()
        self._tell()
        return not self.closing

    def _accept(self, listener):
        conn, name = listener.accept()
        if listener is getattr(self, "gdb_listener", None):
            conn.setblocking(False)
            if not self.probe.connect(conn.fileno(), conn.fileno(), name,
                                      conn.close):
                self.log(f"gdb refused ({name}): one is connected already")
                conn.close()
        else:
            conn.setblocking(False)
            self.bridges.append(conn)
            self.log(f"UART bridge: connected ({name})")

    def _gdb_pty(self):
        fd = self.ptys["gdb"].master
        if not self.probe.client:
            self.probe.connect(fd, fd, "USB")
        if self.probe.client and self.probe.client[0] == fd:
            self.probe.ready(fd)
        else:
            try:
                os.read(fd, 65536)      # a second gdb: the probe has one
            except OSError:
                pass

    def _bridge_in(self, b):
        try:
            data = os.read(b, 65536) if isinstance(b, int) else b.recv(65536)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            if not isinstance(b, int):
                self.log("UART bridge: disconnected")
                self._drop_bridge(b)
            return
        self.out += data

    def run(self, seconds=None):
        end = None if seconds is None else time.monotonic() + seconds
        try:
            while self.step():
                if end is not None and time.monotonic() >= end:
                    return True
        except KeyboardInterrupt:
            return True
        if self.closing:
            return True             # stop(): SIGTERM, as Ctrl-C
        self.log("the Flipper's end of the pins is gone")
        return False

    def stop(self):
        self.closing = True

    def close(self):
        if self.probe:
            self.probe.disconnect()
        for listener in self.listeners:
            listener.close()
        for b in list(self.bridges):
            self._drop_bridge(b)
        for p in self.ptys.values():
            p.close()
        try:
            self.target.line_baud(0)
        except Exception:
            pass
