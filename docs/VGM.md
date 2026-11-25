# The Video Game Module

The Video Game Module (VGM) is Flipper Devices' RP2040 board for the GPIO
header: an HDMI port, a USB-C port, an ICM-42688-P IMU and a breakout of
eleven RP2040 pins. FlipperEmu does not run the RP2040. It models what the
Flipper's firmware sees of the module on the header, which is everything the
firmware and its apps can tell apart:

| On the header | What the VGM does there | Here |
|---|---|---|
| 13, 14 (USART1) | An expansion module: the link, an RPC session, the screen stream, Flipware's coprocessor jobs | `./flipperemu vgm` (scripts/fe_vgm.py) |
| 2, 3, 5, 7 (SPI1, CS on PC3) and 6 (INT1 on PB2) | The IMU, wired straight to the Flipper | `run --spi imu@7` (qemu/hw/arm/flipper/icm42688p.c) |
| 15 (PC1), 16 (PC0) | RUN and USB_BOOT of the RP2040: low holds it in reset or in its UF2 boot loader | `vgm` watches both |
| 10, 12 (SWD) | The RP2040's debug port, which the Video Game Module Tool uses to flash it | not modelled |

```sh
./flipperemu run --spi imu@7 -f firmware.elf --detach   # the IMU on the bus
./flipperemu vgm                                        # the module on 13 and 14
```

`vgm` stays until Ctrl-C (or `--for S`), and writes the HDMI picture to
`vgm-hdmi.png` in the device's folder (`--hdmi FILE.png`; `--text FILE` also
writes the mirrored screen as text, as `shot --text` does).

## In the window

While `vgm` runs, the window draws the module on the back of the Flipper,
its top edge above the GPIO header with the HDMI and USB-C ports in it,
an HDMI cable in the one.
Its light says what it does: amber while it calls the Flipper, blue while it
is linked or its picture is on the Flipper's screen, green while the screen
goes to HDMI, red while the Flipper holds
RUN low, purple in the UF2 boot loader. The HDMI dock (View > The Video Game
Module's HDMI, Ctrl+Shift+H) shows the picture it sends ("no signal" while
its RP2040 is in reset or its boot loader). It comes up by itself when a
module comes on the header, and goes again with it. GPIO > Plug in the Video
Game Module starts `vgm` from the window; a click on the module has its
menu, with Pull it out. `vgm` says what it does in the device's
module.json, the file the Wi-Fi Developer Board uses too
([DEVBOARD.md](DEVBOARD.md)). `scripts/vgm_gui_check.py FIRMWARE --out DIR`
checks all of it off-screen and keeps a picture of each step.

## The link (pins 13 and 14)

What the module does is what the VGM firmware's `app/uart.c` does, in the
same order:

1. It calls: one byte, `F0`, at 9600 baud. The firmware's expansion service
   waits for its falling edge (Settings > Expansion Modules > Listen UART,
   pins 13 and 14), and answers with a heartbeat.
2. It asks for 2 000 000 baud. The firmware confirms at the old rate, and
   both sides switch. (The stock VGM firmware asks for 1 843 200; Flipware's
   VGM firmware asks for 2 000 000, which divides exactly on both chips.)
3. It starts an RPC session over the link.
4. Once per power-up, it shows a picture of its own on the Flipper's screen
   (an RPC virtual display) until a button is released there, then ends it.
   The picture here is a game pad in a frame, not the VGM's own art.
   `--no-splash` skips this step.
5. It starts the screen stream and draws every frame on HDMI.
6. When the link is lost (the Flipper hears nothing for 250 ms, or the module
   nothing for 200 ms), it starts again at 1.

Frames are acknowledged as the VGM firmware acknowledges them. Each side has
one frame of its own in flight until the other side's status, and the
Flipper's data frames are acknowledged as they come, so frames of both
sides cross on the wire. With `-v`, every frame of both sides except
heartbeats is printed.

## HDMI

The VGM draws a 320 by 240 picture and sends it as 640 by 480. Each Flipper
pixel is 2 by 3 of the small picture, so 4 by 6 on HDMI, at (64, 48). Like
the VGM firmware, the model always draws the frame as horizontal
(`orientation_enable` is false in its `frame.c`). The colours are the VGM's
own, orange and black. `--colours firmware` uses the colours that each frame
carries instead: Momentum and Flipware send them (Settings > Momentum > Misc
> VGM Color), and Momentum's build of the VGM firmware draws them.

