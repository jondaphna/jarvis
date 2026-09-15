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
import os
import subprocess
import sys
import urllib.request
import webbrowser
from pathlib import Path

from PyQt6.QtCore import QPoint, Qt, QTimer
from PyQt6.QtGui import QAction, QColor, QCursor, QPainter, QPen
from PyQt6.QtWidgets import QApplication, QMenu, QWidget

from chrome_finder import find_chrome, preferred_profile

HERE = Path(__file__).resolve().parent
WEB_URL = "http://localhost:3000"

DIAMETER = 86
MARGIN = 10                      # room for the glow, so it isn't clipped
CANVAS = DIAMETER + MARGIN * 2

#: The close button, tucked into the top-right corner. Small and set apart
#: from the middle so a drag never lands on it by accident.
CLOSE_CENTRE = (CANVAS - 13, 13)
CLOSE_RADIUS = 10

ACCENT = QColor(0, 212, 255)
DIM = QColor(90, 100, 125)
WAKING = QColor(255, 186, 72)


def browser_profile() -> str:
    """The Chrome profile to open Jarvis in - yours, with your logins."""
    configured = ""
    try:
        sys.path.insert(0, str(HERE))
        from jarvis.config import Settings

        configured = str(Settings.load().get("browser.profile", "") or "")
    except Exception:
        pass
    return preferred_profile(configured)


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
        self._opening = False
        self._chrome: subprocess.Popen | None = None
        self._hover_close = False

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool                  # keeps it out of the taskbar
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setToolTip("Jarvis - click to open, drag to move, "
                        "X to close everything")
        self.setMouseTracking(True)      # so the close button can light up
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

        # Starting the orb starts Jarvis. The point of the orb is to be the
        # only thing you have to launch: by the time the circle is on screen,
        # the microphone should already be live, so "hey Jarvis" works without
        # clicking anything first.
        if not self._online:
            QTimer.singleShot(400, self.open_jarvis)

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
        # Amber while things are starting up, so a cold start looks like
        # progress rather than a click that did nothing.
        colour = WAKING if self._opening else (ACCENT if self._online else DIM)
        lively = self._online or self._opening
        pulse = (math.sin(self._phase * (3 if self._opening else 1)) + 1) / 2
        radius = DIAMETER / 2 - 8 + pulse * 2

        # Glow. Drawn as stacked translucent rings rather than a gradient so it
        # stays crisp on a high-DPI screen.
        # The step is chosen so the widest ring still fits inside the widget.
        # Let it overflow and the corners clip square, which reads as a faint
        # dark box round the orb rather than a glow - the exact thing that made
        # the first version of this look broken.
        for i in range(7, 0, -1):
            ring = QColor(colour)
            ring.setAlphaF(0.045 * (1 if lively else 0.5))
            painter.setBrush(ring)
            painter.setPen(Qt.PenStyle.NoPen)
            grow = min(radius + i * 2.0, CANVAS / 2 - 1)
            painter.drawEllipse(QPoint(int(centre), int(centre)),
                                int(grow), int(grow))

        # The ring itself.
        #
        # Built from scratch rather than fetched with painter.pen(): that
        # returns a copy of the *current* pen, which the glow loop above left
        # as NoPen. Setting a colour and width on a NoPen pen is perfectly
        # legal and draws absolutely nothing.
        pen_colour = QColor(colour)
        pen_colour.setAlphaF(0.85)
        pen = QPen(pen_colour)
        pen.setWidthF(2.2)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QPoint(int(centre), int(centre)),
                            int(radius), int(radius))

        # The core.
        core = QColor(colour)
        core.setAlphaF(0.30 + pulse * 0.28 if lively else 0.16)
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
        painter.setPen(QColor(230, 240, 255) if lively
                       else QColor(150, 160, 180))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "J")

        self._paint_close(painter)

    def _paint_close(self, painter: QPainter) -> None:
        """The little X that shuts everything down."""
        cx, cy = CLOSE_CENTRE
        backing = QColor(18, 20, 34)
        backing.setAlphaF(0.92 if self._hover_close else 0.55)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(backing)
        painter.drawEllipse(QPoint(cx, cy), CLOSE_RADIUS, CLOSE_RADIUS)

        stroke = QColor(255, 120, 120) if self._hover_close else QColor(190, 200, 215)
        pen = QPen(stroke)
        pen.setWidthF(1.9)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        arm = 3.4
        painter.drawLine(int(cx - arm), int(cy - arm), int(cx + arm), int(cy + arm))
        painter.drawLine(int(cx + arm), int(cy - arm), int(cx - arm), int(cy + arm))

    # ------------------------------------------------------------------ #
    # Interaction
    # ------------------------------------------------------------------ #

    def _on_close_button(self, position) -> bool:
        cx, cy = CLOSE_CENTRE
        dx = position.x() - cx
        dy = position.y() - cy
        return (dx * dx + dy * dy) <= (CLOSE_RADIUS + 2) ** 2

    def mousePressEvent(self, event) -> None:     # noqa: N802
        if (event.button() == Qt.MouseButton.LeftButton
                and self._on_close_button(event.position())):
            # Checked before the drag starts, or the press would be swallowed
            # by the move handler and the button would never fire.
            self.shutdown_everything()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_from = (event.globalPosition().toPoint()
                               - self.frameGeometry().topLeft())
            self._dragged = False

    def mouseMoveEvent(self, event) -> None:      # noqa: N802
        hovering = self._on_close_button(event.position())
        if hovering != self._hover_close:
            self._hover_close = hovering
            self.update()
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
        hider = QAction("Hide the orb (leave Jarvis running)", menu)
        hider.triggered.connect(QApplication.instance().quit)
        menu.addAction(hider)

        closer = QAction("Close Jarvis completely", menu)
        closer.triggered.connect(self.shutdown_everything)
        menu.addAction(closer)

        menu.exec(event.globalPos())

    # ------------------------------------------------------------------ #
    # Doing things
    # ------------------------------------------------------------------ #

    def open_jarvis(self) -> None:
        """Start whatever isn't running, then open the window and connect."""
        if not self._online:
            self.start_everything()
            # The web server takes a few seconds to compile on a cold start.
            # Opening the browser first shows an error page, so wait for it.
            self._opening = True
            self.update()
            QTimer.singleShot(1500, self._open_when_ready)
            return
        self._open_window()

    def _open_when_ready(self, attempt: int = 0) -> None:
        self._check_online()
        if self._online:
            self._opening = False
            self.update()
            self._open_window()
            return
        if attempt > 40:                      # about a minute, then give up
            self._opening = False
            self.update()
            return
        QTimer.singleShot(1500, lambda: self._open_when_ready(attempt + 1))

    def _open_window(self) -> None:
        """Open Jarvis in Chrome, connected and listening.

        Chrome specifically, not whatever Windows calls the default browser.
        On a stock machine that default is Edge, and the voice client does not
        work there - so "it opened and nothing happened" was really "it opened
        in the wrong browser".

        --app gives a clean frameless window instead of a tab in whatever you
        already had open, and autostart tells the page to connect by itself so
        you can just talk.
        """
        url = f"{WEB_URL}/?autostart=1"
        chrome = find_chrome()
        if chrome:
            try:
                # Your real profile, not a throwaway one. A private
                # --user-data-dir keeps the microphone permission tidy and is
                # signed into nothing, so every site it opens is a stranger's.
                self._chrome = subprocess.Popen(
                    [chrome, f"--profile-directory={browser_profile()}",
                     f"--app={url}", "--new-window"],
                    cwd=str(HERE))
                return
            except Exception:
                pass
        # No Chrome anywhere: better the default browser than nothing, with a
        # plain URL since --app was the only reason to special-case it.
        webbrowser.open(url)

    def start_everything(self) -> None:
        self._launch("butler-agent.bat", "Jarvis agent")
        self._launch("butler-web.bat", "Jarvis web")

    def _launch(self, script: str, title: str = "") -> None:
        """Start one of the .bat files in its own window.

        The window gets a title beginning "Jarvis " on purpose: that is how the
        close button finds these again later. `start` treats its first quoted
        argument as the title, which is why the empty string used to be there.
        """
        path = HERE / script
        if not path.exists():
            return
        try:
            if sys.platform == "win32":
                subprocess.Popen(
                    ["cmd", "/c", "start", title or "Jarvis", str(path)],
                    cwd=str(HERE), shell=False)
            else:
                subprocess.Popen(["bash", str(path)], cwd=str(HERE))
        except Exception:
            pass

    def shutdown_everything(self) -> None:
        """Close Jarvis completely: the agent, the web app, the window, the orb.

        No confirmation, because the button is small, deliberately out of the
        way of the drag area, and nothing here loses work - the conversation is
        already written to the database as it happens.
        """
        if self._chrome is not None:
            self._kill_tree(self._chrome.pid)
            self._chrome = None

        if sys.platform == "win32":
            # Kills the console windows and everything underneath them - the
            # uv launcher, python, node. Killing only the console would leave
            # the agent running with nothing on screen to stop it.
            for pattern in ("Jarvis agent", "Jarvis web", "Jarvis control"):
                try:
                    subprocess.run(
                        ["taskkill", "/FI", f"WINDOWTITLE eq {pattern}*",
                         "/T", "/F"],
                        capture_output=True, timeout=10, check=False)
                except Exception:
                    pass

        QApplication.instance().quit()

    @staticmethod
    def _kill_tree(pid: int) -> None:
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=10, check=False)
            else:
                os.kill(pid, 15)
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
