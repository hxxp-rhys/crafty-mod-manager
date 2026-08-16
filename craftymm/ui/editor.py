"""A small code editor: line numbers, current-line highlight, find bar, and
syntax highlighting for the file types Minecraft servers actually use
(.properties, .toml, .json, .yml, .cfg, .snbt, .js)."""
from __future__ import annotations

import re
from typing import Optional

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QPainter,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextFormat,
)

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import theme

# --------------------------------------------------------------------------- #
FIND_BACKWARD = QTextDocument.FindFlag.FindBackward
FIND_NONE = QTextDocument.FindFlag(0)
CURRENT_LINE_BG = QColor("#1f232b")

C_COMMENT = QColor("#6b7481")
C_KEY = QColor(theme.CYAN)
C_STRING = QColor("#7fd18c")
C_NUMBER = QColor("#e0a33c")
C_BOOL = QColor(theme.PURPLE)
C_SECTION = QColor(theme.ACCENT)
C_PUNCT = QColor("#8b93a1")


def _fmt(color: QColor, bold: bool = False, italic: bool = False) -> QTextCharFormat:
    f = QTextCharFormat()
    f.setForeground(color)
    if bold:
        f.setFontWeight(QFont.Weight.Bold)
    if italic:
        f.setFontItalic(True)
    return f


class ConfigHighlighter(QSyntaxHighlighter):
    """One highlighter that adapts its rule set to the file extension."""

    def __init__(self, document, ext: str = "") -> None:
        super().__init__(document)
        self.rules: list[tuple[re.Pattern, QTextCharFormat, int]] = []
        self.set_extension(ext)

    def set_extension(self, ext: str) -> None:
        ext = (ext or "").lower().lstrip(".")
        self.ext = ext
        r: list[tuple[re.Pattern, QTextCharFormat, int]] = []

        comment = _fmt(C_COMMENT, italic=True)
        string = _fmt(C_STRING)
        number = _fmt(C_NUMBER)
        boolean = _fmt(C_BOOL, bold=True)
        key = _fmt(C_KEY)
        section = _fmt(C_SECTION, bold=True)

        if ext in ("json", "json5", "mcmeta"):
            r += [
                (re.compile(r'"(?:[^"\\]|\\.)*"\s*(?=:)'), key, 0),
                (re.compile(r'"(?:[^"\\]|\\.)*"'), string, 0),
                (re.compile(r"\b-?\d+(\.\d+)?([eE][+-]?\d+)?\b"), number, 0),
                (re.compile(r"\b(true|false|null)\b"), boolean, 0),
                (re.compile(r"//[^\n]*"), comment, 0),
            ]
        elif ext in ("toml", "tml"):
            r += [
                (re.compile(r"^\s*\[\[?[^\]]+\]\]?"), section, 0),
                (re.compile(r"^\s*([A-Za-z0-9_.\-\"]+)\s*="), key, 1),
                (re.compile(r'"""(?:.|\n)*?"""|"(?:[^"\\]|\\.)*"|\'[^\']*\''), string, 0),
                (re.compile(r"\b-?\d+(\.\d+)?\b"), number, 0),
                (re.compile(r"\b(true|false)\b"), boolean, 0),
                (re.compile(r"#[^\n]*"), comment, 0),
            ]
        elif ext in ("yml", "yaml"):
            r += [
                (re.compile(r"^\s*-?\s*([A-Za-z0-9_.\-\"']+)\s*:"), key, 1),
                (re.compile(r'"(?:[^"\\]|\\.)*"|\'[^\']*\''), string, 0),
                (re.compile(r"\b-?\d+(\.\d+)?\b"), number, 0),
                (re.compile(r"\b(true|false|yes|no|null|~)\b", re.I), boolean, 0),
                (re.compile(r"#[^\n]*"), comment, 0),
            ]
        elif ext in ("properties", "lang", "ini", "cfg", "conf"):
            r += [
                (re.compile(r"^\s*\[[^\]]+\]"), section, 0),
                (re.compile(r"^\s*([^#!=:\n]+?)\s*(?==|:)"), key, 1),
                (re.compile(r"\b-?\d+(\.\d+)?\b"), number, 0),
                (re.compile(r"\b(true|false)\b", re.I), boolean, 0),
                (re.compile(r"^\s*[#!][^\n]*"), comment, 0),
            ]
        elif ext in ("js", "ts", "mjs"):
            r += [
                (
                    re.compile(
                        r"\b(const|let|var|function|return|if|else|for|while|new|"
                        r"class|export|import|from|of|in|=>|await|async)\b"
                    ),
                    boolean,
                    0,
                ),
                (re.compile(r'"(?:[^"\\]|\\.)*"|\'[^\']*\'|`[^`]*`'), string, 0),
                (re.compile(r"\b-?\d+(\.\d+)?\b"), number, 0),
                (re.compile(r"//[^\n]*"), comment, 0),
            ]
        elif ext in ("snbt", "nbt"):
            r += [
                (re.compile(r"[A-Za-z0-9_\-]+(?=\s*:)"), key, 0),
                (re.compile(r'"(?:[^"\\]|\\.)*"'), string, 0),
                (re.compile(r"\b-?\d+(\.\d+)?[bslfdBSLFD]?\b"), number, 0),
                (re.compile(r"\b(true|false)\b"), boolean, 0),
                (re.compile(r"#[^\n]*"), comment, 0),
            ]
        elif ext in ("sh", "bat", "cmd", "mcfunction"):
            r += [
                (re.compile(r'"(?:[^"\\]|\\.)*"|\'[^\']*\''), string, 0),
                (re.compile(r"\b-?\d+\b"), number, 0),
                (re.compile(r"(^\s*(#|::|REM\b)[^\n]*)", re.I), comment, 0),
            ]
        else:  # txt/log/md and anything unknown
            r += [
                (re.compile(r"^\s*[#!][^\n]*"), comment, 0),
                (re.compile(r'"(?:[^"\\]|\\.)*"'), string, 0),
            ]

        self.rules = r
        self.rehighlight()

    def highlightBlock(self, text: str) -> None:  # noqa: N802 (Qt override)
        for pattern, fmt, group in self.rules:
            for m in pattern.finditer(text):
                try:
                    start, end = m.span(group)
                except (IndexError, re.error):  # pragma: no cover
                    continue
                if start >= 0:
                    self.setFormat(start, end - start, fmt)


