"""The always-there orb: a small circle that sits on top of everything.

Close the main window and this stays behind - draggable, always on top, and
listening for the wake word. Say "jarvis" and it swells, shows what it heard,
and answers. Click it to talk without the wake word; double-click to bring the
full window back.

It's a frameless translucent tool window rather than a normal one so it doesn't
take focus from whatever you're working in, and never shows up in the taskbar
or the alt-tab list.
"""

from __future__ import annotations

from PyQt6.QtCore import QPoint, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QCursor, QFont, QPainter, QPainterPath
from PyQt6.QtWidgets import QApplication, QMenu, QWidget

from ..core import events
from ..core.events import log
from .orb import IDLE, LISTENING, SPEAKING, THINKING, Orb
from .theme import ACCENT, CARD, TEXT

IDLE_SIZE = 92
ACTIVE_SIZE = 148
CAPTION_HEIGHT = 46


class FloatingOrb(QWidget):
    """A small always-on-top JARVIS presence."""

    clicked = pyqtSignal()
    open_requested = pyqtSignal()
    quit_requested = pyqtSignal()

    def __init__(self, settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self._caption = ""
        self._drag_from: QPoint | None = None
        self._dragged = False

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool                     # keeps it out of the taskbar
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setToolTip("JARVIS — click to talk, double-click to open, drag to move")

        self.orb = Orb(self)
        self.orb.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        self._caption_timer = QTimer(self)
        self._caption_timer.setSingleShot(True)
        self._caption_timer.timeout.connect(self._clear_caption)

        self._resize_to(IDLE_SIZE)
        self._restore_position()

    # ------------------------------------------------------------------ #
    # Geometry
    # ------------------------------------------------------------------ #

    def _resize_to(self, diameter: int) -> None:
        self._diameter = diameter
        self.setFixedSize(diameter, diameter + CAPTION_HEIGHT)
        # Pin the child explicitly: a minimum size on the Orb would otherwise
        # let it overflow this window and paint over the caption.
        self.orb.setFixedSize(diameter, diameter)
        self.orb.move(0, 0)

    def _restore_position(self) -> None:
        screen = QApplication.primaryScreen()
        area = screen.availableGeometry() if screen else None

        x = self.settings.get("ui.orb_x")
        y = self.settings.get("ui.orb_y")
        if x is None or y is None:
            if area is None:
                x, y = 100, 100
            else:
                x = area.right() - self.width() - 28
                y = area.bottom() - self.height() - 28
        # Don't restore onto a monitor that's since been unplugged.
        if area is not None:
            x = max(area.left(), min(int(x), area.right() - self.width()))
            y = max(area.top(), min(int(y), area.bottom() - self.height()))
        self.move(int(x), int(y))

    def _remember_position(self) -> None:
        self.settings.set("ui.orb_x", self.x())
        self.settings.set("ui.orb_y", self.y())
        try:
            self.settings.save()
        except Exception:
            log.debug("couldn't save the orb position", exc_info=True)

    # ------------------------------------------------------------------ #
    # State
    # ------------------------------------------------------------------ #

    def set_state(self, state: str) -> None:
        self.orb.set_state(state)
        wanted = IDLE_SIZE if state == IDLE else ACTIVE_SIZE
        if wanted != self._diameter:
            centre = self.geometry().center()
            self._resize_to(wanted)
            # Grow from the middle, so it doesn't crawl across the screen.
            self.move(centre.x() - self.width() // 2,
                      centre.y() - self.height() // 2 + CAPTION_HEIGHT // 2)

    def set_level(self, level: float) -> None:
        self.orb.set_level(level)

    def show_caption(self, text: str, seconds: float = 6.0) -> None:
        """Show a line under the orb - what was heard, or what JARVIS said."""
        self._caption = (text or "").strip()
        self.update()
        self._caption_timer.start(int(seconds * 1000))

    def _clear_caption(self) -> None:
        self._caption = ""
        self.update()

    def handle_event(self, kind: str, message: str) -> None:
        """Mirror the app's event stream onto the orb."""
        if kind == events.LISTENING:
            self.set_state(LISTENING)
        elif kind == events.THINKING:
            self.set_state(THINKING)
        elif kind == events.SPEAKING:
            self.set_state(SPEAKING)
            self.show_caption(message, seconds=8)
        elif kind == events.TRANSCRIPT:
            self.set_state(THINKING)
            self.show_caption(f"“{message}”", seconds=5)
        elif kind == events.REPLY:
            self.show_caption(message, seconds=9)
            QTimer.singleShot(1200, lambda: self.set_state(IDLE))
        elif kind == events.APPROVAL_NEEDED:
            self.show_caption("Needs your approval", seconds=12)
        elif kind == events.ERROR:
            self.show_caption(message, seconds=8)
            self.set_state(IDLE)

    # ------------------------------------------------------------------ #
    # Painting
    # ------------------------------------------------------------------ #

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if not self._caption:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        font = QFont()
        font.setPointSize(9)
        painter.setFont(font)

        metrics = painter.fontMetrics()
        text = metrics.elidedText(self._caption, Qt.TextElideMode.ElideRight,
                                  self.width() - 16)
        width = min(self.width() - 4, metrics.horizontalAdvance(text) + 20)
        height = metrics.height() + 12
        left = (self.width() - width) / 2
        top = self._diameter + 2

        bubble = QPainterPath()
        bubble.addRoundedRect(left, top, width, height, 9, 9)
        painter.fillPath(bubble, QColor(CARD))
        painter.setPen(QColor(ACCENT).darker(160))
        painter.drawPath(bubble)

        painter.setPen(QColor(TEXT))
        painter.drawText(int(left), int(top), int(width), int(height),
                         int(Qt.AlignmentFlag.AlignCenter), text)
        painter.end()

    # ------------------------------------------------------------------ #
    # Mouse
    # ------------------------------------------------------------------ #

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_from = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._dragged = False
            event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_from is None:
            return
        if event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_from)
            self._dragged = True
            event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self._dragged:
            self._remember_position()
        else:
            # A click, not a drag: start talking.
            self.clicked.emit()
        self._drag_from = None
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self.open_requested.emit()
        event.accept()

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        menu = QMenu(self)

        talk = QAction("Talk to JARVIS", self)
        talk.triggered.connect(self.clicked.emit)
        menu.addAction(talk)

        open_window = QAction("Open the window", self)
        open_window.triggered.connect(self.open_requested.emit)
        menu.addAction(open_window)

        menu.addSeparator()
        hide = QAction("Hide the orb", self)
        hide.triggered.connect(self.hide)
        menu.addAction(hide)

        quit_action = QAction("Quit JARVIS", self)
        quit_action.triggered.connect(self.quit_requested.emit)
        menu.addAction(quit_action)

        menu.exec(event.globalPos())
