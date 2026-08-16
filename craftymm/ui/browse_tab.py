"""Search Modrinth / CurseForge, pick a version, install it to the server."""
from __future__ import annotations

import webbrowser
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..manager import ModManager
from ..models import ProjectHit, VersionInfo
from . import theme
from .workers import defer, run_task

RES_COLS = ["Mod", "Author", "Downloads", "Source"]


def _downloads(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


class BrowseTab(QWidget):
    busyChanged = Signal(bool)
    statusMessage = Signal(str)
    progressChanged = Signal(str, int, int)
    modsChanged = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.manager: Optional[ModManager] = None
        self.hits: list[ProjectHit] = []
        self.versions: list[VersionInfo] = []
        # Request sequence numbers: a slow reply for an older query must not
        # overwrite the results of a newer one (and get installed by mistake).
        self._search_seq = 0
        self._version_seq = 0
        self._busy = False
        self._build()

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        bar = QHBoxLayout()
        self.query = QLineEdit()
        self.query.setPlaceholderText("Search for a mod…  (blank = most downloaded)")
        self.query.setClearButtonEnabled(True)
        self.query.returnPressed.connect(self.search)

        self.source_box = QComboBox()
        self.source_box.addItem("Modrinth", "modrinth")
        self.source_box.addItem("CurseForge", "curseforge")
        self.source_box.setMinimumWidth(130)

        self.loader_box = QComboBox()
        self.loader_box.addItems(["auto", "fabric", "forge", "neoforge", "quilt", "any"])
        self.loader_box.setMinimumWidth(110)

        self.mc_box = QLineEdit()
        self.mc_box.setPlaceholderText("MC version")
        self.mc_box.setMaximumWidth(120)

        self.search_btn = QPushButton("Search")
        self.search_btn.setProperty("accent", True)
        self.search_btn.clicked.connect(self.search)

        bar.addWidget(self.query, 1)
        bar.addWidget(QLabel("on"))
        bar.addWidget(self.source_box)
        bar.addWidget(QLabel("for"))
        bar.addWidget(self.loader_box)
        bar.addWidget(self.mc_box)
        bar.addWidget(self.search_btn)
        root.addLayout(bar)

        split = QSplitter(Qt.Orientation.Horizontal)

        self.results = QTableWidget(0, len(RES_COLS))
        self.results.setHorizontalHeaderLabels(RES_COLS)
        self.results.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.results.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.results.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.results.setAlternatingRowColors(True)
        self.results.verticalHeader().setVisible(False)
        self.results.verticalHeader().setDefaultSectionSize(28)
        hh = self.results.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in (1, 2, 3):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        self.results.itemSelectionChanged.connect(self._on_pick)
        split.addWidget(self.results)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(8)

        self.detail = QTextBrowser()
        self.detail.setOpenExternalLinks(True)
        self.detail.setMaximumHeight(150)
        self.detail.setHtml("<i>Pick a mod on the left.</i>")
        rl.addWidget(self.detail)

        rl.addWidget(QLabel("Versions"))
        self.version_list = QListWidget()
        self.version_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.version_list.itemSelectionChanged.connect(self._update_buttons)
        rl.addWidget(self.version_list, 1)

        brow = QHBoxLayout()
        self.page_btn = QPushButton("Open page")
        self.page_btn.clicked.connect(self._open_page)
        self.install_btn = QPushButton("Install to server")
        self.install_btn.setProperty("accent", True)
        self.install_btn.clicked.connect(self.install_selected)
        brow.addWidget(self.page_btn)
        brow.addStretch(1)
        brow.addWidget(self.install_btn)
        rl.addLayout(brow)

        split.addWidget(right)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 4)
        root.addWidget(split, 1)

        self.hint = QLabel("")
        self.hint.setStyleSheet(f"color: {theme.FG_DIM};")
        self.hint.setWordWrap(True)
        root.addWidget(self.hint)

        self._update_buttons()

    # ------------------------------------------------------------------ #
    def set_manager(self, manager: Optional[ModManager]) -> None:
        self.manager = manager
        if manager:
            cf = manager.providers.get("curseforge")
            self.source_box.model().item(1).setEnabled(bool(cf and cf.available))
            if not (cf and cf.available):
                self.hint.setText(
                    "CurseForge is greyed out until you add an API key in Settings. "
                    "Modrinth needs no key."
                )
            else:
                self.hint.setText("")
            if manager.loader:
                self.loader_box.setCurrentText(manager.loader)
            if manager.mc_version and not self.mc_box.text():
                self.mc_box.setText(manager.mc_version)
        self._update_buttons()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.busyChanged.emit(busy)
        self._update_buttons()

    def _update_buttons(self) -> None:
        ready = self.manager is not None and not self._busy
        self.search_btn.setEnabled(ready)
        self.install_btn.setEnabled(ready and self.version_list.currentRow() >= 0)
        self.page_btn.setEnabled(self.results.currentRow() >= 0)

    # -- search ------------------------------------------------------------ #
    def search(self) -> None:
        if not self.manager or self._busy:
            return
        source = self.source_box.currentData()
        provider = self.manager.providers.get(source)
        if not provider or not provider.available:
            QMessageBox.information(
                self,
                "Provider unavailable",
                "Add a CurseForge API key in Settings to search CurseForge.",
            )
            return
        loader = self.loader_box.currentText()
        if loader == "auto":
            loader = self.manager.loader
        elif loader == "any":
            loader = ""
        mcv = self.mc_box.text().strip()
        query = self.query.text().strip()

        self._search_seq += 1
        seq = self._search_seq
        self._set_busy(True)
        self.statusMessage.emit("Searching…")
        run_task(
            provider.search,
            query,
            loader=loader,
            mc_version=mcv,
            limit=50,
            on_done=lambda hits: self._show_results(hits, seq),
            on_error=self._error,
            on_finished=lambda: self._set_busy(False),
        )

    def _show_results(self, hits: list[ProjectHit], seq: int = 0) -> None:
        if seq and seq != self._search_seq:
            return  # a newer search already answered
        self.hits = hits
        self._version_seq += 1  # any in-flight version lookup is now stale
        self.results.setRowCount(len(hits))
        for row, h in enumerate(hits):
            title = QTableWidgetItem(h.title)
            title.setToolTip(h.description[:400])
            self.results.setItem(row, 0, title)
            self.results.setItem(row, 1, QTableWidgetItem(h.author))
            self.results.setItem(row, 2, QTableWidgetItem(_downloads(h.downloads)))
            self.results.setItem(
                row, 3, QTableWidgetItem("Modrinth" if h.source == "modrinth" else "CurseForge")
            )
        self.version_list.clear()
        self.versions = []
        self.detail.setHtml("<i>Pick a mod on the left.</i>")
        self.statusMessage.emit(f"{len(hits)} result(s).")
        self._update_buttons()

    # -- versions ---------------------------------------------------------- #
    def _on_pick(self) -> None:
        row = self.results.currentRow()
        if row < 0 or row >= len(self.hits) or not self.manager:
            return
        hit = self.hits[row]
        self.detail.setHtml(
            f"<h3 style='margin:0'>{hit.title}</h3>"
            f"<p style='color:{theme.FG_DIM};margin:4px 0'>"
            f"by {hit.author or 'unknown'} · {_downloads(hit.downloads)} downloads</p>"
            f"<p>{hit.description}</p>"
            + (
                f"<p style='color:{theme.FG_DIM}'>{', '.join(hit.categories[:10])}</p>"
                if hit.categories
                else ""
            )
        )
        provider = self.manager.providers.get(hit.source)
        if not provider:
            return
        loader = self.loader_box.currentText()
        if loader == "auto":
            loader = self.manager.loader
        elif loader == "any":
            loader = ""
        mcv = self.mc_box.text().strip()

        self._version_seq += 1
        seq = self._version_seq
        self.versions = []
        self.version_list.clear()
        self.version_list.addItem("loading…")
        self._update_buttons()
        self._set_busy(True)
        run_task(
            provider.versions,
            hit.project_id,
            loader,
            mcv,
            on_done=lambda vs: self._show_versions(vs, seq),
            on_error=self._error,
            on_finished=lambda: self._set_busy(False),
        )

    def _show_versions(self, versions: list[VersionInfo], seq: int = 0) -> None:
        if seq and seq != self._version_seq:
            return  # the user has moved on to a different mod
        self.versions = versions
        self.version_list.clear()
        if not versions:
            self.version_list.addItem(
                "No builds match that loader / MC version — widen the filters."
            )
            self._update_buttons()
            return
        for v in versions:
            item = QListWidgetItem(str(v))
            colour = {
                "release": theme.OK,
                "beta": theme.WARN,
                "alpha": theme.ERR,
            }.get(v.release_type, theme.FG)
            item.setForeground(QColor(colour))
            item.setToolTip(
                f"{v.filename}\n{v.date_published}\nloaders: {', '.join(v.loaders)}"
            )
            self.version_list.addItem(item)
        self.version_list.setCurrentRow(0)
        self._update_buttons()

    # -- install ----------------------------------------------------------- #
    def install_selected(self) -> None:
        row = self.version_list.currentRow()
        if not self.manager or row < 0 or row >= len(self.versions):
            return
        version = self.versions[row]
        mgr = self.manager

        existing = next(
            (
                m
                for m in mgr.mods
                if m.project_id and m.project_id == version.project_id
            ),
            None,
        )
        if existing:
            answer = QMessageBox.question(
                self,
                "Already installed",
                f"{existing.display_name} is already on the server as "
                f"{existing.filename}.\n\nReplace it with {version.filename}?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        deps = [
            d
            for d in version.dependencies
            if (d.get("dependency_type") == "required" or d.get("relationType") == 3)
        ]
        if deps:
            QMessageBox.information(
                self,
                "Dependencies",
                f"{version.filename} lists {len(deps)} required dependency/ies. "
                "This tool installs the jar you picked only — check the project page "
                "and install any missing dependencies yourself.",
            )

        self._set_busy(True)
        run_task(
            mgr.install_version,
            version,
            replace=existing,
            on_progress=self.progressChanged.emit,
            on_done=lambda name: (
                self.statusMessage.emit(f"Installed {name}."),
                defer(self.modsChanged.emit),
            ),
            on_error=self._error,
            on_finished=lambda: self._set_busy(False),
        )

    def _open_page(self) -> None:
        row = self.results.currentRow()
        if 0 <= row < len(self.hits) and self.hits[row].page_url:
            webbrowser.open(self.hits[row].page_url)

    def _error(self, msg: str, tb: str) -> None:
        QMessageBox.critical(self, "Search failed", msg)
        self.statusMessage.emit(f"Error: {msg}")
