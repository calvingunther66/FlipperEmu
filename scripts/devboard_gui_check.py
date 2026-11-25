#!/usr/bin/env python3
# Copyright (c) 2026, the FlipperEmu contributors
# SPDX-License-Identifier: BSD-3-Clause
"""Off-screen check of the Wi-Fi Developer Board in the window (fe_gui.py).

    scripts/devboard_gui_check.py firmware/official-1.5.1/flipper-z-f7-update-1.5.1.tgz --out shots

Boots a throwaway device with `flipperemu run`, which opens the window
off-screen, as scripts/gui_check.py does.  Then `flipperemu devboard` puts
the board on its header: the window draws it standing up from the GPIO
sockets (the view grows by it) and says so in its console.  A gdb client on
the board's GDB server (this script, speaking the protocol) connects, scans
and attaches after `sysctl debug 1`, and the board in the window says so
(and its LED), as its tooltip does; the board's menu has gdb's target command.
Its menu's Pull it out ends `devboard` (it exits 0), and the window shrinks
back.  Then the board with ESP32 Marauder, and examples/devboard/marauder.js
on the Flipper scans: the board in the window says it scans, then how many
access points it found.  Pictures of each step go to --out.  Exit status 0 =
pass.
"""
import argparse
import importlib.machinery
import importlib.util
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6 import QtCore, QtWidgets  # noqa: E402
from PySide6.QtCore import QPoint  # noqa: E402

import check_limit  # noqa: E402
import fe_devboard  # noqa: E402
import fe_gui  # noqa: E402
import fe_gui_view  # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append((label, bool(ok)))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}", flush=True)
    if detail and not ok:
        print("        " + str(detail).strip().replace("\n", "\n        "),
              flush=True)
    return ok


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Gdb:
    """Just enough of a gdb: packets to the board's GDB server, answers"""

    def __init__(self, port):
        self.s = socket.create_connection(("127.0.0.1", port), 10)
        self.s.settimeout(30)
        self.buf = b""

    def ask(self, payload):
        self.s.sendall(fe_devboard.frame(payload.encode()))
        while True:
            start = self.buf.find(b"$")
            end = self.buf.find(b"#", start + 1) if start >= 0 else -1
            if start >= 0 and end >= 0 and len(self.buf) >= end + 3:
                body = self.buf[start + 1:end]
                self.buf = self.buf[end + 3:]
                self.s.sendall(b"+")
                if body.startswith(b"O") and body != b"OK":
                    continue                    # console output: more comes
                return body.decode(errors="replace")
            data = self.s.recv(65536)
            if not data:
                return ""
            self.buf += data

    def monitor(self, command):
        return self.ask("qRcmd," + command.encode().hex())

    def close(self):
        self.s.close()


