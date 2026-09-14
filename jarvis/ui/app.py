"""The JARVIS desktop window."""

from __future__ import annotations

import sys
from typing import Any

from PyQt6.QtCore import Qt, QTimer, pyqtSlot
from PyQt6.QtGui import QAction, QIcon, QKeySequence, QPainter, QPixmap, QShortcut
from PyQt6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu,
    QMessageBox, QPushButton, QSystemTrayIcon, QTabWidget, QTextEdit,
    QVBoxLayout, QWidget,
)

from ..config import Config
from ..core import events
from ..core.events import log, setup_logging
from .bridge import AsyncRunner, EventRelay
from .floating_orb import FloatingOrb
from .orb import IDLE, LISTENING, SPEAKING, THINKING, Orb
from .panels import ActivityPanel, ApprovalsPanel, MissionsPanel, SettingsPanel, muted
from .theme import ACCENT, DANGER, MUTED, STYLESHEET, SUCCESS, WARNING


class MainWindow(QMainWindow):
    def __init__(self, app, runner: AsyncRunner) -> None:
        super().__init__()
        self.app = app
        self.runner = runner
        self.voice_active = False
        self._voice_future: Any = None

        self.setWindowTitle("JARVIS")
        self.resize(1080, 720)
        self.setWindowIcon(_make_icon())

        self._build()
        self._wire_events()
        self._install_approval_handler()

        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self._periodic_refresh)
        self.refresh_timer.start(15000)

    # ------------------------------------------------------------------ #
    # Layout
    # ------------------------------------------------------------------ #

    def _build(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # --- header ----------------------------------------------------- #
        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(20, 14, 20, 8)

        title = QLabel("◉ JARVIS")
        title.setObjectName("title")
        header_layout.addWidget(title)

        self.status_label = QLabel("")
        self.status_label.setObjectName("subtitle")
        header_layout.addWidget(self.status_label)
        header_layout.addStretch()

        self.approval_badge = QPushButton("")
        self.approval_badge.setObjectName("ghost")
        self.approval_badge.clicked.connect(lambda: self.tabs.setCurrentIndex(2))
        header_layout.addWidget(self.approval_badge)
        root.addWidget(header)

        # --- tabs -------------------------------------------------------- #
        self.tabs = QTabWidget()
        self.tabs.addTab(self._assistant_tab(), "Assistant")
        self.missions_panel = MissionsPanel(self.app, self.runner)
        self.missions_panel.run_requested.connect(self._run_mission)
        self.tabs.addTab(self.missions_panel, "Missions")
        self.approvals_panel = ApprovalsPanel(self.app)
        self.approvals_panel.changed.connect(self._refresh_badges)
        self.tabs.addTab(self.approvals_panel, "Approvals")
        self.activity_panel = ActivityPanel(self.app)
        self.tabs.addTab(self.activity_panel, "Activity")
        self.settings_panel = SettingsPanel(self.app)
        self.tabs.addTab(self.settings_panel, "Settings")
        root.addWidget(self.tabs, 1)

        self.statusBar().showMessage(self._status_line())
        self._refresh_badges()
        self._build_tray()
        self._build_floating_orb()

        QShortcut(QKeySequence("Ctrl+L"), self, activated=self._clear_conversation)
        QShortcut(QKeySequence("Ctrl+Space"), self, activated=self._toggle_voice)

    def _assistant_tab(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(18, 14, 18, 18)
        layout.setSpacing(18)

        # Left: the orb and the voice button.
        left = QVBoxLayout()
        self.orb = Orb()
        self.orb.setMinimumSize(190, 190)
        left.addWidget(self.orb, 1)

        self.voice_button = QPushButton("Hold a conversation")
        self.voice_button.setObjectName("primary")
        self.voice_button.clicked.connect(self._toggle_voice)
        left.addWidget(self.voice_button)

        self.voice_hint = muted("")
        self.voice_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        left.addWidget(self.voice_hint)
        left_widget = QWidget()
        left_widget.setLayout(left)
        left_widget.setFixedWidth(250)
        layout.addWidget(left_widget)

        # Right: the conversation.
        right = QVBoxLayout()
        self.conversation = QTextEdit()
        self.conversation.setReadOnly(True)
        right.addWidget(self.conversation, 1)

        input_row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText(
            "Tell JARVIS what to do…  (\"tidy my downloads\", \"you can use Photoshop\")")
        self.input.returnPressed.connect(self._send)
        input_row.addWidget(self.input, 1)
        send = QPushButton("Send")
        send.setObjectName("primary")
        send.clicked.connect(self._send)
        input_row.addWidget(send)
        right.addLayout(input_row)

        right_widget = QWidget()
        right_widget.setLayout(right)
        layout.addWidget(right_widget, 1)

        self._say_system(
            "Ready. Ask me anything, or tell me to do something on this machine.\n"
            "I work freely inside your workspace folder. Spending, posting and "
            "messaging need your explicit go-ahead in the request itself.")
        return page

    def _build_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = None
            return
        self.tray = QSystemTrayIcon(_make_icon(), self)
        menu = QMenu()
        show = QAction("Show JARVIS", self)
        show.triggered.connect(self._restore)
        menu.addAction(show)
        talk = QAction("Start listening", self)
        talk.triggered.connect(self._toggle_voice)
        menu.addAction(talk)
        orb_action = QAction("Show the orb", self)
        orb_action.triggered.connect(self._show_floating_orb)
        menu.addAction(orb_action)
        menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self._quit_everything)
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.setToolTip("JARVIS")
        self.tray.activated.connect(
            lambda reason: self._restore()
            if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.show()

    def _restore(self) -> None:
        if getattr(self, "floating", None) is not None:
            self.floating.hide()
        self.showNormal()
        self.raise_()
        self.activateWindow()

    # ------------------------------------------------------------------ #
    # Conversation
    # ------------------------------------------------------------------ #

    def _say_system(self, text: str) -> None:
        self.conversation.append(
            f"<p style='color:{MUTED}'>{_escape(text).replace(chr(10), '<br>')}</p>")

    def _say_user(self, text: str) -> None:
        self.conversation.append(
            f"<p><b style='color:{ACCENT}'>you</b><br>{_escape(text)}</p>")

    def _say_jarvis(self, text: str) -> None:
        self.conversation.append(
            f"<p><b>jarvis</b><br>{_escape(text).replace(chr(10), '<br>')}</p>")

    def _clear_conversation(self) -> None:
        self.conversation.clear()
        self.app.conversation_id = None
        self._say_system("New conversation.")

    def _send(self) -> None:
        text = self.input.text().strip()
        if not text:
            return
        if not self.app.brain.ready():
            QMessageBox.warning(
                self, "No AI configured",
                "Add your Claude API key in Settings, then restart JARVIS.")
            return

        self.input.clear()
        self._say_user(text)
        self.orb.set_state(THINKING)
        self.statusBar().showMessage("Thinking…")

        def done(reply: Any, error: Exception | None) -> None:
            self.orb.set_state(IDLE)
            self.statusBar().showMessage(self._status_line())
            if error is not None:
                self._say_system(f"Something went wrong: {error}")
                return
            if reply is not None:
                self._say_jarvis(reply.text)
                if reply.blocked:
                    for note in reply.blocked:
                        self.conversation.append(
                            f"<p style='color:{WARNING}'>{_escape(note)}</p>")
            self._refresh_badges()

        self.runner.submit(self.app.ask(text, speak=self.voice_active), done)

    # ------------------------------------------------------------------ #
    # Voice
    # ------------------------------------------------------------------ #

    def _toggle_voice(self) -> None:
        if self.voice_active:
            self.voice_active = False
            if self._voice_future is not None:
                self._voice_future.cancel()
            self.voice_button.setText("Hold a conversation")
            self.voice_hint.setText("")
            self.orb.set_state(IDLE)
            return

        if not self.app.listener.available():
            QMessageBox.information(self, "Voice input unavailable",
                                    self.app.listener.why_unavailable())
            return

        self.voice_active = True
        self.voice_button.setText("Stop listening")
        word = self.app.settings.get("voice.wake_word", "jarvis")
        self.voice_hint.setText(f"Say “{word}” to get my attention")
        self.orb.set_state(LISTENING)

        def done(_result: Any, error: Exception | None) -> None:
            self.voice_active = False
            self.voice_button.setText("Hold a conversation")
            self.orb.set_state(IDLE)
            if error is not None:
                self._say_system(f"Voice stopped: {error}")

        self._voice_future = self.runner.submit(
            self.app.voice_loop(wake_word=True,
                                should_stop=lambda: not self.voice_active),
            done)
        if getattr(self, "floating", None) is not None and self.floating.isVisible():
            self.floating.set_state(LISTENING)

    # ------------------------------------------------------------------ #
    # Events
    # ------------------------------------------------------------------ #

    def _wire_events(self) -> None:
        self.relay = EventRelay(self)
        self.relay.event.connect(self._on_event)

    @pyqtSlot(str, str, object)
    def _on_event(self, kind: str, message: str, data: dict) -> None:
        self.activity_panel.append(kind, message)
        if getattr(self, "floating", None) is not None and self.floating.isVisible():
            self.floating.handle_event(kind, message)

        if kind == events.LISTENING:
            self.orb.set_state(LISTENING)
        elif kind == events.THINKING:
            self.orb.set_state(THINKING)
        elif kind == events.SPEAKING:
            self.orb.set_state(SPEAKING)
        elif kind == events.TRANSCRIPT:
            self._say_user(message)
        elif kind == events.REPLY:
            self._say_jarvis(message)
            self.orb.set_state(IDLE)
        elif kind == events.APPROVAL_NEEDED:
            self._refresh_badges()
            if getattr(self, "tray", None):
                self.tray.showMessage("JARVIS needs your approval", message,
                                      QSystemTrayIcon.MessageIcon.Warning, 8000)
        elif kind in (events.MISSION_DONE, events.MISSION_FAILED):
            self.missions_panel.refresh()
            if getattr(self, "tray", None):
                self.tray.showMessage("JARVIS", message,
                                      QSystemTrayIcon.MessageIcon.Information, 6000)
        elif kind == events.TOOL_CALL:
            self.statusBar().showMessage(message[:120])

    def _run_mission(self, mission_id: str) -> None:
        self.statusBar().showMessage(f"Running {mission_id}…")

        def done(result: Any, error: Exception | None) -> None:
            self.missions_panel.refresh()
            self._refresh_badges()
            if error is not None:
                QMessageBox.warning(self, "Mission failed", str(error))
                return
            if result is not None:
                colour = {"success": SUCCESS, "partial": WARNING}.get(result.status, DANGER)
                self.conversation.append(
                    f"<p style='color:{colour}'>Mission {mission_id}: "
                    f"{result.status} — {_escape(result.summary())}</p>")
            self.statusBar().showMessage(self._status_line())

        self.runner.submit(self.app.run_mission(mission_id, trigger="ui",
                                                unattended=False), done)

    def _install_approval_handler(self) -> None:
        """With the window open, ask in a dialog instead of queueing silently."""
        import asyncio

        async def ask(request, risk, reason) -> bool:
            loop = asyncio.get_running_loop()
            future: asyncio.Future = loop.create_future()

            def prompt() -> None:
                answer = QMessageBox.question(
                    self, "Permission needed",
                    f"{request.describe()}\n\n{reason}\n\nAllow this?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No)
                allowed = answer == QMessageBox.StandardButton.Yes
                loop.call_soon_threadsafe(
                    lambda: None if future.done() else future.set_result(allowed))

            QTimer.singleShot(0, prompt)
            return await future

        self.app.set_approval_handler(ask)


    # ------------------------------------------------------------------ #
    # The floating orb
    # ------------------------------------------------------------------ #

    def _build_floating_orb(self) -> None:
        """A small always-on-top presence that outlives the main window."""
        self.floating: FloatingOrb | None = None
        if not self.app.settings.get("ui.floating_orb", True):
            return
        self.floating = FloatingOrb(self.app.settings)
        self.floating.clicked.connect(self._orb_clicked)
        self.floating.open_requested.connect(self._restore)
        self.floating.quit_requested.connect(self._quit_everything)

    def _show_floating_orb(self) -> None:
        if self.floating is None:
            return
        self.floating.show()
        # Closing the window is how most people will leave JARVIS running, so
        # that's the moment the wake word needs to start working.
        if self.app.listener.available() and not self.voice_active:
            self._toggle_voice()

    def _orb_clicked(self) -> None:
        """Click the orb to talk, without needing the wake word."""
        if not self.app.listener.available():
            self._restore()
            return
        if self.voice_active:
            return
        self._toggle_voice()

    def _quit_everything(self) -> None:
        if self.floating is not None:
            self.floating.hide()
        self.voice_active = False
        QApplication.quit()

    # ------------------------------------------------------------------ #
    # Chrome
    # ------------------------------------------------------------------ #

    def _status_line(self) -> str:
        stats = self.app.memory.stats()
        brain = "Claude" if self.app.brain.ready() else "no AI key"
        return (f"{brain} · {len(self.app.tools.names())} tools · "
                f"{len(self.app.scheduler.missions)} missions · "
                f"${stats['spend_24h']:.3f} today")

    def _refresh_badges(self) -> None:
        pending = len(self.app.pending_approvals())
        if pending:
            self.approval_badge.setText(f"⚑ {pending} waiting")
            self.approval_badge.setStyleSheet(f"color:{WARNING}; font-weight:600;")
        else:
            self.approval_badge.setText("")
        self.status_label.setText(
            "  listening" if self.voice_active else "")

    def _periodic_refresh(self) -> None:
        self._refresh_badges()
        self.activity_panel.refresh_stats()
        self.statusBar().showMessage(self._status_line())

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Closing leaves the orb on screen, listening, missions still running."""
        orb = getattr(self, "floating", None)
        tray = getattr(self, "tray", None)

        if orb is not None or (tray is not None and tray.isVisible()):
            event.ignore()
            self.hide()
            if orb is not None:
                self._show_floating_orb()
            if tray is not None and tray.isVisible():
                tray.showMessage(
                    "JARVIS is still here",
                    "The orb stays on screen - say the wake word, or click it. "
                    "Quit from the orb's right-click menu.",
                    QSystemTrayIcon.MessageIcon.Information, 4000)
            return
        event.accept()


def _escape(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _make_icon() -> QIcon:
    """A cyan ring, drawn rather than shipped as a file."""
    from PyQt6.QtGui import QColor, QPen

    pixmap = QPixmap(64, 64)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(ACCENT))
    pen.setWidth(5)
    painter.setPen(pen)
    painter.drawEllipse(9, 9, 46, 46)
    painter.setBrush(QColor(ACCENT))
    painter.drawEllipse(24, 24, 16, 16)
    painter.end()
    return QIcon(pixmap)


def run_ui() -> int:
    """Launch the desktop app. Returns a process exit code."""
    setup_logging()

    qt_app = QApplication(sys.argv)
    qt_app.setApplicationName("JARVIS")
    qt_app.setStyleSheet(STYLESHEET)
    qt_app.setQuitOnLastWindowClosed(False)

    runner = AsyncRunner()

    from ..core.assistant import Assistant

    config = Config()
    if config.vault.needs_passphrase:
        from PyQt6.QtWidgets import QInputDialog, QLineEdit as _LineEdit
        passphrase, ok = QInputDialog.getText(
            None, "JARVIS", "Vault passphrase:", _LineEdit.EchoMode.Password)
        if not ok:
            return 1
        config.vault.unlock(passphrase)

    assistant = Assistant(config)
    future = runner.submit(assistant.start(with_scheduler=True))
    try:
        future.result(timeout=120)
    except Exception as exc:
        log.exception("startup failed")
        QMessageBox.critical(None, "JARVIS couldn't start", str(exc))
        runner.stop()
        return 1

    window = MainWindow(assistant, runner)
    window.show()

    if not assistant.brain.ready():
        QMessageBox.information(
            window, "Almost there",
            "JARVIS needs a Claude API key before it can think.\n\n"
            "Open Settings, paste the key from console.anthropic.com, save, "
            "then restart.")
        window.tabs.setCurrentIndex(4)

    code = qt_app.exec()
    if getattr(window, "floating", None) is not None:
        window.floating.hide()
    assistant.shutdown()
    runner.stop()
    return code
