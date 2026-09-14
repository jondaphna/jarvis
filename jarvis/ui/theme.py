"""Dark theme: near-black, cyan accent, one font. Stark's HUD, minus the clutter."""

from __future__ import annotations

BG = "#0a0a12"
SURFACE = "#12121d"
CARD = "#1a1a2a"
BORDER = "#252539"
ACCENT = "#00d4ff"
ACCENT_DIM = "#0a7fa0"
TEXT = "#e8e8f0"
MUTED = "#8a8aa0"
SUCCESS = "#3ddc84"
WARNING = "#ffb454"
DANGER = "#ff5c5c"

FONT = "Segoe UI, SF Pro Text, Inter, system-ui, sans-serif"
MONO = "Cascadia Mono, SF Mono, Consolas, monospace"

STYLESHEET = f"""
QWidget {{
    background: {BG};
    color: {TEXT};
    font-family: {FONT};
    font-size: 13px;
}}
QMainWindow, QDialog {{ background: {BG}; }}

QLabel#title    {{ font-size: 21px; font-weight: 600; letter-spacing: 2px; }}
QLabel#subtitle {{ color: {MUTED}; font-size: 12px; }}
QLabel#heading  {{ font-size: 15px; font-weight: 600; padding: 2px 0; }}
QLabel#muted    {{ color: {MUTED}; }}
QLabel#danger   {{ color: {DANGER}; }}
QLabel#warning  {{ color: {WARNING}; }}
QLabel#success  {{ color: {SUCCESS}; }}

QFrame#card {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}

QPushButton {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 7px;
    padding: 8px 16px;
    color: {TEXT};
}}
QPushButton:hover  {{ border-color: {ACCENT_DIM}; background: {CARD}; }}
QPushButton:pressed {{ background: {BORDER}; }}
QPushButton:disabled {{ color: {MUTED}; border-color: {SURFACE}; }}

QPushButton#primary {{
    background: {ACCENT_DIM};
    border: 1px solid {ACCENT};
    color: #04141a;
    font-weight: 600;
}}
QPushButton#primary:hover {{ background: {ACCENT}; }}
QPushButton#danger {{ border-color: #5c2a2a; color: {DANGER}; }}
QPushButton#danger:hover {{ background: #2a1414; }}
QPushButton#ghost {{ background: transparent; border: none; color: {MUTED}; }}
QPushButton#ghost:hover {{ color: {ACCENT}; }}

QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 7px;
    padding: 8px 10px;
    selection-background-color: {ACCENT_DIM};
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
    background: {SURFACE}; border: 1px solid {BORDER};
    selection-background-color: {ACCENT_DIM};
}}

QTabWidget::pane {{ border: none; }}
QTabBar::tab {{
    background: transparent;
    padding: 9px 18px;
    margin-right: 4px;
    border-bottom: 2px solid transparent;
    color: {MUTED};
}}
QTabBar::tab:selected {{ color: {ACCENT}; border-bottom-color: {ACCENT}; }}
QTabBar::tab:hover {{ color: {TEXT}; }}

QListWidget, QTableWidget, QTreeWidget {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 8px;
    outline: none;
}}
QListWidget::item {{ padding: 9px; border-radius: 6px; }}
QListWidget::item:selected {{ background: {BORDER}; color: {ACCENT}; }}
QHeaderView::section {{
    background: {CARD}; border: none; padding: 7px; color: {MUTED};
}}

QScrollBar:vertical {{ background: transparent; width: 9px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {ACCENT_DIM}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 9px; }}
QScrollBar::handle:horizontal {{ background: {BORDER}; border-radius: 4px; }}

QCheckBox::indicator {{
    width: 17px; height: 17px; border-radius: 5px;
    border: 1px solid {BORDER}; background: {SURFACE};
}}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}

QMenu {{ background: {CARD}; border: 1px solid {BORDER}; padding: 5px; }}
QMenu::item {{ padding: 7px 22px; border-radius: 5px; }}
QMenu::item:selected {{ background: {BORDER}; color: {ACCENT}; }}

QStatusBar {{ color: {MUTED}; border-top: 1px solid {BORDER}; }}
QToolTip {{
    background: {CARD}; color: {TEXT};
    border: 1px solid {ACCENT_DIM}; padding: 6px;
}}
"""
