"""A single dark stylesheet, plus the colour tokens the editor reuses."""
from __future__ import annotations

# --- tokens ---------------------------------------------------------------- #
BG = "#16181d"
BG_ALT = "#1c1f26"
BG_RAISED = "#22262f"
BORDER = "#2e333d"
FG = "#e4e7ec"
FG_DIM = "#9aa3b0"
ACCENT = "#4f8cff"
ACCENT_DIM = "#3a6cc9"
OK = "#3fbf7f"
WARN = "#e0a33c"
ERR = "#e5534b"
PURPLE = "#b085f5"
CYAN = "#4cc9d9"

STYLESHEET = f"""
QWidget {{
    background: {BG};
    color: {FG};
    font-family: "Segoe UI", "Inter", system-ui, sans-serif;
    font-size: 10pt;
}}
QMainWindow, QDialog {{ background: {BG}; }}

QToolBar {{
    background: {BG_ALT};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 4px 6px;
    spacing: 4px;
}}

QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 6px; top: -1px; }}
QTabBar::tab {{
    background: transparent;
    color: {FG_DIM};
    padding: 8px 16px;
    border: 1px solid transparent;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
}}
QTabBar::tab:selected {{
    background: {BG_ALT};
    color: {FG};
    border-color: {BORDER};
    border-bottom-color: {BG_ALT};
}}
QTabBar::tab:hover:!selected {{ color: {FG}; }}

QPushButton {{
    background: {BG_RAISED};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 6px 14px;
    color: {FG};
}}
QPushButton:hover {{ border-color: {ACCENT_DIM}; }}
QPushButton:pressed {{ background: {BG_ALT}; }}
QPushButton:disabled {{ color: #5d646f; border-color: #262a32; }}
QPushButton[accent="true"] {{
    background: {ACCENT}; border-color: {ACCENT}; color: #ffffff; font-weight: 600;
}}
QPushButton[accent="true"]:hover {{ background: {ACCENT_DIM}; }}
QPushButton[danger="true"]:hover {{ border-color: {ERR}; color: {ERR}; }}

QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QComboBox {{
    background: {BG_ALT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 5px 8px;
    selection-background-color: {ACCENT_DIM};
}}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus {{
    border-color: {ACCENT};
}}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{
    background: {BG_ALT}; border: 1px solid {BORDER};
    selection-background-color: {ACCENT_DIM};
}}

QTableView, QTreeView, QListView {{
    background: {BG_ALT};
    alternate-background-color: #1a1d24;
    border: 1px solid {BORDER};
    border-radius: 6px;
    gridline-color: {BORDER};
    selection-background-color: {ACCENT_DIM};
    selection-color: #ffffff;
}}
QHeaderView::section {{
    background: {BG_RAISED};
    color: {FG_DIM};
    padding: 6px 8px;
    border: none;
    border-right: 1px solid {BORDER};
    border-bottom: 1px solid {BORDER};
    font-weight: 600;
}}

QGroupBox {{
    border: 1px solid {BORDER};
    border-radius: 8px;
    margin-top: 14px;
    padding-top: 8px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin; left: 10px; padding: 0 6px; color: {FG_DIM};
}}

QStatusBar {{ background: {BG_ALT}; border-top: 1px solid {BORDER}; color: {FG_DIM}; }}
QStatusBar::item {{ border: none; }}

QProgressBar {{
    background: {BG_ALT}; border: 1px solid {BORDER};
    border-radius: 6px; text-align: center; height: 16px;
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 5px; }}

QScrollBar:vertical {{ background: transparent; width: 12px; margin: 0; }}
QScrollBar::handle:vertical {{
    background: #333944; border-radius: 6px; min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: #414855; }}
QScrollBar:horizontal {{ background: transparent; height: 12px; }}
QScrollBar::handle:horizontal {{ background: #333944; border-radius: 6px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

QSplitter::handle {{ background: {BORDER}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}

QCheckBox::indicator, QRadioButton::indicator {{
    width: 15px; height: 15px; border: 1px solid {BORDER};
    border-radius: 4px; background: {BG_ALT};
}}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}

QMenu {{ background: {BG_RAISED}; border: 1px solid {BORDER}; padding: 4px; }}
QMenu::item {{ padding: 6px 24px 6px 12px; border-radius: 4px; }}
QMenu::item:selected {{ background: {ACCENT_DIM}; }}

QToolTip {{
    background: {BG_RAISED}; color: {FG};
    border: 1px solid {BORDER}; padding: 4px 6px;
}}
"""
