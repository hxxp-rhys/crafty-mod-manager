"""Main window: profile picker, power controls, and the four tabs."""
from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QStatusBar,
    QTabWidget,
    QToolBar,
    QWidget,
)

from .. import APP_TITLE, __version__
from ..backends import CraftyBackend, SSHBackend
from ..backends.base import BackendError, ServerBackend
from ..config import Profile, Settings
from ..manager import ModManager
from ..providers import CurseForgeProvider, ModrinthProvider
from . import theme
from .browse_tab import BrowseTab
from .connection_dialog import ConnectionDialog
from .dialogs import BackupsDialog, SettingsDialog
from .files_tab import FilesTab
from .mods_tab import ModsTab
from .workers import run_task

log = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings = settings
        self.backend: Optional[ServerBackend] = None
        self.manager: Optional[ModManager] = None
        self._busy_count = 0

        self.setWindowTitle(f"{APP_TITLE} {__version__}")
        self.resize(1360, 860)
        self._build()
        self._reload_profiles()

        if not settings.profiles:
            QTimer.singleShot(250, self._first_run)
        elif settings.current():
            QTimer.singleShot(150, self.connect_current)

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        bar = QToolBar()
        bar.setMovable(False)
        self.addToolBar(bar)

        self.profile_box = QComboBox()
        self.profile_box.setMinimumWidth(200)
        self.profile_box.setToolTip("Saved connections")
        self.profile_box.currentIndexChanged.connect(self._on_profile_changed)
        bar.addWidget(self.profile_box)

        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setProperty("accent", True)
        self.connect_btn.clicked.connect(self.connect_current)
        bar.addWidget(self.connect_btn)

        for label, tip, slot in (
            ("New", "Add a connection", self.new_profile),
            ("Edit", "Edit this connection", self.edit_profile),
            ("Delete", "Remove this connection", self.delete_profile),
        ):
            b = QPushButton(label)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            bar.addWidget(b)

        spacer = QWidget()
        spacer.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        bar.addWidget(spacer)

        self.power_label = QLabel("—")
        self.power_label.setStyleSheet(f"color: {theme.FG_DIM}; padding-right: 8px;")
        bar.addWidget(self.power_label)

        self.start_btn = QPushButton("Start")
        self.stop_btn = QPushButton("Stop")
        self.restart_btn = QPushButton("Restart")
        self.start_btn.clicked.connect(lambda: self.power("start"))
        self.stop_btn.clicked.connect(lambda: self.power("stop"))
        self.restart_btn.clicked.connect(lambda: self.power("restart"))
        for b in (self.start_btn, self.stop_btn, self.restart_btn):
            bar.addWidget(b)

        self.backups_btn = QPushButton("Backups")
        self.backups_btn.setToolTip("Restore a replaced jar or an earlier config")
        self.backups_btn.clicked.connect(self.show_backups)
        bar.addWidget(self.backups_btn)

        self.settings_btn = QPushButton("Settings")
        self.settings_btn.clicked.connect(self.show_settings)
        bar.addWidget(self.settings_btn)

        # --- tabs
        self.tabs = QTabWidget()
        self.mods_tab = ModsTab()
        self.browse_tab = BrowseTab()
        self.files_tab = FilesTab()
        self.tabs.addTab(self.mods_tab, "Installed mods")
        self.tabs.addTab(self.browse_tab, "Find mods")
        self.tabs.addTab(self.files_tab, "Server files")
        self.setCentralWidget(self.tabs)

        for tab in (self.mods_tab, self.browse_tab, self.files_tab):
            tab.busyChanged.connect(self._on_busy)
            tab.statusMessage.connect(self._status)
            tab.progressChanged.connect(self._progress)
        self.browse_tab.modsChanged.connect(self.mods_tab.refresh)

        # --- status bar
        sb = QStatusBar()
        self.setStatusBar(sb)
        self.status_label = QLabel("Not connected.")
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(260)
        self.progress.setVisible(False)
        sb.addWidget(self.status_label, 1)
        sb.addPermanentWidget(self.progress)

        # --- shortcuts
        for key, slot in (
            ("F5", self.mods_tab.refresh),
            ("Ctrl+R", self.connect_current),
            ("Ctrl+S", lambda: self.files_tab.save()),
        ):
            act = QAction(self)
            act.setShortcut(QKeySequence(key))
            act.triggered.connect(slot)
            self.addAction(act)

        self._set_connected(False)

    # -- profiles --------------------------------------------------------- #
    def _reload_profiles(self) -> None:
        self.profile_box.blockSignals(True)
        self.profile_box.clear()
        for p in self.settings.profiles:
            kind = "Crafty" if p.transport == "crafty" else "SSH"
            self.profile_box.addItem(f"{p.name}  ({kind})", p.id)
        current = self.settings.current()
        if current:
            i = self.profile_box.findData(current.id)
            if i >= 0:
                self.profile_box.setCurrentIndex(i)
        self.profile_box.blockSignals(False)
        has = bool(self.settings.profiles)
        self.connect_btn.setEnabled(has)

    def _on_profile_changed(self) -> None:
        pid = self.profile_box.currentData()
        if pid:
            self.settings.active_profile = pid
            self.settings.save()
            self._disconnect()

    def current_profile(self) -> Optional[Profile]:
        pid = self.profile_box.currentData()
        return self.settings.get_profile(pid) if pid else None

    def _first_run(self) -> None:
        QMessageBox.information(
            self,
            "Welcome",
            "Let's set up a connection.\n\n"
            "• Crafty Controller: paste your panel URL and an API key "
            "(Crafty → your user → API keys), then pick the server.\n"
            "• SSH/SFTP: host, user, and the folder your server lives in.\n\n"
            "Nothing is written to your server until you ask for it.",
        )
        self.new_profile()

    def new_profile(self) -> None:
        dlg = ConnectionDialog(self.settings, None, self)
        if dlg.exec():
            self._reload_profiles()
            self.connect_current()

    def edit_profile(self) -> None:
        p = self.current_profile()
        if not p:
            return
        dlg = ConnectionDialog(self.settings, p, self)
        if dlg.exec():
            self._reload_profiles()
            self.connect_current()

    def delete_profile(self) -> None:
        p = self.current_profile()
        if not p:
            return
        answer = QMessageBox.question(
            self,
            "Delete connection",
            f"Delete the profile '{p.name}'?\n\n"
            "Saved credentials go too. Nothing on the server is touched.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._disconnect()
        self.settings.remove_profile(p.id)
        self.settings.save()
        self._reload_profiles()

    # -- connect ---------------------------------------------------------- #
    def _make_backend(self, p: Profile) -> ServerBackend:
        if p.transport == "crafty":
            backend = CraftyBackend(
                base_url=p.crafty_url,
                token=p.get_secret("crafty_token") if p.crafty_auth_mode == "token" else "",
                username=p.crafty_username,
                password=p.get_secret("crafty_password"),
                verify_ssl=p.crafty_verify_ssl,
            )
        else:
            backend = SSHBackend(
                host=p.ssh_host,
                port=p.ssh_port,
                username=p.ssh_user,
                password=p.get_secret("ssh_password"),
                key_path=p.ssh_key_path,
                key_passphrase=p.get_secret("ssh_key_passphrase"),
                auth_mode=p.ssh_auth_mode,
                root=p.ssh_root,
                start_cmd=p.ssh_start_cmd,
                stop_cmd=p.ssh_stop_cmd,
                restart_cmd=p.ssh_restart_cmd,
                status_cmd=p.ssh_status_cmd,
            )
        return backend

    def connect_current(self) -> None:
        p = self.current_profile()
        if not p:
            return
        self._disconnect()
        self._status(f"Connecting to {p.name}…")
        self.connect_btn.setEnabled(False)

        def work():
            backend = self._make_backend(p)
            backend.connect()
            backend.select_server(
                p.crafty_server_id if p.transport == "crafty" else p.ssh_root
            )
            return backend

        run_task(
            work,
            on_done=lambda backend: self._connected(p, backend),
            on_error=self._connect_failed,
            on_finished=lambda: self.connect_btn.setEnabled(True),
        )

    def _connected(self, profile: Profile, backend: ServerBackend) -> None:
        self.backend = backend
        providers = {
            "modrinth": ModrinthProvider(),
            "curseforge": CurseForgeProvider(self.settings.get_curseforge_key()),
        }
        self.manager = ModManager(backend, profile, self.settings, providers)
        for tab in (self.mods_tab, self.browse_tab, self.files_tab):
            tab.set_manager(self.manager)
        self._set_connected(True)
        self._status(f"Connected to {profile.name}. Scanning mods…")
        self.mods_tab.refresh()
        self.refresh_power()

    def _connect_failed(self, msg: str, tb: str) -> None:
        self._set_connected(False)
        QMessageBox.critical(
            self,
            "Could not connect",
            f"{msg}\n\nCheck the connection settings, then try again.",
        )
        self._status(f"Connection failed: {msg}")

    def _disconnect(self) -> None:
        if self.backend:
            try:
                self.backend.close()
            except Exception:  # pragma: no cover
                pass
        self.backend = None
        self.manager = None
        for tab in (self.mods_tab, self.browse_tab, self.files_tab):
            tab.set_manager(None)
        self._set_connected(False)

    def _set_connected(self, connected: bool) -> None:
        for b in (self.start_btn, self.stop_btn, self.restart_btn, self.backups_btn):
            b.setEnabled(connected)
        if not connected:
            self.power_label.setText("—")
            self.status_label.setText("Not connected.")

    # -- power ------------------------------------------------------------ #
    def power(self, action: str) -> None:
        if not self.backend:
            return
        if action in ("stop", "restart"):
            answer = QMessageBox.question(
                self,
                f"{action.capitalize()} server",
                f"Send '{action}' to the server now?\n\n"
                "Players online will be disconnected.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._status(f"Sending {action}…")
        run_task(
            self.backend.power,
            action,
            on_done=lambda _: (
                self._status(f"Sent '{action}'."),
                QTimer.singleShot(3000, self.refresh_power),
            ),
            on_error=lambda msg, tb: QMessageBox.warning(self, "Power command", msg),
        )

    def refresh_power(self) -> None:
        if not self.backend:
            return
        run_task(
            self.backend.status,
            on_done=self._show_power,
            on_error=lambda msg, tb: self.power_label.setText("status unavailable"),
        )

    def _show_power(self, stats: dict) -> None:
        if not stats:
            self.power_label.setText("status unavailable")
            return
        if "output" in stats:  # SSH status command
            lines = str(stats["output"]).splitlines()
            first = lines[0][:60] if lines else (
                "ok" if stats.get("exit_code") == 0 else "no output"
            )
            self.power_label.setText(first)
            return
        running = stats.get("running")
        players = stats.get("online")
        maxp = stats.get("max")
        cpu = stats.get("cpu")
        mem = stats.get("mem")
        bits = ["● running" if running else "○ stopped"]
        if running:
            if players is not None and maxp is not None:
                bits.append(f"{players}/{maxp} players")
            if cpu is not None:
                bits.append(f"CPU {cpu}%")
            if mem:
                bits.append(f"RAM {mem}")
        self.power_label.setText("   ".join(str(b) for b in bits))
        self.power_label.setStyleSheet(
            f"color: {theme.OK if running else theme.FG_DIM}; padding-right: 8px;"
        )

    # -- dialogs ---------------------------------------------------------- #
    def show_backups(self) -> None:
        if self.manager:
            BackupsDialog(self.manager, self).exec()

    def show_settings(self) -> None:
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec() and self.manager:
            cf = self.manager.providers.get("curseforge")
            if isinstance(cf, CurseForgeProvider):
                cf.set_key(self.settings.get_curseforge_key())
            self.browse_tab.set_manager(self.manager)

    # -- status ----------------------------------------------------------- #
    def _status(self, text: str) -> None:
        self.status_label.setText(text)

    def _progress(self, message: str, current: int, total: int) -> None:
        self.status_label.setText(message)
        if total > 0:
            self.progress.setVisible(True)
            self.progress.setMaximum(total)
            self.progress.setValue(current)
        else:
            self.progress.setVisible(False)

    def _on_busy(self, busy: bool) -> None:
        self._busy_count += 1 if busy else -1
        self._busy_count = max(0, self._busy_count)
        if self._busy_count == 0:
            self.progress.setVisible(False)
            self.setCursor(Qt.CursorShape.ArrowCursor)
        else:
            self.setCursor(Qt.CursorShape.BusyCursor)

    # -- shutdown --------------------------------------------------------- #
    def closeEvent(self, event):  # noqa: N802
        if self.files_tab.editor_pane.editor.is_dirty:
            answer = QMessageBox.question(
                self,
                "Unsaved changes",
                f"{self.files_tab.open_path} has unsaved changes. Quit anyway?",
                QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Discard:
                event.ignore()
                return
        self.settings.save()
        self._disconnect()
        event.accept()
