"""SSH / SFTP backend (paramiko).

Works with any host: the server root is a directory you point at, power control
is whatever shell commands you configure (systemctl, docker, screen, tmux, ...).
"""
from __future__ import annotations

import io
import logging
import posixpath
import stat as statmod
from typing import Optional

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


def _human(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.0f} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} TB"


class SSHBackend(ServerBackend):
    kind = "ssh"
    supports_power = True
    supports_move = True

    def __init__(
        self,
        host: str,
        port: int = 22,
        username: str = "",
        password: str = "",
        key_path: str = "",
        key_passphrase: str = "",
        auth_mode: str = "password",
        root: str = "/",
        start_cmd: str = "",
        stop_cmd: str = "",
        restart_cmd: str = "",
        status_cmd: str = "",
        timeout: int = 20,
    ) -> None:
        super().__init__()
        self.host = (host or "").strip()
        self.port = int(port or 22)
        self.username = username
        self.password = password
        self.key_path = key_path
        self.key_passphrase = key_passphrase
        self.auth_mode = auth_mode
        self.root = "/" + norm(root) if root else "/"
        self.commands = {
            "start": start_cmd,
            "stop": stop_cmd,
            "restart": restart_cmd,
            "status": status_cmd,
        }
        self.timeout = timeout
        self._client = None
        self._sftp = None

    # ------------------------------------------------------------------ #
    def connect(self) -> None:
        try:
            import paramiko
        except ImportError as exc:  # pragma: no cover
            raise BackendError(
                "paramiko is not installed - run: pip install paramiko"
            ) from exc

        if not self.host:
            raise BackendError("SSH host is empty.")

        client = paramiko.SSHClient()
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        kwargs = {
            "hostname": self.host,
            "port": self.port,
            "username": self.username or None,
            "timeout": self.timeout,
            "allow_agent": self.auth_mode == "agent",
            "look_for_keys": self.auth_mode in ("agent", "key"),
        }
        if self.auth_mode == "password":
            kwargs["password"] = self.password
        elif self.auth_mode == "key" and self.key_path:
            kwargs["key_filename"] = self.key_path
            if self.key_passphrase:
                kwargs["passphrase"] = self.key_passphrase

        try:
            client.connect(**kwargs)  # type: ignore[arg-type]
        except paramiko.AuthenticationException as exc:
            raise AuthError(f"SSH authentication failed for {self.username}@{self.host}") from exc
        except Exception as exc:
            raise BackendError(f"SSH connection to {self.host}:{self.port} failed: {exc}") from exc

        self._client = client
        try:
            self._sftp = client.open_sftp()
        except Exception as exc:
            raise BackendError(f"Could not open an SFTP channel: {exc}") from exc

        try:
            self._sftp.stat(self.root)
        except IOError as exc:
            raise BackendError(f"Server root '{self.root}' is not reachable: {exc}") from exc
        self.connected = True

    def close(self) -> None:
        for obj in (self._sftp, self._client):
            try:
                if obj:
                    obj.close()
            except Exception:  # pragma: no cover
                pass
        self._sftp = None
        self._client = None
        self.connected = False

    # ------------------------------------------------------------------ #
    def _abs(self, rel: str) -> str:
        r = norm(rel)
        return posixpath.join(self.root, r) if r else self.root

    def _need(self):
        if not self._sftp:
            raise BackendError("Not connected.")
        return self._sftp

    # -- servers --------------------------------------------------------- #
    def list_servers(self) -> list[ServerRef]:
        return [ServerRef(id=self.root, name=posixpath.basename(self.root) or self.root,
                          path=self.root, server_type="ssh")]

    def select_server(self, server_id: str) -> None:
        # For SSH the "server" is just the root directory.
        if server_id and server_id.startswith("/"):
            self.root = server_id
        self.server_id = self.root

    # -- filesystem ------------------------------------------------------ #
    def list_dir(self, path: str = "") -> list[RemoteEntry]:
        sftp = self._need()
        rel = norm(path)
        target = self._abs(rel)
        try:
            attrs = sftp.listdir_attr(target)
        except IOError as exc:
            raise NotFoundError(f"Cannot list {target}: {exc}") from exc
        out: list[RemoteEntry] = []
        for a in attrs:
            is_dir = statmod.S_ISDIR(a.st_mode or 0)
            size = int(a.st_size or 0)
            out.append(
                RemoteEntry(
                    name=a.filename,
                    path=join(rel, a.filename),
                    is_dir=is_dir,
                    size=0 if is_dir else size,
                    size_text="" if is_dir else _human(size),
                    modified=float(a.st_mtime) if a.st_mtime else None,
                )
            )
        out.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return out

    def read_text(self, path: str) -> tuple[str, Optional[float]]:
        sftp = self._need()
        target = self._abs(path)
        try:
            st = sftp.stat(target)
            with sftp.open(target, "rb") as fh:
                fh.prefetch()
                raw = fh.read()
        except IOError as exc:
            raise NotFoundError(f"Cannot read {target}: {exc}") from exc
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")
        return text, float(st.st_mtime) if st.st_mtime else None

    def write_text(
        self,
        path: str,
        contents: str,
        expect_mtime: Optional[float] = None,
        overwrite: bool = False,
    ) -> Optional[float]:
        sftp = self._need()
        target = self._abs(path)
        if expect_mtime is not None and not overwrite:
            try:
                st = sftp.stat(target)
                if st.st_mtime and float(st.st_mtime) > float(expect_mtime) + 1:
                    raise ConflictError("The remote file changed since you opened it.")
            except IOError:
                pass  # new file
        data = contents.encode("utf-8")
        tmp = target + ".craftymm.tmp"
        try:
            with sftp.open(tmp, "wb") as fh:
                fh.set_pipelined(True)
                fh.write(data)
            self._replace(tmp, target)
        except IOError as exc:
            try:
                sftp.remove(tmp)
            except IOError:
                pass
            raise BackendError(f"Cannot write {target}: {exc}") from exc
        try:
            return float(sftp.stat(target).st_mtime or 0) or None
        except IOError:
            return None

    def read_bytes(self, path: str, progress: ProgressCb = None) -> bytes:
        sftp = self._need()
        target = self._abs(path)
        buf = io.BytesIO()
        try:
            total = int(sftp.stat(target).st_size or 0)
            with sftp.open(target, "rb") as fh:
                fh.prefetch()
                while True:
                    chunk = fh.read(256 * 1024)
                    if not chunk:
                        break
                    buf.write(chunk)
                    if progress:
                        progress(buf.tell(), total)
        except IOError as exc:
            raise NotFoundError(f"Cannot download {target}: {exc}") from exc
        return buf.getvalue()

    def upload_bytes(
        self, directory: str, filename: str, data: bytes, progress: ProgressCb = None
    ) -> None:
        sftp = self._need()
        self._ensure_dir(norm(directory))
        target = posixpath.join(self._abs(directory), filename)
        tmp = target + ".craftymm.part"
        total = len(data)
        try:
            with sftp.open(tmp, "wb") as fh:
                fh.set_pipelined(True)
                for off in range(0, total, 256 * 1024):
                    fh.write(data[off : off + 256 * 1024])
                    if progress:
                        progress(min(off + 256 * 1024, total), total)
            self._replace(tmp, target)
        except IOError as exc:
            try:
                sftp.remove(tmp)
            except IOError:
                pass
            raise BackendError(f"Upload of {filename} failed: {exc}") from exc

    def _replace(self, tmp: str, target: str) -> None:
        """Move tmp over target. posix_rename is atomic and overwrites, so the
        target is never missing even for an instant. Only fall back to
        remove-then-rename on servers that don't implement the extension."""
        sftp = self._need()
        try:
            sftp.posix_rename(tmp, target)
            return
        except (IOError, AttributeError):
            pass
        try:
            sftp.remove(target)
        except IOError:
            pass
        sftp.rename(tmp, target)

    def _ensure_dir(self, rel: str) -> None:
        if not rel:
            return
        sftp = self._need()
        parts = rel.split("/")
        cur = ""
        for p in parts:
            cur = join(cur, p)
            full = self._abs(cur)
            try:
                sftp.stat(full)
            except IOError:
                try:
                    sftp.mkdir(full)
                except IOError as exc:
                    raise BackendError(f"Cannot create {full}: {exc}") from exc

    def delete(self, path: str) -> None:
        sftp = self._need()
        target = self._abs(path)
        try:
            st = sftp.stat(target)
        except IOError as exc:
            raise NotFoundError(f"{target} does not exist") from exc
        if statmod.S_ISDIR(st.st_mode or 0):
            self._rmtree(target)
        else:
            sftp.remove(target)

    def _rmtree(self, abs_path: str) -> None:
        sftp = self._need()
        for a in sftp.listdir_attr(abs_path):
            child = posixpath.join(abs_path, a.filename)
            if statmod.S_ISDIR(a.st_mode or 0):
                self._rmtree(child)
            else:
                sftp.remove(child)
        sftp.rmdir(abs_path)

    def rename(self, path: str, new_name: str) -> None:
        sftp = self._need()
        src = self._abs(path)
        dst = posixpath.join(posixpath.dirname(src), new_name)
        try:
            sftp.rename(src, dst)
        except IOError as exc:
            raise BackendError(f"Rename failed: {exc}") from exc

    def make_dir(self, parent: str, name: str) -> None:
        self._ensure_dir(join(parent, name))

    def make_file(self, parent: str, name: str) -> None:
        self.upload_bytes(parent, name, b"")

    def move(self, source: str, target_dir: str) -> None:
        sftp = self._need()
        self._ensure_dir(norm(target_dir))
        leaf = norm(source).rsplit("/", 1)[-1]
        sftp.rename(self._abs(source), posixpath.join(self._abs(target_dir), leaf))

    def copy(self, source: str, target_dir: str) -> None:
        leaf = norm(source).rsplit("/", 1)[-1]
        self.upload_bytes(target_dir, leaf, self.read_bytes(source))

    def exists(self, path: str) -> bool:
        try:
            self._need().stat(self._abs(path))
            return True
        except IOError:
            return False

    # -- power ----------------------------------------------------------- #
    def run(self, command: str, timeout: int = 60) -> tuple[int, str, str]:
        if not self._client:
            raise BackendError("Not connected.")
        _, stdout, stderr = self._client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        return stdout.channel.recv_exit_status(), out, err

    def power(self, action: str) -> None:
        cmd = self.commands.get(action, "")
        if not cmd:
            raise BackendError(
                f"No '{action}' command is configured for this SSH profile. "
                "Set one in the connection settings (e.g. "
                "'systemctl --user restart minecraft')."
            )
        code, out, err = self.run(cmd)
        if code != 0:
            raise BackendError(f"Command exited {code}: {(err or out).strip()[:400]}")

    def status(self) -> dict:
        cmd = self.commands.get("status", "")
        if not cmd:
            return {}
        try:
            code, out, err = self.run(cmd, timeout=20)
        except BackendError as exc:
            return {"error": str(exc)}
        return {"exit_code": code, "output": (out or err).strip()[:4000]}
