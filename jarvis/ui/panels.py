"""The Missions, Approvals, Activity and Settings panels."""

from __future__ import annotations

import json
from typing import Any

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFormLayout, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox,
    QTextEdit, QVBoxLayout, QWidget,
)

from .. import paths
from ..config import KEY_SPECS
from ..core import events
from .theme import ACCENT, DANGER, MUTED, SUCCESS, WARNING


def card(*children: QWidget, spacing: int = 10) -> QFrame:
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(16, 14, 16, 14)
    layout.setSpacing(spacing)
    for child in children:
        layout.addWidget(child)
    return frame


def heading(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("heading")
    return label


def muted(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("muted")
    label.setWordWrap(True)
    return label


# --------------------------------------------------------------------------- #
# Missions
# --------------------------------------------------------------------------- #

class MissionsPanel(QWidget):
    """Every saved mission: schedule, last result, run now, enable/disable."""

    run_requested = pyqtSignal(str)

    def __init__(self, app, runner, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.app = app
        self.runner = runner

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 18)
        layout.setSpacing(12)

        header = QHBoxLayout()
        header.addWidget(heading("Missions"))
        header.addStretch()
        new_button = QPushButton("New mission")
        new_button.setObjectName("primary")
        new_button.clicked.connect(self._new_mission)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        header.addWidget(refresh)
        header.addWidget(new_button)
        layout.addLayout(header)

        layout.addWidget(muted(
            "A mission is a job JARVIS runs on a schedule while you're away. "
            "Steps hand their results to the next step."))

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.container = QWidget()
        self.list_layout = QVBoxLayout(self.container)
        self.list_layout.setSpacing(10)
        self.list_layout.addStretch()
        self.scroll.setWidget(self.container)
        layout.addWidget(self.scroll, 1)

        self.refresh()

    def refresh(self) -> None:
        while self.list_layout.count() > 1:
            item = self.list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        rows = self.app.scheduler.next_runs()
        if not rows:
            self.list_layout.insertWidget(0, card(muted(
                "No missions yet. Click 'New mission', or just ask JARVIS: "
                "\"make a mission that emails me a news summary every morning\".")))
            return

        for row in rows:
            self.list_layout.insertWidget(self.list_layout.count() - 1,
                                          self._mission_card(row))

    def _mission_card(self, row: dict[str, Any]) -> QFrame:
        title = QLabel(f"<b>{row['name']}</b>")
        status_bits = [f"{row['steps']} steps", row["schedule"]]
        if row["next_run"]:
            status_bits.append(f"next {row['next_run'].replace('T', ' ')}")
        if row["last_status"]:
            colour = {"success": SUCCESS, "partial": WARNING}.get(row["last_status"], DANGER)
            status_bits.append(f"<span style='color:{colour}'>last: "
                               f"{row['last_status']}</span>")
        subtitle = muted(" · ".join(status_bits))

        top = QHBoxLayout()
        text_column = QVBoxLayout()
        text_column.addWidget(title)
        text_column.addWidget(subtitle)
        top.addLayout(text_column, 1)

        if row["running"]:
            badge = QLabel("RUNNING")
            badge.setStyleSheet(f"color:{ACCENT}; font-weight:600;")
            top.addWidget(badge)

        enabled = QCheckBox("Scheduled")
        enabled.setChecked(row["enabled"])
        enabled.toggled.connect(
            lambda state, mission_id=row["id"]: self._toggle(mission_id, state))
        top.addWidget(enabled)

        run_button = QPushButton("Run now")
        run_button.setObjectName("primary")
        run_button.clicked.connect(lambda _, mission_id=row["id"]:
                                   self.run_requested.emit(mission_id))
        top.addWidget(run_button)

        edit_button = QPushButton("Edit")
        edit_button.clicked.connect(lambda _, mission_id=row["id"]:
                                    self._edit(mission_id))
        top.addWidget(edit_button)

        wrapper = QWidget()
        wrapper.setLayout(top)
        children = [wrapper]

        if row["authorizations"]:
            for auth in row["authorizations"]:
                label = QLabel(f"⚑ Authorised: {auth}")
                label.setWordWrap(True)
                label.setStyleSheet(f"color:{WARNING};")
                children.append(label)

        problems = self.app.scheduler.check(self.app.scheduler.get(row["id"]))
        if problems:
            label = QLabel("⚠ " + "; ".join(problems[:2]))
            label.setWordWrap(True)
            label.setStyleSheet(f"color:{DANGER};")
            children.append(label)

        return card(*children)

    def _toggle(self, mission_id: str, enabled: bool) -> None:
        self.app.scheduler.set_enabled(mission_id, enabled)
        self.refresh()

    def _new_mission(self) -> None:
        dialog = MissionEditor(self.app, parent=self)
        if dialog.exec():
            self.refresh()

    def _edit(self, mission_id: str) -> None:
        mission = self.app.scheduler.get(mission_id)
        if mission is None:
            return
        dialog = MissionEditor(self.app, mission=mission, parent=self)
        if dialog.exec():
            self.refresh()


class MissionEditor(QDialog):
    """Edit a mission as JSON, with validation before it can be saved.

    A node-graph editor would look better in a screenshot; this validates
    against the real plugin registry and tells you exactly what's wrong, which
    is what actually stops a mission failing at 2am.
    """

    def __init__(self, app, mission=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.app = app
        self.mission = mission
        self.setWindowTitle("Edit mission" if mission else "New mission")
        self.resize(760, 640)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        layout.addWidget(heading(mission.name if mission else "New mission"))
        layout.addWidget(muted(
            "Steps run top to bottom. Use {output_key} to reuse an earlier result. "
            "Run 'Check' before saving - it validates every plugin name and "
            "placeholder against what's actually installed."))

        available = ", ".join(sorted(self.app.plugins.plugins))
        layout.addWidget(muted(f"Plugins available: {available}"))

        self.editor = QPlainTextEdit()
        self.editor.setPlainText(json.dumps(
            mission.to_dict() if mission else _blank_mission(), indent=2))
        self.editor.setStyleSheet("font-family: Cascadia Mono, Consolas, monospace;")
        layout.addWidget(self.editor, 1)

        self.feedback = QLabel("")
        self.feedback.setWordWrap(True)
        layout.addWidget(self.feedback)

        buttons = QHBoxLayout()
        check = QPushButton("Check")
        check.clicked.connect(self._check)
        buttons.addWidget(check)
        buttons.addStretch()
        if mission is not None:
            delete = QPushButton("Delete")
            delete.setObjectName("danger")
            delete.clicked.connect(self._delete)
            buttons.addWidget(delete)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        save = QPushButton("Save")
        save.setObjectName("primary")
        save.clicked.connect(self._save)
        buttons.addWidget(save)
        layout.addLayout(buttons)

    def _parse(self):
        from ..core.mission import Mission, MissionError
        try:
            data = json.loads(self.editor.toPlainText())
        except json.JSONDecodeError as exc:
            raise MissionError(f"That isn't valid JSON: {exc}") from exc
        return Mission.from_dict(data)

    def _check(self) -> bool:
        from ..core.mission import MissionError
        try:
            mission = self._parse()
        except MissionError as exc:
            self.feedback.setText(f"<span style='color:{DANGER}'>{exc}</span>")
            return False
        problems = self.app.scheduler.check(mission)
        if problems:
            self.feedback.setText(
                f"<span style='color:{DANGER}'>" +
                "<br>".join(f"• {p}" for p in problems) + "</span>")
            return False
        self.feedback.setText(
            f"<span style='color:{SUCCESS}'>Looks good — {len(mission.steps)} steps."
            "</span>")
        return True

    def _save(self) -> None:
        if not self._check():
            return
        mission = self._parse()
        self.app.scheduler.save(mission)
        self.accept()

    def _delete(self) -> None:
        if self.mission is None:
            return
        confirm = QMessageBox.question(
            self, "Delete mission",
            f"Delete '{self.mission.name}'? This can't be undone.")
        if confirm == QMessageBox.StandardButton.Yes:
            self.app.scheduler.delete(self.mission.id)
            self.accept()


def _blank_mission() -> dict[str, Any]:
    return {
        "id": "my_mission",
        "name": "My mission",
        "description": "What this does",
        "schedule": "0 7 * * *",
        "enabled": False,
        "steps": [
            {"name": "Search", "plugin": "web_search",
             "params": {"query": "something worth knowing", "num_results": 6},
             "output_key": "research"},
            {"name": "Summarise", "plugin": "llm",
             "params": {"prompt": "Summarise this into five points: {research}"},
             "output_key": "summary"},
            {"name": "Save it", "plugin": "write_file",
             "params": {"path": "briefs/summary.md", "content": "{summary}"}},
        ],
    }


# --------------------------------------------------------------------------- #
# Approvals
# --------------------------------------------------------------------------- #

class ApprovalsPanel(QWidget):
    """Everything JARVIS wanted to do but wasn't allowed to without you."""

    changed = pyqtSignal()

    def __init__(self, app, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.app = app

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 18)
        layout.setSpacing(12)

        header = QHBoxLayout()
        header.addWidget(heading("Waiting for you"))
        header.addStretch()
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        header.addWidget(refresh)
        layout.addLayout(header)

        layout.addWidget(muted(
            "Spending money, posting publicly, sending messages and changing system "
            "settings are blocked unless you authorise them in the request itself. "
            "Anything JARVIS wanted to do anyway ends up here."))

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.container = QWidget()
        self.list_layout = QVBoxLayout(self.container)
        self.list_layout.setSpacing(10)
        self.list_layout.addStretch()
        self.scroll.setWidget(self.container)
        layout.addWidget(self.scroll, 1)

        self.refresh()

    def refresh(self) -> None:
        while self.list_layout.count() > 1:
            item = self.list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        pending = self.app.pending_approvals()
        if not pending:
            self.list_layout.insertWidget(0, card(
                muted("Nothing waiting. JARVIS hasn't been stopped by anything.")))
            return

        for item in pending:
            self.list_layout.insertWidget(self.list_layout.count() - 1,
                                          self._approval_card(item))

    def _approval_card(self, item: dict[str, Any]) -> QFrame:
        colour = DANGER if item["risk"] == "high" else WARNING
        title = QLabel(f"<b>{item['summary']}</b>")
        title.setWordWrap(True)
        detail = muted(f"{item['risk']} risk · {item['capability']} · "
                       f"asked by {item['requested_by']} at {item['at']}")
        reason = (item.get("payload") or {}).get("reason", "")

        buttons = QHBoxLayout()
        buttons.addStretch()
        deny = QPushButton("No")
        deny.setObjectName("danger")
        deny.clicked.connect(lambda _, i=item["id"]: self._decide(i, False, False))
        approve = QPushButton("Allow once")
        approve.setObjectName("primary")
        approve.clicked.connect(lambda _, i=item["id"]: self._decide(i, True, False))
        always = QPushButton("Always allow")
        always.clicked.connect(lambda _, i=item["id"]: self._decide(i, True, True))
        buttons.addWidget(deny)
        buttons.addWidget(always)
        buttons.addWidget(approve)
        wrapper = QWidget()
        wrapper.setLayout(buttons)

        badge = QLabel(item["risk"].upper())
        badge.setStyleSheet(f"color:{colour}; font-weight:600;")

        children = [badge, title, detail]
        if reason:
            children.append(muted(reason))
        children.append(wrapper)
        return card(*children)

    def _decide(self, approval_id: int, approve: bool, always: bool) -> None:
        if approve:
            self.app.approve(approval_id, always=always)
        else:
            self.app.deny(approval_id)
        self.refresh()
        self.changed.emit()


# --------------------------------------------------------------------------- #
# Activity
# --------------------------------------------------------------------------- #

class ActivityPanel(QWidget):
    """A live feed of everything JARVIS does, plus what it cost."""

    def __init__(self, app, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.app = app

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 18)
        layout.setSpacing(12)

        header = QHBoxLayout()
        header.addWidget(heading("Activity"))
        header.addStretch()
        self.stats = muted("")
        header.addWidget(self.stats)
        layout.addLayout(header)

        self.feed = QTextEdit()
        self.feed.setReadOnly(True)
        self.feed.setStyleSheet("font-family: Cascadia Mono, Consolas, monospace;"
                                "font-size: 12px;")
        layout.addWidget(self.feed, 1)

        self.refresh_stats()

    def append(self, kind: str, message: str) -> None:
        colour = {
            events.TOOL_CALL: ACCENT,
            events.PERMISSION: WARNING,
            events.APPROVAL_NEEDED: DANGER,
            events.ERROR: DANGER,
            events.MISSION_START: ACCENT,
            events.MISSION_DONE: SUCCESS,
            events.MISSION_FAILED: DANGER,
        }.get(kind, MUTED)
        from datetime import datetime
        stamp = datetime.now().strftime("%H:%M:%S")
        self.feed.append(
            f"<span style='color:{MUTED}'>{stamp}</span> "
            f"<span style='color:{colour}'>{kind}</span> {message}")

    def refresh_stats(self) -> None:
        stats = self.app.memory.stats()
        self.stats.setText(
            f"today ${stats['spend_24h']:.3f} · 7 days ${stats['spend_7d']:.3f} · "
            f"{sum(stats['runs'].values())} mission runs · "
            f"{stats['pending_approvals']} waiting")


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

class SettingsPanel(QWidget):
    """Keys, workspace, voice, autonomy - everything the wizard asks, editable."""

    saved = pyqtSignal()

    def __init__(self, app, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.app = app
        self.key_fields: dict[str, QLineEdit] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 14, 18, 18)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setSpacing(14)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        # --- identity --------------------------------------------------- #
        identity = QFormLayout()
        self.user_name = QLineEdit(self.app.settings.get("user_name", ""))
        identity.addRow("Your name", self.user_name)
        self.workspace = QLineEdit(str(self.app.settings.workspace))
        identity.addRow("Workspace folder", self.workspace)
        identity_widget = QWidget()
        identity_widget.setLayout(identity)
        layout.addWidget(card(heading("You"), identity_widget))

        # --- autonomy ---------------------------------------------------- #
        autonomy = QFormLayout()
        self.allowed_apps = QLineEdit(
            ", ".join(self.app.settings.get("autonomy.allowed_apps", [])))
        autonomy.addRow("Apps JARVIS may open", self.allowed_apps)
        self.spend_cap = QSpinBox()
        self.spend_cap.setRange(0, 1000)
        self.spend_cap.setPrefix("$ ")
        self.spend_cap.setValue(int(self.app.settings.get("autonomy.daily_spend_cap_usd", 5)))
        autonomy.addRow("Daily AI spend cap", self.spend_cap)
        self.auto_low = QCheckBox("Work freely inside the workspace without asking")
        self.auto_low.setChecked(self.app.settings.get("autonomy.auto_approve_low_risk", True))
        autonomy.addRow("", self.auto_low)
        autonomy_widget = QWidget()
        autonomy_widget.setLayout(autonomy)
        layout.addWidget(card(
            heading("Autonomy"),
            muted("Spending, posting, messaging and system changes stay blocked "
                  "unless you authorise them in the request itself. That isn't "
                  "configurable here on purpose."),
            autonomy_widget))

        # --- voice -------------------------------------------------------- #
        voice = QFormLayout()
        self.tts_engine = QComboBox()
        self.tts_engine.addItems(["auto", "edge", "cartesia", "elevenlabs", "pyttsx3"])
        self.tts_engine.setCurrentText(self.app.settings.get("voice.tts_engine", "auto"))
        voice.addRow("Voice output", self.tts_engine)
        self.stt_engine = QComboBox()
        self.stt_engine.addItems(["auto", "faster-whisper", "deepgram"])
        self.stt_engine.setCurrentText(self.app.settings.get("voice.stt_engine", "auto"))
        voice.addRow("Speech input", self.stt_engine)
        self.edge_voice = QLineEdit(self.app.settings.get("voice.edge_voice", ""))
        voice.addRow("Voice name", self.edge_voice)
        self.wake_word = QLineEdit(self.app.settings.get("voice.wake_word", "jarvis"))
        voice.addRow("Wake word", self.wake_word)
        voice_widget = QWidget()
        voice_widget.setLayout(voice)
        layout.addWidget(card(
            heading("Voice"),
            muted(f"In: {self.app.listener.transcriber.describe()}   "
                  f"Out: {self.app.speech.describe()}"),
            voice_widget))

        # --- models -------------------------------------------------------- #
        models = QFormLayout()
        self.model_voice = QLineEdit(self.app.settings.model_for("voice"))
        models.addRow("Voice replies", self.model_voice)
        self.model_general = QLineEdit(self.app.settings.model_for("general"))
        models.addRow("General", self.model_general)
        self.model_deep = QLineEdit(self.app.settings.model_for("deep"))
        models.addRow("Overnight missions", self.model_deep)
        models_widget = QWidget()
        models_widget.setLayout(models)
        layout.addWidget(card(heading("Models"), models_widget))

        # --- keys ----------------------------------------------------------- #
        keys = QFormLayout()
        stored = set(self.app.config.vault.names())
        for spec in KEY_SPECS:
            field = QLineEdit()
            field.setEchoMode(QLineEdit.EchoMode.Password)
            field.setPlaceholderText("stored" if spec.name in stored
                                     else f"{spec.where} — {spec.free_tier}")
            self.key_fields[spec.name] = field
            label = spec.label + (" ✓" if spec.name in stored else "")
            keys.addRow(label, field)
        keys_widget = QWidget()
        keys_widget.setLayout(keys)
        layout.addWidget(card(
            heading("API keys"),
            muted("Stored encrypted on this machine. Leave blank to keep what's "
                  "already there. Only the Claude key is required."),
            keys_widget))

        layout.addWidget(muted(f"Config folder: {paths.ROOT}"))
        layout.addStretch()

        save = QPushButton("Save settings")
        save.setObjectName("primary")
        save.clicked.connect(self._save)
        outer.addWidget(save)

    def _save(self) -> None:
        settings = self.app.settings
        settings.set("user_name", self.user_name.text().strip())
        settings.set("autonomy.workspace", self.workspace.text().strip())
        settings.set("autonomy.allowed_apps",
                     [a.strip().lower() for a in self.allowed_apps.text().split(",")
                      if a.strip()])
        settings.set("autonomy.daily_spend_cap_usd", float(self.spend_cap.value()))
        settings.set("autonomy.auto_approve_low_risk", self.auto_low.isChecked())
        settings.set("voice.tts_engine", self.tts_engine.currentText())
        settings.set("voice.stt_engine", self.stt_engine.currentText())
        settings.set("voice.edge_voice", self.edge_voice.text().strip())
        settings.set("voice.wake_word", self.wake_word.text().strip() or "jarvis")
        settings.set("models.voice", self.model_voice.text().strip())
        settings.set("models.general", self.model_general.text().strip())
        settings.set("models.deep", self.model_deep.text().strip())
        settings.save()

        updates = {name: field.text().strip()
                   for name, field in self.key_fields.items() if field.text().strip()}
        if updates:
            self.app.config.vault.set_many(updates)
            for field in self.key_fields.values():
                field.clear()

        QMessageBox.information(
            self, "Saved",
            "Settings saved." + ("\n\nRestart JARVIS to pick up new API keys."
                                 if updates else ""))
        self.saved.emit()
