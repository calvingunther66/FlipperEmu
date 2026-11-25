"""The Video Game Module, drawn on the window's Flipper, and its HDMI picture.

`./flipperemu vgm` says what the module does in the device's module.json
(module "vgm", its "state" and its "hdmi" picture).  draw() puts the module
where a real one is: on the back of the Flipper, plugged into the GPIO header,
its top edge standing above the Flipper's with the HDMI and USB-C ports in
it, an HDMI cable in its port while it has a picture to send.  Its light says
what it does.  HdmiDock shows the picture that goes out on HDMI.

The drawing is in the units of fe_gui_view (one LCD pixel) and in the theme's
colours, as the Flipper's body is.

Copyright (c) 2026, Kara Zajac
SPDX-License-Identifier: BSD-3-Clause
"""
import os

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainterPath, QPen

from fe_gui_view import BODY, BX, BY, GPIO_PINS, faded, mix

HEIGHT = 26.0               # how far it stands above the Flipper's top edge
SHELL = QRectF(BX + 64, BY - HEIGHT, 266, HEIGHT + 14)  # (its lower part
#                             is behind the Flipper)
HDMI = QRectF(BX + 150, BY - HEIGHT - 0.2, 20, 4.2)     # the ports, in its
USB_C = QRectF(BX + 236, BY - HEIGHT - 0.2, 11.5, 3.6)  # top edge
LIGHT = QPointF(BX + 300, BY - HEIGHT + 9.5)
LABEL = QRectF(BX + 186, BY - HEIGHT + 5, 104, 9)

# what it does -> its light, and what the pointer over it says
STATES = {
    "calling": ("#f9b234", "calling the Flipper (Settings > Expansion "
                           "Modules > Listen UART: pins 13 and 14)"),
    "linked": ("#4a86e8", "linked at 2 Mbaud, an RPC session open"),
    "splash": ("#4a86e8", "its picture is on the Flipper's screen until a "
                          "button is released there"),
    "streaming": ("#40c057", "the Flipper's screen on HDMI"),
    "reset": ("#e03131", "in reset: the Flipper holds RUN (pin 15) low"),
    "boot": ("#ae3ec9", "in its UF2 boot loader: the Flipper holds "
                        "USB_BOOT (pin 16) low"),
}
SENDS = ("linked", "splash", "streaming")       # its HDMI cable is in


def tip(info):
    state = (info or {}).get("state")
    if state in STATES:
        return f"The Video Game Module: {STATES[state][1]}. Click for its menu"
    return ("The Video Game Module (its IMU on the header's SPI bus); nothing "
            "runs on its RP2040 (./flipperemu vgm). Click for its menu")


# where a click means the module: what shows of it, and its cable
AREA = QRectF(SHELL.x(), SHELL.y() - 10, SHELL.width(), HEIGHT + 10 - 0.5)
ROOM = HEIGHT + 34      # how far above the body's box it and its cable reach


def shell_path():
    path = QPainterPath()
    path.addRoundedRect(SHELL, 6.5, 6.5)
    return path


