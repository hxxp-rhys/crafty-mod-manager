"""A stand-in Crafty Controller v4 API, backed by a real temp directory.

It mirrors the semantics of the real Tornado handlers in crafty-4 closely enough
to exercise CraftyBackend end to end: bearer auth, the {"status": "ok", "data": …}
envelope, the modified_epoch/409 write conflict rule, and the header-driven
upload protocol (both plain and chunked).
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HUMAN_TIME_FORMAT = "%Y/%m/%d %H:%M"
TOKEN = "test-token-abc123"
SERVER_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _human_size(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if f < 1024:
            return f"{f:.0f} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} TB"


class FakeCraftyHandler(BaseHTTPRequestHandler):
    root: Path = Path(".")
    calls: list = []

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence
        pass

    # -- plumbing -------------------------------------------------------- #
    def _json(self, code: int, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _raw(self, code: int, data: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _authed(self) -> bool:
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            return auth.split(None, 1)[1] == TOKEN
        return False

    def _deny(self) -> None:
        self._json(403, {"status": "error", "error": "ACCESS_DENIED"})

    def _abs(self, rel: str) -> Path:
        rel = (rel or "").strip("/")
        p = (self.root / rel).resolve()
        if not str(p).startswith(str(self.root.resolve())):
            raise PermissionError("traversal")
        return p

    def _rel(self, p: Path) -> str:
        return p.resolve().relative_to(self.root.resolve()).as_posix()

    # -- routes ---------------------------------------------------------- #
    def do_GET(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        self.calls.append(("GET", path))
        if path.rstrip("/") == "/api/v2/servers":
            if not self._authed():
                return self._deny()
            return self._json(
                200,
                {
                    "status": "ok",
                    "data": [
                        {
                            "server_id": SERVER_ID,
                            "server_name": "Test Server",
                            "path": str(self.root),
                            "type": "minecraft-java",
                        }
                    ],
                },
            )
        if path.endswith("/stats") or path.endswith("/stats/"):
            if not self._authed():
                return self._deny()
            return self._json(
                200,
                {
                    "status": "ok",
                    "data": {"running": True, "online": 3, "max": 20,
                             "cpu": 12.5, "mem": "2.1GB"},
                },
            )
        if "/files/" in path and path.rstrip("/").endswith("/download"):
            if not self._authed():
                return self._deny()
            inner = path.split("/files/", 1)[1]
            inner = inner.rstrip("/")[: -len("/download")]
            target = self._abs(urllib.parse.unquote(inner))
            if not target.is_file():
                return self._json(404, {"status": "error", "error": "File not found"})
            return self._raw(200, target.read_bytes())
        return self._json(404, {"status": "error", "error": "NOT_FOUND"})

    def do_POST(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        self.calls.append(("POST", path))
        body = self._body()

        if path.rstrip("/") == "/api/v2/auth/login":
            data = json.loads(body or b"{}")
            if data.get("username") == "admin" and data.get("password") == "hunter2":
                return self._json(
                    200, {"status": "ok", "data": {"token": TOKEN, "user_id": "1"}}
                )
            return self._json(401, {"status": "error", "error": "INCORRECT_CREDENTIALS"})

        if not self._authed():
            return self._deny()

        if path.rstrip("/").endswith("/files/upload"):
            return self._upload(body)

        for op in ("move", "copy"):
            if path.rstrip("/").endswith(f"/files/{op}"):
                data = json.loads(body)
                for obj in data["file_system_objects"]:
                    src = self._abs(obj["source_path"])
                    dst = self._abs(obj["target_path"]) / src.name
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    if op == "move":
                        src.rename(dst)
                    else:
                        dst.write_bytes(src.read_bytes())
                return self._json(200, {"status": "ok"})

        if "/action/" in path:
            return self._json(200, {"status": "ok"})

        if path.rstrip("/").endswith("/files"):
            return self._list_or_read(json.loads(body))

        return self._json(404, {"status": "error", "error": "NOT_FOUND"})

    def do_PATCH(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        self.calls.append(("PATCH", path))
        if not self._authed():
            return self._deny()
        data = json.loads(self._body())

        if path.rstrip("/").endswith("/files/create"):  # rename
            src = self._abs(data["path"])
            dst = src.parent / data["new_name"]
            if dst.exists():
                return self._json(400, {"status": "error", "error": "FILE EXISTS"})
            src.rename(dst)
            return self._json(200, {"status": "ok"})

        if path.rstrip("/").endswith("/files"):  # write
            target = self._abs(data["path"])
            if not target.exists():
                return self._json(400, {"status": "error", "error": "NO_SUCH_FILE"})
            # Same rule as the real handler.
            if target.stat().st_mtime > data.get("modified_epoch", 1.5) and not data.get(
                "overwrite"
            ):
                self.send_response(409)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            target.write_text(data["contents"], encoding="utf-8")
            st = target.stat()
            return self._json(
                200,
                {
                    "status": "ok",
                    "data": {
                        "attributes": {
                            "mime": "text/plain",
                            "modified": datetime.fromtimestamp(st.st_mtime).strftime(
                                HUMAN_TIME_FORMAT
                            ),
                            "size": _human_size(st.st_size),
                            "modified_epoch": st.st_mtime,
                        }
                    },
                },
            )
        return self._json(404, {"status": "error", "error": "NOT_FOUND"})

    def do_PUT(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        self.calls.append(("PUT", path))
        if not self._authed():
            return self._deny()
        data = json.loads(self._body())
        if path.rstrip("/").endswith("/files/create"):
            target = self._abs(data.get("parent", "")) / data["name"]
            if target.exists():
                return self._json(400, {"status": "error", "error": "FILE EXISTS"})
            if data.get("directory"):
                target.mkdir()
            else:
                target.write_text("", encoding="utf-8")
            return self._json(200, {"status": "ok"})
        return self._json(404, {"status": "error", "error": "NOT_FOUND"})

    def do_DELETE(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        self.calls.append(("DELETE", path))
        if not self._authed():
            return self._deny()
        data = json.loads(self._body())
        import shutil

        for obj in data["file_system_objects"]:
            target = self._abs(obj["filename"])
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
        return self._json(200, {"status": "ok"})

    # -- helpers --------------------------------------------------------- #
    def _list_or_read(self, data: dict):
        target = self._abs(data.get("path", ""))
        if not target.exists():
            return self._json(400, {"status": "error", "error": "NO_SUCH_PATH"})
        if target.is_dir():
            out = {
                "root_path": {
                    "local_path": data.get("path", ""),
                    "path": str(target),
                    "top": target == self.root.resolve(),
                    "modified": target.stat().st_mtime,
                }
            }
            for child in sorted(target.iterdir()):
                st = child.stat()
                out[child.name] = {
                    "path": self._rel(child),
                    "dir": child.is_dir(),
                    "excluded": False,
                    "modified": datetime.fromtimestamp(st.st_mtime).strftime(
                        HUMAN_TIME_FORMAT
                    ),
                    "size": _human_size(st.st_size if child.is_file() else 0),
                    "permissions": {"can_read": True, "can_write": True,
                                    "can_execute": False},
                }
            return self._json(200, {"status": "ok", "data": out})

        st = target.stat()
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            return self._json(
                400, {"status": "error", "error": "DECODE_ERROR", "error_data": str(exc)}
            )
        return self._json(
            200,
            {
                "status": "ok",
                "data": {
                    "content": content,
                    "attributes": {
                        "mime": "text/plain",
                        "modified": datetime.fromtimestamp(st.st_mtime).strftime(
                            HUMAN_TIME_FORMAT
                        ),
                        "size": _human_size(st.st_size),
                        "modified_epoch": st.st_mtime,
                    },
                },
            },
        )

    _chunk_store: dict = {}

    def _upload(self, body: bytes):
        h = self.headers
        filename = h.get("fileName")
        location = h.get("location") or ""
        chunked = h.get("chunked")
        chunk_id = h.get("chunkId")
        file_id = h.get("fileId")
        total_chunks = int(h.get("totalChunks") or 0)

        upload_dir = self._abs(location)
        if not upload_dir.exists():
            # The real handler stats the dir before creating it -> 500.
            return self._json(
                500, {"status": "error", "error": "NO_SUCH_DIR", "error_data": location}
            )

        if not chunked:
            (upload_dir / filename).write_bytes(body)
            return self._json(
                200, {"status": "completed", "data": {"message": "File uploaded"}}
            )

        if chunk_id is None:
            self._chunk_store[file_id] = {}
            return self._json(200, {"status": "ok", "data": {"file-id": file_id}})

        want = h.get("chunkHash")
        got = hashlib.sha256(body).hexdigest()
        if str(want) != str(got):
            return self._json(400, {"status": "error", "error": "INVALID_HASH"})

        store = self._chunk_store.setdefault(file_id, {})
        store[int(chunk_id)] = body
        if len(store) == total_chunks:
            (upload_dir / filename).write_bytes(
                b"".join(store[i] for i in range(total_chunks))
            )
            self._chunk_store.pop(file_id, None)
            return self._json(
                200, {"status": "completed", "data": {"message": "File uploaded"}}
            )
        return self._json(200, {"status": "partial", "data": {"message": "chunk ok"}})


class FakeCrafty:
    """Context manager that runs the fake server on a free port."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        handler = type(
            "BoundHandler", (FakeCraftyHandler,), {"root": self.root, "calls": []}
        )
        self.handler = handler
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "FakeCrafty":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
