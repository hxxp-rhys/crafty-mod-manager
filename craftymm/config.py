"""Profile / settings storage.

Config lives in %APPDATA%\\CraftyModManager on Windows (XDG dirs elsewhere).
Secrets (API tokens, SSH passwords) go into the OS credential store via
``keyring`` when it is available; otherwise they are stored in the config file
and the profile is flagged ``insecure_secrets`` so the UI can warn about it.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

APP_NAME = "CraftyModManager"
KEYRING_SERVICE = "CraftyModManager"

try:  # pragma: no cover - environment dependent
    import keyring
    from keyring.errors import KeyringError

    _kr_backend = keyring.get_keyring()
    # keyring's "fail" backend raises on use; detect it up front.
    KEYRING_OK = "fail" not in type(_kr_backend).__module__.lower()
except Exception:  # pragma: no cover
    keyring = None  # type: ignore[assignment]
    KeyringError = Exception  # type: ignore[misc,assignment]
    KEYRING_OK = False


def app_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    p = Path(base) / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def data_dir(*parts: str) -> Path:
    p = app_dir().joinpath(*parts)
    p.mkdir(parents=True, exist_ok=True)
    return p


CONFIG_PATH = app_dir() / "config.json"


# --------------------------------------------------------------------------- #
#  Profiles
# --------------------------------------------------------------------------- #
@dataclass
class Profile:
    """One saved connection."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = "New profile"
    transport: str = "crafty"  # "crafty" | "ssh"

    # --- Crafty -----------------------------------------------------------
    crafty_url: str = "https://127.0.0.1:8443"
    crafty_auth_mode: str = "token"  # "token" | "password"
    crafty_username: str = ""
    crafty_verify_ssl: bool = False
    crafty_server_id: str = ""

    # --- SSH --------------------------------------------------------------
    ssh_host: str = ""
    ssh_port: int = 22
    ssh_user: str = ""
    ssh_auth_mode: str = "password"  # "password" | "key" | "agent"
    ssh_key_path: str = ""
    ssh_root: str = "/opt/minecraft"
    ssh_start_cmd: str = ""
    ssh_stop_cmd: str = ""
    ssh_restart_cmd: str = ""
    ssh_status_cmd: str = ""

    # --- Server layout ----------------------------------------------------
    mods_dir: str = "mods"
    config_dirs: list[str] = field(
        default_factory=lambda: ["config", "defaultconfigs", "kubejs"]
    )
    loader: str = "auto"  # auto | fabric | forge | neoforge | quilt
    mc_version: str = ""  # "" == any

    # --- Bookkeeping ------------------------------------------------------
    insecure_secrets: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Profile":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})

    # -- secrets -----------------------------------------------------------
    def _secret_key(self, which: str) -> str:
        return f"{self.id}:{which}"

    def get_secret(self, which: str) -> str:
        """which: 'crafty_token' | 'crafty_password' | 'ssh_password' |
        'ssh_key_passphrase' | 'curseforge_key'"""
        if KEYRING_OK:
            try:
                return keyring.get_password(KEYRING_SERVICE, self._secret_key(which)) or ""
            except KeyringError as exc:  # pragma: no cover
                log.warning("keyring read failed: %s", exc)
        return _fallback_secrets().get(self._secret_key(which), "")

    def set_secret(self, which: str, value: str) -> None:
        key = self._secret_key(which)
        if KEYRING_OK:
            try:
                if value:
                    keyring.set_password(KEYRING_SERVICE, key, value)
                else:
                    try:
                        keyring.delete_password(KEYRING_SERVICE, key)
                    except Exception:
                        pass
                self.insecure_secrets = False
                return
            except KeyringError as exc:  # pragma: no cover
                log.warning("keyring write failed: %s", exc)
        store = _fallback_secrets()
        if value:
            store[key] = value
        else:
            store.pop(key, None)
        _write_fallback_secrets(store)
        self.insecure_secrets = True

    def clear_secrets(self) -> None:
        for which in (
            "crafty_token",
            "crafty_password",
            "ssh_password",
            "ssh_key_passphrase",
        ):
            self.set_secret(which, "")


_FALLBACK_PATH = app_dir() / "secrets.json"


def _fallback_secrets() -> dict[str, str]:
    if not _FALLBACK_PATH.exists():
        return {}
    try:
        return json.loads(_FALLBACK_PATH.read_text("utf-8"))
    except Exception:
        return {}


def _write_fallback_secrets(store: dict[str, str]) -> None:
    _FALLBACK_PATH.write_text(json.dumps(store, indent=2), "utf-8")
    try:  # best effort on POSIX
        os.chmod(_FALLBACK_PATH, 0o600)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
#  App-wide settings
# --------------------------------------------------------------------------- #
@dataclass
class Settings:
    profiles: list[Profile] = field(default_factory=list)
    active_profile: str = ""
    curseforge_key_set: bool = False
    theme: str = "dark"
    backup_configs: bool = True
    backup_mods: bool = True
    keep_backups: int = 25
    window_geometry: str = ""

    # ------------------------------------------------------------------ #
    @classmethod
    def load(cls) -> "Settings":
        if not CONFIG_PATH.exists():
            return cls()
        try:
            raw = json.loads(CONFIG_PATH.read_text("utf-8"))
        except Exception as exc:
            log.error("config unreadable (%s) - starting fresh", exc)
            return cls()
        profiles = [Profile.from_dict(p) for p in raw.get("profiles", [])]
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        kwargs = {k: v for k, v in raw.items() if k in known and k != "profiles"}
        s = cls(profiles=profiles, **kwargs)
        return s

    def save(self) -> None:
        payload = asdict(self)
        payload["profiles"] = [p.to_dict() for p in self.profiles]
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), "utf-8")
        tmp.replace(CONFIG_PATH)

    # ------------------------------------------------------------------ #
    def get_profile(self, pid: str) -> Optional[Profile]:
        return next((p for p in self.profiles if p.id == pid), None)

    def current(self) -> Optional[Profile]:
        return self.get_profile(self.active_profile) or (
            self.profiles[0] if self.profiles else None
        )

    def add_profile(self, p: Profile) -> None:
        self.profiles.append(p)
        self.active_profile = p.id

    def remove_profile(self, pid: str) -> None:
        p = self.get_profile(pid)
        if p:
            p.clear_secrets()
            self.profiles = [x for x in self.profiles if x.id != pid]
            if self.active_profile == pid:
                self.active_profile = self.profiles[0].id if self.profiles else ""

    # -- global CurseForge key (shared across profiles) ------------------ #
    def get_curseforge_key(self) -> str:
        if KEYRING_OK:
            try:
                return keyring.get_password(KEYRING_SERVICE, "global:curseforge") or ""
            except KeyringError:  # pragma: no cover
                pass
        return _fallback_secrets().get("global:curseforge", "")

    def set_curseforge_key(self, value: str) -> None:
        self.curseforge_key_set = bool(value)
        if KEYRING_OK:
            try:
                if value:
                    keyring.set_password(KEYRING_SERVICE, "global:curseforge", value)
                else:
                    try:
                        keyring.delete_password(KEYRING_SERVICE, "global:curseforge")
                    except Exception:
                        pass
                return
            except KeyringError:  # pragma: no cover
                pass
        store = _fallback_secrets()
        if value:
            store["global:curseforge"] = value
        else:
            store.pop("global:curseforge", None)
        _write_fallback_secrets(store)
