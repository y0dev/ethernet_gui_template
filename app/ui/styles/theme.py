"""Dark industrial theme: palette constants and the application stylesheet."""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication

BG = "#14181e"          # window background (dark slate)
BG_ALT = "#1a2029"      # panels
BG_RAISED = "#212935"   # inputs, headers
BORDER = "#2e3846"
TEXT = "#d6dde6"
TEXT_DIM = "#8b97a7"
ACCENT = "#3d8bfd"      # blue accent
ACCENT_DIM = "#24538f"
OK = "#2fbf71"          # connected
WARN = "#e0a526"
ERROR = "#e5484d"       # error indicators
RX_COLOR = "#5fb3ff"
TX_COLOR = "#f0a35e"

#: keys accepted in project.json -> "theme"
THEME_KEYS = {
    "background": "BG", "panel": "BG_ALT", "input": "BG_RAISED", "border": "BORDER",
    "text": "TEXT", "text_dim": "TEXT_DIM", "accent": "ACCENT", "accent_dim": "ACCENT_DIM",
    "ok": "OK", "warning": "WARN", "error": "ERROR", "rx": "RX_COLOR", "tx": "TX_COLOR",
}

STATE_COLORS: dict[str, str] = {}
PORTING_STATUS_COLORS: dict[str, str] = {}
LOG_LEVEL_COLORS: dict[str, str] = {}


def _rebuild_maps() -> None:
    STATE_COLORS.clear()
    STATE_COLORS.update({"Connected": OK, "Listening": OK, "Connecting": WARN, "Disconnected": TEXT_DIM,
                         "Error": ERROR})
    PORTING_STATUS_COLORS.clear()
    PORTING_STATUS_COLORS.update({
        "Not Analyzed": TEXT_DIM, "Analyzed": "#9aa9bd", "Ready to Port": "#7fb2ff", "In Progress": ACCENT,
        "Ported": "#b48cff", "Verification Required": WARN, "Verified": OK, "Blocked": ERROR,
    })
    LOG_LEVEL_COLORS.clear()
    LOG_LEVEL_COLORS.update({"DEBUG": TEXT_DIM, "INFO": TEXT, "WARNING": WARN, "ERROR": ERROR, "CRITICAL": ERROR})


_rebuild_maps()


def configure(overrides: dict[str, str]) -> None:
    """Apply colour overrides (``{"accent": "#ff8800"}``) before any window is built."""
    global STYLESHEET
    unknown = sorted(set(overrides) - set(THEME_KEYS))
    if unknown:
        raise ValueError(f"unknown theme keys {unknown}; allowed: {sorted(THEME_KEYS)}")
    for key, value in overrides.items():
        if not QColor(value).isValid():
            raise ValueError(f"theme.{key}: {value!r} is not a colour")
        globals()[THEME_KEYS[key]] = value
    _rebuild_maps()
    STYLESHEET = build_stylesheet()


def monospace_font(point_size: int = 10) -> QFont:
    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    font.setPointSize(point_size)
    return font