class Driver:
    """fe_gui.run's stand-in: the same window, driven by this script"""

    def __init__(self, name, out, timeout):
        self.name, self.out, self.timeout = name, out, timeout

    def __call__(self, name, directory, launcher, session=None, scale=None):
        self.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        self.win = fe_gui.MainWindow(name, directory, launcher, session, scale=2)
        self.win.show()
        self.win.fit()
        try:
            self.checks()
        finally:
            if not self.win.closing:
                self.win.close()
            self.spin(0.3)
        return self.win.exit_code

    # ---- helpers

    def spin(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.app.processEvents(QtCore.QEventLoop.AllEvents, 20)
            time.sleep(0.01)

    def until(self, cond, timeout):
        end = time.monotonic() + timeout
        while not cond():
            if time.monotonic() > end:
                return False
            self.spin(0.05)
        return True

    def shot(self, tag):
        self.win.grab().save(str(self.out / f"devboard-{tag}.png"))

    def fe(self, *args):
        return subprocess.Popen([sys.executable, str(ROOT / "flipperemu"), "-d",
                                 self.name, *map(str, args)], text=True,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)

    def wait(self, p, timeout=120):
        """a command started with fe(), to its end: (exit status, output)"""
        if not self.until(lambda: p.poll() is not None, timeout):
            p.kill()
        return p.wait(), p.stdout.read()

    def board_up(self, *more):
        p = self.fe("devboard", *more)
        os.set_blocking(p.stdout.fileno(), False)
        said = []

        def ready():
            try:
                said.append(p.stdout.read() or "")
            except (OSError, TypeError):
                pass
            return "Ctrl-C pulls it out" in "".join(said) or p.poll() is not None
        self.until(ready, 30)
        os.set_blocking(p.stdout.fileno(), True)
        return p, "".join(said)

    def info(self):
        return self.win.view.module_info

    def module_point(self):
        r = fe_gui_view.DEVBOARD
        v = self.win.view
        return QPoint(round(r.center().x() * v.k),
                      round((r.center().y() + v.top) * v.k))

    # ---- the checks

    def checks(self):
        w, v = self.win, self.win.view
        if not check("it starts", self.until(lambda: w.state == "on"
                                             and v.frame is not None,
                                             self.timeout)):
            return
        self.spin(4)
        h0 = v.height()
        self.shot("0-none")

        gdb_port, uart_port = free_port(), free_port()
        p, said = self.board_up("--gdb-port", gdb_port, "--uart-port",
                                uart_port)
        if not check("devboard: the board is on the header", p and
                     "Ctrl-C pulls it out" in said, said):
            return
        check("the window draws it on the header",
              self.until(lambda: v.module == "devboard", 5)
              and self.info().get("firmware") == "blackmagic", self.info())
        check("... and the view grows by it",
              self.until(lambda: v.height() > h0, 3),
              f"{h0} -> {v.height()}")
        check("... and its console says so", "Developer Board is on the "
              "GPIO header" in w.log.text.toPlainText())
        check("... and a click on the GPIO pins still finds pin 13",
              v.pin_at(v.pin_point(13)) == 13)
        self.spin(0.5)
        self.shot("1-blackmagic")

        g = Gdb(gdb_port)
        check("a gdb connects: the board says so", self.until(
            lambda: self.info().get("gdb") == "connected", 5), self.info())
        g.ask("qSupported:multiprocess+")
        self.wait(self.fe("cli", "sysctl debug 1"), 60)
        scan = g.monitor("swdp_scan")
        attach = g.ask("vAttach;1")
        check("it attaches after sysctl debug 1", attach.startswith("T"),
              f"{scan!r} {attach!r}")
        check("... and the board says so, with its LED",
              self.until(lambda: self.info().get("gdb") == "attached", 5)
              and v.devboard_led(self.info()) == "#ffb72e", self.info())
        tip = v.tip("module")
        check("its tooltip has gdb's port, attached",
              f"localhost:{gdb_port}" in tip and "attached" in tip, tip)
        check("the module is under the pointer there",
              v.zone_at(self.module_point()) == "module")
        self.spin(0.5)
        self.shot("2-attached")
        w.show_module_menu(QtCore.QPoint(0, 0))
        texts = [a.text() for a in w.module_menu.actions()]
        check("its menu has gdb's target command and Pull it out",
              any("gdb" in t for t in texts) and "Pull it out" in texts, texts)
        w.module_menu.close()
        g.ask("D")
        g.close()

        # Pull it out, from the menu
        pull = next(a for a in w.module_menu.actions()
                    if a.text() == "Pull it out")
        pull.trigger()
        code, text = self.wait(p, 20)
        check("Pull it out ends devboard (exit status 0)", code == 0, text)
        check("... and the window draws no board, as small as before",
              self.until(lambda: v.module is None and v.height() == h0, 5),
              f"{v.module} {v.height()}")

        # Marauder, scanning for the Flipper's script
        p, said = self.board_up("--firmware", "marauder")
        if not check("devboard --firmware marauder", p and "ESP32 Marauder"
                     in said, said):
            return
        check("the window draws it, with Marauder", self.until(
            lambda: self.info().get("firmware") == "marauder", 5), self.info())
        self.wait(self.fe("push", ROOT / "examples/devboard/marauder.js",
                          "/ext"))
        js = self.fe("cli", "js /ext/marauder.js")
        check("the script's scan shows on the board", self.until(
            lambda: self.info().get("mode") == "scanall", 60), self.info())
        self.spin(2.5)
        self.shot("3-marauder-scanning")
        code, text = self.wait(js, 120)
        aps = len(fe_devboard.DEFAULT_SCENE["aps"])
        check("then it stops, with the access points it found",
              self.until(lambda: self.info().get("mode") == ""
                         and self.info().get("aps", 0) >= aps - 1, 10),
              f"{self.info()} {text[-400:]}")
        self.spin(0.5)
        self.shot("4-marauder-found")
        p.terminate()
        self.wait(p, 20)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("firmware")
    ap.add_argument("--timeout", type=float, default=90)
    ap.add_argument("--out", help="keep the pictures here")
    args = ap.parse_args()
    check_limit.arm(1200)
    name = f"dbg-{os.getpid()}"
    check_limit.device(name)
    devdir = ROOT / "devices" / name
    out = pathlib.Path(args.out) if args.out else pathlib.Path(
        tempfile.mkdtemp(prefix="fe-devboard-gui-"))
    out.mkdir(parents=True, exist_ok=True)
    devdir.mkdir(parents=True, exist_ok=True)
    uart = devdir / "uart.log"
    os.environ["FLIPPEREMU_GUI_CONFIG"] = str(out / "gui.ini")
    fe_gui.run = Driver(name, out, args.timeout)
    loader = importlib.machinery.SourceFileLoader(
        "flipperemu_launcher", str(ROOT / "flipperemu"))
    launcher = importlib.util.module_from_spec(
        importlib.util.spec_from_loader(loader.name, loader))
    sys.modules[loader.name] = launcher
    loader.exec_module(launcher)
    sys.argv = ["flipperemu", "-d", name, "run", "-f", args.firmware,
                "--audio", "none", "--serial", f"file:{uart}"]
    try:
        launcher.main()
    finally:
        shutil.rmtree(devdir, ignore_errors=True)
    print(f"  pictures: {out}")
    return 0 if results and all(ok for _, ok in results) else 1


if __name__ == "__main__":
    sys.exit(main())
