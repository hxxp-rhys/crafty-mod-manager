"""Backups browser and app settings."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..config import KEYRING_OK, Settings, app_dir
from ..manager import ModManager
from . import theme
from .workers import run_task

COLS = ["When", "Kind", "Remote path", "Size", "Note"]


def _human(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if f < 1024:
            return f"{f:.0f} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} TB"


class BackupsDialog(QDialog):
    def __init__(self, manager: ModManager, parent=None) -> None:
        super().__init__(parent)
        self.manager = manager
        self.setWindowTitle("Local backups")
        self.resize(880, 520)
        self.entries = manager.backups.all()

        root = QVBoxLayout(self)
        root.setSpacing(10)

        blurb = QLabel(
            "Every jar this app replaced or removed, and every config it saved, is "
            "copied here first. Restoring uploads the saved copy back to the server."
        )
        blurb.setWordWrap(True)
        blurb.setStyleSheet(f"color: {theme.FG_DIM};")
        root.addWidget(blurb)

        self.table = QTableWidget(len(self.entries), len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for c in (0, 1, 3, 4):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)

        for row, e in enumerate(self.entries):
            self.table.setItem(row, 0, QTableWidgetItem(e["time_text"]))
            self.table.setItem(row, 1, QTableWidgetItem(e["kind"]))
            self.table.setItem(row, 2, QTableWidgetItem(e["remote_path"]))
            self.table.setItem(row, 3, QTableWidgetItem(_human(e["size"])))
            self.table.setItem(row, 4, QTableWidgetItem(e.get("note", "")))
        root.addWidget(self.table, 1)

        self.status = QLabel("")
        self.status.setStyleSheet(f"color: {theme.FG_DIM};")
        root.addWidget(self.status)

        buttons = QDialogButtonBox()
        restore = QPushButton("Restore to server")
        restore.setProperty("accent", True)
        restore.clicked.connect(self.restore)
        save_as = QPushButton("Save a copy…")
        save_as.clicked.connect(self.save_copy)
        open_folder = QPushButton("Open backups folder")
        open_folder.clicked.connect(self.open_folder)
        buttons.addButton(restore, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(save_as, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(open_folder, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _selected(self) -> Optional[dict]:
        row = self.table.currentRow()
        return self.entries[row] if 0 <= row < len(self.entries) else None

    def restore(self) -> None:
        entry = self._selected()
        if not entry:
            return
        answer = QMessageBox.question(
            self,
            "Restore",
            f"Upload the {entry['time_text']} copy back to\n{entry['remote_path']}?\n\n"
            "Whatever is there now will be overwritten.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.status.setText("Restoring…")
        run_task(
            self.manager.restore_backup,
            entry["id"],
            on_done=lambda p: self.status.setText(f"✓ Restored {p}"),
            on_error=lambda msg, tb: self.status.setText(f"✕ {msg}"),
        )

    def save_copy(self) -> None:
        entry = self._selected()
        if not entry:
            return
        name = entry["remote_path"].rsplit("/", 1)[-1]
        dest, _ = QFileDialog.getSaveFileName(self, "Save a copy", name)
        if not dest:
            return
        Path(dest).write_bytes(self.manager.backups.read(entry["id"]))
        self.status.setText(f"✓ Saved to {dest}")

    def open_folder(self) -> None:
        path = str(self.manager.backups.root)
        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer", path])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except OSError as exc:
            self.status.setText(f"Could not open {path}: {exc}")


# --------------------------------------------------------------------------- #
class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Settings")
        self.setMinimumWidth(520)

        root = QVBoxLayout(self)
        root.setSpacing(12)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.cf_key = QLineEdit(settings.get_curseforge_key())
        self.cf_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.cf_key.setPlaceholderText("paste your CurseForge API key")
        form.addRow("CurseForge API key", self.cf_key)

        hint = QLabel(
            "Free key from <a href='https://console.curseforge.com/'>"
            "console.curseforge.com</a> → API Keys. Modrinth needs no key."
        )
        hint.setOpenExternalLinks(True)
        hint.setStyleSheet(f"color: {theme.FG_DIM};")
        form.addRow("", hint)

        self.backup_configs = QCheckBox("Back up config files before saving")
        self.backup_configs.setChecked(settings.backup_configs)
        self.backup_mods = QCheckBox("Back up mod jars before replacing or removing")
        self.backup_mods.setChecked(settings.backup_mods)
        form.addRow("", self.backup_configs)
        form.addRow("", self.backup_mods)

        self.keep = QSpinBox()
        self.keep.setRange(1, 200)
        self.keep.setValue(settings.keep_backups)
        self.keep.setMaximumWidth(100)
        form.addRow("Backups kept per file", self.keep)

        root.addLayout(form)

        where = QLabel(
            f"Settings and backups live in <code>{app_dir()}</code>.<br>"
            + (
                "Secrets are stored in the Windows Credential Manager."
                if KEYRING_OK
                else f"<span style='color:{theme.WARN}'>The OS credential store isn't "
                "available, so secrets are kept in a local file.</span>"
            )
        )
        where.setWordWrap(True)
        where.setStyleSheet(f"color: {theme.FG_DIM};")
        root.addWidget(where)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _save(self) -> None:
        self.settings.set_curseforge_key(self.cf_key.text().strip())
        self.settings.backup_configs = self.backup_configs.isChecked()
        self.settings.backup_mods = self.backup_mods.isChecked()
        self.settings.keep_backups = self.keep.value()
        self.settings.save()
        self.accept()
