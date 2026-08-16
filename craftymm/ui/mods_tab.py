"""Installed-mods view: scan, identify, update, enable/disable, pin, remove."""
from __future__ import annotations

import webbrowser
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..manager import ModManager
from ..models import ModEntry, VersionInfo
from . import theme
from .workers import defer, run_task

COLS = ["Mod", "File", "Installed", "Latest", "Loader", "MC", "Source", "Status"]
C_NAME, C_FILE, C_CUR, C_NEW, C_LOADER, C_MC, C_SRC, C_STATUS = range(8)


def _human(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if f < 1024:
            return f"{f:.0f} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} TB"


class ModsTab(QWidget):
    busyChanged = Signal(bool)
    statusMessage = Signal(str)
    progressChanged = Signal(str, int, int)
    modsChanged = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.manager: Optional[ModManager] = None
        self._busy = False
        self._build()

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        # --- action bar
        bar = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh")
        self.identify_btn = QPushButton("Identify + check updates")
        self.identify_btn.setProperty("accent", True)
        self.update_btn = QPushButton("Update selected")
        self.update_all_btn = QPushButton("Update all")
        self.toggle_btn = QPushButton("Enable / disable")
        self.pin_btn = QPushButton("Pin / unpin")
        self.install_btn = QPushButton("Install from file…")
        self.delete_btn = QPushButton("Remove")
        self.delete_btn.setProperty("danger", True)

        self.refresh_btn.clicked.connect(self.refresh)
        self.identify_btn.clicked.connect(self.identify_and_check)
        self.update_btn.clicked.connect(self.update_selected)
        self.update_all_btn.clicked.connect(self.update_all)
        self.toggle_btn.clicked.connect(self.toggle_selected)
        self.pin_btn.clicked.connect(self.pin_selected)
        self.install_btn.clicked.connect(self.install_from_file)
        self.delete_btn.clicked.connect(self.delete_selected)

        for b in (
            self.refresh_btn,
            self.identify_btn,
            self.update_btn,
            self.update_all_btn,
            self.toggle_btn,
            self.pin_btn,
            self.install_btn,
        ):
            bar.addWidget(b)
        bar.addStretch(1)
        bar.addWidget(self.delete_btn)
        root.addLayout(bar)

        # --- filter row
        frow = QHBoxLayout()
        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText("Filter by name, file, mod id…")
        self.filter_input.setClearButtonEnabled(True)
        self.filter_input.textChanged.connect(self._apply_filter)
        self.only_updates = QCheckBox("Only mods with updates")
        self.only_updates.toggled.connect(self._apply_filter)
        self.only_disabled = QCheckBox("Only disabled")
        self.only_disabled.toggled.connect(self._apply_filter)
        frow.addWidget(self.filter_input, 1)
        frow.addWidget(self.only_updates)
        frow.addWidget(self.only_disabled)
        root.addLayout(frow)

        # --- table
        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(28)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.doubleClicked.connect(lambda _: self._open_page())

        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(C_NAME, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(C_FILE, QHeaderView.ResizeMode.Interactive)
        for c in (C_CUR, C_NEW, C_LOADER, C_MC, C_SRC, C_STATUS):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setColumnWidth(C_FILE, 260)
        root.addWidget(self.table, 1)

        self.summary = QLabel("Not connected.")
        self.summary.setStyleSheet(f"color: {theme.FG_DIM};")
        root.addWidget(self.summary)

        self._update_buttons()

    # ------------------------------------------------------------------ #
    def set_manager(self, manager: Optional[ModManager]) -> None:
        self.manager = manager
        self.table.setRowCount(0)
        self.summary.setText("Not connected." if manager is None else "Press Refresh.")
        self._update_buttons()

    @property
    def mods(self) -> list[ModEntry]:
        return self.manager.mods if self.manager else []

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.busyChanged.emit(busy)
        self._update_buttons()

    def _update_buttons(self) -> None:
        has_mgr = self.manager is not None and not self._busy
        sel = self.selected_mods()
        self.refresh_btn.setEnabled(has_mgr)
        self.identify_btn.setEnabled(has_mgr and bool(self.mods))
        self.install_btn.setEnabled(has_mgr)
        self.toggle_btn.setEnabled(has_mgr and bool(sel))
        self.pin_btn.setEnabled(has_mgr and bool(sel))
        self.delete_btn.setEnabled(has_mgr and bool(sel))
        self.update_btn.setEnabled(
            has_mgr and any(m.update_available for m in sel)
        )
        self.update_all_btn.setEnabled(
            has_mgr and any(m.update_available for m in self.mods)
        )

    # -- data flow -------------------------------------------------------- #
    def refresh(self) -> None:
        if not self.manager or self._busy:
            return
        self._set_busy(True)
        self.statusMessage.emit("Scanning the mods folder…")
        run_task(
            self.manager.scan,
            on_progress=self.progressChanged.emit,
            on_done=lambda _: (self._populate(), self.statusMessage.emit(
                f"Found {len(self.mods)} mod(s).")),
            on_error=self._error,
            on_finished=lambda: self._set_busy(False),
        )

    def identify_and_check(self) -> None:
        if not self.manager or self._busy:
            return
        self._set_busy(True)
        mgr = self.manager

        def work(progress=None):
            matched = mgr.identify(progress=progress)
            mgr.resolve_names(progress=progress)
            updates = mgr.check_updates(progress=progress)
            return matched, updates

        def done(result):
            matched, updates = result
            self._populate()
            unknown = sum(1 for m in self.mods if not m.source)
            msg = f"Identified {matched} new mod(s); {updates} update(s) available."
            if unknown:
                msg += f" {unknown} jar(s) couldn't be matched to a platform."
            self.statusMessage.emit(msg)

        run_task(
            work,
            on_progress=self.progressChanged.emit,
            on_done=done,
            on_error=self._error,
            on_finished=lambda: self._set_busy(False),
        )

    # -- updates ---------------------------------------------------------- #
    def update_selected(self) -> None:
        self._do_updates([m for m in self.selected_mods() if m.update_available])

    def update_all(self) -> None:
        self._do_updates([m for m in self.mods if m.update_available])

    def _do_updates(self, targets: list[ModEntry]) -> None:
        if not self.manager or not targets or self._busy:
            return
        lines = "\n".join(
            f"  • {m.display_name}:  {m.current_version}  →  {m.latest_version_number}"
            for m in targets[:25]
        )
        more = f"\n  … and {len(targets) - 25} more" if len(targets) > 25 else ""
        answer = QMessageBox.question(
            self,
            "Update mods",
            f"Update {len(targets)} mod(s)?\n\n{lines}{more}\n\n"
            "The current jars are copied to local backups first, so you can roll "
            "back from the Backups dialog.\n\n"
            "Stop the server before updating — swapping jars on a running server "
            "usually corrupts the world or crashes it.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        mgr = self.manager
        provider_map = mgr.providers
        loader, mcv = mgr.loader, mgr.mc_version

        def work(progress=None):
            ok, failed = [], []
            for i, m in enumerate(targets, 1):
                if progress:
                    progress(f"Updating {m.display_name}", i, len(targets))
                try:
                    prov = provider_map.get(m.source)
                    if not prov:
                        raise RuntimeError(f"no provider for '{m.source}'")
                    version = self._resolve_version(prov, m, loader, mcv)
                    if version is None:
                        raise RuntimeError("could not resolve the target version")
                    mgr.install_version(version, replace=m)
                    ok.append(m.display_name)
                except Exception as exc:  # surface per-mod, keep going
                    failed.append(f"{m.display_name}: {exc}")
            return ok, failed

        self._set_busy(True)
        run_task(
            work,
            on_progress=self.progressChanged.emit,
            on_done=self._updates_done,
            on_error=self._error,
            on_finished=lambda: self._set_busy(False),
        )

    @staticmethod
    def _resolve_version(prov, mod: ModEntry, loader: str, mcv: str):
        if mod.source == "modrinth":
            return prov.version_by_id(mod.latest_version_id)
        versions = prov.versions(mod.project_id, loader, mcv)
        return next(
            (v for v in versions if v.version_id == mod.latest_version_id),
            versions[0] if versions else None,
        )

    def _updates_done(self, result) -> None:
        ok, failed = result
        if failed:
            QMessageBox.warning(
                self,
                "Some updates failed",
                f"Updated {len(ok)} mod(s).\n\nFailed:\n" + "\n".join(failed[:15]),
            )
        self.statusMessage.emit(f"Updated {len(ok)} mod(s).")
        self.modsChanged.emit()
        defer(self.refresh)

    # -- simple mutations -------------------------------------------------- #
    def toggle_selected(self) -> None:
        sel = self.selected_mods()
        if not self.manager or not sel:
            return
        mgr = self.manager
        target_state = not all(m.disabled for m in sel)

        def work(progress=None):
            for i, m in enumerate(sel, 1):
                if progress:
                    progress(f"{'Disabling' if target_state else 'Enabling'} {m.filename}",
                             i, len(sel))
                mgr.set_disabled(m, target_state)
            return len(sel)

        self._set_busy(True)
        run_task(
            work,
            on_progress=self.progressChanged.emit,
            on_done=lambda n: (
                self.statusMessage.emit(
                    f"{'Disabled' if target_state else 'Enabled'} {n} mod(s)."
                ),
                defer(self.refresh),
            ),
            on_error=self._error,
            on_finished=lambda: self._set_busy(False),
        )

    def pin_selected(self) -> None:
        sel = self.selected_mods()
        if not self.manager or not sel:
            return
        target = not all(m.pinned for m in sel)
        for m in sel:
            self.manager.set_pinned(m, target)
        self._populate()
        self.statusMessage.emit(
            f"{'Pinned' if target else 'Unpinned'} {len(sel)} mod(s). "
            "Pinned mods are skipped by update checks."
        )

    def delete_selected(self) -> None:
        sel = self.selected_mods()
        if not self.manager or not sel:
            return
        names = "\n".join(f"  • {m.filename}" for m in sel[:20])
        more = f"\n  … and {len(sel) - 20} more" if len(sel) > 20 else ""
        answer = QMessageBox.question(
            self,
            "Remove mods",
            f"Delete {len(sel)} file(s) from the server?\n\n{names}{more}\n\n"
            "A copy is kept in local backups first.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        mgr = self.manager

        def work(progress=None):
            for i, m in enumerate(sel, 1):
                if progress:
                    progress(f"Removing {m.filename}", i, len(sel))
                mgr.delete_mod(m)
            return len(sel)

        self._set_busy(True)
        run_task(
            work,
            on_progress=self.progressChanged.emit,
            on_done=lambda n: (self.statusMessage.emit(f"Removed {n} mod(s)."),
                               defer(self.refresh)),
            on_error=self._error,
            on_finished=lambda: self._set_busy(False),
        )

    def install_from_file(self) -> None:
        if not self.manager:
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Choose mod jars", "", "Mod jars (*.jar);;All files (*)"
        )
        if not paths:
            return
        mgr = self.manager

        def work(progress=None):
            for i, p in enumerate(paths, 1):
                if progress:
                    progress(f"Uploading {p}", i, len(paths))
                mgr.install_local_jar(p)
            return len(paths)

        self._set_busy(True)
        run_task(
            work,
            on_progress=self.progressChanged.emit,
            on_done=lambda n: (self.statusMessage.emit(f"Uploaded {n} jar(s)."),
                               defer(self.refresh)),
            on_error=self._error,
            on_finished=lambda: self._set_busy(False),
        )

    # -- table ------------------------------------------------------------ #
    def _populate(self) -> None:
        mods = sorted(self.mods, key=lambda m: m.display_name.lower())
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(mods))
        for row, m in enumerate(mods):
            self._fill_row(row, m)
        self.table.horizontalHeader().setSortIndicator(
            C_NAME, Qt.SortOrder.AscendingOrder
        )
        self.table.setSortingEnabled(True)
        self._apply_filter()

        n_up = sum(1 for m in mods if m.update_available)
        n_dis = sum(1 for m in mods if m.disabled)
        n_unk = sum(1 for m in mods if not m.source)
        mgr = self.manager
        env = ""
        if mgr:
            env = (
                f"  ·  loader: {mgr.loader or 'unknown'}"
                f"  ·  MC: {mgr.mc_version or 'unknown'}"
            )
        self.summary.setText(
            f"{len(mods)} mods  ·  {n_up} updatable  ·  {n_dis} disabled  "
            f"·  {n_unk} unidentified{env}"
        )
        self._update_buttons()

    def _fill_row(self, row: int, m: ModEntry) -> None:
        def item(text: str, colour: Optional[str] = None, bold: bool = False):
            it = QTableWidgetItem(text)
            if colour:
                it.setForeground(QColor(colour))
            if bold:
                f = it.font()
                f.setBold(True)
                it.setFont(f)
            return it

        name_item = item(m.display_name, bold=m.update_available)
        name_item.setData(Qt.ItemDataRole.UserRole, m.filename)
        tip = [f"<b>{m.display_name}</b>"]
        if m.meta.mod_id:
            tip.append(f"mod id: <code>{m.meta.mod_id}</code>")
        if m.meta.description:
            tip.append(m.meta.description[:300])
        if m.meta.depends:
            tip.append("depends on: " + ", ".join(m.meta.depends[:12]))
        tip.append(f"{_human(m.size)} · {m.sha1[:12]}…")
        if m.meta.error:
            tip.append(f"<span style='color:{theme.WARN}'>{m.meta.error}</span>")
        name_item.setToolTip("<br>".join(tip))

        if m.disabled:
            name_item.setForeground(QColor(theme.FG_DIM))

        status, colour = self._status_for(m)

        self.table.setItem(row, C_NAME, name_item)
        self.table.setItem(row, C_FILE, item(m.filename, theme.FG_DIM))
        self.table.setItem(row, C_CUR, item(m.current_version))
        self.table.setItem(
            row,
            C_NEW,
            item(
                m.latest_version_number if m.update_available else "",
                theme.OK if m.update_available else None,
                bold=m.update_available,
            ),
        )
        self.table.setItem(row, C_LOADER, item(m.meta.loader or "?", theme.FG_DIM))
        self.table.setItem(
            row, C_MC, item(", ".join(m.meta.mc_versions[:2]), theme.FG_DIM)
        )
        self.table.setItem(
            row,
            C_SRC,
            item(
                {"modrinth": "Modrinth", "curseforge": "CurseForge"}.get(m.source, "—"),
                theme.FG_DIM,
            ),
        )
        self.table.setItem(row, C_STATUS, item(status, colour))

    @staticmethod
    def _status_for(m: ModEntry) -> tuple[str, str]:
        if m.meta.error:
            return "unreadable", theme.ERR
        if m.disabled:
            return "disabled", theme.FG_DIM
        if m.pinned:
            return "pinned", theme.PURPLE
        if m.update_available:
            return "update", theme.OK
        if not m.source:
            return "unknown", theme.WARN
        return "current", theme.FG_DIM

    def _apply_filter(self) -> None:
        needle = self.filter_input.text().strip().lower()
        only_up = self.only_updates.isChecked()
        only_dis = self.only_disabled.isChecked()
        by_file = {m.filename: m for m in self.mods}
        for row in range(self.table.rowCount()):
            name_item = self.table.item(row, C_NAME)
            if name_item is None:
                continue
            m = by_file.get(name_item.data(Qt.ItemDataRole.UserRole))
            if m is None:
                continue
            hay = " ".join(
                [m.display_name, m.filename, m.meta.mod_id, m.meta.name, m.source]
            ).lower()
            visible = (not needle or needle in hay)
            if only_up and not m.update_available:
                visible = False
            if only_dis and not m.disabled:
                visible = False
            self.table.setRowHidden(row, not visible)

    def selected_mods(self) -> list[ModEntry]:
        by_file = {m.filename: m for m in self.mods}
        out: list[ModEntry] = []
        for idx in self.table.selectionModel().selectedRows() if self.table.selectionModel() else []:
            it = self.table.item(idx.row(), C_NAME)
            if it:
                m = by_file.get(it.data(Qt.ItemDataRole.UserRole))
                if m:
                    out.append(m)
        return out

    # -- context menu ------------------------------------------------------ #
    def _context_menu(self, pos) -> None:
        sel = self.selected_mods()
        if not sel:
            return
        menu = QMenu(self)
        act_page = QAction("Open project page", self)
        act_page.triggered.connect(self._open_page)
        act_page.setEnabled(bool(sel[0].page_url))
        menu.addAction(act_page)
        menu.addSeparator()
        for label, slot in (
            ("Update", self.update_selected),
            ("Enable / disable", self.toggle_selected),
            ("Pin / unpin", self.pin_selected),
        ):
            a = QAction(label, self)
            a.triggered.connect(slot)
            menu.addAction(a)
        menu.addSeparator()
        a = QAction("Copy SHA-1", self)
        a.triggered.connect(lambda: self._copy(sel[0].sha1))
        menu.addAction(a)
        a = QAction("Remove from server", self)
        a.triggered.connect(self.delete_selected)
        menu.addAction(a)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _open_page(self) -> None:
        sel = self.selected_mods()
        if sel and sel[0].page_url:
            webbrowser.open(sel[0].page_url)

    def _copy(self, text: str) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(text)
        self.statusMessage.emit("Copied to clipboard.")

    # ---------------------------------------------------------------------- #
    def _error(self, msg: str, tb: str) -> None:
        QMessageBox.critical(self, "Something went wrong", msg)
        self.statusMessage.emit(f"Error: {msg}")
