"""Crafty Controller v4 backend (REST API v2).

Endpoint map (verified against crafty-controller/crafty-4 @ master):

  POST   /api/v2/auth/login                              {username,password,totp?}
  GET    /api/v2/servers/                                -> list of servers
  GET    /api/v2/servers/{id}/                           -> server record
  GET    /api/v2/servers/{id}/stats/                     -> live stats
  POST   /api/v2/servers/{id}/action/{action}            start_server / stop_server /
                                                         restart_server / kill_server /
                                                         backup_server
  POST   /api/v2/servers/{id}/files                      {path[,modified_epoch]}  (list OR read)
  PATCH  /api/v2/servers/{id}/files                      {path,contents,modified_epoch,overwrite}
  DELETE /api/v2/servers/{id}/files                      {file_system_objects:[{filename}]}
  PUT    /api/v2/servers/{id}/files/create               {parent,name,directory}
  PATCH  /api/v2/servers/{id}/files/create               {path,new_name}
  POST   /api/v2/servers/{id}/files/(move|copy)          {file_system_objects:[{source_path,target_path}]}
  POST   /api/v2/servers/{id}/files/zip                  {folder,proc_id}
  GET    /api/v2/servers/{id}/files/{path}/download      -> octet-stream
  POST   /api/v2/servers/{id}/files/upload               headers-driven upload
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import urllib.parse
import uuid
from datetime import datetime
from typing import Any, Optional

import requests

from ..models import RemoteEntry, ServerRef
from .base import (
    AuthError,
    BackendError,
    ConflictError,
    NotFoundError,
    ProgressCb,
    ServerBackend,
    join,
    norm,
    parent_of,
)

log = logging.getLogger(__name__)

CHUNK_THRESHOLD = 8 * 1024 * 1024  # switch to chunked upload above this
CHUNK_SIZE = 4 * 1024 * 1024
HUMAN_TIME_FORMAT = "%Y/%m/%d %H:%M"

_SIZE_UNITS = {
    "B": 1,
    "KB": 1024,
    "MB": 1024**2,
    "GB": 1024**3,
    "TB": 1024**4,
    "KIB": 1024,
    "MIB": 1024**2,
    "GIB": 1024**3,
}


def _parse_size(text: Any) -> int:
    """Crafty returns human-readable sizes ('4.2 MB'); recover a byte count."""
    if isinstance(text, (int, float)):
        return int(text)
    if not text:
        return 0
    parts = str(text).strip().split()
    try:
        if len(parts) == 2:
            return int(float(parts[0]) * _SIZE_UNITS.get(parts[1].upper(), 1))
        return int(float(parts[0]))
    except (ValueError, TypeError):
        return 0


def _parse_time(text: Any) -> Optional[float]:
    if isinstance(text, (int, float)):
        return float(text)
    if not text:
        return None
    try:
        return datetime.strptime(str(text), HUMAN_TIME_FORMAT).timestamp()
    except ValueError:
        return None


class CraftyBackend(ServerBackend):
    kind = "crafty"
    supports_power = True
    supports_move = True

    def __init__(
        self,
        base_url: str,
        token: str = "",
        username: str = "",
        password: str = "",
        totp: str = "",
        verify_ssl: bool = False,
        timeout: int = 30,
    ) -> None:
        super().__init__()
        self.base_url = self._normalise_url(base_url)
        self._token = token.strip()
        self._username = username
        self._password = password
        self._totp = totp
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self.session = requests.Session()
        self.session.verify = verify_ssl
        if not verify_ssl:
            try:
                import urllib3

                urllib3.disable_warnings(
                    urllib3.exceptions.InsecureRequestWarning  # type: ignore[attr-defined]
                )
            except Exception:  # pragma: no cover
                pass
        self._server_cache: dict[str, dict] = {}

    # ------------------------------------------------------------------ #
    @staticmethod
    def _normalise_url(url: str) -> str:
        url = (url or "").strip().rstrip("/")
        if not url:
            raise BackendError("Crafty URL is empty.")
        if "://" not in url:
            url = "https://" + url
        return url

    # ------------------------------------------------------------------ #
    def connect(self) -> None:
        if not self._token:
            if not (self._username and self._password):
                raise AuthError("No API token and no username/password supplied.")
            self._token = self._login()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
                "User-Agent": "CraftyModManager/1.0",
            }
        )
        # Cheap probe that also validates the token.
        self.list_servers()
        self.connected = True

    def _login(self) -> str:
        payload: dict[str, Any] = {
            "username": self._username,
            "password": self._password,
        }
        if self._totp:
            payload["totp"] = self._totp
        try:
            r = self.session.post(
                f"{self.base_url}/api/v2/auth/login",
                json=payload,
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
        except requests.RequestException as exc:
            raise BackendError(f"Could not reach Crafty at {self.base_url}: {exc}") from exc
        if r.status_code in (401, 403):
            raise AuthError("Crafty rejected those credentials.")
        try:
            body = r.json()
        except ValueError:
            raise BackendError(
                f"Crafty returned a non-JSON login response (HTTP {r.status_code}). "
                "Is that URL really a Crafty Controller instance?"
            )
        if body.get("status") != "ok":
            raise AuthError(
                f"Login failed: {body.get('error') or body.get('error_data') or body}"
            )
        token = (body.get("data") or {}).get("token")
        if not token:
            raise AuthError("Login succeeded but no token was returned.")
        return token

    @property
    def token(self) -> str:
        return self._token

    # ------------------------------------------------------------------ #
    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: Any = None,
        expect_json: bool = True,
        stream: bool = False,
        headers: Optional[dict] = None,
        data: Optional[bytes] = None,
        allowed: tuple[int, ...] = (),
    ) -> Any:
        url = self._url(path)
        kwargs: dict[str, Any] = {
            "timeout": self.timeout,
            "verify": self.verify_ssl,
            "stream": stream,
        }
        if payload is not None:
            kwargs["json"] = payload
        if data is not None:
            kwargs["data"] = data
        if headers:
            kwargs["headers"] = headers
        try:
            r = self.session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            raise BackendError(f"{method} {path} failed: {exc}") from exc

        if r.status_code in allowed:
            return r
        if r.status_code in (401, 403):
            raise AuthError(
                "Crafty denied that request (401/403). The token may have expired, "
                "or this API key lacks the required permission."
            )
        if r.status_code == 404:
            raise NotFoundError(f"Not found: {path}")
        if r.status_code == 409:
            raise ConflictError("The remote file changed since you opened it.")
        if r.status_code == 304:
            return r
        if not expect_json or stream:
            if r.status_code >= 400:
                raise BackendError(f"HTTP {r.status_code} from {path}: {r.text[:300]}")
            return r

        try:
            body = r.json()
        except ValueError:
            raise BackendError(
                f"HTTP {r.status_code} from {path}: non-JSON response "
                f"({r.text[:200]!r})"
            )
        if body.get("status") not in ("ok", "completed", "partial"):
            err = body.get("error") or "ERROR"
            detail = body.get("error_data") or body.get("data") or ""
            raise BackendError(f"{err}: {detail}" if detail else str(err))
        return body

    # -- servers --------------------------------------------------------- #
    def list_servers(self) -> list[ServerRef]:
        body = self._request("GET", "/api/v2/servers/")
        out: list[ServerRef] = []
        for s in body.get("data") or []:
            sid = str(s.get("server_id") or s.get("id") or "")
            if not sid:
                continue
            self._server_cache[sid] = s
            out.append(
                ServerRef(
                    id=sid,
                    name=str(s.get("server_name") or sid),
                    path=str(s.get("path") or ""),
                    server_type=str(s.get("type") or s.get("server_type") or ""),
                    raw=s,
                )
            )
        return out

    def status(self) -> dict:
        if not self.server_id:
            return {}
        try:
            body = self._request("GET", f"/api/v2/servers/{self.server_id}/stats/")
        except BackendError as exc:
            return {"error": str(exc)}
        data = body.get("data")
        if isinstance(data, list):
            data = data[0] if data else {}
        return data or {}

    def power(self, action: str) -> None:
        if not self.server_id:
            raise BackendError("No server selected.")
        mapping = {
            "start": "start_server",
            "stop": "stop_server",
            "restart": "restart_server",
            "kill": "kill_server",
            "backup": "backup_server",
        }
        act = mapping.get(action, action)
        self._request("POST", f"/api/v2/servers/{self.server_id}/action/{act}")

    def send_command(self, command: str) -> None:
        if not self.server_id:
            raise BackendError("No server selected.")
        self._request(
            "POST",
            f"/api/v2/servers/{self.server_id}/stdin",
            data=command.encode("utf-8"),
        )

    # -- filesystem ------------------------------------------------------ #
    def _files_path(self) -> str:
        if not self.server_id:
            raise BackendError("No server selected.")
        return f"/api/v2/servers/{self.server_id}/files"

    def list_dir(self, path: str = "") -> list[RemoteEntry]:
        rel = norm(path)
        body = self._request("POST", self._files_path(), payload={"path": rel})
        data = body.get("data") or {}
        entries: list[RemoteEntry] = []
        for name, info in data.items():
            if name == "root_path" or not isinstance(info, dict):
                continue
            child = norm(info.get("path") or join(rel, name))
            entries.append(
                RemoteEntry(
                    name=name,
                    path=child,
                    is_dir=bool(info.get("dir")),
                    size=_parse_size(info.get("size")),
                    size_text=str(info.get("size") or ""),
                    modified=_parse_time(info.get("modified")),
                    modified_text=str(info.get("modified") or ""),
                )
            )
        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return entries

    def read_text(self, path: str) -> tuple[str, Optional[float]]:
        body = self._request("POST", self._files_path(), payload={"path": norm(path)})
        data = body.get("data") or {}
        if "content" not in data:
            raise BackendError(f"{path} is a directory, not a file.")
        attrs = data.get("attributes") or {}
        return str(data.get("content") or ""), attrs.get("modified_epoch")

    def write_text(
        self,
        path: str,
        contents: str,
        expect_mtime: Optional[float] = None,
        overwrite: bool = False,
    ) -> Optional[float]:
        payload: dict[str, Any] = {
            "path": norm(path),
            "contents": contents,
            "overwrite": bool(overwrite),
        }
        # Crafty compares st_mtime > modified_epoch; without a value it defaults
        # to 1.5 and every save would 409. Send what we read, or force overwrite.
        if expect_mtime is not None:
            payload["modified_epoch"] = float(expect_mtime)
        elif not overwrite:
            payload["overwrite"] = True
        r = self._request("PATCH", self._files_path(), payload=payload)
        if isinstance(r, requests.Response):  # 304 path
            return expect_mtime
        return ((r.get("data") or {}).get("attributes") or {}).get("modified_epoch")

    def read_bytes(self, path: str, progress: ProgressCb = None) -> bytes:
        rel = norm(path)
        quoted = urllib.parse.quote(rel, safe="/")
        r = self._request(
            "GET",
            f"{self._files_path()}/{quoted}/download",
            expect_json=False,
            stream=True,
        )
        total = int(r.headers.get("Content-Length") or 0)
        buf = bytearray()
        for chunk in r.iter_content(chunk_size=256 * 1024):
            if chunk:
                buf.extend(chunk)
                if progress:
                    progress(len(buf), total)
        return bytes(buf)

    def upload_bytes(
        self, directory: str, filename: str, data: bytes, progress: ProgressCb = None
    ) -> None:
        rel_dir = norm(directory)
        size = len(data)
        base_headers = {
            "fileName": filename,
            "location": rel_dir,
            "fileSize": str(size),
            "Content-Type": "application/octet-stream",
        }
        url = f"{self._files_path()}/upload"

        # Crafty's upload handler stats the destination before creating it, so a
        # missing folder 500s. Make sure it exists first (cheap, idempotent).
        if rel_dir and not self.exists(rel_dir):
            self._ensure_dirs(rel_dir)

        if size <= CHUNK_THRESHOLD:
            self._request("POST", url, headers=base_headers, data=data, expect_json=True)
            if progress:
                progress(size, size)
            return

        file_id = uuid.uuid4().hex
        total_chunks = (size + CHUNK_SIZE - 1) // CHUNK_SIZE
        init_headers = dict(base_headers)
        init_headers.update({"chunked": "true", "fileId": file_id,
                             "totalChunks": str(total_chunks)})
        # Handshake: chunked with no chunkId returns the file id.
        self._request("POST", url, headers=init_headers, data=b"", expect_json=True)

        sent = 0
        for idx in range(total_chunks):
            chunk = data[idx * CHUNK_SIZE : (idx + 1) * CHUNK_SIZE]
            headers = dict(base_headers)
            headers.update(
                {
                    "chunked": "true",
                    "fileId": file_id,
                    "chunkId": str(idx),
                    "totalChunks": str(total_chunks),
                    "chunkHash": hashlib.sha256(chunk).hexdigest(),
                }
            )
            self._request("POST", url, headers=headers, data=chunk, expect_json=True)
            sent += len(chunk)
            if progress:
                progress(sent, size)

    def delete(self, path: str) -> None:
        self._request(
            "DELETE",
            self._files_path(),
            payload={"file_system_objects": [{"filename": norm(path)}]},
        )

    def rename(self, path: str, new_name: str) -> None:
        self._request(
            "PATCH",
            f"{self._files_path()}/create",
            payload={"path": norm(path), "new_name": new_name},
        )

    def make_dir(self, parent: str, name: str) -> None:
        self._request(
            "PUT",
            f"{self._files_path()}/create",
            payload={"parent": norm(parent), "name": name, "directory": True},
        )

    def _ensure_dirs(self, rel: str) -> None:
        """mkdir -p, relative to the server root."""
        built = ""
        for seg in norm(rel).split("/"):
            if not seg:
                continue
            parent, built = built, join(built, seg)
            if self.exists(built):
                continue
            try:
                self.make_dir(parent, seg)
            except BackendError as exc:
                if "FILE EXISTS" not in str(exc).upper():
                    raise

    def make_file(self, parent: str, name: str) -> None:
        self._request(
            "PUT",
            f"{self._files_path()}/create",
            payload={"parent": norm(parent), "name": name, "directory": False},
        )

    def move(self, source: str, target_dir: str) -> None:
        self._file_op("move", source, target_dir)

    def copy(self, source: str, target_dir: str) -> None:
        self._file_op("copy", source, target_dir)

    def _file_op(self, op: str, source: str, target_dir: str) -> None:
        self._request(
            "POST",
            f"{self._files_path()}/{op}",
            payload={
                "file_system_objects": [
                    {"source_path": norm(source), "target_path": norm(target_dir)}
                ]
            },
        )

    def exists(self, path: str) -> bool:
        rel = norm(path)
        if not rel:
            return True
        try:
            entries = self.list_dir(parent_of(rel))
        except BackendError:
            return False
        leaf = rel.rsplit("/", 1)[-1]
        return any(e.name == leaf for e in entries)
