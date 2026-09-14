"""The pulsing circle. Idle breathing, listening ripples, speaking bloom.

Purely decorative, but it does one useful job: you can tell at a glance
whether JARVIS is listening, thinking, or talking.
"""

from __future__ import annotations

import math

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSlot
from PyQt6.QtGui import QColor, QPainter, QPen, QRadialGradient
from PyQt6.QtWidgets import QWidget

from .theme import ACCENT, BG

IDLE = "idle"
LISTENING = "listening"
THINKING = "thinking"
SPEAKING = "speaking"


class Orb(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Kept small so the floating orb can be 92px; the main window
        # gives it room via its layout instead.
        self.setMinimumSize(64, 64)
        self._phase = 0.0
        self._state = IDLE
        self._level = 0.0          # live mic/speech level, 0-1
        self._target_level = 0.0

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(33)      # ~30fps is plenty and stays cheap

    # -- state ---------------------------------------------------------- #

    def set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            self.update()

    def state(self) -> str:
        return self._state

    def set_level(self, level: float) -> None:
        self._target_level = max(0.0, min(1.0, level))

    @pyqtSlot()
    def _tick(self) -> None:
        speed = {IDLE: 0.03, LISTENING: 0.08, THINKING: 0.13, SPEAKING: 0.1}
        self._phase = (self._phase + speed.get(self._state, 0.03)) % (2 * math.pi)
        # Ease towards the target so the orb never jitters.
        self._level += (self._target_level - self._level) * 0.25
        self.update()

    # -- painting ------------------------------------------------------- #

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(BG))

        centre = QPointF(self.width() / 2, self.height() / 2)
        base = min(self.width(), self.height()) * 0.28
        breathe = math.sin(self._phase) * (base * 0.06)
        radius = base + breathe + (self._level * base * 0.3)

        accent = QColor(ACCENT)

        # Outer rings - more of them, and brighter, the busier JARVIS is.
        rings = {IDLE: 1, LISTENING: 3, THINKING: 2, SPEAKING: 3}.get(self._state, 1)
        for index in range(rings):
            offset = (self._phase / (2 * math.pi) + index / max(rings, 1)) % 1.0
            ring_radius = radius * (1.15 + offset * 0.85)
            alpha = int(90 * (1 - offset) * (0.4 + self._level * 0.6))
            if alpha <= 2:
                continue
            pen = QPen(QColor(accent.red(), accent.green(), accent.blue(), alpha))
            pen.setWidthF(1.6)
            painter.setPen(pen)
            painter.drawEllipse(centre, ring_radius, ring_radius)

        # Core glow
        gradient = QRadialGradient(centre, radius * 1.5)
        gradient.setColorAt(0.0, QColor(accent.red(), accent.green(), accent.blue(), 210))
        gradient.setColorAt(0.45, QColor(accent.red(), accent.green(), accent.blue(), 80))
        gradient.setColorAt(1.0, QColor(accent.red(), accent.green(), accent.blue(), 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gradient)
        painter.drawEllipse(centre, radius * 1.5, radius * 1.5)

        # Solid centre
        painter.setBrush(QColor(accent.red(), accent.green(), accent.blue(), 235))
        painter.drawEllipse(centre, radius * 0.42, radius * 0.42)

        # Thinking gets an orbiting arc so it reads as "working", not "stuck".
        if self._state == THINKING:
            pen = QPen(accent)
            pen.setWidthF(2.4)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            box = QRectF(centre.x() - radius * 1.2, centre.y() - radius * 1.2,
                         radius * 2.4, radius * 2.4)
            start = int(math.degrees(self._phase) * 16)
            painter.drawArc(box, start, 80 * 16)

        painter.end()
