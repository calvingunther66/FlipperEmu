# The Wi-Fi Developer Board

The Wi-Fi Developer Board is Flipper Devices' ESP32-S2 board for the GPIO
header. `./flipperemu devboard` puts an emulated one on a running device's
header. It's a program on your computer at the far end of the pins that the
real board uses:

| Header pins | Flipper side | Board side |
|---|---|---|
| 13, 14 (TX, RX) | USART1 | the ESP32-S2's UART0 |
| 10, 12 (SWC, SIO) | SWCLK, SWDIO of the STM32WB55 | two GPIOs, driven by the probe in its firmware |
| 9 (3V3), 8/11/18 (GND) | power | power |

The board's behaviour comes from its firmware, and two firmwares are
modelled:

- **`blackmagic`** (the default) is the firmware the board ships with,
  [flipperdevices/blackmagic-esp32-s2](https://github.com/flipperdevices/blackmagic-esp32-s2),
  which runs Black Magic Probe `v1.9.0-1-g1cdc914`. It makes the board a
  debug probe for the Flipper.
- **`marauder`** is [ESP32 Marauder](https://github.com/justcallmekoko/ESP32Marauder)
  v1.21.0, built for "Flipper Zero Dev Board". It's the command line that the
  WiFi Marauder app on the Flipper drives.

```sh
./flipperemu run -f firmware.tgz                 # a device
./flipperemu devboard                            # second terminal: the board, as it ships
./flipperemu devboard --firmware marauder        # ... or with Marauder
```

The board stays on the header until you press Ctrl-C, until `--for S` runs
out, or until the device stops. It holds pins 13 and 14 while it's there, the
same way `expansion` does, so `uart --port usart` and the expansion service
can't use them at the same time. That's what a physical board does too.

### In the window

When the device runs in the window (`./flipperemu run` with the GUI), the
board appears on top of the Flipper. It stands up from the GPIO sockets with
its ESP32-S2 module, its USB-C port, BOOT and RESET, and its LED, and the
window grows to fit it. The board shows which firmware it runs and what it's
doing, such as `gdb :2345 attached` or `scanall · 6 APs`. Its LED shows that
too, in this picture's own colours:

| LED | Black Magic Probe | Marauder |
|---|---|---|
| blue | no gdb | nothing running |
| green | a gdb is connected | a scan or a sniff runs |
| amber | attached to the Flipper's core | |
| red | | an attack runs |

These colours are the window's, not the real board's.

Hover over the board for its ports. Click it, or right-click it, for its
menu, which has gdb's and fbt's commands to copy and **Pull it out**. Pull it
out ends `devboard` as Ctrl-C does. The console says when a board goes on
the header and when it comes off.

`devboard` tells the window what's on the header through
`devices/NAME/module.json`, a small JSON object such as
`{"module": "devboard", "firmware": "blackmagic", "gdb": "attached",
"gdb_port": 2345, "uart_port": 3456, "pid": 4242}`. It writes the file again
when anything in it changes, and removes it on exit. The window ignores a
file whose process has gone. Another module can use the same file: the
window draws a kind that `fe_gui_view.MODULE_ROOM` knows with that view's
`draw_KIND`, and gives it a `tip_KIND` and a `fill_menu_KIND` in `fe_gui`.

## Black Magic Probe (the stock firmware)

```
$ ./flipperemu devboard
[flipper] the Wi-Fi Developer Board is on the header, with Black Magic Probe (its stock firmware):
[flipper]   GDB server (Black Magic Probe): TCP port 2345; also devices/flipper/devboard-gdb.tty
[flipper]   the Flipper's USART (pins 13 and 14) at 230400 baud: TCP port 3456; also devices/flipper/devboard-uart.tty
[flipper]   gdb: target extended-remote localhost:2345, then monitor swdp_scan and attach 1 (in a firmware's tree: ./fbt blackmagic BLACKMAGIC=localhost:2345)
[flipper]   Ctrl-C pulls it out
```

The real board offers these over Wi-Fi (its access point, `blackmagic` /
`iamwitcher`, or your network) and over USB. Here the Wi-Fi ports are TCP
ports on this computer (`--listen 0.0.0.0` exposes them to other machines,
like the board's Wi-Fi), and the USB serial ports are ptys:

| | Real board | Here |
|---|---|---|
| GDB server | `blackmagic.local:2345`, first USB serial port | `localhost:2345` (`--gdb-port`), `devices/NAME/devboard-gdb.tty` |
| UART bridge (the Flipper's log) | TCP port 3456, second USB serial port, 230400 baud | `localhost:3456` (`--uart-port`), `devices/NAME/devboard-uart.tty` |

### Debugging and flashing with fbt

In a firmware's source tree (official, Momentum, Unleashed, Flipware...):

```sh
./fbt blackmagic BLACKMAGIC=localhost:2345      # gdb with the firmware's scripts, attached
./fbt flash SWD_TRANSPORT=blackmagic_wifi SWD_TRANSPORT_SERIAL=127.0.0.1
```

VS Code's "Attach FW (blackmagic)" configuration (`./fbt vscode_dist`) works
too. Give it `localhost:2345` as the probe's address.

Turn on Debug first (Settings > System > Debug, or
`./flipperemu cli "sysctl debug 1"`). With Debug off, the firmware puts its
SWD pins into analog mode and the probe can't reach the core. On real
hardware and here, `monitor swdp_scan` then says `SW-DP scan failed!`, and the
board's log says why. `./fbt`'s `enable_debug` step looks for a USB Flipper
named `flip_*` and doesn't find the emulated one, so it skips itself quietly.

### What the probe does

The board answers GDB the way Black Magic Probe does. Packets the probe
answers itself are answered here. Packets that reach the processor go to
QEMU's own gdb stub, which the board opens when it attaches and closes when
it detaches.

- **Scan and attach.** `monitor swdp_scan` finds `STM32WBxx M4`. `attach 1`
  halts the processor (QEMU stops the machine) and `detach` lets it run.
  `kill` resets and detaches, and `run` resets. When gdb disconnects, the
  probe detaches.
- **The memory map** is the one the probe builds for an STM32WB: flash at
  0x08000000 in 4 KiB pages, sized from the chip's FLASHSIZE register, plus
  the RAM. It keeps the probe's own slip, too: SRAM2 at 0x10000000 is listed
  as 0x40 bytes, where 0x40 KiB was meant.
- **Flash.** gdb's `load` erases and writes the flash with vFlashErase and
  vFlashWrite. CPU2's secure area (from FLASH->SFR, as the chip has it) is
  refused, as on the chip. A plain memory write to the flash is refused
  (E01), as a bus error is. What `load` writes reaches the machine's flash
  and also the flash's image (`qemu-io` in QEMU's monitor), so it survives a
  restart like a real flash.
- **`compare-sections`** gets its CRCs from the probe (qCRC) over the flash.
- **Breakpoints and watchpoints** in hardware are the processor's: six
  breakpoints (FPB) and four watchpoints (DWT). gdb uses hardware breakpoints
  in the flash on its own. A seventh is refused.
- **A BKPT instruction halts the processor**, as it does with a debugger on
  SWD, and `continue` goes on from the instruction after it (the probe steps
  past a BKPT). The firmware has a BKPT where the loader starts an app and
  where it crashes. Both run only when the firmware's debugger flag is set
  (`furi_hal_debug_gdb_session_active`, which the firmware's gdb script
  sets). QEMU's processor model has no halting debug: a BKPT that ran would
  be a HardFault there. So the board puts a stub breakpoint on every BKPT in
  the flash when it attaches, and on every BKPT that gdb writes into RAM
  (gdb's own breakpoints outside the flash), and QEMU stops there before the
  instruction runs. A breakpoint of the stub's changes nothing in memory, so
  a halfword that only looks like a BKPT costs nothing.
- **Monitor commands** are the probe's general ones: `version`, `help`,
  `swdp_scan`, `jtag_scan` (it fails: the board wires no JTAG), `auto_scan`,
  `targets`, `morse`, `halt_timeout`, `connect_rst`, `reset` (pulses nRST),
  `heapinfo`. Once attached, the target's commands are there too:
  `vector_catch`, `erase_bank1` (the flash below CPU2's part), `erase_bank2`,
  and `option` (read only). `monitor debug_bmp`, which fbt sends, isn't in
  this build of the probe, so it answers as the board does: "Target does not
  support this command."
- **One gdb at a time**, as on the board. A second connection is closed.

The emulator's own `./flipperemu gdb` is still there and is quicker for
everyday debugging. The board is for workflows that expect a probe: fbt's
flash and debug targets, an IDE's blackmagic configuration, and scripts
written for the board.

### What isn't modelled

- The web interface (`http://192.168.4.1`, `blackmagic.local`), mDNS, and
  the Wi-Fi settings. The ports are on this computer.
- DAPLink mode (the board's other USB mode).
- The board's own command line (UART1 on its GPIO 17/18) and its LED.
- Flashing the board's ESP32-S2 (its BOOT button and ROM bootloader):
  `--firmware` chooses what it runs instead.
- RTOS threads: Black Magic Probe doesn't do them either.

## ESP32 Marauder

```sh
./flipperemu devboard --firmware marauder --scene examples/devboard/scene.json
```

Marauder's command line is on pins 13 and 14 at 115200 baud, which is where
the WiFi Marauder app opens its port. The board prints Marauder's banner
when it powers up. (On a real Flipper the expansion service may take that
first byte for a module's call, and it does here too.) It echoes each
command as `#command`, answers it in Marauder's own words, and prompts with
`> `.

The air around the board is a scene: access points (ESSID, BSSID, channel,
signal, security, brand, stations) and probe requests. It's a JSON file in
the shape of `examples/devboard/scene.json`, or the built-in one. A scan
finds the scene a little at a time, the way a real scan meets the beacons:

| Command | What it does here |
|---|---|
| `scanall` | access points (`-41 Ch: 1 a4:2b:... ESSID: Bramble-Home 64 00`), then their stations (`3: sta: ... -> ap: ...`) |
| `sniffbeacon`, `sniffprobe`, `sniffraw`, `sniffdeauth`, `sniffpmkid` | Marauder's start lines and what the scene has for each |
| `stopscan` | `Stopping WiFi tran/recv` |
| `list -a` / `-s` / `-c` / `-p` | what was found, as Marauder lists it, `(selected)` marks |
| `select -a` / `-s` / `-c` with indices, `all` or `-f "contains ..."` | toggles, with the counts |
| `info`, `info -a N` | the board, or one access point |
| `ssid -a -n NAME`, `ssid -a -g N`, `ssid -r N` | the SSID list |
| `attack -t deauth / beacon -l -r -a / probe / rickroll / ...` | Marauder's checks (a target selected, SSIDs in the list) and start lines. Nothing is transmitted. |
| `channel [-s N]`, `settings`, `clearlist`, `help`, `reboot` | as on the board |

`examples/devboard/marauder.js` is a script for the Flipper that drives the
board the way the app does. It scans for five seconds, stops, and lists the
results.

Not modelled: PCAP streams to the app (`-serial` and the `[BUF/BEGIN]`
markers), Bluetooth, the evil portal, GPS, the SD card, joining a network,
and anything that would need real radio.

## Tests

- `scripts/devboard_selftest.py` tests the board on its own, without
  FlipperEmu's QEMU. The probe runs against a real gdb and the gdb stub of
  the distribution's `qemu-system-arm` (its netduinoplus2, a Cortex-M4 with
  flash at 0x08000000), with a small program
  (`scripts/devboard_selftest/target.c`). Marauder runs against a Flipper's
  side played by the script. It needs `qemu-system-arm`,
  `arm-none-eabi-gcc` and `gdb-multiarch`.
- `scripts/devboard_gui_check.py FIRMWARE --out DIR` tests the board in the
  window, off-screen. The board appears on the header and says when a gdb
  connects and attaches. Pull it out removes it, and Marauder shows a scan
  from `marauder.js`. It saves a picture of each step in DIR.
- `scripts/devboard_check.py FIRMWARE` tests the board on the emulator with a
  Flipper firmware. With Debug off the scan fails, and after `sysctl debug 1`
  it attaches and detaches. An app's start halts at the loader's BKPT when
  the firmware's symbols are there, the log appears on the UART bridge,
  `load` writes a page that survives a restart, and Marauder answers a script
  on the Flipper.
