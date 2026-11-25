// Copyright (c) 2026, the FlipperEmu contributors
// SPDX-License-Identifier: BSD-3-Clause
//
// Drives ESP32 Marauder on the Wi-Fi Developer Board as the WiFi Marauder
// app does: its command line on pins 13 (TX) and 14 (RX) at 115200 baud.
// Scans for five seconds, stops, and lists what it found; every line the
// board sent is printed.
//
//     ./flipperemu devboard --firmware marauder &
//     ./flipperemu push examples/devboard/marauder.js /ext
//     ./flipperemu cli "js /ext/marauder.js"
let serial = require("serial");
serial.setup("usart", 115200);

// Listens for about `ms` milliseconds of quiet: only a read that waited out
// its 250 ms counts (a line, and the empty read at its "\r\n", do not).
function listen(ms) {
    let left = ms;
    let got = false;
    while (left > 0) {
        let line = serial.readln(250);
        if (line !== undefined && line.length > 0) {
            print(line);
            got = true;
        } else {
            if (!got) left -= 250;
            got = false;
        }
    }
}

serial.write("\n");
listen(500);
serial.write("scanall\n");
listen(5000);
serial.write("stopscan\n");
listen(500);
serial.write("list -a\n");
listen(1000);
serial.end();
print("marauder done");
