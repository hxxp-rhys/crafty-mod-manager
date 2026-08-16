"""Remote file browser + config editor with conflict detection and backups."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QColor, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..backends.base import BackendError, ConflictError, join, norm, parent_of
from ..manager import ModManager, is_text_file
from ..models import RemoteEntry
from . import theme
from .editor import EditorPane
from .workers import defer, run_task

MAX_EDIT_BYTES = 4 * 1024 * 1024


def _human(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if f < 1024:
            return f"{f:.0f} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} TB"


class FilesTab(QWidget):
    busyChanged = Signal(bool)
    statusMessage = Signal(str)
    progressChanged = Signal(str, int, int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.manager: Optional[ModManager] = None
        self.cwd = ""
        self.entries: list[RemoteEntry] = []
        self.open_path = ""
        self.open_mtime: Optional[float] = None
        self._busy = False
        self._build()

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        nav = QHBoxLayout()
        self.up_btn = QPushButton("↑ Up")
        self.up_btn.clicked.connect(self.go_up)
        self.path_input = QLineEdit()
        self.path_input.setPlaceholderText("server root")
        self.path_input.returnPressed.connect(
            lambda: self.navigate(self.path_input.text())
        )
        self.shortcut_box = QComboBox()
        self.shortcut_box.setMinimumWidth(150)
        self.shortcut_box.activated.connect(
            lambda i: self.navigate(self.shortcut_box.itemData(i) or "")
        )
        self.reload_btn = QPushButton("Reload")
        self.reload_btn.clicked.connect(lambda: self.navigate(self.cwd))
        nav.addWidget(self.up_btn)
        nav.addWidget(self.path_input, 1)
        nav.addWidget(QLabel("Go to"))
        nav.addWidget(self.shortcut_box)
        nav.addWidget(self.reload_btn)
        root.addLayout(nav)

        split = QSplitter(Qt.Orientation.Horizontal)

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(6)
        self.listing = QListWidget()
        self.listing.setAlternatingRowColors(True)
        self.listing.itemDoubleClicked.connect(self._activate)
        self.listing.currentItemChanged.connect(lambda *_: self._update_buttons())
        self.listing.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.listing.customContextMenuRequested.connect(self._context_menu)
        ll.addWidget(self.listing, 1)

        frow = QHBoxLayout()
        self.newfile_btn = QPushButton("New file")
        self.newdir_btn = QPushButton("New folder")
        self.upload_btn = QPushButton("Upload…")
        self.download_btn = QPushButton("Download…")
        self.newfile_btn.clicked.connect(lambda: self._create(False))
        self.newdir_btn.clicked.connect(lambda: self._create(True))
        self.upload_btn.clicked.connect(self.upload)
        self.download_btn.clicked.connect(self.download)
        for b in (self.newfile_btn, self.newdir_btn, self.upload_btn, self.download_btn):
            frow.addWidget(b)
        ll.addLayout(frow)
        split.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(6)

        head = QHBoxLayout()
        self.open_label = QLabel("No file open")
        self.open_label.setStyleSheet(f"color: {theme.FG_DIM};")
        self.save_btn = QPushButton("Save")
        self.save_btn.setProperty("accent", True)
        self.save_btn.clicked.connect(self.save)
        self.revert_btn = QPushButton("Revert")
        self.revert_btn.clicked.connect(self.revert)
        self.history_btn = QPushButton("History…")
        self.history_btn.clicked.connect(self.show_history)
        head.addWidget(self.open_label, 1)
        head.addWidget(self.history_btn)
        head.addWidget(self.revert_btn)
        head.addWidget(self.save_btn)
        rl.addLayout(head)

        self.editor_pane = EditorPane()
        self.editor_pane.editor.dirtyChanged.connect(self._on_dirty)
        rl.addWidget(self.editor_pane, 1)
        split.addWidget(right)

        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 5)
        root.addWidget(split, 1)

        self._update_buttons()

    # ------------------------------------------------------------------ #
    def set_manager(self, manager: Optional[ModManager]) -> None:
        self.manager = manager
        self.listing.clear()
        self.entries = []
        self.cwd = ""
        self._close_editor()
        self.shortcut_box.clear()
        if manager:
            self.shortcut_box.addItem("server root", "")
            self.shortcut_box.addItem("mods", manager.mods_dir)
            for d in manager.profile.config_dirs:
                self.shortcut_box.addItem(d, norm(d))
            self.navigate("")
        self._update_buttons()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.busyChanged.emit(busy)
        self._update_buttons()

    def _update_buttons(self) -> None:
        ready = self.manager is not None and not self._busy
        cur = self.current_entry()
        for b in (self.up_btn, self.reload_btn, self.newfile_btn,
                  self.newdir_btn, self.upload_btn):
            b.setEnabled(ready)
        self.download_btn.setEnabled(ready and cur is not None and not cur.is_dir)
        dirty = self.editor_pane.editor.is_dirty
        self.save_btn.setEnabled(ready and bool(self.open_path) and dirty)
        self.revert_btn.setEnabled(bool(self.open_path) and dirty)
        self.history_btn.setEnabled(ready and bool(self.open_path))

    def _on_dirty(self, dirty: bool) -> None:
        if self.open_path:
            self.open_label.setText(f"{'● ' if dirty else ''}{self.open_path}")
            self.open_label.setStyleSheet(
                f"color: {theme.WARN if dirty else theme.FG_DIM};"
            )
        self._update_buttons()

    # -- navigation --------------------------------------------------------- #
    def navigate(self, path: str) -> None:
        if not self.manager or self._busy:
            return
        target = norm(path)
        self._set_busy(True)
        run_task(
            self.manager.backend.list_dir,
            target,
            on_done=lambda entries: self._show_listing(target, entries),
            on_error=self._error,
            on_finished=lambda: self._set_busy(False),
        )

    def go_up(self) -> None:
        if self.cwd:
            self.navigate(parent_of(self.cwd))

    def _show_listing(self, path: str, entries: list[RemoteEntry]) -> None:
        self.cwd = path
        self.entries = entries
        self.path_input.setText(path)
        self.listing.clear()
        for e in entries:
            label = f"{'📁' if e.is_dir else '📄'}  {e.name}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, e.path)
            if e.is_dir:
                item.setForeground(QColor(theme.ACCENT))
            elif not is_text_file(e.name):
                item.setForeground(QColor(theme.FG_DIM))
            tip = [e.path]
            if not e.is_dir:
                tip.append(e.size_text or _human(e.size))
            if e.modified_text:
                tip.append(e.modified_text)
            item.setToolTip("  ·  ".join(tip))
            self.listing.addItem(item)
        self.statusMessage.emit(f"{len(entries)} item(s) in /{path or ''}")
        self._update_buttons()

    def current_entry(self) -> Optional[RemoteEntry]:
        item = self.listing.currentItem()
        if not item:
            return None
        path = item.data(Qt.ItemDataRole.UserRole)
        return next((e for e in self.entries if e.path == path), None)

    def _activate(self, item: QListWidgetItem) -> None:
        entry = self.current_entry()
        if not entry:
            return
        if entry.is_dir:
            self.navigate(entry.path)
        else:
            self.open_file(entry)

    # -- editing ------------------------------------------------------------ #
    def open_file(self, entry: RemoteEntry) -> None:
        if not self.manager:
            return
        if not self._confirm_discard():
            return
        if entry.size > MAX_EDIT_BYTES:
            QMessageBox.information(
                self,
                "Too large to edit",
                f"{entry.name} is {_human(entry.size)}. Use Download instead.",
            )
            return
        if not is_text_file(entry.name):
            answer = QMessageBox.question(
                self,
                "Open as text?",
                f"{entry.name} doesn't look like a text file. Open it anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._set_busy(True)
        run_task(
            self.manager.read_config,
            entry.path,
            on_done=lambda res: self._loaded(entry, res),
            on_error=self._error,
            on_finished=lambda: self._set_busy(False),
        )

    def _loaded(self, entry: RemoteEntry, result) -> None:
        text, mtime = result
        self.open_path = entry.path
        self.open_mtime = mtime
        ext = entry.name.rsplit(".", 1)[-1] if "." in entry.name else ""
        self.editor_pane.editor.set_content(text, ext)
        self._on_dirty(False)
        self.statusMessage.emit(f"Opened {entry.path}")

    def _close_editor(self) -> None:
        self.open_path = ""
        self.open_mtime = None
        self.editor_pane.editor.set_content("", "")
        self.open_label.setText("No file open")
        self.open_label.setStyleSheet(f"color: {theme.FG_DIM};")

    def save(self, force: bool = False) -> None:
        if not self.manager or not self.open_path:
            return
        text = self.editor_pane.editor.toPlainText()
        original = self.editor_pane.editor.clean_text
        mgr = self.manager
        path, mtime = self.open_path, self.open_mtime

        def work(progress=None):
            return mgr.write_config(
                path,
                text,
                expect_mtime=mtime,
                overwrite=force,
                original=original,
            )

        def done(new_mtime):
            self.open_mtime = new_mtime
            self.editor_pane.editor.mark_clean()
            self.statusMessage.emit(f"Saved {path}")

        def failed(msg: str, tb: str):
            if "changed since" in msg.lower() or "conflict" in msg.lower():
                answer = QMessageBox.question(
                    self,
                    "File changed on the server",
                    f"{path} was modified on the server after you opened it.\n\n"
                    "Overwrite it with your version?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                )
                if answer == QMessageBox.StandardButton.Yes:
                    self.save(force=True)
                return
            self._error(msg, tb)

        self._set_busy(True)
        run_task(
            work,
            on_done=done,
            on_error=failed,
            on_finished=lambda: self._set_busy(False),
        )

    def revert(self) -> None:
        self.editor_pane.editor.setPlainText(self.editor_pane.editor.clean_text)
        self.editor_pane.editor.mark_clean()

    def _confirm_discard(self) -> bool:
        if not self.editor_pane.editor.is_dirty:
            return True
        answer = QMessageBox.question(
            self,
            "Unsaved changes",
            f"You have unsaved changes in {self.open_path}. Discard them?",
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Discard

    # -- history ------------------------------------------------------------ #
    def show_history(self) -> None:
        if not self.manager or not self.open_path:
            return
        entries = self.manager.backups.for_path(self.open_path)
        if not entries:
            QMessageBox.information(
                self,
                "No history",
                f"No local backups of {self.open_path} yet. One is written every "
                "time you save from here.",
            )
            return
        labels = [f"{e['time_text']}  ·  {_human(e['size'])}" for e in entries]
        choice, ok = QInputDialog.getItem(
            self, "Restore a previous version", "Backups:", labels, 0, False
        )
        if not ok:
            return
        entry = entries[labels.index(choice)]
        data = self.manager.backups.read(entry["id"])
        self.editor_pane.editor.setPlainText(data.decode("utf-8", "replace"))
        self.statusMessage.emit(
            f"Loaded backup from {entry['time_text']} into the editor — "
            "press Save to push it to the server."
        )

    # -- file operations ---------------------------------------------------- #
    def _create(self, directory: bool) -> None:
        if not self.manager:
            return
        name, ok = QInputDialog.getText(
            self, "New folder" if directory else "New file", "Name:"
        )
        if not ok or not name.strip():
            return
        name = name.strip()
        mgr = self.manager
        fn = mgr.backend.make_dir if directory else mgr.backend.make_file
        self._set_busy(True)
        run_task(
            fn,
            self.cwd,
            name,
            on_done=lambda _: defer(self.navigate, self.cwd),
            on_error=self._error,
            on_finished=lambda: self._set_busy(False),
        )

    def upload(self) -> None:
        if not self.manager:
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "Upload files")
        if not paths:
            return
        mgr, cwd = self.manager, self.cwd

        def work(progress=None):
            from pathlib import Path

            for i, p in enumerate(paths, 1):
                pp = Path(p)
                if progress:
                    progress(f"Uploading {pp.name}", i, len(paths))
                mgr.backend.upload_bytes(cwd, pp.name, pp.read_bytes())
            return len(paths)

        self._set_busy(True)
        run_task(
            work,
            on_progress=self.progressChanged.emit,
            on_done=lambda n: (self.statusMessage.emit(f"Uploaded {n} file(s)."),
                               defer(self.navigate, self.cwd)),
            on_error=self._error,
            on_finished=lambda: self._set_busy(False),
        )

    def download(self) -> None:
        entry = self.current_entry()
        if not self.manager or not entry or entry.is_dir:
            return
        dest, _ = QFileDialog.getSaveFileName(self, "Save as", entry.name)
        if not dest:
            return
        mgr = self.manager

        def work(progress=None):
            data = mgr.backend.read_bytes(
                entry.path,
                progress=(lambda cur, tot: progress(f"Downloading {entry.name}", cur, tot))
                if progress
                else None,
            )
            with open(dest, "wb") as fh:
                fh.write(data)
            return dest

        self._set_busy(True)
        run_task(
            work,
            on_progress=self.progressChanged.emit,
            on_done=lambda p: self.statusMessage.emit(f"Saved to {p}"),
            on_error=self._error,
            on_finished=lambda: self._set_busy(False),
        )

    def _context_menu(self, pos) -> None:
        entry = self.current_entry()
        if not entry or not self.manager:
            return
        menu = QMenu(self)
        if entry.is_dir:
            a = QAction("Open", self)
            a.triggered.connect(lambda: self.navigate(entry.path))
        else:
            a = QAction("Edit", self)
            a.triggered.connect(lambda: self.open_file(entry))
        menu.addAction(a)

        a = QAction("Rename…", self)
        a.triggered.connect(lambda: self._rename(entry))
        menu.addAction(a)

        if not entry.is_dir:
            a = QAction("Download…", self)
            a.triggered.connect(self.download)
            menu.addAction(a)

        menu.addSeparator()
        a = QAction("Delete", self)
        a.triggered.connect(lambda: self._delete(entry))
        menu.addAction(a)
        menu.exec(self.listing.viewport().mapToGlobal(pos))

    def _rename(self, entry: RemoteEntry) -> None:
        if not self.manager:
            return
        name, ok = QInputDialog.getText(self, "Rename", "New name:", text=entry.name)
        if not ok or not name.strip() or name.strip() == entry.name:
            return
        self._set_busy(True)
        run_task(
            self.manager.backend.rename,
            entry.path,
            name.strip(),
            on_done=lambda _: defer(self.navigate, self.cwd),
            on_error=self._error,
            on_finished=lambda: self._set_busy(False),
        )

    def _delete(self, entry: RemoteEntry) -> None:
        if not self.manager:
            return
        answer = QMessageBox.question(
            self,
            "Delete",
            f"Delete {entry.path}?" + ("\n\nThis removes the whole folder." if entry.is_dir else ""),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._set_busy(True)
        run_task(
            self.manager.backend.delete,
            entry.path,
            on_done=lambda _: (
                defer(self.navigate, self.cwd),
                self._close_editor() if self.open_path == entry.path else None,
            ),
            on_error=self._error,
            on_finished=lambda: self._set_busy(False),
        )

    def _error(self, msg: str, tb: str) -> None:
        QMessageBox.critical(self, "File operation failed", msg)
        self.statusMessage.emit(f"Error: {msg}")
