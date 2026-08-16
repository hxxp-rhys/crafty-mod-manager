"""Transport-agnostic server backend interface.

Every path handed to / returned from a backend is POSIX-style and *relative to
the server root*. "" means the server root itself.
"""
from __future__ import annotations

import posixpath
from abc import ABC, abstractmethod
from typing import Callable, Optional

from ..models import RemoteEntry, ServerRef

ProgressCb = Optional[Callable[[int, int], None]]


class BackendError(Exception):
    """Any backend failure the UI should surface verbatim."""


class AuthError(BackendError):
    pass


class ConflictError(BackendError):
    """Remote file changed since it was read."""


class NotFoundError(BackendError):
    pass


def norm(path: str) -> str:
    """Normalise a relative path: forward slashes, no leading/trailing slash,
    no '..' escapes."""
    if not path:
        return ""
    p = str(path).replace("\\", "/").strip("/")
    parts: list[str] = []
    for seg in p.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    return "/".join(parts)


def join(*parts: str) -> str:
    return norm(posixpath.join(*[p for p in parts if p]))


def parent_of(path: str) -> str:
    p = norm(path)
    return p.rsplit("/", 1)[0] if "/" in p else ""


class ServerBackend(ABC):
    """Common surface for the Crafty API backend and the SSH/SFTP backend."""

    kind: str = "base"
    supports_power: bool = False
    supports_move: bool = False

    def __init__(self) -> None:
        self.server_id: str = ""
        self.connected: bool = False

    # -- lifecycle ------------------------------------------------------- #
    @abstractmethod
    def connect(self) -> None: ...

    def close(self) -> None:
        self.connected = False

    def __enter__(self) -> "ServerBackend":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- servers --------------------------------------------------------- #
    @abstractmethod
    def list_servers(self) -> list[ServerRef]: ...

    def select_server(self, server_id: str) -> None:
        self.server_id = server_id

    # -- filesystem ------------------------------------------------------ #
    @abstractmethod
    def list_dir(self, path: str = "") -> list[RemoteEntry]: ...

    @abstractmethod
    def read_text(self, path: str) -> tuple[str, Optional[float]]:
        """Return (contents, modified_epoch)."""

    @abstractmethod
    def write_text(
        self,
        path: str,
        contents: str,
        expect_mtime: Optional[float] = None,
        overwrite: bool = False,
    ) -> Optional[float]:
        """Write text. Raises ConflictError when the remote changed and
        ``overwrite`` is False. Returns the new modified epoch when known."""

    @abstractmethod
    def read_bytes(self, path: str, progress: ProgressCb = None) -> bytes: ...

    @abstractmethod
    def upload_bytes(
        self, directory: str, filename: str, data: bytes, progress: ProgressCb = None
    ) -> None: ...

    @abstractmethod
    def delete(self, path: str) -> None: ...

    @abstractmethod
    def rename(self, path: str, new_name: str) -> None: ...

    @abstractmethod
    def make_dir(self, parent: str, name: str) -> None: ...

    @abstractmethod
    def make_file(self, parent: str, name: str) -> None: ...

    @abstractmethod
    def exists(self, path: str) -> bool: ...

    def move(self, source: str, target_dir: str) -> None:
        raise BackendError("This backend does not support move/copy.")

    def copy(self, source: str, target_dir: str) -> None:
        raise BackendError("This backend does not support move/copy.")

    # -- power ----------------------------------------------------------- #
    def power(self, action: str) -> None:
        raise BackendError("Power control is not available on this connection.")

    def status(self) -> dict:
        return {}
