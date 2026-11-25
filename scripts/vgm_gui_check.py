#!/usr/bin/env python3
# Copyright (c) 2026, the FlipperEmu contributors
# SPDX-License-Identifier: BSD-3-Clause
"""Off-screen check of the Video Game Module in the window (fe_gui.py).

    scripts/vgm_gui_check.py FIRMWARE --out shots

Boots a throwaway device with `flipperemu run --spi imu@7`, which opens the
window off-screen, as scripts/gui_check.py does.  GPIO > Plug in the Video
Game Module starts `flipperemu vgm`: the window draws the module on the back
of the Flipper, its top edge above the header (the view grows by it), and
says so in its console.  Its picture is on the Flipper's screen and its light
is blue; a button released ends it, and the screen goes to HDMI: the light
is green, an HDMI cable is in its port, and the HDMI dock shows the picture.
The firmware's RUN line held low turns the light red, and released, the
module starts again with its picture.  Its menu's Pull it out ends `vgm`
and the window shrinks back.  Pictures of each step go to --out.  Exit
status 0 = pass.
"""
import argparse
import importlib.machinery
import importlib.util
import os
import pathlib
import shutil
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
import fe_gui  # noqa: E402
import fe_gui_vgm  # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append((label, bool(ok)))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}", flush=True)
    if detail and not ok:
        print("        " + str(detail).strip().replace("\n", "\n        "),
              flush=True)
    return ok


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
        self.win.grab().save(str(self.out / f"vgm-{tag}.png"))

    def fe(self, *args, timeout=60):
        """a command of the launcher, run to its end while the window runs:
        (exit status, output)"""
        p = subprocess.Popen([sys.executable, str(ROOT / "flipperemu"), "-d",
                              self.name, *map(str, args)], text=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT)
        if not self.until(lambda: p.poll() is not None, timeout):
            p.kill()
        return p.wait(), p.stdout.read()

    def state(self):
        return self.win.view.module_info.get("state")

    def module_point(self):
        r, v = fe_gui_vgm.AREA, self.win.view
        return QPoint(round(r.center().x() * v.k),
                      round((r.center().y() + v.top) * v.k))

    def console(self):
        return self.win.log.text.toPlainText()

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

        w.fill_gpio_menu()
        plug = next((a for a in w.menu_gpio.actions()
                     if a.text() == "Plug in the Video Game Module"), None)
        if not check("the GPIO menu can plug in the Video Game Module",
                     plug is not None and plug.isEnabled(),
                     [a.text() for a in w.menu_gpio.actions()]):
            return
        plug.trigger()
        check("the window draws it on the header",
              self.until(lambda: v.module == "vgm", 15), self.console())
        check("... and the view grows by it",
              self.until(lambda: v.height() > h0, 3), f"{h0} -> {v.height()}")
        check("... and its console says so",
              "Video Game Module is on the GPIO header" in self.console())
        check("... and a click on the GPIO pins still finds pin 13",
              v.pin_at(v.pin_point(13)) == 13)
        check("the module is under the pointer above the Flipper",
              v.zone_at(self.module_point()) == "module")

        check("its picture is on the Flipper's screen: the light is blue",
              self.until(lambda: self.state() == "splash", 20),
              f"{v.module_info}\n{self.console()[-1500:]}")
        self.spin(1.0)
        self.shot("1-splash")
        self.fe("press", "ok")
        check("a button released there: the screen goes to HDMI",
              self.until(lambda: self.state() == "streaming", 20),
              v.module_info)
        check("... the HDMI dock shows the picture", self.until(
            lambda: not w.hdmi.isHidden() and w.hdmi.image is not None, 10),
            f"hidden {w.hdmi.isHidden()}, {w.hdmi.picture.text()!r}")
        tip = v.tip("module")
        check("its tooltip says so", "on HDMI" in tip, tip)
        self.fe("press", "ok")              # the menu, on both screens
        self.spin(2.5)
        self.shot("2-streaming")
        try:
            shutil.copy(v.module_info["hdmi"], self.out / "vgm-hdmi.png")
        except (OSError, KeyError):
            pass

        self.fe("cli", "gpio mode PC1 1")
        self.fe("cli", "gpio set PC1 0")
        check("RUN held low: the light is red",
              self.until(lambda: self.state() == "reset", 15), v.module_info)
        self.spin(0.5)
        self.shot("3-reset")
        self.fe("cli", "gpio set PC1 1")
        check("RUN high again: its picture first, as at power-up",
              self.until(lambda: self.state() == "splash", 20), v.module_info)
        self.fe("press", "ok")
        check("... and HDMI again",
              self.until(lambda: self.state() == "streaming", 20),
              v.module_info)
        self.fe("cli", "gpio mode PC1 0")

        w.show_module_menu(QtCore.QPoint(0, 0))
        texts = [a.text() for a in w.module_menu.actions()]
        check("its menu shows the HDMI picture and has Pull it out",
              "Show its HDMI picture" in texts and "Pull it out" in texts,
              texts)
        w.module_menu.close()
        pull = next(a for a in w.module_menu.actions()
                    if a.text() == "Pull it out")
        pull.trigger()
        check("Pull it out: the window draws no module, as small as before",
              self.until(lambda: v.module is None and v.height() == h0, 10),
              f"{v.module} {v.height()}")
        check("... and its console says the module ended well",
              self.until(lambda: "Ended: the Video Game Module"
                         in self.console(), 10), self.console()[-1500:])
        self.spin(0.5)
        self.shot("4-out")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("firmware")
    ap.add_argument("--timeout", type=float, default=90)
    ap.add_argument("--out", help="keep the pictures here")
    args = ap.parse_args()
    check_limit.arm(900)
    name = f"vgmg-{os.getpid()}"
    check_limit.device(name)
    devdir = ROOT / "devices" / name
    out = pathlib.Path(args.out) if args.out else pathlib.Path(
        tempfile.mkdtemp(prefix="fe-vgm-gui-"))
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
                "--audio", "none", "--serial", f"file:{uart}",
                "--spi", "imu@7"]
    try:
        launcher.main()
    finally:
        shutil.rmtree(devdir, ignore_errors=True)
    print(f"  pictures: {out}")
    print(f"{sum(ok for _, ok in results)}/{len(results)} passed")
    return 0 if results and all(ok for _, ok in results) else 1


if __name__ == "__main__":
    sys.exit(main())
