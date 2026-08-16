"""CurseForge (api.curseforge.com/v1). Requires a free API key.

Get one at https://console.curseforge.com/ -> API Keys, then paste it into
Settings -> CurseForge API key.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

import requests

from ..models import ProjectHit, VersionInfo
from .base import ModProvider, ProviderError, RateLimited

log = logging.getLogger(__name__)

API = "https://api.curseforge.com"
SITE = "https://www.curseforge.com/minecraft/mc-mods"
GAME_MINECRAFT = 432
CLASS_MODS = 6

# modLoaderType enum
LOADER_IDS = {
    "any": 0,
    "forge": 1,
    "cauldron": 2,
    "liteloader": 3,
    "fabric": 4,
    "quilt": 5,
    "neoforge": 6,
}
RELEASE_TYPES = {1: "release", 2: "beta", 3: "alpha"}


class CurseForgeProvider(ModProvider):
    name = "curseforge"
    label = "CurseForge"
    needs_key = True

    def __init__(self, api_key: str = "", timeout: int = 25) -> None:
        self.api_key = (api_key or "").strip()
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "CraftyModManager/1.0",
            }
        )
        if self.api_key:
            self.session.headers["x-api-key"] = self.api_key

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def set_key(self, key: str) -> None:
        self.api_key = (key or "").strip()
        if self.api_key:
            self.session.headers["x-api-key"] = self.api_key
        else:
            self.session.headers.pop("x-api-key", None)

    # ------------------------------------------------------------------ #
    def _call(self, method: str, path: str, **kw) -> Any:
        if not self.api_key:
            raise ProviderError(
                "No CurseForge API key set. Add one in Settings, or use Modrinth."
            )
        url = f"{API}{path}"
        for attempt in range(3):
            try:
                r = self.session.request(method, url, timeout=self.timeout, **kw)
            except requests.RequestException as exc:
                if attempt == 2:
                    raise ProviderError(f"CurseForge unreachable: {exc}") from exc
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code in (401, 403):
                raise ProviderError("CurseForge rejected the API key (401/403).")
            if r.status_code == 429:
                if attempt == 2:
                    raise RateLimited("CurseForge rate limit hit; try again shortly.")
                time.sleep(3 * (attempt + 1))
                continue
            if r.status_code == 404:
                return None
            if r.status_code >= 400:
                raise ProviderError(f"CurseForge HTTP {r.status_code}: {r.text[:200]}")
            try:
                return r.json()
            except ValueError as exc:
                raise ProviderError(f"CurseForge returned invalid JSON: {exc}") from exc
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
        params: dict[str, Any] = {
            "gameId": GAME_MINECRAFT,
            "classId": CLASS_MODS,
            "index": max(0, offset),
            "pageSize": max(1, min(limit, 50)),
            "sortField": 2 if query else 6,  # 2=Popularity, 6=TotalDownloads
            "sortOrder": "desc",
        }
        if query:
            params["searchFilter"] = query
        if mc_version:
            params["gameVersion"] = mc_version
        lid = LOADER_IDS.get((loader or "").lower())
        if lid:
            params["modLoaderType"] = lid

        data = self._call("GET", "/v1/mods/search", params=params) or {}
        hits: list[ProjectHit] = []
        for m in data.get("data") or []:
            slug = str(m.get("slug") or "")
            authors = m.get("authors") or []
            logo = (m.get("logo") or {}).get("thumbnailUrl") or ""
            hits.append(
                ProjectHit(
                    source=self.name,
                    project_id=str(m.get("id") or ""),
                    slug=slug,
                    title=str(m.get("name") or slug),
                    description=str(m.get("summary") or ""),
                    downloads=int(m.get("downloadCount") or 0),
                    author=str(authors[0].get("name")) if authors else "",
                    icon_url=str(logo),
                    categories=[str(c.get("name")) for c in (m.get("categories") or [])],
                    page_url=str(m.get("links", {}).get("websiteUrl") or f"{SITE}/{slug}"),
                    raw=m,
                )
            )
        return hits

    # ------------------------------------------------------------------ #
    def versions(
        self, project_id: str, loader: str = "", mc_version: str = ""
    ) -> list[VersionInfo]:
        params: dict[str, Any] = {"pageSize": 50}
        if mc_version:
            params["gameVersion"] = mc_version
        lid = LOADER_IDS.get((loader or "").lower())
        if lid:
            params["modLoaderType"] = lid
        data = self._call("GET", f"/v1/mods/{project_id}/files", params=params) or {}
        return [self._version(f, project_id) for f in (data.get("data") or [])]

    def _version(self, f: dict, project_id: str = "") -> VersionInfo:
        hashes = {int(h.get("algo", 0)): str(h.get("value", "")) for h in (f.get("hashes") or [])}
        gv = [
            str(v)
            for v in (f.get("gameVersions") or [])
            if str(v)[:1].isdigit()
        ]
        loaders = [
            str(v).lower()
            for v in (f.get("gameVersions") or [])
            if not str(v)[:1].isdigit()
        ]
        url = str(f.get("downloadUrl") or "")
        fid = str(f.get("id") or "")
        pid = str(project_id or f.get("modId") or "")
        if not url and fid and pid:
            # CurseForge nulls downloadUrl for projects with 3rd-party downloads
            # disabled; reconstruct the CDN path.
            n = int(fid)
            url = (
                f"https://edge.forgecdn.net/files/{n // 1000}/{n % 1000}/"
                f"{f.get('fileName')}"
            )
        return VersionInfo(
            source="curseforge",
            version_id=fid,
            project_id=pid,
            name=str(f.get("displayName") or f.get("fileName") or ""),
            version_number=str(f.get("displayName") or f.get("fileName") or ""),
            filename=str(f.get("fileName") or ""),
            download_url=url,
            size=int(f.get("fileLength") or 0),
            game_versions=gv,
            loaders=loaders,
            release_type=RELEASE_TYPES.get(int(f.get("releaseType") or 1), "release"),
            date_published=str(f.get("fileDate") or "")[:10],
            sha1=hashes.get(1, ""),
            dependencies=list(f.get("dependencies") or []),
            raw=f,
        )

    def download_url_for(self, project_id: str, file_id: str) -> str:
        d = self._call("GET", f"/v1/mods/{project_id}/files/{file_id}/download-url")
        return str((d or {}).get("data") or "")

    # ------------------------------------------------------------------ #
    def identify(self, mods: list) -> dict:
        """POST /v1/fingerprints/matches - resolve jars by murmur2 fingerprint."""
        prints = [m.murmur2 for m in mods if m.murmur2]
        if not prints:
            return {}
        by_print: dict[int, dict] = {}
        for batch in _chunks(prints, 100):
            data = self._call(
                "POST", "/v1/fingerprints/matches", json={"fingerprints": batch}
            ) or {}
            for match in ((data.get("data") or {}).get("exactMatches") or []):
                f = match.get("file") or {}
                fp = int(f.get("fileFingerprint") or 0)
                if fp:
                    by_print[fp] = f
        out: dict[str, VersionInfo] = {}
        for m in mods:
            f = by_print.get(m.murmur2)
            if f:
                out[m.filename] = self._version(f)
        return out

    def latest_for(self, mods: list, loader: str = "", mc_version: str = "") -> dict:
        """No batch 'newest' endpoint - query per identified project."""
        out: dict[str, VersionInfo] = {}
        seen: dict[str, list[VersionInfo]] = {}
        for m in mods:
            if m.source != self.name or not m.project_id:
                continue
            if m.project_id not in seen:
                try:
                    seen[m.project_id] = self.versions(m.project_id, loader, mc_version)
                except ProviderError as exc:
                    log.warning("CurseForge versions(%s) failed: %s", m.project_id, exc)
                    seen[m.project_id] = []
            releases = [v for v in seen[m.project_id] if v.release_type == "release"]
            pick = (releases or seen[m.project_id])
            if pick:
                out[m.filename] = pick[0]
        return out

    # ------------------------------------------------------------------ #
    def download(self, version: VersionInfo) -> bytes:
        url = version.download_url
        if not url and version.project_id and version.version_id:
            url = self.download_url_for(version.project_id, version.version_id)
        if not url:
            raise ProviderError(
                f"CurseForge did not provide a download URL for {version.filename}. "
                "The author may have disabled third-party downloads - grab the jar "
                "from the website and use 'Install from file'."
            )
        try:
            r = self.session.get(url, timeout=180, stream=True)
            r.raise_for_status()
        except requests.RequestException as exc:
            raise ProviderError(f"Download failed: {exc}") from exc
        return r.content


def _chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]
