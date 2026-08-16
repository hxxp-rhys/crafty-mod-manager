"""Stand-in Modrinth + CurseForge APIs for provider tests."""
from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CF_KEY = "cf-test-key"

# Two fake Modrinth versions of one project; JAR_A is "installed", JAR_B is newer.
MR_PROJECT = "sodium-id"
MR_V1 = {
    "id": "ver-1",
    "project_id": MR_PROJECT,
    "name": "Sodium 0.5.8",
    "version_number": "0.5.8",
    "game_versions": ["1.20.1"],
    "loaders": ["fabric"],
    "version_type": "release",
    "date_published": "2024-01-01T00:00:00Z",
    "dependencies": [],
    "files": [
        {
            "primary": True,
            "filename": "sodium-fabric-0.5.8.jar",
            "url": "PLACEHOLDER/files/sodium-fabric-0.5.8.jar",
            "size": 11,
            "hashes": {"sha1": "SHA1_A", "sha512": "SHA512_A"},
        }
    ],
}
MR_V2 = {
    "id": "ver-2",
    "project_id": MR_PROJECT,
    "name": "Sodium 0.5.9",
    "version_number": "0.5.9",
    "game_versions": ["1.20.1"],
    "loaders": ["fabric"],
    "version_type": "release",
    "date_published": "2024-06-01T00:00:00Z",
    "dependencies": [{"dependency_type": "required", "project_id": "fabric-api"}],
    "files": [
        {
            "primary": True,
            "filename": "sodium-fabric-0.5.9.jar",
            "url": "PLACEHOLDER/files/sodium-fabric-0.5.9.jar",
            "size": 11,
            "hashes": {"sha1": "SHA1_B", "sha512": "SHA512_B"},
        }
    ],
}


class FakePlatformHandler(BaseHTTPRequestHandler):
    files: dict = {}
    sha1_a = ""
    sha1_b = ""
    base = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _raw(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/java-archive")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _versions(self):
        import copy

        out = []
        for v, sha in ((MR_V2, self.sha1_b), (MR_V1, self.sha1_a)):
            v = copy.deepcopy(v)
            v["files"][0]["url"] = v["files"][0]["url"].replace("PLACEHOLDER", self.base)
            v["files"][0]["hashes"]["sha1"] = sha
            out.append(v)
        return out

    # ------------------------------------------------------------------ #
    def do_GET(self):  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        p = u.path

        if p == "/v2/search":
            facets = json.loads(q.get("facets", ["[]"])[0])
            return self._json(
                200,
                {
                    "hits": [
                        {
                            "project_id": MR_PROJECT,
                            "slug": "sodium",
                            "title": "Sodium",
                            "description": "Rendering engine",
                            "downloads": 4_200_000,
                            "author": "jellysquid3",
                            "categories": ["optimization", "fabric"],
                        }
                    ],
                    "total_hits": 1,
                    "_echo_facets": facets,
                },
            )
        if p == f"/v2/project/{MR_PROJECT}/version":
            return self._json(200, self._versions())
        if p == f"/v2/project/{MR_PROJECT}":
            return self._json(200, {"title": "Sodium", "slug": "sodium"})
        if p.startswith("/v2/version/"):
            vid = p.rsplit("/", 1)[-1]
            v = next((x for x in self._versions() if x["id"] == vid), None)
            return self._json(200, v) if v else self._json(404, {})
        if p.startswith("/files/"):
            name = p.rsplit("/", 1)[-1]
            data = self.files.get(name)
            return self._raw(200, data) if data else self._json(404, {})

        # --- CurseForge
        if p == "/v1/mods/search":
            if self.headers.get("x-api-key") != CF_KEY:
                return self._json(403, {"error": "bad key"})
            return self._json(
                200,
                {
                    "data": [
                        {
                            "id": 238222,
                            "slug": "jei",
                            "name": "Just Enough Items",
                            "summary": "Item and recipe viewer",
                            "downloadCount": 300_000_000,
                            "authors": [{"name": "mezz"}],
                            "categories": [{"name": "Map and Information"}],
                            "links": {"websiteUrl": "https://example.test/jei"},
                            "logo": {"thumbnailUrl": ""},
                        }
                    ],
                    "pagination": {"totalCount": 1},
                },
            )
        if p.startswith("/v1/mods/") and p.endswith("/files"):
            if self.headers.get("x-api-key") != CF_KEY:
                return self._json(403, {"error": "bad key"})
            return self._json(
                200,
                {
                    "data": [
                        {
                            "id": 5000002,
                            "modId": 238222,
                            "displayName": "jei-1.20.1-15.3.0.jar",
                            "fileName": "jei-1.20.1-15.3.0.jar",
                            "releaseType": 1,
                            "fileLength": 11,
                            "fileDate": "2024-06-01T00:00:00Z",
                            "downloadUrl": f"{self.base}/files/jei-1.20.1-15.3.0.jar",
                            "gameVersions": ["1.20.1", "Forge"],
                            "hashes": [{"algo": 1, "value": "deadbeef"}],
                            "dependencies": [],
                        }
                    ]
                },
            )
        return self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        p = u.path

        if p == "/v2/version_files":
            versions = {v["files"][0]["hashes"]["sha1"]: v for v in self._versions()}
            return self._json(
                200, {h: versions[h] for h in body.get("hashes", []) if h in versions}
            )
        if p == "/v2/version_files/update":
            # Whatever known hash you send, the newest build comes back.
            known = {v["files"][0]["hashes"]["sha1"] for v in self._versions()}
            newest = self._versions()[0]
            return self._json(
                200, {h: newest for h in body.get("hashes", []) if h in known}
            )
        if p == "/v1/fingerprints/matches":
            if self.headers.get("x-api-key") != CF_KEY:
                return self._json(403, {"error": "bad key"})
            prints = body.get("fingerprints", [])
            return self._json(
                200,
                {
                    "data": {
                        "exactMatches": [
                            {
                                "file": {
                                    "id": 5000001,
                                    "modId": 238222,
                                    "fileFingerprint": prints[0] if prints else 0,
                                    "displayName": "jei-1.20.1-15.2.0.jar",
                                    "fileName": "jei-1.20.1-15.2.0.jar",
                                    "releaseType": 1,
                                    "gameVersions": ["1.20.1", "Forge"],
                                    "hashes": [],
                                    "dependencies": [],
                                }
                            }
                        ]
                        if prints
                        else []
                    }
                },
            )
        return self._json(404, {"error": "not found"})


class FakePlatforms:
    def __init__(self, files: dict, sha1_a: str, sha1_b: str) -> None:
        handler = type(
            "BoundPlatform",
            (FakePlatformHandler,),
            {"files": files, "sha1_a": sha1_a, "sha1_b": sha1_b},
        )
        self.handler = handler
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        handler.base = self.url
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
