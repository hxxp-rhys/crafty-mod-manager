"""Modrinth (api.modrinth.com/v2). No API key needed."""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

import requests

from ..models import ProjectHit, VersionInfo
from .base import ModProvider, ProviderError, RateLimited

log = logging.getLogger(__name__)

API = "https://api.modrinth.com/v2"
SITE = "https://modrinth.com"
UA = "CraftyModManager/1.0 (self-hosted Minecraft server admin tool)"


class ModrinthProvider(ModProvider):
    name = "modrinth"
    label = "Modrinth"
    needs_key = False

    def __init__(self, timeout: int = 25) -> None:
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA, "Accept": "application/json"})

    # ------------------------------------------------------------------ #
    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        return self._call("GET", path, params=params)

    def _post(self, path: str, payload: Any) -> Any:
        return self._call("POST", path, json=payload)

    def _call(self, method: str, path: str, **kw) -> Any:
        url = f"{API}{path}"
        for attempt in range(3):
            try:
                r = self.session.request(method, url, timeout=self.timeout, **kw)
            except requests.RequestException as exc:
                if attempt == 2:
                    raise ProviderError(f"Modrinth unreachable: {exc}") from exc
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code == 429:
                wait = int(r.headers.get("X-Ratelimit-Reset") or 5)
                if attempt == 2:
                    raise RateLimited(
                        f"Modrinth rate limit hit; try again in {wait}s."
                    )
                time.sleep(min(wait, 10))
                continue
            if r.status_code == 404:
                return None
            if r.status_code >= 400:
                raise ProviderError(f"Modrinth HTTP {r.status_code}: {r.text[:200]}")
            if not r.content:
                return None
            try:
                return r.json()
            except ValueError as exc:
                raise ProviderError(f"Modrinth returned invalid JSON: {exc}") from exc
        return None

    # ------------------------------------------------------------------ #
    def search(
        self,
        query: str,
        loader: str = "",
        mc_version: str = "",
        offset: int = 0,
        limit: int = 30,
        project_type: str = "mod",
    ) -> list[ProjectHit]:
        facets: list[list[str]] = []
        if project_type:
            facets.append([f"project_type:{project_type}"])
        if loader and loader not in ("auto", "unknown", "multi"):
            facets.append([f"categories:{loader}"])
        if mc_version:
            facets.append([f"versions:{mc_version}"])
        params: dict[str, Any] = {
            "limit": max(1, min(limit, 100)),
            "offset": max(0, offset),
            "index": "relevance" if query else "downloads",
        }
        if query:
            params["query"] = query
        if facets:
            params["facets"] = json.dumps(facets)

        data = self._get("/search", params) or {}
        hits: list[ProjectHit] = []
        for h in data.get("hits") or []:
            slug = str(h.get("slug") or h.get("project_id") or "")
            hits.append(
                ProjectHit(
                    source=self.name,
                    project_id=str(h.get("project_id") or ""),
                    slug=slug,
                    title=str(h.get("title") or slug),
                    description=str(h.get("description") or ""),
                    downloads=int(h.get("downloads") or 0),
                    author=str(h.get("author") or ""),
                    icon_url=str(h.get("icon_url") or ""),
                    categories=[str(c) for c in (h.get("categories") or [])],
                    page_url=f"{SITE}/mod/{slug}",
                    raw=h,
                )
            )
        return hits

    # ------------------------------------------------------------------ #
    def versions(
        self, project_id: str, loader: str = "", mc_version: str = ""
    ) -> list[VersionInfo]:
        params: dict[str, Any] = {}
        if loader and loader not in ("auto", "unknown", "multi"):
            params["loaders"] = json.dumps([loader])
        if mc_version:
            params["game_versions"] = json.dumps([mc_version])
        data = self._get(f"/project/{project_id}/version", params) or []
        return [self._version(v) for v in data]

    def version_by_id(self, version_id: str) -> Optional[VersionInfo]:
        d = self._get(f"/version/{version_id}")
        return self._version(d) if d else None

    def project(self, project_id: str) -> Optional[dict]:
        return self._get(f"/project/{project_id}")

    @staticmethod
    def _version(v: dict) -> VersionInfo:
        files = v.get("files") or []
        primary = next((f for f in files if f.get("primary")), files[0] if files else {})
        hashes = primary.get("hashes") or {}
        return VersionInfo(
            source="modrinth",
            version_id=str(v.get("id") or ""),
            project_id=str(v.get("project_id") or ""),
            name=str(v.get("name") or ""),
            version_number=str(v.get("version_number") or ""),
            filename=str(primary.get("filename") or ""),
            download_url=str(primary.get("url") or ""),
            size=int(primary.get("size") or 0),
            game_versions=[str(g) for g in (v.get("game_versions") or [])],
            loaders=[str(x) for x in (v.get("loaders") or [])],
            release_type=str(v.get("version_type") or ""),
            date_published=str(v.get("date_published") or "")[:10],
            sha1=str(hashes.get("sha1") or ""),
            sha512=str(hashes.get("sha512") or ""),
            dependencies=list(v.get("dependencies") or []),
            raw=v,
        )

    # ------------------------------------------------------------------ #
    def identify(self, mods: list) -> dict:
        """POST /version_files - resolve installed jars by sha1."""
        hashes = [m.sha1 for m in mods if m.sha1]
        if not hashes:
            return {}
        out: dict[str, VersionInfo] = {}
        by_hash: dict[str, Any] = {}
        for batch in _chunks(hashes, 200):
            got = self._post(
                "/version_files", {"hashes": batch, "algorithm": "sha1"}
            ) or {}
            by_hash.update(got)
        for m in mods:
            v = by_hash.get(m.sha1)
            if v:
                out[m.filename] = self._version(v)
        return out

    def latest_for(self, mods: list, loader: str = "", mc_version: str = "") -> dict:
        """POST /version_files/update - newest build matching loader + MC version."""
        hashes = [m.sha1 for m in mods if m.sha1]
        if not hashes:
            return {}
        payload: dict[str, Any] = {"algorithm": "sha1"}
        if loader and loader not in ("auto", "unknown", "multi"):
            payload["loaders"] = [loader]
        if mc_version:
            payload["game_versions"] = [mc_version]
        by_hash: dict[str, Any] = {}
        for batch in _chunks(hashes, 200):
            got = self._post(
                "/version_files/update", dict(payload, hashes=batch)
            ) or {}
            by_hash.update(got)
        out: dict[str, VersionInfo] = {}
        for m in mods:
            v = by_hash.get(m.sha1)
            if v:
                out[m.filename] = self._version(v)
        return out

    # ------------------------------------------------------------------ #
    def download(self, version: VersionInfo) -> bytes:
        if not version.download_url:
            raise ProviderError(f"No download URL for {version.version_number}.")
        try:
            r = self.session.get(version.download_url, timeout=120, stream=True)
            r.raise_for_status()
        except requests.RequestException as exc:
            raise ProviderError(f"Download failed: {exc}") from exc
        return r.content


def _chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]
