"""The Video Game Module, as the Flipper's firmware meets it on the GPIO header.

The VGM is an RP2040 board.  Its UART0 is on the Flipper's USART pins (13 TX,
14 RX), where it is an expansion module (fe_expansion.py) that mirrors the
Flipper's screen to its HDMI port.  What its firmware does there, and what is
done here, in the same order:

  1. it calls (a byte, 0xF0, at 9600 baud), has the Flipper's heartbeat and
     asks for 2 000 000 baud;
  2. starts an RPC session;
  3. once a power-up: shows a picture of its own on the Flipper's screen (a
     "virtual display") until a button is released there, and ends it;
  4. asks for the screen stream, and draws every frame of it on HDMI:
     640 by 480, each pixel of the Flipper's four wide and six high;
  5. when the link is lost (the Flipper hears nothing for 250 ms, or the
     module nothing for 200 ms), starts again at 1.

Flipware (the firmware's fork with a coprocessor service, `coproc`) sends the
module jobs as well, in frames of a type of their own (6), beside the RPC
session.  The module here does them as the VGM firmware of that fork does:

    0  CRC-32 (IEEE)           -> 4 bytes, the low first
    1  heatshrink compress     -> a block as compress_encode() makes it
    2  heatshrink decompress   -> the bytes
    3  HID inject              -> 1 byte, 0 when every record was good
    4  GPIO of the breakout    -> a status and a value for each record

Its breakout pins (16, 17, 21 to 29; ADC on 26 to 29) are modelled here: a
mode, a level that the module drives, and what the outside holds.

A coprocessor frame (the layout of the firmware's ExpansionFrameCoproc):

    06 JOB JOB TASK FLAGS SEQ SEQ TOTAL TOTAL LEN PAYLOAD... CHECKSUM

FLAGS: 1 a request (Flipper to module), 2 the first chunk, 4 the last, 8 the
job failed.  Each chunk is confirmed with a status, as a data frame is.

Written from the firmwares' sources: the VGM's app/uart.c, frame.c, coproc*.c
(github.com/calvingunther66/video-game-module, feature/idea-list) and
Flipware's applications/services/expansion and coproc.

Copyright (c) 2026, Kara Zajac
SPDX-License-Identifier: BSD-3-Clause
"""
import time
import zlib

import fe_expansion as x
import fe_heatshrink

COPROC = 6
REQUEST, FIRST, LAST, FAILED = 1, 2, 4, 8
COPROC_HEAD = 9                 # job 2, task 1, flags 1, seq 2, total 2, len 1
COPROC_MOST = 56                # payload bytes in a chunk
MESSAGE_MOST = 4096             # a job's or a result's bytes, at most

CALL = b"\xf0"                  # the VGM's call: one pulse, high to low
BAUD = 2_000_000                # what it asks for (it divides on both ends)
IDLE = 0.2                      # a quiet link this long: a heartbeat
PERIOD = 0.08                   # ... and here, sooner: an emulated Flipper
                                # on a busy computer is slow to answer

TASKS = {0: "CRC-32", 1: "compress", 2: "decompress", 3: "HID inject",
         4: "GPIO"}

# PB.Main's fields (flipper.proto)
F_COMMAND_ID, F_STATUS, F_HAS_NEXT, F_EMPTY = 1, 2, 3, 4
F_PING_REQUEST = 5
F_STREAM_START, F_STREAM_STOP, F_SCREEN_FRAME, F_INPUT = 20, 21, 22, 23
F_DISPLAY_START, F_DISPLAY_STOP = 26, 27
CONTENT = {4: "empty", 5: "ping request", 6: "ping response",
           20: "start screen stream", 21: "stop screen stream",
           22: "screen frame", 23: "input event", 26: "start virtual display",
           27: "stop virtual display"}
KEYS = ["up", "down", "right", "left", "ok", "back"]
PRESS, RELEASE = 0, 1