def draw(p, t, info, hover):
    """On the back of the Flipper: only what is above its body shows (the
    view draws modules after the body, so the body is cut out of it)"""
    state = (info or {}).get("state")
    p.save()
    room = QPainterPath()
    room.addRect(QRectF(-100, -200, 1000, 400))
    p.setClipPath(room.subtracted(BODY))
    p.setBrush(Qt.NoBrush)                      # the shadow it casts
    for width, alpha in ((6, 5), (3.5, 8), (1.6, 12)):
        p.setPen(QPen(QColor(0, 0, 0, alpha), width))
        p.drawPath(shell_path().translated(-0.4, 1.4))
    face = QtGui.QLinearGradient(0, SHELL.top(), 0, BY)
    body = mix(t["body"], "#ffffff", 0.12) if hover else t["body"]
    face.setColorAt(0.0, mix(body, t["light"], 0.55))
    face.setColorAt(0.25, body)
    face.setColorAt(1.0, mix(body, t["shade"], 0.45))
    p.setPen(QPen(t["edge"], 0.9))
    p.setBrush(face)
    p.drawPath(shell_path())
    p.setPen(Qt.NoPen)                          # its top face, in the light
    p.setBrush(mix(body, t["light"], 0.8))
    p.drawRoundedRect(QRectF(SHELL.x() + 5, SHELL.y() + 0.6,
                             SHELL.width() - 10, 2.4), 1.2, 1.2)
    # the ports in its top edge: HDMI (its shape: wider above) and USB-C
    p.setBrush(t["port"])
    hdmi = QtGui.QPolygonF([
        QPointF(HDMI.left(), HDMI.top()), QPointF(HDMI.right(), HDMI.top()),
        QPointF(HDMI.right(), HDMI.top() + 2.4),
        QPointF(HDMI.right() - 2.2, HDMI.bottom()),
        QPointF(HDMI.left() + 2.2, HDMI.bottom()),
        QPointF(HDMI.left(), HDMI.top() + 2.4)])
    p.drawPolygon(hdmi)
    p.drawRoundedRect(USB_C, 1.8, 1.8)
    p.setBrush(QColor("#c3c6cd"))               # their contacts
    p.drawRect(QRectF(HDMI.left() + 3, HDMI.top() + 1.2, HDMI.width() - 6, 1))
    p.drawRoundedRect(QRectF(USB_C.left() + 2.4, USB_C.top() + 1.3,
                             USB_C.width() - 4.8, 1.0), 0.5, 0.5)
    # its name, as the Flipper's is: in the accent colour, small
    font = QtGui.QFont()
    font.setPixelSize(5)
    font.setBold(True)
    font.setLetterSpacing(QtGui.QFont.AbsoluteSpacing, 0.6)
    p.setFont(font)
    p.setPen(t["text"])
    p.drawText(LABEL, Qt.AlignRight | Qt.AlignVCenter, "VIDEO GAME MODULE")
    p.setPen(Qt.NoPen)
    # its light: what it does
    colour = QColor(STATES[state][0]) if state in STATES else None
    if colour is not None:
        glow = QtGui.QRadialGradient(LIGHT, 7)
        glow.setColorAt(0, faded(colour, 150))
        glow.setColorAt(1, faded(colour, 0))
        p.setBrush(glow)
        p.drawEllipse(LIGHT, 7, 7)
        p.setBrush(mix("#ffffff", colour, 0.6))
    else:
        p.setBrush(mix(t["shade"], "#000000", 0.45))
    p.drawEllipse(LIGHT, 1.8, 1.8)
    if state in SENDS:                          # an HDMI cable, up and away
        plug = QRectF(HDMI.left() - 2.5, HDMI.top() - 9, HDMI.width() + 5, 9.5)
        wire = QPainterPath()
        wire.moveTo(plug.center().x(), plug.top())
        top = SHELL.top() - 30
        wire.cubicTo(QPointF(plug.center().x(), plug.top() - 8),
                     QPointF(plug.center().x() - 26, top + 16),
                     QPointF(plug.center().x() - 34, top))
        p.setPen(QPen(QColor("#2b2c31"), 4.4, Qt.SolidLine, Qt.RoundCap))
        p.setBrush(Qt.NoBrush)
        p.drawPath(wire)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#44464d") if not hover else QColor("#5a5d66"))
        p.drawRoundedRect(plug, 2.2, 2.2)
        p.setBrush(QColor("#c3c6cd"))
        p.drawRect(QRectF(HDMI.left() + 1, HDMI.top() - 0.6,
                          HDMI.width() - 2, 1.2))
    p.restore()


class HdmiDock(QtWidgets.QDockWidget):
    """The picture that goes out of the module's HDMI port, as `vgm` writes
    it (vgm-hdmi.png), at its 4:3, as large as the dock is"""

    def __init__(self, parent=None):
        super().__init__("HDMI", parent)
        self.setObjectName("hdmi")
        self.setFeatures(QtWidgets.QDockWidget.DockWidgetClosable
                         | QtWidgets.QDockWidget.DockWidgetMovable
                         | QtWidgets.QDockWidget.DockWidgetFloatable)
        self.picture = QtWidgets.QLabel()
        self.picture.setAlignment(Qt.AlignCenter)
        self.picture.setMinimumSize(320, 240)
        self.picture.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                                   QtWidgets.QSizePolicy.Ignored)
        self.picture.setStyleSheet("background: #000; color: #8c8fa1")
        self.setWidget(self.picture)
        self.path = None
        self.stamp = None
        self.image = None
        self.say("No Video Game Module on the header:\n"
                 "./flipperemu vgm, or GPIO > Video Game Module")

    def say(self, text):
        self.image = None
        self.picture.setPixmap(QtGui.QPixmap())
        self.picture.setText(text)

    def show_file(self, path):
        """Look at the picture again (cheap when it has not changed)"""
        if not path:
            if self.path:
                self.path, self.stamp = None, None
                self.say("No Video Game Module on the header:\n"
                         "./flipperemu vgm, or GPIO > Video Game Module")
            return
        try:
            stamp = os.stat(path).st_mtime_ns
        except OSError:
            if self.path != path:
                self.path, self.stamp = path, None
                self.say("The module has sent nothing to HDMI yet")
            return
        if (path, stamp) == (self.path, self.stamp):
            return
        image = QtGui.QImage(path)
        if image.isNull():          # (half written: the next look has it)
            return
        self.path, self.stamp, self.image = path, stamp, image
        self.picture.setText("")
        self.fit()

    def fit(self):
        if self.image is None:
            return
        self.picture.setPixmap(QtGui.QPixmap.fromImage(self.image.scaled(
            self.picture.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.fit()
