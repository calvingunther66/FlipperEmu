"""Heatshrink, the LZSS of the Flipper's firmware (lib/heatshrink, used by its
toolbox's compress.c), as the C library does it, byte for byte.

The firmware's compressed blocks begin with four bytes of its own:

    01 00 SIZE SIZE     compressed: SIZE bytes of heatshrink follow (low first)
    00                  stored: the bytes follow as they are

encode() and decode() are the raw streams; pack() and unpack() are the
firmware's blocks, as compress_encode() and compress_decode() make and take
them, with a window of 2^8 and a lookahead of 2^4 bytes.

The encoder is the C one's state machine, step for step: its window starts as
zeros that a match may point into, and a match is the longest one, the
nearest of equals.  Its output is the C library's, which the firmware checks
(`storage heatshrink` compares an offloaded result with its own).

Copyright (c) 2026, Kara Zajac
SPDX-License-Identifier: BSD-3-Clause
"""

WINDOW_BITS = 8         # the firmware's (compress_config_heatshrink_default)
LOOKAHEAD_BITS = 4


class _Bits:
    """Bits out, the highest first, as the C encoder pushes them"""

    def __init__(self):
        self.out = bytearray()
        self.byte = 0
        self.mask = 0x80

    def push(self, count, bits):
        if count == 8 and self.mask == 0x80:
            self.out.append(bits & 0xFF)
            return
        for i in range(count - 1, -1, -1):
            if bits & (1 << i):
                self.byte |= self.mask
            self.mask >>= 1
            if not self.mask:
                self.out.append(self.byte)
                self.byte, self.mask = 0, 0x80

    def flush(self):
        if self.mask != 0x80:
            self.out.append(self.byte)
        return bytes(self.out)


def encode(data, window_bits=WINDOW_BITS, lookahead_bits=LOOKAHEAD_BITS):
    """The heatshrink stream of these bytes (heatshrink_encoder: sink all,
    poll, finish)"""
    ws, la = 1 << window_bits, 1 << lookahead_bits
    buf = bytearray(2 * ws)
    index = [-1] * (2 * ws)
    bits = _Bits()
    data = bytes(data)
    taken = 0               # bytes of data sunk so far
    size = 0                # bytes of input in the buffer (input_size)
    msi = 0                 # match_scan_index
    finishing = False
    # the cost of a backreference, in bytes, that a match must beat
    even = (1 + window_bits + lookahead_bits) // 8

    while True:
        # sink until the input half of the buffer is full or the data ends
        n = min(ws - size, len(data) - taken)
        buf[ws + size:ws + size + n] = data[taken:taken + n]
        size += n
        taken += n
        if size < ws:
            if taken < len(data):
                continue
            finishing = True            # heatshrink_encoder_finish()
        # do_indexing: for each byte, where that value was before
        last = [-1] * 256
        for i in range(ws + size):
            v = buf[i]
            index[i] = last[v]
            last[v] = i
        while True:                     # st_step_search and the yields
            if msi > size - (1 if finishing else la):
                break
            end = ws + msi
            start = end - ws
            most = min(la, size - msi)
            best, at = 0, -1
            pos = index[end]
            while pos - start >= 0:
                if buf[pos + best] != buf[end + best]:
                    pos = index[pos]
                    continue
                n = 1
                while n < most and buf[pos + n] == buf[end + n]:
                    n += 1
                if n > best:
                    best, at = n, pos
                    if n == most:
                        break
                pos = index[pos]
            if best > even:
                bits.push(1, 0)                         # backref marker
                back = end - at - 1
                if window_bits > 8:
                    bits.push(window_bits - 8, back >> 8)
                    bits.push(8, back & 0xFF)
                else:
                    bits.push(window_bits, back)
                bits.push(lookahead_bits, best - 1)
                msi += best
            else:
                msi += 1
                bits.push(1, 1)                         # literal marker
                bits.push(8, buf[ws + msi - 1])
        if finishing:
            return bits.flush()
        # save_backlog: what was looked at becomes the window
        rem = ws - msi
        buf[0:ws + rem] = buf[ws - rem:2 * ws]
        size -= ws - rem
        msi = 0


def decode(stream, window_bits=WINDOW_BITS, lookahead_bits=LOOKAHEAD_BITS,
           limit=None):
    """The bytes of a heatshrink stream.  Bits left over at its end that
    make no whole literal or backreference are ignored, as the C decoder
    does.  ValueError: more than `limit` bytes"""
    ws = 1 << window_bits
    window = bytearray(ws)
    head = 0
    out = bytearray()
    total = len(stream) * 8
    at = 0

    def take(count):
        nonlocal at
        if at + count > total:
            return None
        v = 0
        for _ in range(count):
            v = (v << 1) | ((stream[at >> 3] >> (7 - (at & 7))) & 1)
            at += 1
        return v

    def put(c):
        nonlocal head
        out.append(c)
        if limit is not None and len(out) > limit:
            raise ValueError(f"more than {limit} bytes")
        window[head & (ws - 1)] = c
        head += 1

    while True:
        tag = take(1)
        if tag is None:
            break
        if tag:
            c = take(8)
            if c is None:
                break
            put(c)
            continue
        back = take(window_bits)
        if back is None:
            break
        count = take(lookahead_bits)
        if count is None:
            break
        back += 1
        for _ in range(count + 1):
            put(window[(head - back) & (ws - 1)])
    return bytes(out)


HEADER = 4
MOST = 4096             # a coprocessor message's room (COPROC_MAX_MESSAGE_SIZE)


def pack(data, room=MOST):
    """A block as compress_encode() makes one: compressed when that is
    smaller, else stored.  None: it does not fit in `room` bytes."""
    if not data:
        return None
    stream = encode(data)
    size = HEADER + len(stream)
    if size <= room and size < len(data) + 1 and len(stream) <= 0xFFFF:
        return bytes([1, 0, len(stream) & 0xFF, len(stream) >> 8]) + stream
    if room > len(data):
        return b"\0" + bytes(data)
    return None


def unpack(block, room=MOST):
    """The bytes of a block (compress_decode()).  None: it is no such block,
    or what it holds is more than `room` bytes."""
    if not block:
        return None
    if not block[0]:
        return bytes(block[1:]) if len(block) - 1 <= room else None
    if len(block) < HEADER:
        return None
    size = block[2] | block[3] << 8
    if size > len(block) - HEADER:
        return None
    try:
        return decode(block[HEADER:HEADER + size], limit=room - 1)
    except ValueError:
        return None