def frame_size(head):
    """The size of the frame that begins so: fe_expansion's kinds, and the
    coprocessor's (None: more bytes say)"""
    if head[0] != COPROC:
        return x.frame_size(head)
    if len(head) < 1 + COPROC_HEAD:
        return None
    if head[COPROC_HEAD] > COPROC_MOST:
        raise ValueError(f"a coprocessor chunk of {head[COPROC_HEAD]} bytes "
                         f"(it has {COPROC_MOST} at most)")
    return 1 + COPROC_HEAD + head[COPROC_HEAD] + 1


def coproc_frame(job, task, flags, seq, total, payload):
    return x.frame(COPROC, job.to_bytes(2, "little") + bytes([task, flags])
                   + seq.to_bytes(2, "little") + total.to_bytes(2, "little")
                   + bytes([len(payload)]) + bytes(payload))


def coproc_fields(data):
    """(job, task, flags, seq, total, payload) of a coprocessor frame"""
    b = data[1:]
    return (int.from_bytes(b[0:2], "little"), b[2], b[3],
            int.from_bytes(b[4:6], "little"), int.from_bytes(b[6:8], "little"),
            bytes(b[COPROC_HEAD:COPROC_HEAD + b[8]]))


def in_words(data):
    if data[0] != COPROC:
        return x.in_words(data)
    job, task, flags, seq, total, payload = coproc_fields(data)
    what = "job" if flags & REQUEST else "result"
    note = (f"{what} {job} ({TASKS.get(task, f'task {task}')}) chunk "
            f"{seq + 1}/{total}, {len(payload)} bytes"
            + (", failed" if flags & FAILED else ""))
    return "coproc", note


# ---- protobuf: the few messages the VGM sends, by hand -------------------

def field(number, value):
    """A field: an int as a varint, bytes as a length and the bytes"""
    if isinstance(value, int):
        return x.varint(number << 3) + x.varint(value)
    return x.varint(number << 3 | 2) + x.varint(len(value)) + bytes(value)


def main_message(command_id, content, body=b""):
    """A PB.Main, behind its length"""
    main = field(F_COMMAND_ID, command_id) + field(content, body)
    return x.varint(len(main)) + main


def content_of(main):
    """The field number of a PB.Main's content (None: it has none)"""
    for number in main:
        if number > F_HAS_NEXT:
            return number
    return None


# ---- the breakout's GPIO (task 4) ----------------------------------------

BREAKOUT = (16, 17, 21, 22, 23, 24, 25, 26, 27, 28, 29)
ADC_PINS = (26, 27, 28, 29)
MODES = {0: "input", 1: "output", 2: "pull-up", 3: "pull-down"}
VDD_MV = 3300
(G_OK, G_BAD_PIN, G_BAD_OP, G_BAD_ARG, G_NO_ADC, G_NOT_OUTPUT) = range(6)