def build_stylesheet() -> str:
    """Qt stylesheet for the current colours."""
    return f"""
* {{ outline: 0; }}
QWidget {{ background: {BG}; color: {TEXT}; font-size: 10pt; }}
QMainWindow::separator {{ background: {BORDER}; width: 2px; height: 2px; }}
QToolTip {{ background: {BG_RAISED}; color: {TEXT}; border: 1px solid {BORDER}; padding: 4px; }}

QFrame#Sidebar {{ background: {BG_ALT}; border-right: 1px solid {BORDER}; }}
QListWidget#NavList {{ background: transparent; border: none; padding: 6px 0; }}
QListWidget#NavList::item {{ padding: 9px 14px; border-left: 3px solid transparent; color: {TEXT_DIM}; }}
QListWidget#NavList::item:hover {{ background: {BG_RAISED}; color: {TEXT}; }}
QListWidget#NavList::item:selected {{ background: {BG_RAISED}; color: {TEXT}; border-left: 3px solid {ACCENT}; }}
QLabel#AppTitle {{ font-size: 11pt; font-weight: 600; padding: 14px 14px 2px 14px; background: transparent; }}
QLabel#AppSubtitle {{ color: {TEXT_DIM}; font-size: 8pt; padding: 0 14px 10px 14px; background: transparent; }}

QLabel#PageTitle {{ font-size: 14pt; font-weight: 600; }}
QLabel#PageSubtitle {{ color: {TEXT_DIM}; }}
QLabel#Dim {{ color: {TEXT_DIM}; }}
QLabel#SectionTitle {{ color: {TEXT_DIM}; font-size: 8pt; font-weight: 600; letter-spacing: 1px; }}

QFrame#Card {{ background: {BG_ALT}; border: 1px solid {BORDER}; border-radius: 4px; }}
QLabel#CardValue {{ font-size: 18pt; font-weight: 600; background: transparent; }}
QLabel#CardLabel {{ color: {TEXT_DIM}; font-size: 8pt; background: transparent; }}

QPushButton {{
    background: {BG_RAISED}; border: 1px solid {BORDER}; border-radius: 3px;
    padding: 5px 12px; color: {TEXT};
}}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:pressed {{ background: {ACCENT_DIM}; }}
QPushButton:disabled {{ color: {TEXT_DIM}; border-color: {BG_RAISED}; }}
QPushButton#Primary {{ background: {ACCENT}; border-color: {ACCENT}; color: white; }}
QPushButton#Primary:hover {{ background: #5a9dff; }}
QPushButton#Danger {{ border-color: {ERROR}; color: {ERROR}; }}
QPushButton:checked {{ background: {ACCENT_DIM}; border-color: {ACCENT}; }}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background: {BG_RAISED}; border: 1px solid {BORDER}; border-radius: 3px;
    padding: 4px 6px; selection-background-color: {ACCENT_DIM};
}}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus, QTextEdit:focus {{ border-color: {ACCENT}; }}
QComboBox QAbstractItemView {{ background: {BG_RAISED}; border: 1px solid {BORDER}; selection-background-color: {ACCENT_DIM}; }}
QCheckBox::indicator {{ width: 14px; height: 14px; border: 1px solid {BORDER}; background: {BG_RAISED}; border-radius: 2px; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QRadioButton::indicator {{ width: 12px; height: 12px; border: 1px solid {BORDER}; background: {BG_RAISED}; border-radius: 7px; }}
QRadioButton::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}

QTableView, QTreeView, QListView {{
    background: {BG_ALT}; alternate-background-color: #1d242e; border: 1px solid {BORDER};
    gridline-color: {BORDER}; selection-background-color: {ACCENT_DIM}; selection-color: white;
}}
QHeaderView::section {{
    background: {BG_RAISED}; color: {TEXT_DIM}; border: none; border-right: 1px solid {BORDER};
    border-bottom: 1px solid {BORDER}; padding: 5px 6px; font-weight: 600;
}}
QTableCornerButton::section {{ background: {BG_RAISED}; border: none; }}

QTabWidget::pane {{ border: 1px solid {BORDER}; top: -1px; }}
QTabBar::tab {{ background: {BG_ALT}; color: {TEXT_DIM}; padding: 6px 14px; border: 1px solid {BORDER}; border-bottom: none; }}
QTabBar::tab:selected {{ background: {BG}; color: {TEXT}; border-top: 2px solid {ACCENT}; }}

QGroupBox {{ border: 1px solid {BORDER}; border-radius: 4px; margin-top: 14px; padding-top: 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 8px; padding: 0 4px; color: {TEXT_DIM}; }}

QDockWidget {{ titlebar-close-icon: none; }}
QDockWidget::title {{ background: {BG_RAISED}; padding: 5px 8px; border-bottom: 1px solid {BORDER}; }}
QStatusBar {{ background: {BG_ALT}; border-top: 1px solid {BORDER}; color: {TEXT_DIM}; }}
QStatusBar QLabel {{ background: transparent; padding: 0 8px; }}
QMenuBar {{ background: {BG_ALT}; border-bottom: 1px solid {BORDER}; }}
QMenuBar::item:selected, QMenu::item:selected {{ background: {ACCENT_DIM}; }}
QMenu {{ background: {BG_RAISED}; border: 1px solid {BORDER}; }}
QToolBar {{ background: {BG}; border: none; spacing: 6px; }}
QSplitter::handle {{ background: {BORDER}; }}
QScrollBar:vertical {{ background: {BG}; width: 10px; margin: 0; }}
QScrollBar:horizontal {{ background: {BG}; height: 10px; margin: 0; }}
QScrollBar::handle {{ background: {BORDER}; border-radius: 4px; min-height: 24px; min-width: 24px; }}
QScrollBar::handle:hover {{ background: #3d4a5c; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
"""


STYLESHEET = build_stylesheet()


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    palette = QPalette()
    roles = {
        QPalette.ColorRole.Window: BG, QPalette.ColorRole.WindowText: TEXT,
        QPalette.ColorRole.Base: BG_ALT, QPalette.ColorRole.AlternateBase: "#1d242e",
        QPalette.ColorRole.Text: TEXT, QPalette.ColorRole.Button: BG_RAISED,
        QPalette.ColorRole.ButtonText: TEXT, QPalette.ColorRole.Highlight: ACCENT_DIM,
        QPalette.ColorRole.HighlightedText: "#ffffff", QPalette.ColorRole.ToolTipBase: BG_RAISED,
        QPalette.ColorRole.ToolTipText: TEXT, QPalette.ColorRole.PlaceholderText: TEXT_DIM,
        QPalette.ColorRole.Link: ACCENT,
    }
    for role, color in roles.items():
        palette.setColor(role, QColor(color))
    app.setPalette(palette)
    app.setStyleSheet(build_stylesheet())
