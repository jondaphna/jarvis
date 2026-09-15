"""The little circle that sits on top of everything.

Jarvis lives in a browser tab, which is fine until the tab is behind six other
windows and you just want to say something. This is the shortcut: a small
always-on-top circle you can drag anywhere on screen. Click it and Jarvis opens,
ready to talk.

It is a frameless translucent *tool* window, which on Windows means three
things that all matter: it never takes focus from what you're working in, it
never appears in the taskbar, and it never shows up in alt-tab. It sits there
quietly until you want it.

The ring around it is a status light. Filled and bright means the web app is
up and Jarvis is ready. Hollow and dim means nothing is running - right-click
and start it.

Run it with:  butler-orb.bat
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.request
import webbrowser
from pathlib import Path

from PyQt6.QtCore import QPoint, Qt, QTimer
from PyQt6.QtGui import QAction, QColor, QCursor, QPainter
from PyQt6.QtWidgets import QApplication, QMenu, QWidget

HERE = Path(__file__).resolve().parent
WEB_URL = "http://localhost:3000"

DIAMETER = 86
MARGIN = 10                      # room for the glow, so it isn't clipped
CANVAS = DIAMETER + MARGIN * 2

ACCENT = QColor(0, 212, 255)
DIM = QColor(90, 100, 125)


def _state_file() -> Path:
    """Where the orb remembers where you put it."""
    try:
        sys.path.insert(0, str(HERE))
        from jarvis import paths

        paths.ensure_dirs()
        return paths.ROOT / "orb.json"
    except Exception:
        return HERE / ".orb.json"


class Orb(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._drag_from: QPoint | None = None
        self._dragged = False
        self._phase = 0.0
        self._online = False

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool                  # keeps it out of the taskbar
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setToolTip("Jarvis - click to open, drag to move, right-click for more")
        self.setFixedSize(CANVAS, CANVAS)

        self._restore_position()

        # The gentle breathing that says it's alive rather than a screenshot.
        self._beat = QTimer(self)
        self._beat.timeout.connect(self._tick)
        self._beat.start(40)

        # Is the web app up? Cheap enough to ask every few seconds.
        self._probe = QTimer(self)
        self._probe.timeout.connect(self._check_online)
        self._probe.start(4000)
        self._check_online()

    # ------------------------------------------------------------------ #
    # Where it sits
    # ------------------------------------------------------------------ #

    def _restore_position(self) -> None:
        screen = QApplication.primaryScreen().availableGeometry()
        default = QPoint(screen.right() - CANVAS - 28,
                         screen.bottom() - CANVAS - 60)
        try:
            saved = json.loads(_state_file().read_text(encoding="utf-8"))
            point = QPoint(int(saved["x"]), int(saved["y"]))
            # A monitor that has since been unplugged would strand it offscreen.
            if screen.contains(QPoint(point.x() + CANVAS // 2,
                                      point.y() + CANVAS // 2)):
                self.move(point)
                return
        except Exception:
            pass
        self.move(default)

    def _remember_position(self) -> None:
        try:
            _state_file().write_text(
                json.dumps({"x": self.x(), "y": self.y()}), encoding="utf-8")
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Is anything running?
    # ------------------------------------------------------------------ #

    def _check_online(self) -> None:
        was = self._online
        try:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}))       # never via a proxy
            with opener.open(WEB_URL, timeout=1.5):
                self._online = True
        except Exception:
            self._online = False
        if was != self._online:
            self.update()

    def _tick(self) -> None:
        self._phase += 0.05
        self.update()

    # ------------------------------------------------------------------ #
    # Drawing
    # ------------------------------------------------------------------ #

    def paintEvent(self, event) -> None:      # noqa: N802 - Qt naming
        import math

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        centre = CANVAS / 2
        colour = ACCENT if self._online else DIM
        pulse = (math.sin(self._phase) + 1) / 2          # 0..1
        radius = DIAMETER / 2 - 8 + pulse * 2

        # Glow. Drawn as stacked translucent rings rather than a gradient so it
        # stays crisp on a high-DPI screen.
        # The step is chosen so the widest ring still fits inside the widget.
        # Let it overflow and the corners clip square, which reads as a faint
        # dark box round the orb rather than a glow - the exact thing that made
        # the first version of this look broken.
        for i in range(7, 0, -1):
            ring = QColor(colour)
            ring.setAlphaF(0.045 * (1 if self._online else 0.5))
            painter.setBrush(ring)
            painter.setPen(Qt.PenStyle.NoPen)
            grow = min(radius + i * 2.0, CANVAS / 2 - 1)
            painter.drawEllipse(QPoint(int(centre), int(centre)),
                                int(grow), int(grow))

        # The ring itself.
        pen_colour = QColor(colour)
        pen_colour.setAlphaF(0.85)
        pen = painter.pen()
        pen.setColor(pen_colour)
        pen.setWidthF(2.2)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QPoint(int(centre), int(centre)),
                            int(radius), int(radius))

        # The core.
        core = QColor(colour)
        core.setAlphaF(0.30 + pulse * 0.28 if self._online else 0.16)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(core)
        painter.drawEllipse(QPoint(int(centre), int(centre)),
                            int(radius * 0.52), int(radius * 0.52))

        # A dark disc behind the letter so it reads on any wallpaper.
        backing = QColor(10, 12, 24)
        backing.setAlphaF(0.55)
        painter.setBrush(backing)
        painter.drawEllipse(QPoint(int(centre), int(centre)),
                            int(radius * 0.40), int(radius * 0.40))

        font = painter.font()
        font.setPointSizeF(DIAMETER * 0.26)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(230, 240, 255) if self._online
                       else QColor(150, 160, 180))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "J")

    # ------------------------------------------------------------------ #
    # Interaction
    # ------------------------------------------------------------------ #

    def mousePressEvent(self, event) -> None:     # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_from = (event.globalPosition().toPoint()
                               - self.frameGeometry().topLeft())
            self._dragged = False

    def mouseMoveEvent(self, event) -> None:      # noqa: N802
        if self._drag_from is None:
            return
        self.move(event.globalPosition().toPoint() - self._drag_from)
        self._dragged = True

    def mouseReleaseEvent(self, event) -> None:   # noqa: N802
        if self._drag_from is not None and self._dragged:
            self._remember_position()
        elif self._drag_from is not None:
            # A click, not a drag.
            self.open_jarvis()
        self._drag_from = None

    def contextMenuEvent(self, event) -> None:    # noqa: N802
        menu = QMenu()

        opener = QAction("Open Jarvis", menu)
        opener.triggered.connect(self.open_jarvis)
        menu.addAction(opener)

        if not self._online:
            starter = QAction("Start Jarvis (agent + web)", menu)
            starter.triggered.connect(self.start_everything)
            menu.addAction(starter)

        talk = QAction("Talk in a terminal", menu)
        talk.triggered.connect(lambda: self._launch("butler-talk.bat"))
        menu.addAction(talk)

        menu.addSeparator()
        quitter = QAction("Hide the orb", menu)
        quitter.triggered.connect(QApplication.instance().quit)
        menu.addAction(quitter)

        menu.exec(event.globalPos())

    # ------------------------------------------------------------------ #
    # Doing things
    # ------------------------------------------------------------------ #

    def open_jarvis(self) -> None:
        if not self._online:
            self.start_everything()
        webbrowser.open(WEB_URL)

    def start_everything(self) -> None:
        self._launch("butler-agent.bat")
        self._launch("butler-web.bat")

    def _launch(self, script: str) -> None:
        """Start one of the .bat files in its own window."""
        path = HERE / script
        if not path.exists():
            return
        try:
            if sys.platform == "win32":
                subprocess.Popen(["cmd", "/c", "start", "", str(path)],
                                 cwd=str(HERE), shell=False)
            else:
                subprocess.Popen(["bash", str(path)], cwd=str(HERE))
        except Exception:
            pass


def main() -> int:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)
    orb = Orb()
    orb.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