class Breakout:
    """The VGM's breakout pins.  `hold` is what the outside puts on them:
    {pin: mV}; a pin that nothing holds reads its pull (none: low)."""

    def __init__(self, hold=None):
        self.mode = {}          # pin: 0..3; a pin never set is an input
        self.level = {}         # pin: what it drives as an output
        self.hold = dict(hold or {})

    def mv(self, pin):
        if self.mode.get(pin) == 1:
            return VDD_MV if self.level.get(pin) else 0
        if pin in self.hold:
            return self.hold[pin]
        return VDD_MV if self.mode.get(pin) == 2 else 0

    def record(self, op, pin, arg):
        """One record -> (status, value)"""
        if pin not in BREAKOUT:
            return G_BAD_PIN, 0
        if op == 0:
            if arg > 3:
                return G_BAD_ARG, 0
            self.mode[pin] = arg
            self.level[pin] = 0     # gpio_init(): an input that drives low
            return G_OK, 0
        if op == 1:
            if self.mode.get(pin) != 1:
                return G_NOT_OUTPUT, 0
            self.level[pin] = 1 if arg else 0
            return G_OK, 0
        if op == 2:
            return G_OK, 1 if self.mv(pin) * 2 > VDD_MV else 0
        if op == 3:
            if pin not in ADC_PINS:
                return G_NO_ADC, 0
            # adc_gpio_init(): the pin is analog now, its pulls are off
            if self.mode.get(pin) != 1:
                self.mode[pin] = 0
            return G_OK, min(4095, self.mv(pin) * 4095 // VDD_MV)
        return G_BAD_OP, 0

    def run(self, data):
        if not data or len(data) % 3:
            return None
        out = bytearray()
        for i in range(0, len(data), 3):
            status, value = self.record(*data[i:i + 3])
            out += bytes([status]) + value.to_bytes(2, "little")
        return bytes(out)


def hid_inject(data, log):
    """Task 3: records of [type, size (2 bytes), payload], which the VGM's USB
    HID device sends to the computer on its USB-C port.  None is plugged in
    here: they are told to `log`.  -> 0 when every record was good"""
    names = {1: "key press", 2: "key release", 4: "mouse move",
             5: "mouse click", 6: "mouse scroll", 8: "release all"}
    sizes = {1: 2, 2: 2, 4: 2, 5: 2, 6: 1, 8: 0}
    at, good = 0, True
    while at < len(data):
        if len(data) - at < 3:
            return 1
        kind, size = data[at], int.from_bytes(data[at + 1:at + 3], "little")
        at += 3
        if size > len(data) - at:
            return 1
        payload = data[at:at + size]
        at += size
        if kind not in names:
            continue
        if size != sizes[kind]:
            good = False
            continue
        log(f"USB HID: {names[kind]} {payload.hex(' ')}".rstrip())
    return 0 if good else 1


class Jobs:
    """What the VGM's compute task does with a job: (failed, result)"""

    def __init__(self, breakout=None, log=None):
        self.breakout = breakout or Breakout()
        self.log = log or (lambda text: None)
        self.done = {}          # task: how many

    def run(self, task, data):
        result = None
        if task == 0:
            result = (zlib.crc32(data) & 0xFFFFFFFF).to_bytes(4, "little")
        elif task == 1:
            result = fe_heatshrink.pack(data)
        elif task == 2:
            result = fe_heatshrink.unpack(data)
        elif task == 3:
            result = bytes([hid_inject(data, self.log)])
        elif task == 4:
            result = self.breakout.run(data)
        if result is None:
            return True, b""
        self.done[task] = self.done.get(task, 0) + 1
        return False, result


# ---- HDMI: the picture the VGM puts out ----------------------------------

W, H = 128, 64
HDMI_W, HDMI_H = 640, 480
# frame.c: a 320 by 240 picture, sent at 640 by 480 with each pixel doubled;
# a pixel of the Flipper's is 2 by 3 of it, 32 + 2x across and 3 (y + 8)
# down: 4 by 6 on HDMI, from (64, 48)
LEFT, TOP, PX_W, PX_H = 64, 48, 4, 6


def rgb565(v):
    r, g, b = v >> 11, (v >> 5) & 0x3F, v & 0x1F
    return (r << 3 | r >> 2, g << 2 | g >> 4, b << 3 | b >> 2)


STOCK_BG = rgb565(0xFC00)       # the VGM's orange
STOCK_FG = rgb565(0x0000)


def frame_colour(value, default, now):
    """A ScreenFrame's colour (Momentum's ScreenFrameColor: the mode, then
    red, green, blue) -> RGB.  None: the firmware sent none"""
    if value is None:
        return default
    mode = value & 0xFF
    if mode == 1:
        return ((value >> 8) & 0xFF, (value >> 16) & 0xFF, (value >> 24) & 0xFF)
    if mode == 2:                       # rainbow: a turn of hue in 5 s
        import colorsys
        r, g, b = colorsys.hsv_to_rgb((now / 5.0) % 1.0, 1.0, 1.0)
        return (int(r * 255), int(g * 255), int(b * 255))
    return default


def pixels(data):
    """A screen frame's 1024 bytes (the LCD's pages: a byte is 8 pixels down)
    -> 8192 pixels, row by row, 1 for one that is on"""
    out = bytearray(W * H)
    for y in range(H):
        page, bit = (y >> 3) * W, 1 << (y & 7)
        for xx in range(W):
            out[y * W + xx] = 1 if data[page + xx] & bit else 0
    return bytes(out)


def hdmi(bits, bg, fg):
    """The 640 by 480 picture: rows of RGB bytes"""
    bg, fg = bytes(bg), bytes(fg)
    blank = bg * HDMI_W
    rows = [blank] * HDMI_H
    margin = bg * LEFT
    for y in range(H):
        line = b"".join(fg * PX_W if b else bg * PX_W
                        for b in bits[y * W:(y + 1) * W])
        row = margin + line + bg * (HDMI_W - LEFT - W * PX_W)
        for k in range(PX_H):
            rows[TOP + y * PX_H + k] = row
    return rows


def png(rows, width, height):
    """RGB rows -> a PNG file's bytes"""
    def chunk(kind, data):
        return (len(data).to_bytes(4, "big") + kind + data
                + zlib.crc32(kind + data).to_bytes(4, "big"))
    raw = b"".join(b"\0" + row for row in rows)
    head = (width.to_bytes(4, "big") + height.to_bytes(4, "big")
            + bytes([8, 2, 0, 0, 0]))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", head)
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def splash():
    """The picture the module shows on the Flipper's own screen until a
    button is released there (the VGM's is its own art; this one is a
    game pad in a frame): 1024 bytes of XBM, rows of 16 bytes, the lowest
    bit the leftmost pixel, as the firmware's virtual display draws them
    (rpc_gui.c, canvas_draw_xbm), not the LCD pages of a screen frame"""
    on = set()
    for xx in range(W):
        on |= {(xx, 0), (xx, 1), (xx, H - 2), (xx, H - 1)}
    for y in range(H):
        on |= {(0, y), (1, y), (W - 2, y), (W - 1, y)}
    # the pad: a rounded body, a cross on the left, two buttons on the right
    for y in range(20, 44):
        for xx in range(34, 94):
            inside = 36 <= xx <= 91 and 22 <= y <= 41
            corner = (xx in (34, 35, 92, 93)) and (y in (20, 21, 42, 43))
            if not inside and not corner:
                on.add((xx, y))
    for k in range(-5, 6):
        on |= {(48 + k, 32), (48 + k, 31), (48, 32 + k), (49, 32 + k)}
    for cx, cy in ((76, 28), (82, 35)):
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                if dx * dx + dy * dy <= 5:
                    on.add((cx + dx, cy + dy))
    data = bytearray(1024)
    for xx, y in on:
        data[y * (W // 8) + (xx >> 3)] |= 1 << (xx & 7)
    return bytes(data)


class Hdmi:
    """Where the frames go: a PNG that is written anew (`path`, at most every
    `every` s, and on the last frame), a text of the screen (`text`), and a
    count"""

    def __init__(self, path=None, text=None, colours="stock", every=0.1):
        self.path, self.text, self.colours, self.every = (path, text,
                                                          colours, every)
        self.frames = 0
        self.bits = None
        self.last = None        # (data, bg value, fg value)
        self.wrote = 0.0
        self.dirty = False

    def colours_of(self, bg, fg, now):
        if self.colours == "stock":
            return STOCK_BG, STOCK_FG
        return (frame_colour(bg, STOCK_BG, now),
                frame_colour(fg, STOCK_FG, now))

    def show(self, data, orientation=0, bg=None, fg=None):
        """A frame of the stream.  The VGM draws every one as horizontal
        (its frame.c has orientation_enable false)."""
        self.frames += 1
        self.last = (bytes(data), bg, fg)
        self.bits = pixels(data)
        self.dirty = True
        self.flush()

    def flush(self, force=False):
        if not self.dirty or not self.last:
            return
        now = time.monotonic()
        if not force and now - self.wrote < self.every:
            return
        self.dirty = False
        self.wrote = now
        if self.path:
            bg, fg = self.colours_of(self.last[1], self.last[2], now)
            _atomic(self.path, png(hdmi(self.bits, bg, fg), HDMI_W, HDMI_H))
        if self.text:
            import fe_screen
            _atomic(self.text, fe_screen.to_text(self.bits).encode())


def _atomic(path, data):
    import os
    tmp = f"{path}.new"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


# ---- the link -------------------------------------------------------------

class Vgm:
    """The module's side of the link: `send(bytes)` puts bytes on the
    Flipper's RX pin, `receive(seconds)` is what came (b"": nothing), and
    `said(who, data, note)` is told of every frame but heartbeats."""

    def __init__(self, send, receive, said=None, jobs=None, hdmi=None,
                 patience=1.0, log=None):
        self.send_raw, self.receive = send, receive
        self.said = said or (lambda who, data, note: None)
        self.log = log or (lambda text: None)
        self.jobs = jobs or Jobs(log=self.log)
        self.hdmi = hdmi or Hdmi()
        self.patience = patience
        self.buffer = b""
        self.stream = b""       # the RPC session's bytes, not yet read
        self.statuses = []      # the Flipper's statuses, not yet waited for
        self.spoke = 0.0        # when we last sent anything
        self.heard = 0.0        # ... and last heard anything
        self.command = 0
        self.inbound = None     # a job that is being put together
        self.results = []       # (job, task, failed, bytes) to be sent
        self.quiet_frames = True    # data frames and acks are not told
        self.counts = {"data in": 0, "data out": 0, "jobs": 0, "results": 0}

    # -- frames
    def _send(self, data, tell=True):
        self.send_raw(data)
        self.spoke = time.monotonic()
        if tell and data[0] != x.HEARTBEAT:
            self.said("module", data, "")

    def _status(self, error=0):
        self._send(x.frame(x.STATUS, [error]), tell=not self.quiet_frames)

    def _frames(self, seconds):
        """Frames that came within that time (a frame that has begun is
        waited for, up to `patience`)"""
        out = []
        end = time.monotonic() + seconds
        while True:
            while self.buffer:
                try:
                    size = frame_size(self.buffer)
                except ValueError as e:
                    raise x.Lost(f"the Flipper sent what is no frame: {e} "
                                 f"({self.buffer[:12].hex(' ')})") from None
                if not size or len(self.buffer) < size:
                    break
                data, self.buffer = self.buffer[:size], self.buffer[size:]
                if x.checksum(data[:-1]) != data[-1]:
                    raise x.Lost("a frame of the Flipper's has a wrong "
                                 f"checksum ({data[:12].hex(' ')})")
                out.append(data)
            now = time.monotonic()
            if out and not self.buffer:
                return out
            left = end - now
            if left <= 0:
                if not self.buffer or now - self.heard > self.patience:
                    if self.buffer:
                        raise x.Lost("a frame of the Flipper's stopped in its "
                                     f"middle ({self.buffer[:12].hex(' ')})")
                    return out
                left = 0.01
            got = self.receive(min(left, 0.02))
            if got:
                self.heard = time.monotonic()
                self.buffer += got

    def _handle(self, data):
        kind = data[0]
        if kind == x.DATA:
            self.counts["data in"] += 1
            if not self.quiet_frames:
                self.said("Flipper", data, "")
            self._status(0)
            self.stream += data[2:-1]
        elif kind == x.STATUS:
            if not self.quiet_frames:
                self.said("Flipper", data, "")
            self.statuses.append(data[1])
        elif kind == x.HEARTBEAT:
            pass
        elif kind == COPROC:
            self.said("Flipper", data, in_words(data)[1])
            self._job_chunk(data)
        else:
            raise x.Lost(f"the Flipper sent a {x.NAMES.get(kind, kind)} "
                         f"frame that the module has no use for "
                         f"({data.hex(' ')})")

    def _job_chunk(self, data):
        """uart.c's expansion_handle_coproc_frame()"""
        job, task, flags, seq, total, payload = coproc_fields(data)
        error = 0
        if flags & REQUEST:
            if flags & FIRST:
                self.inbound = [job, 0, bytearray()]
            if (not self.inbound or job != self.inbound[0]
                    or seq != self.inbound[1]
                    or len(payload) > MESSAGE_MOST - len(self.inbound[2])):
                self.inbound = None
                error = 1
            else:
                self.inbound[1] += 1
                self.inbound[2] += payload
                if flags & LAST:
                    self.counts["jobs"] += 1
                    failed, result = self.jobs.run(task, bytes(self.inbound[2]))
                    self.log(f"coproc: job {job}, {TASKS.get(task, task)}, "
                             f"{len(self.inbound[2])} bytes -> "
                             + ("failed" if failed else f"{len(result)} bytes"))
                    self.results.append((job, task, failed, result))
                    self.inbound = None
        self._status(error)

    def pump(self, seconds):
        """Take what comes for so long: a heartbeat when we have been quiet
        for a while, the jobs' results sent when they are done"""
        end = time.monotonic() + seconds
        while True:
            for data in self._frames(min(PERIOD / 2, max(0, end -
                                                        time.monotonic()))):
                self._handle(data)
            self._send_results()
            now = time.monotonic()
            if now - self.heard > max(self.patience, 0.25) and self.heard:
                raise x.Lost(f"nothing came from the Flipper in "
                             f"{now - self.heard:.2f} s")
            if now - self.spoke >= PERIOD:
                self._send(x.frame(x.HEARTBEAT))
            if now >= end:
                return

    def _wait_status(self, what):
        end = time.monotonic() + self.patience
        while not self.statuses:
            if time.monotonic() > end:
                raise x.Lost(f"the Flipper did not confirm {what} in "
                             f"{self.patience:g} s")
            for data in self._frames(0.02):
                self._handle(data)
            if not self.statuses and time.monotonic() - self.spoke >= PERIOD:
                self._send(x.frame(x.HEARTBEAT))
        error = self.statuses.pop(0)
        if error:
            raise x.Lost(f"the Flipper refused {what}: "
                         + x.ERRORS.get(error, f"error {error}"))

    def _send_results(self):
        while self.results:
            job, task, failed, data = self.results.pop(0)
            total = max(1, -(-len(data) // COPROC_MOST))
            for seq in range(total):
                flags = (FIRST if seq == 0 else 0) | (
                    LAST if seq == total - 1 else 0) | (FAILED if failed else 0)
                piece = data[seq * COPROC_MOST:(seq + 1) * COPROC_MOST]
                chunk = coproc_frame(job, task, flags, seq, total, piece)
                self._send(chunk)
                self._wait_status(f"chunk {seq + 1} of job {job}'s result")
            self.counts["results"] += 1

    # -- the steps of uart_task()
    def call(self):
        """Call, and have the Flipper's heartbeat (at 9600 baud)"""
        self.buffer, self.stream, self.statuses = b"", b"", []
        self.inbound, self.results = None, []
        self.send_raw(CALL)
        self.spoke = time.monotonic()
        self.said("module", b"", "the call")
        beat = x.frame(x.HEARTBEAT)
        seen, end = b"", self.spoke + self.patience
        while True:
            at = seen.find(beat)
            if at >= 0:
                self.buffer = seen[at + len(beat):]
                self.heard = time.monotonic()
                return
            left = end - time.monotonic()
            if left <= 0:
                raise x.Lost("the Flipper did not answer the call in "
                             f"{self.patience:g} s")
            seen += self.receive(min(left, 0.05))

    def handshake(self, rate=BAUD):
        self._send(x.frame(x.BAUD_RATE, rate.to_bytes(4, "little")))
        self._wait_status(f"{rate} baud")
        time.sleep(x.DEAD_TIME + 0.005)

    def control(self, command):
        self._send(x.frame(x.CONTROL, [command]))
        self._wait_status(f"to {x.COMMANDS.get(command, command)}")

    def next_command(self):
        self.command = self.command % 0xFFFFFFFF + 1
        return self.command

    def write(self, data):
        """Bytes for the RPC session, in data frames, each confirmed"""
        for at in range(0, len(data), x.DATA_MOST):
            piece = data[at:at + x.DATA_MOST]
            self._send(x.frame(x.DATA, bytes([len(piece)]) + piece),
                       tell=not self.quiet_frames)
            self.counts["data out"] += 1
            self._wait_status("a data frame")

    def message(self, seconds):
        """The RPC session's next message: its fields (None: none came)"""
        end = time.monotonic() + seconds
        while True:
            got = x.message(self.stream)
            if got:
                main, self.stream = got
                return main
            left = end - time.monotonic()
            if left <= 0:
                return None
            self.pump(min(left, PERIOD))

    def request(self, content, body=b"", seconds=None):
        """Send a request and have its answer (what the RPC session says in
        between, a screen frame or a button, is handled as it comes)"""
        command = self.next_command()
        self.write(main_message(command, content, body))
        end = time.monotonic() + (seconds or self.patience * 2)
        while True:
            main = self.message(max(0.0, end - time.monotonic()))
            if main is None:
                raise x.Lost(f"the RPC session did not answer the "
                             f"{CONTENT.get(content, content)} request")
            if main.get(F_COMMAND_ID, 0) == command:
                if main.get(F_STATUS, 0):
                    raise x.Lost(f"the RPC session refused the "
                                 f"{CONTENT.get(content, content)} request: "
                                 f"status {main.get(F_STATUS)}")
                return main
            self.unasked(main)

    def unasked(self, main):
        """A message nobody waited for: a frame of the stream, or a button
        from the virtual display"""
        kind = content_of(main)
        if kind == F_SCREEN_FRAME:
            frame = x.fields(main[F_SCREEN_FRAME])
            data = frame.get(1, b"")
            if len(data) == 1024:
                self.hdmi.show(data, frame.get(2, 0), frame.get(3),
                               frame.get(4))
        elif kind == F_INPUT:
            event = x.fields(main[F_INPUT])
            self.last_input = (event.get(1, 0), event.get(2, 0))

    def virtual_display(self, picture, until=None):
        """Show a picture on the Flipper's screen until a button is released
        there, as the VGM does once a power-up.  False: `until()` came
        first (the display is left as it is: the module is going)"""
        first = field(1, picture) + field(2, 0)
        self.request(F_DISPLAY_START, field(1, first) + field(2, 1))
        self.log("virtual display: the VGM's picture is on the Flipper's "
                 "screen until a button is released there")
        self.last_input = None
        while True:
            if until and until():
                return False
            main = self.message(0.1)
            if main is None:
                continue
            if content_of(main) == F_INPUT:
                event = x.fields(main[F_INPUT])
                key, kind = event.get(1, 0), event.get(2, 0)
                if kind == RELEASE:
                    self.log(f"virtual display: {KEYS[key] if key < 6 else key}"
                             " released")
                    break
            else:
                self.unasked(main)
        self.request(F_DISPLAY_STOP)
        return True

    def stream_screen(self):
        self.request(F_STREAM_START)

    def run(self, seconds=None, splash=True, until=None, rate=BAUD):
        """uart_task(), for so long (None: for ever) or until `until()`.
        -> how many times the link was made"""
        made, shown = 0, not splash
        end = None if seconds is None else time.monotonic() + seconds
        while end is None or time.monotonic() < end:
            if until and until():
                break
            try:
                self.call()
                self.handshake(rate)
                self.control(x.START_RPC)
                self.log(f"linked at {rate} baud, RPC session started")
                if not shown:
                    if not self.virtual_display(splash_picture(), until):
                        break
                    shown = True
                self.stream_screen()
                made += 1
                self.log("screen stream: on HDMI")
                while end is None or time.monotonic() < end:
                    if until and until():
                        break
                    main = self.message(0.1 if end is None else
                                        max(0.0, min(0.1, end -
                                                     time.monotonic())))
                    if main is not None:
                        if main.get(F_COMMAND_ID, 0):
                            continue    # an answer to nothing of ours
                        self.unasked(main)
                    self.hdmi.flush()
                else:
                    break
                break
            except x.Lost as e:
                self.log(f"link lost: {e}; calling again")
                time.sleep(0.25)
        self.hdmi.flush(force=True)
        return made


def splash_picture():
    return splash()
