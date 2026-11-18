"""Shared vector icons, rendered locally at the display's pixel density."""
from functools import lru_cache
from pathlib import Path
from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

ROOT = Path(__file__).parent / "resources/icons"


@lru_cache(maxsize=256)
def icon(name, color="#ececec", size=20, dpr=1.0):
    path = ROOT / (name + ".svg")
    raw = path.read_bytes().replace(b"currentColor", color.encode("ascii"))
    renderer = QSvgRenderer(QByteArray(raw))
    if not renderer.isValid():
        raise ValueError("Invalid icon: " + name)
    pixels = max(1, round(size*dpr))
    pixmap = QPixmap(pixels, pixels)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    pixmap.setDevicePixelRatio(dpr)
    return QIcon(pixmap)


def decorate(button, name, *, icon_only=False, label=None):
    button.setProperty("uiIcon", name)
    text = label or button.text()
    button.setAccessibleName(text)
    if icon_only:
        button.setText("")
        button.setObjectName("iconButton")
        button.setFixedSize(38, 38)
    if not button.toolTip():
        button.setToolTip(text)
    button.setIconSize(QSize(18, 18))
    button.setIcon(icon(name, dpr=button.devicePixelRatioF()))
    return button


def refresh_icons(window, palette):
    from PySide6.QtWidgets import QAbstractButton
    for button in window.findChildren(QAbstractButton):
        name = button.property("uiIcon")
        if name:
            color = palette["text"]
            if button.objectName() == "primary":
                color = palette["side"]
            button.setIcon(icon(name, color, 18, button.devicePixelRatioF()))