Flipware's VGM firmware can also run a desktop shell of its own on HDMI and
on its USB display port (a file browser and an app dock). That is the VGM's
own UI, built on the same link, and is not modelled here. The mirror is.

## Coprocessor jobs (Flipware)

Flipware adds a frame type to the expansion protocol: a coprocessor frame,
type 6, beside the RPC session. The Flipper sends it a job in chunks, and
the module answers with the result in chunks:

```
06 JOB(2) TASK FLAGS SEQ(2) TOTAL(2) LEN PAYLOAD(LEN, up to 56) CHECKSUM
FLAGS: 1 request (Flipper to module), 2 first chunk, 4 last chunk, 8 failed
```

Every chunk is confirmed with a status, as a data frame is. A job or result
is 4096 bytes at most. The module runs these tasks as Flipware's VGM
firmware runs them (`app/coproc*.c`):

| Task | Input | Result |
|---|---|---|
| 0 CRC-32 | bytes | 4 bytes, the low first (IEEE 802.3) |
| 1 heatshrink compress | bytes | a block as the firmware's `compress_encode()` makes it (window 2^8, lookahead 2^4) |
| 2 heatshrink decompress | such a block | the bytes |
| 3 HID inject | records `[type, size(2), payload]` | 1 byte, 0 when all were good. The keys and mouse moves are logged, because no computer is on the VGM's USB-C here |
| 4 GPIO | records `[op, pin, arg]` | `[status, value(2)]` for each |

The compressor (scripts/fe_heatshrink.py) is the C library's state machine
step for step. Its blocks are byte for byte the firmware's own, so
`storage heatshrink FILE` on Flipware says "matches local compress_encode:
yes" for an offloaded run, as it does with a real VGM.

The firmware's own checks use these tasks:

```
>: storage crc32 /ext/file          # offloaded when a VGM answers
>: storage heatshrink /ext/file     # compress and decompress offloaded, checked against local
>: vgm gpio 26 adc                  # the breakout's GPIO
```

### The breakout

The GPIO task reaches the breakout pins only: 16, 17 and 21 to 29, with an
ADC on 26 to 29. Any other pin is refused per record ("not a VGM breakout
pin"). The module keeps each pin's mode and the level it drives as an
output. What the outside holds on a pin is set with
`vgm --pin 26=1.2V --pin 22=high`. An input that nothing holds reads its
pull: high with a pull-up, otherwise low. The ADC reads 12 bits of 3.3 V.

## The IMU (pins 2, 3, 5, 6 and 7)

On a VGM the ICM-42688-P is wired to the Flipper, not to the RP2040, and the
Flipper's apps talk to it directly: Air Mouse, Air Arkanoid and the JS `vgm`
module. `run --spi imu@7` (the device `icm42688p`, chip select on pin 7, PC3)
puts it on the header's SPI bus. Its INT1 is on pin 6 (PB2): when it is
asserted, the IMU holds that pin as `gpio` would. The datasheet behaviour
modelled is what those apps use: WHO_AM_I `0x47`, the soft reset, register
banks 0 and 4, the data registers at the full scale and byte order the
firmware chose, the gyro's user offsets, the FIFO in stream mode (16-byte
packets at the output data rate), the threshold and data-ready interrupts,
pulsed or latched.

Its motion is set while it runs:

```sh
./flipperemu vgm --imu                              # what it measures now
./flipperemu vgm --imu gyro-z=90000 accel-x=-250    # turn at 90 °/s, tilt
```

Accelerations are in mg (default: 0, 0, 1000, flat and at rest), rotation in
m°/s, temperature in m°C. The model has no noise and no self-test.

## The RUN and USB_BOOT lines (pins 15 and 16)

The Flipper can reset the RP2040: RUN, pin 15 (PC1), is pulled low through
a MOSFET on the module. `vgm` watches the pin. While the firmware drives it
low, the module says nothing, and the Flipper's expansion service loses the
link. When it goes high again, the module starts as at power-up, with its
picture first. USB_BOOT, pin 16 (PC0), held low, keeps the RP2040 in its UF2
boot loader. Its firmware does not run there, so the module stays silent.

## Not modelled

- Flashing the RP2040 over SWD (pins 10 and 12), as the Video Game Module
  Tool does. The tool's own checks of the module fail here.
- The VGM's USB-C side: its CLI, its HID device, and the USB display stream
  of Flipware's desktop.
- The desktop shell of Flipware's VGM firmware.
- Power: the module draws nothing, and needs no 5 V on pin 1.