# --------------------------------------------------------------------------- #
class _LineNumbers(QWidget):
    def __init__(self, editor: "CodeEditor") -> None:
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self.editor.line_number_width(), 0)

    def paintEvent(self, event):  # noqa: N802
        self.editor.paint_line_numbers(event)


class CodeEditor(QPlainTextEdit):
    dirtyChanged = Signal(bool)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        font.setPointSize(10)
        self.setFont(font)
        self.setTabStopDistance(4 * self.fontMetrics().horizontalAdvance(" "))
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

        self._numbers = _LineNumbers(self)
        self.blockCountChanged.connect(lambda _: self._update_margin())
        self.updateRequest.connect(self._update_numbers)
        self.cursorPositionChanged.connect(self._highlight_current_line)
        self._update_margin()
        self._highlight_current_line()

        self.highlighter = ConfigHighlighter(self.document())
        self._clean_text = ""
        self.textChanged.connect(self._check_dirty)

    # -- dirty tracking -------------------------------------------------- #
    def set_content(self, text: str, ext: str = "") -> None:
        self.highlighter.set_extension(ext)
        self.setPlainText(text)
        self._clean_text = text
        self.dirtyChanged.emit(False)

    def mark_clean(self) -> None:
        self._clean_text = self.toPlainText()
        self.dirtyChanged.emit(False)

    @property
    def is_dirty(self) -> bool:
        return self.toPlainText() != self._clean_text

    @property
    def clean_text(self) -> str:
        return self._clean_text

    def _check_dirty(self) -> None:
        self.dirtyChanged.emit(self.is_dirty)

    # -- line numbers ---------------------------------------------------- #
    def line_number_width(self) -> int:
        digits = max(3, len(str(max(1, self.blockCount()))))
        return 14 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_margin(self) -> None:
        self.setViewportMargins(self.line_number_width(), 0, 0, 0)

    def _update_numbers(self, rect: QRect, dy: int) -> None:
        if dy:
            self._numbers.scroll(0, dy)
        else:
            self._numbers.update(0, rect.y(), self._numbers.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_margin()

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        cr = self.contentsRect()
        self._numbers.setGeometry(
            QRect(cr.left(), cr.top(), self.line_number_width(), cr.height())
        )

    def paint_line_numbers(self, event) -> None:
        painter = QPainter(self._numbers)
        painter.fillRect(event.rect(), QColor(theme.BG_ALT))
        block = self.firstVisibleBlock()
        number = block.blockNumber()
        top = self.blockBoundingGeometry(block).translated(self.contentOffset()).top()
        bottom = top + self.blockBoundingRect(block).height()
        current = self.textCursor().blockNumber()
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.setPen(
                    QColor(theme.FG) if number == current else QColor("#4d5561")
                )
                painter.drawText(
                    0,
                    int(top),
                    self._numbers.width() - 7,
                    self.fontMetrics().height(),
                    Qt.AlignmentFlag.AlignRight,
                    str(number + 1),
                )
            block = block.next()
            top = bottom
            bottom = top + self.blockBoundingRect(block).height()
            number += 1

    def _highlight_current_line(self) -> None:
        sel = QTextEdit.ExtraSelection()
        sel.format.setBackground(CURRENT_LINE_BG)
        sel.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
        sel.cursor = self.textCursor()
        sel.cursor.clearSelection()
        self.setExtraSelections([sel])


# --------------------------------------------------------------------------- #
class EditorPane(QWidget):
    """CodeEditor + a find bar."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.editor = CodeEditor(self)

        self.find_input = QLineEdit(self)
        self.find_input.setPlaceholderText("Find…")
        self.find_input.setClearButtonEnabled(True)
        self.find_input.returnPressed.connect(self.find_next)
        prev_btn = QPushButton("Prev", self)
        next_btn = QPushButton("Next", self)
        for b, tip in ((prev_btn, "Find previous (Shift+Enter)"), (next_btn, "Find next (Enter)")):
            b.setFixedWidth(62)
            b.setToolTip(tip)
        prev_btn.clicked.connect(self.find_prev)
        next_btn.clicked.connect(self.find_next)
        self.find_status = QLabel("", self)
        self.find_status.setStyleSheet(f"color: {theme.FG_DIM};")

        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.addWidget(self.find_input, 1)
        bar.addWidget(prev_btn)
        bar.addWidget(next_btn)
        bar.addWidget(self.find_status)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addLayout(bar)
        layout.addWidget(self.editor, 1)

    def find_next(self) -> None:
        self._find(False)

    def find_prev(self) -> None:
        self._find(True)

    def _find(self, backwards: bool) -> None:
        needle = self.find_input.text()
        if not needle:
            self.find_status.setText("")
            return
        flags = FIND_BACKWARD if backwards else FIND_NONE
        found = self.editor.find(needle, flags)
        if not found:  # wrap around
            cursor = self.editor.textCursor()
            cursor.movePosition(
                QTextCursor.MoveOperation.End
                if backwards
                else QTextCursor.MoveOperation.Start
            )
            self.editor.setTextCursor(cursor)
            found = self.editor.find(needle, flags)
        self.find_status.setText("" if found else "no match")
