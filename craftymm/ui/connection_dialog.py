"""Connection profile editor: pick Crafty API or SSH/SFTP, test, save."""
from __future__ import annotations

import copy
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..backends import CraftyBackend, SSHBackend
from ..backends.base import BackendError
from ..config import KEYRING_OK, Profile, Settings
from . import theme
from .workers import run_task

LOADERS = ["auto", "fabric", "forge", "neoforge", "quilt"]


class ConnectionDialog(QDialog):
    def __init__(
        self, settings: Settings, profile: Optional[Profile] = None, parent=None
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.is_new = profile is None
        self.profile = copy.deepcopy(profile) if profile else Profile()
        self.setWindowTitle("New connection" if self.is_new else "Edit connection")
        self.setMinimumWidth(560)
        self._servers: list = []
        self._build()
        self._load()

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(12)

        self.name_input = QLineEdit()
        self.transport_box = QComboBox()
        self.transport_box.addItem("Crafty Controller v4 API", "crafty")
        self.transport_box.addItem("SSH / SFTP", "ssh")
        self.transport_box.currentIndexChanged.connect(self._on_transport)

        top = QFormLayout()
        top.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        top.addRow("Profile name", self.name_input)
        top.addRow("Connect via", self.transport_box)
        root.addLayout(top)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._crafty_page())
        self.stack.addWidget(self._ssh_page())
        root.addWidget(self.stack)

        root.addWidget(self._layout_page())

        if not KEYRING_OK:
            warn = QLabel(
                "⚠ Windows Credential Manager isn't reachable, so passwords and tokens "
                "will be saved to a local file instead. Prefer a scoped API key here."
            )
            warn.setWordWrap(True)
            warn.setStyleSheet(f"color: {theme.WARN};")
            root.addWidget(warn)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        buttons = QDialogButtonBox()
        self.test_btn = QPushButton("Test connection")
        self.test_btn.clicked.connect(self._test)
        buttons.addButton(self.test_btn, QDialogButtonBox.ButtonRole.ActionRole)
        save = buttons.addButton("Save", QDialogButtonBox.ButtonRole.AcceptRole)
        save.setProperty("accent", True)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # -- pages ----------------------------------------------------------- #
    def _crafty_page(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.crafty_url = QLineEdit()
        self.crafty_url.setPlaceholderText("https://your-server:8443")

        self.crafty_auth = QComboBox()
        self.crafty_auth.addItem("API key (recommended)", "token")
        self.crafty_auth.addItem("Username + password", "password")
        self.crafty_auth.currentIndexChanged.connect(self._on_crafty_auth)

        self.crafty_token = QLineEdit()
        self.crafty_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.crafty_token.setPlaceholderText("Crafty → your user → API keys")

        self.crafty_user = QLineEdit()
        self.crafty_pass = QLineEdit()
        self.crafty_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self.crafty_totp = QLineEdit()
        self.crafty_totp.setPlaceholderText("only if 2FA is on")
        self.crafty_totp.setMaximumWidth(140)

        self.crafty_verify = QCheckBox("Verify the TLS certificate")
        self.crafty_verify.setToolTip(
            "Crafty ships a self-signed certificate by default. Leave this off "
            "unless you've installed a real certificate."
        )

        self.server_box = QComboBox()
        self.server_box.setPlaceholderText("Test the connection to load servers")
        refresh = QPushButton("Load servers")
        refresh.clicked.connect(self._load_servers)
        srow = QHBoxLayout()
        srow.addWidget(self.server_box, 1)
        srow.addWidget(refresh)

        form.addRow("Crafty URL", self.crafty_url)
        form.addRow("Authentication", self.crafty_auth)
        self._row_token = self._add_row(form, "API key", self.crafty_token)
        self._row_user = self._add_row(form, "Username", self.crafty_user)
        self._row_pass = self._add_row(form, "Password", self.crafty_pass)
        self._row_totp = self._add_row(form, "2FA code", self.crafty_totp)
        form.addRow("", self.crafty_verify)
        form.addRow("Server", srow)
        return page

    @staticmethod
    def _add_row(form: QFormLayout, label: str, widget: QWidget) -> tuple:
        lab = QLabel(label)
        form.addRow(lab, widget)
        return lab, widget

    def _ssh_page(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)

        conn = QGroupBox("Connection")
        form = QFormLayout(conn)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.ssh_host = QLineEdit()
        self.ssh_port = QSpinBox()
        self.ssh_port.setRange(1, 65535)
        self.ssh_port.setValue(22)
        self.ssh_port.setMaximumWidth(100)
        self.ssh_user = QLineEdit()

        self.ssh_auth = QComboBox()
        self.ssh_auth.addItem("Password", "password")
        self.ssh_auth.addItem("Private key file", "key")
        self.ssh_auth.addItem("SSH agent / default keys", "agent")
        self.ssh_auth.currentIndexChanged.connect(self._on_ssh_auth)

        self.ssh_pass = QLineEdit()
        self.ssh_pass.setEchoMode(QLineEdit.EchoMode.Password)

        self.ssh_key = QLineEdit()
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._pick_key)
        krow = QHBoxLayout()
        krow.addWidget(self.ssh_key, 1)
        krow.addWidget(browse)
        krow_w = QWidget()
        krow_w.setLayout(krow)
        krow.setContentsMargins(0, 0, 0, 0)

        self.ssh_keypass = QLineEdit()
        self.ssh_keypass.setEchoMode(QLineEdit.EchoMode.Password)
        self.ssh_keypass.setPlaceholderText("leave blank if the key isn't encrypted")

        self.ssh_root = QLineEdit()
        self.ssh_root.setPlaceholderText("/opt/minecraft/survival")

        hrow = QHBoxLayout()
        hrow.addWidget(self.ssh_host, 1)
        hrow.addWidget(QLabel("Port"))
        hrow.addWidget(self.ssh_port)
        hrow_w = QWidget()
        hrow_w.setLayout(hrow)
        hrow.setContentsMargins(0, 0, 0, 0)

        form.addRow("Host", hrow_w)
        form.addRow("Username", self.ssh_user)
        form.addRow("Authentication", self.ssh_auth)
        self._row_sshpass = self._add_row(form, "Password", self.ssh_pass)
        self._row_sshkey = self._add_row(form, "Key file", krow_w)
        self._row_sshkeypass = self._add_row(form, "Key passphrase", self.ssh_keypass)
        form.addRow("Server folder", self.ssh_root)
        outer.addWidget(conn)

        cmds = QGroupBox("Power commands (optional)")
        cform = QFormLayout(cmds)
        cform.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.cmd_start = QLineEdit()
        self.cmd_stop = QLineEdit()
        self.cmd_restart = QLineEdit()
        self.cmd_status = QLineEdit()
        self.cmd_start.setPlaceholderText("sudo systemctl start minecraft")
        self.cmd_stop.setPlaceholderText("sudo systemctl stop minecraft")
        self.cmd_restart.setPlaceholderText("sudo systemctl restart minecraft")
        self.cmd_status.setPlaceholderText("systemctl is-active minecraft")
        cform.addRow("Start", self.cmd_start)
        cform.addRow("Stop", self.cmd_stop)
        cform.addRow("Restart", self.cmd_restart)
        cform.addRow("Status", self.cmd_status)
        outer.addWidget(cmds)
        return page

    def _layout_page(self) -> QWidget:
        box = QGroupBox("Server layout")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.mods_dir = QLineEdit()
        self.mods_dir.setPlaceholderText("mods")
        self.config_dirs = QLineEdit()
        self.config_dirs.setPlaceholderText("config, defaultconfigs, kubejs")
        self.loader_box = QComboBox()
        self.loader_box.addItems(LOADERS)
        self.mc_version = QLineEdit()
        self.mc_version.setPlaceholderText("blank = detect from installed mods")
        self.mc_version.setMaximumWidth(200)
        form.addRow("Mods folder", self.mods_dir)
        form.addRow("Config folders", self.config_dirs)
        form.addRow("Mod loader", self.loader_box)
        form.addRow("Minecraft version", self.mc_version)
        return box

    # -- state ----------------------------------------------------------- #
    def _load(self) -> None:
        p = self.profile
        self.name_input.setText(p.name)
        self.transport_box.setCurrentIndex(0 if p.transport == "crafty" else 1)

        self.crafty_url.setText(p.crafty_url)
        self.crafty_auth.setCurrentIndex(0 if p.crafty_auth_mode == "token" else 1)
        self.crafty_user.setText(p.crafty_username)
        self.crafty_verify.setChecked(p.crafty_verify_ssl)
        if not self.is_new:
            self.crafty_token.setText(p.get_secret("crafty_token"))
            self.crafty_pass.setText(p.get_secret("crafty_password"))
            self.ssh_pass.setText(p.get_secret("ssh_password"))
            self.ssh_keypass.setText(p.get_secret("ssh_key_passphrase"))
        if p.crafty_server_id:
            self.server_box.addItem(p.crafty_server_id, p.crafty_server_id)

        self.ssh_host.setText(p.ssh_host)
        self.ssh_port.setValue(p.ssh_port or 22)
        self.ssh_user.setText(p.ssh_user)
        idx = {"password": 0, "key": 1, "agent": 2}.get(p.ssh_auth_mode, 0)
        self.ssh_auth.setCurrentIndex(idx)
        self.ssh_key.setText(p.ssh_key_path)
        self.ssh_root.setText(p.ssh_root)
        self.cmd_start.setText(p.ssh_start_cmd)
        self.cmd_stop.setText(p.ssh_stop_cmd)
        self.cmd_restart.setText(p.ssh_restart_cmd)
        self.cmd_status.setText(p.ssh_status_cmd)

        self.mods_dir.setText(p.mods_dir)
        self.config_dirs.setText(", ".join(p.config_dirs))
        self.loader_box.setCurrentText(p.loader if p.loader in LOADERS else "auto")
        self.mc_version.setText(p.mc_version)

        self._on_transport()
        self._on_crafty_auth()
        self._on_ssh_auth()

    def _harvest(self) -> Profile:
        p = self.profile
        p.name = self.name_input.text().strip() or "Unnamed"
        p.transport = self.transport_box.currentData()

        p.crafty_url = self.crafty_url.text().strip()
        p.crafty_auth_mode = self.crafty_auth.currentData()
        p.crafty_username = self.crafty_user.text().strip()
        p.crafty_verify_ssl = self.crafty_verify.isChecked()
        p.crafty_server_id = self.server_box.currentData() or ""

        p.ssh_host = self.ssh_host.text().strip()
        p.ssh_port = self.ssh_port.value()
        p.ssh_user = self.ssh_user.text().strip()
        p.ssh_auth_mode = self.ssh_auth.currentData()
        p.ssh_key_path = self.ssh_key.text().strip()
        p.ssh_root = self.ssh_root.text().strip() or "/"
        p.ssh_start_cmd = self.cmd_start.text().strip()
        p.ssh_stop_cmd = self.cmd_stop.text().strip()
        p.ssh_restart_cmd = self.cmd_restart.text().strip()
        p.ssh_status_cmd = self.cmd_status.text().strip()

        p.mods_dir = self.mods_dir.text().strip() or "mods"
        p.config_dirs = [
            s.strip() for s in self.config_dirs.text().split(",") if s.strip()
        ] or ["config"]
        p.loader = self.loader_box.currentText()
        p.mc_version = self.mc_version.text().strip()
        return p

    # -- visibility ------------------------------------------------------ #
    def _on_transport(self) -> None:
        self.stack.setCurrentIndex(0 if self.transport_box.currentData() == "crafty" else 1)

    def _on_crafty_auth(self) -> None:
        token_mode = self.crafty_auth.currentData() == "token"
        for lab, w in (self._row_token,):
            lab.setVisible(token_mode)
            w.setVisible(token_mode)
        for row in (self._row_user, self._row_pass, self._row_totp):
            row[0].setVisible(not token_mode)
            row[1].setVisible(not token_mode)

    def _on_ssh_auth(self) -> None:
        mode = self.ssh_auth.currentData()
        for row, want in (
            (self._row_sshpass, mode == "password"),
            (self._row_sshkey, mode == "key"),
            (self._row_sshkeypass, mode == "key"),
        ):
            row[0].setVisible(want)
            row[1].setVisible(want)

    def _pick_key(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select a private key")
        if path:
            self.ssh_key.setText(path)

    # -- test / load ------------------------------------------------------ #
    def build_backend(self):
        p = self._harvest()
        if p.transport == "crafty":
            return CraftyBackend(
                base_url=p.crafty_url,
                token=self.crafty_token.text().strip()
                if p.crafty_auth_mode == "token"
                else "",
                username=p.crafty_username,
                password=self.crafty_pass.text(),
                totp=self.crafty_totp.text().strip(),
                verify_ssl=p.crafty_verify_ssl,
            )
        return SSHBackend(
            host=p.ssh_host,
            port=p.ssh_port,
            username=p.ssh_user,
            password=self.ssh_pass.text(),
            key_path=p.ssh_key_path,
            key_passphrase=self.ssh_keypass.text(),
            auth_mode=p.ssh_auth_mode,
            root=p.ssh_root,
            start_cmd=p.ssh_start_cmd,
            stop_cmd=p.ssh_stop_cmd,
            restart_cmd=p.ssh_restart_cmd,
            status_cmd=p.ssh_status_cmd,
        )

    def _set_status(self, text: str, colour: str = theme.FG_DIM) -> None:
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {colour};")

    def _test(self) -> None:
        self.test_btn.setEnabled(False)
        self._set_status("Connecting…")

        # Everything the worker needs is read here, on the GUI thread. Touching
        # widgets from a QThreadPool thread is undefined behaviour.
        backend = self.build_backend()
        is_ssh = self.transport_box.currentData() == "ssh"
        ssh_root = self.ssh_root.text().strip() or "/"
        mods_dir = self.mods_dir.text().strip() or "mods"

        def work():
            backend.connect()
            servers = backend.list_servers()
            mods_found = None
            if is_ssh:
                backend.select_server(ssh_root)
                try:
                    backend.list_dir(mods_dir)
                    mods_found = True
                except BackendError:
                    mods_found = False
            backend.close()
            return servers, mods_found

        run_task(
            work,
            on_done=self._test_ok,
            on_error=lambda msg, tb: (
                self._set_status(f"✕ {msg}", theme.ERR),
                self.test_btn.setEnabled(True),
            ),
        )

    def _test_ok(self, result) -> None:
        servers, mods_found = result
        self.test_btn.setEnabled(True)
        self._servers = servers
        if self.transport_box.currentData() == "crafty":
            self._fill_servers(servers)
            self._set_status(
                f"✓ Connected. {len(servers)} server(s) visible to this account.",
                theme.OK,
            )
        else:
            extra = (
                ""
                if mods_found
                else "  (heads-up: the mods folder wasn't found under that path)"
            )
            self._set_status(f"✓ SSH connection works.{extra}",
                             theme.OK if mods_found else theme.WARN)

    def _load_servers(self) -> None:
        self._test()

    def _fill_servers(self, servers) -> None:
        current = self.profile.crafty_server_id
        self.server_box.clear()
        for s in servers:
            self.server_box.addItem(f"{s.name}  ({s.id[:8]}…)", s.id)
        if current:
            i = self.server_box.findData(current)
            if i >= 0:
                self.server_box.setCurrentIndex(i)

    # -- save ------------------------------------------------------------ #
    def _accept(self) -> None:
        p = self._harvest()
        if p.transport == "crafty":
            if not p.crafty_url:
                return self._warn("Enter the Crafty URL.")
            if p.crafty_auth_mode == "token" and not self.crafty_token.text().strip():
                return self._warn("Enter an API key, or switch to username + password.")
            if p.crafty_auth_mode == "password" and not p.crafty_username:
                return self._warn("Enter a username.")
            if not p.crafty_server_id:
                return self._warn(
                    "Pick a server. Hit 'Load servers' after entering your credentials."
                )
        else:
            if not p.ssh_host:
                return self._warn("Enter the SSH host.")
            if not p.ssh_user:
                return self._warn("Enter the SSH username.")

        p.set_secret("crafty_token", self.crafty_token.text().strip())
        p.set_secret("crafty_password", self.crafty_pass.text())
        p.set_secret("ssh_password", self.ssh_pass.text())
        p.set_secret("ssh_key_passphrase", self.ssh_keypass.text())

        if self.is_new:
            self.settings.add_profile(p)
        else:
            for i, existing in enumerate(self.settings.profiles):
                if existing.id == p.id:
                    self.settings.profiles[i] = p
                    break
            self.settings.active_profile = p.id
        self.settings.save()
        self.accept()

    def _warn(self, msg: str) -> None:
        QMessageBox.warning(self, "Missing details", msg)
