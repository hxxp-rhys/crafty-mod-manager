"""Orchestration: scan the mods folder, identify jars, install/update/roll back,
and keep local backups of everything we overwrite.

Nothing in here touches Qt, so it can be exercised from tests or a script.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Iterable, Optional

from .backends.base import BackendError, ServerBackend, join, norm
from .config import Profile, Settings, data_dir
from .models import ModEntry, VersionInfo
from .modmeta import murmur2_of, parse_jar, sha1_of, sha512_of
from .providers.base import ModProvider, ProviderError

log = logging.getLogger(__name__)

JAR_EXT = (".jar", ".jar.disabled")
Progress = Optional[Callable[[str, int, int], None]]

TEXT_EXT = {
    "properties", "toml", "json", "json5", "yml", "yaml", "cfg", "conf", "ini",
    "txt", "md", "snbt", "js", "ts", "sh", "bat", "xml", "log", "csv", "mcfunction",
    "hjson", "lang", "nbt5", "sk",
}


def is_text_file(name: str) -> bool:
    return name.rsplit(".", 1)[-1].lower() in TEXT_EXT if "." in name else False


# --------------------------------------------------------------------------- #
#  Local jar cache (avoid re-downloading jars we've already hashed)
# --------------------------------------------------------------------------- #
class JarCache:
    def __init__(self, profile_id: str) -> None:
        self.dir = data_dir("cache", profile_id)
        self.index_path = self.dir / "index.json"
        self.index: dict[str, dict] = {}
        if self.index_path.exists():
            try:
                self.index = json.loads(self.index_path.read_text("utf-8"))
            except Exception:
                self.index = {}

    @staticmethod
    def key(filename: str, size: int, modified: Optional[float]) -> str:
        return f"{filename}|{size}|{int(modified or 0)}"

    def get(self, k: str) -> Optional[dict]:
        return self.index.get(k)

    def put(self, k: str, payload: dict) -> None:
        self.index[k] = payload
        self._flush()

    def _flush(self) -> None:
        try:
            self.index_path.write_text(json.dumps(self.index), "utf-8")
        except OSError as exc:  # pragma: no cover
            log.warning("cache write failed: %s", exc)

    def prune(self, keep: Iterable[str]) -> None:
        keepset = set(keep)
        removed = [k for k in self.index if k not in keepset]
        for k in removed:
            self.index.pop(k, None)
        if removed:
            self._flush()


# --------------------------------------------------------------------------- #
#  Local state (pins, manual source overrides)
# --------------------------------------------------------------------------- #
class ModState:
    def __init__(self, profile_id: str) -> None:
        self.path = data_dir("state") / f"{profile_id}.json"
        self.data: dict[str, dict] = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text("utf-8"))
            except Exception:
                self.data = {}

    def get(self, sha1: str) -> dict:
        return self.data.get(sha1, {})

    def set(self, sha1: str, **kw) -> None:
        if not sha1:
            return
        entry = self.data.setdefault(sha1, {})
        entry.update(kw)
        self.save()

    def save(self) -> None:
        try:
            self.path.write_text(json.dumps(self.data, indent=2), "utf-8")
        except OSError as exc:  # pragma: no cover
            log.warning("state write failed: %s", exc)


# --------------------------------------------------------------------------- #
#  Backups
# --------------------------------------------------------------------------- #
class BackupStore:
    """Every jar we replace/delete and every config we overwrite gets copied
    here first, with a manifest so it can be restored."""

    def __init__(self, profile_id: str, keep: int = 25) -> None:
        self.root = data_dir("backups", profile_id)
        self.manifest_path = self.root / "manifest.json"
        self.keep = keep
        self.entries: list[dict] = []
        if self.manifest_path.exists():
            try:
                self.entries = json.loads(self.manifest_path.read_text("utf-8"))
            except Exception:
                self.entries = []

    def _save(self) -> None:
        try:
            self.manifest_path.write_text(json.dumps(self.entries, indent=2), "utf-8")
        except OSError as exc:  # pragma: no cover
            log.warning("manifest write failed: %s", exc)

    def add(self, remote_path: str, data: bytes, kind: str, note: str = "") -> dict:
        ts = time.time()
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", remote_path)[-120:]
        # uuid suffix: two saves inside the same second must not collide, or
        # pruning the older manifest entry would delete the newer file.
        local = self.root / f"{int(ts)}_{uuid.uuid4().hex[:8]}_{safe}"
        local.write_bytes(data)
        entry = {
            "id": local.name,
            "remote_path": remote_path,
            "kind": kind,  # "mod" | "config"
            "note": note,
            "size": len(data),
            "timestamp": ts,
            "time_text": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
        }
        self.entries.insert(0, entry)
        self._prune()
        self._save()
        return entry

    def _prune(self) -> None:
        # Keep the newest N per remote_path.
        seen: dict[str, int] = {}
        survivors: list[dict] = []
        for e in self.entries:
            n = seen.get(e["remote_path"], 0)
            if n < self.keep:
                survivors.append(e)
                seen[e["remote_path"]] = n + 1
            else:
                try:
                    (self.root / e["id"]).unlink(missing_ok=True)
                except OSError:
                    pass
        self.entries = survivors

    def read(self, backup_id: str) -> bytes:
        p = self.root / backup_id
        if not p.exists():
            raise BackendError(f"Backup {backup_id} is missing from disk.")
        return p.read_bytes()

    def for_path(self, remote_path: str) -> list[dict]:
        return [e for e in self.entries if e["remote_path"] == remote_path]

    def all(self) -> list[dict]:
        return list(self.entries)


# --------------------------------------------------------------------------- #
#  Manager
# --------------------------------------------------------------------------- #
class ModManager:
    def __init__(
        self,
        backend: ServerBackend,
        profile: Profile,
        settings: Settings,
        providers: dict[str, ModProvider],
    ) -> None:
        self.backend = backend
        self.profile = profile
        self.settings = settings
        self.providers = providers
        self.cache = JarCache(profile.id)
        self.state = ModState(profile.id)
        self.backups = BackupStore(profile.id, keep=settings.keep_backups)
        self.mods: list[ModEntry] = []
        self.detected_loader: str = ""
        self.detected_mc: str = ""

    # -- helpers --------------------------------------------------------- #
    @property
    def mods_dir(self) -> str:
        return norm(self.profile.mods_dir or "mods")

    @property
    def loader(self) -> str:
        if self.profile.loader and self.profile.loader != "auto":
            return self.profile.loader
        return self.detected_loader or ""

    @property
    def mc_version(self) -> str:
        return self.profile.mc_version or self.detected_mc or ""

    # -- scan ------------------------------------------------------------ #
    def scan(self, progress: Progress = None) -> list[ModEntry]:
        """List the mods folder, hash + parse every jar (cached by size/mtime)."""
        try:
            entries = self.backend.list_dir(self.mods_dir)
        except BackendError as exc:
            raise BackendError(
                f"Could not open '{self.mods_dir}' on the server: {exc}\n"
                "Check the mods folder path in the connection settings."
            ) from exc

        jars = [
            e
            for e in entries
            if not e.is_dir and e.name.lower().endswith(JAR_EXT)
        ]
        total = len(jars)
        out: list[ModEntry] = []
        live_keys: list[str] = []

        for i, e in enumerate(jars, 1):
            if progress:
                progress(f"Reading {e.name}", i, total)
            key = JarCache.key(e.name, e.size, e.modified)
            live_keys.append(key)
            cached = self.cache.get(key)
            mod = ModEntry(
                filename=e.name,
                path=e.path,
                size=e.size,
                modified=e.modified,
            )
            if cached:
                mod.sha1 = cached.get("sha1", "")
                mod.sha512 = cached.get("sha512", "")
                mod.murmur2 = int(cached.get("murmur2") or 0)
                mod.meta = _meta_from_dict(cached.get("meta") or {})
            else:
                try:
                    data = self.backend.read_bytes(e.path)
                except BackendError as exc:
                    mod.meta.error = f"Could not download: {exc}"
                    out.append(mod)
                    continue
                mod.sha1 = sha1_of(data)
                mod.sha512 = sha512_of(data)
                mod.murmur2 = murmur2_of(data)
                mod.meta = parse_jar(data, e.name)
                self.cache.put(
                    key,
                    {
                        "sha1": mod.sha1,
                        "sha512": mod.sha512,
                        "murmur2": mod.murmur2,
                        "meta": asdict(mod.meta),
                    },
                )
            st = self.state.get(mod.sha1)
            mod.pinned = bool(st.get("pinned"))
            if st.get("source"):
                mod.source = st["source"]
                mod.project_id = st.get("project_id", "")
                mod.project_name = st.get("project_name", "")
                mod.version_id = st.get("version_id", "")
                mod.version_number = st.get("version_number", "")
                mod.page_url = st.get("page_url", "")
                mod.hash_known = True
            out.append(mod)

        self.cache.prune(live_keys)
        self.mods = out
        self._detect_environment()
        return out

    def _detect_environment(self) -> None:
        loaders: dict[str, int] = {}
        mcs: dict[str, int] = {}
        for m in self.mods:
            ldr = m.meta.loader
            if ldr and ldr not in ("unknown", "multi"):
                loaders[ldr] = loaders.get(ldr, 0) + 1
            for v in m.meta.mc_versions:
                if re.fullmatch(r"1\.\d{1,2}(\.\d{1,2})?", v):
                    mcs[v] = mcs.get(v, 0) + 1
        self.detected_loader = max(loaders, key=loaders.get) if loaders else ""
        self.detected_mc = max(mcs, key=mcs.get) if mcs else ""

    # -- identify -------------------------------------------------------- #
    def identify(self, progress: Progress = None) -> int:
        """Match local jars to Modrinth / CurseForge projects by hash."""
        unknown = [m for m in self.mods if not m.hash_known and m.sha1]
        if not unknown:
            return 0
        matched = 0

        mr = self.providers.get("modrinth")
        if mr and mr.available:
            if progress:
                progress("Matching against Modrinth…", 1, 2)
            try:
                found = mr.identify(unknown)
            except ProviderError as exc:
                log.warning("Modrinth identify failed: %s", exc)
                found = {}
            for m in unknown:
                v = found.get(m.filename)
                if not v:
                    continue
                self._apply_identity(m, v, "modrinth")
                matched += 1

        remaining = [m for m in unknown if not m.hash_known]
        cf = self.providers.get("curseforge")
        if remaining and cf and cf.available:
            if progress:
                progress("Matching against CurseForge…", 2, 2)
            try:
                found = cf.identify(remaining)
            except ProviderError as exc:
                log.warning("CurseForge identify failed: %s", exc)
                found = {}
            for m in remaining:
                v = found.get(m.filename)
                if not v:
                    continue
                self._apply_identity(m, v, "curseforge")
                matched += 1
        return matched

    def _apply_identity(self, m: ModEntry, v: VersionInfo, source: str) -> None:
        m.source = source
        m.project_id = v.project_id
        m.version_id = v.version_id
        m.version_number = v.version_number or v.name
        m.hash_known = True
        if source == "modrinth":
            m.page_url = f"https://modrinth.com/mod/{v.project_id}"
        else:
            m.page_url = f"https://www.curseforge.com/projects/{v.project_id}"
        if not m.project_name:
            m.project_name = m.meta.name or v.name
        self.state.set(
            m.sha1,
            source=source,
            project_id=v.project_id,
            project_name=m.project_name,
            version_id=v.version_id,
            version_number=m.version_number,
            page_url=m.page_url,
        )

    def resolve_names(self, progress: Progress = None) -> None:
        """Fill in nice project titles for Modrinth-identified mods."""
        mr = self.providers.get("modrinth")
        if not mr:
            return
        todo = [
            m
            for m in self.mods
            if m.source == "modrinth" and m.project_id and not m.project_name
        ]
        for i, m in enumerate(todo, 1):
            if progress:
                progress(f"Resolving {m.filename}", i, len(todo))
            try:
                proj = mr.project(m.project_id)
            except ProviderError:
                continue
            if proj:
                m.project_name = str(proj.get("title") or "")
                m.project_slug = str(proj.get("slug") or "")
                if m.project_slug:
                    m.page_url = f"https://modrinth.com/mod/{m.project_slug}"
                self.state.set(
                    m.sha1, project_name=m.project_name, page_url=m.page_url
                )

    # -- updates --------------------------------------------------------- #
    def check_updates(self, progress: Progress = None) -> int:
        loader = self.loader
        mcv = self.mc_version
        found = 0

        mr = self.providers.get("modrinth")
        mr_mods = [m for m in self.mods if m.sha1 and m.source in ("modrinth", "")]
        if mr and mr.available and mr_mods:
            if progress:
                progress("Checking Modrinth for updates…", 1, 2)
            try:
                latest = mr.latest_for(mr_mods, loader, mcv)
            except ProviderError as exc:
                log.warning("Modrinth update check failed: %s", exc)
                latest = {}
            for m in mr_mods:
                v = latest.get(m.filename)
                if v:
                    self._apply_latest(m, v)
                    found += int(m.update_available)

        cf = self.providers.get("curseforge")
        cf_mods = [m for m in self.mods if m.source == "curseforge" and m.project_id]
        if cf and cf.available and cf_mods:
            if progress:
                progress("Checking CurseForge for updates…", 2, 2)
            try:
                latest = cf.latest_for(cf_mods, loader, mcv)
            except ProviderError as exc:
                log.warning("CurseForge update check failed: %s", exc)
                latest = {}
            for m in cf_mods:
                v = latest.get(m.filename)
                if v:
                    self._apply_latest(m, v)
                    found += int(m.update_available)
        return found

    @staticmethod
    def _apply_latest(m: ModEntry, v: VersionInfo) -> None:
        m.latest_version_id = v.version_id
        m.latest_version_number = v.version_number or v.name
        m.latest_filename = v.filename
        m.latest_url = v.download_url
        m.latest_date = v.date_published
        if not m.version_id:
            # Modrinth's update endpoint echoes the current version when it is
            # already newest; treat a matching sha1 as "current".
            if v.sha1 and v.sha1 == m.sha1:
                m.version_id = v.version_id
                m.version_number = v.version_number
                m.source = m.source or v.source
                m.project_id = m.project_id or v.project_id

    # -- mutations ------------------------------------------------------- #
    def install_version(
        self,
        version: VersionInfo,
        replace: Optional[ModEntry] = None,
        progress: Progress = None,
    ) -> str:
        """Download a version and put it in the mods folder. When ``replace`` is
        given, the old jar is backed up locally and then removed."""
        provider = self.providers.get(version.source)
        if not provider:
            raise BackendError(f"No provider for source '{version.source}'.")
        if progress:
            progress(f"Downloading {version.filename or version.version_number}", 1, 3)
        data = provider.download(version)
        if not data:
            raise BackendError("Downloaded file was empty.")
        if version.sha1 and sha1_of(data) != version.sha1:
            raise BackendError(
                f"Checksum mismatch on {version.filename} - refusing to install."
            )

        filename = version.filename or f"{version.version_number}.jar"
        if not filename.lower().endswith(".jar"):
            filename += ".jar"
        # Replacing a disabled jar must not silently re-enable the mod.
        if replace is not None and replace.disabled:
            filename += ".disabled"

        if replace is not None:
            if progress:
                progress(f"Backing up {replace.filename}", 2, 3)
            self._backup_mod(replace)

        if progress:
            progress(f"Uploading {filename}", 3, 3)
        self.backend.upload_bytes(self.mods_dir, filename, data)

        if replace is not None and replace.filename != filename:
            try:
                self.backend.delete(replace.path)
            except BackendError as exc:
                log.warning("Could not remove old jar %s: %s", replace.path, exc)

        # Remember identity for the new file so the next scan skips a lookup.
        self.state.set(
            sha1_of(data),
            source=version.source,
            project_id=version.project_id,
            project_name=replace.project_name if replace else version.name,
            version_id=version.version_id,
            version_number=version.version_number,
            page_url=(
                f"https://modrinth.com/mod/{version.project_id}"
                if version.source == "modrinth"
                else f"https://www.curseforge.com/projects/{version.project_id}"
            ),
        )
        return filename

    def install_local_jar(self, local_path: str, progress: Progress = None) -> str:
        p = Path(local_path)
        data = p.read_bytes()
        if progress:
            progress(f"Uploading {p.name}", 1, 1)
        self.backend.upload_bytes(self.mods_dir, p.name, data)
        return p.name

    def _backup_mod(self, mod: ModEntry) -> None:
        """Copy a jar into the local backup store.

        If backups are enabled and this fails, we raise rather than let the
        caller delete or overwrite: the UI promises a backup was taken, so
        silently skipping it would be a data-loss bug.
        """
        if not self.settings.backup_mods:
            return
        try:
            data = self.backend.read_bytes(mod.path)
        except BackendError as exc:
            raise BackendError(
                f"Could not back up {mod.filename} before changing it: {exc}\n\n"
                "Nothing was modified. Fix the connection, or turn off "
                "'Back up mod jars' in Settings to proceed without a backup."
            ) from exc
        try:
            self.backups.add(
                mod.path, data, "mod", note=f"{mod.display_name} {mod.current_version}"
            )
        except OSError as exc:
            raise BackendError(
                f"Could not write the local backup of {mod.filename}: {exc}\n\n"
                "Nothing was modified. Check free disk space."
            ) from exc

    def set_disabled(self, mod: ModEntry, disabled: bool) -> str:
        if mod.disabled == disabled:
            return mod.filename
        new_name = (
            mod.filename + ".disabled" if disabled else mod.filename[: -len(".disabled")]
        )
        self.backend.rename(mod.path, new_name)
        return new_name

    def delete_mod(self, mod: ModEntry, backup: bool = True) -> None:
        if backup:
            self._backup_mod(mod)
        self.backend.delete(mod.path)

    def set_pinned(self, mod: ModEntry, pinned: bool) -> None:
        mod.pinned = pinned
        self.state.set(mod.sha1, pinned=pinned)

    def restore_backup(self, backup_id: str, progress: Progress = None) -> str:
        entry = next((e for e in self.backups.all() if e["id"] == backup_id), None)
        if not entry:
            raise BackendError("That backup is no longer in the manifest.")
        data = self.backups.read(backup_id)
        remote = entry["remote_path"]
        directory = remote.rsplit("/", 1)[0] if "/" in remote else ""
        filename = remote.rsplit("/", 1)[-1]
        if progress:
            progress(f"Restoring {filename}", 1, 1)
        self.backend.upload_bytes(directory, filename, data)
        return remote

    # -- config files ---------------------------------------------------- #
    def read_config(self, path: str) -> tuple[str, Optional[float]]:
        return self.backend.read_text(path)

    def write_config(
        self,
        path: str,
        contents: str,
        expect_mtime: Optional[float] = None,
        overwrite: bool = False,
        original: Optional[str] = None,
    ) -> Optional[float]:
        if self.settings.backup_configs:
            # Back up what is about to be destroyed. On a forced overwrite the
            # remote has moved on since we opened it, so the stale editor copy
            # is the wrong thing to save - re-read the live file instead.
            snapshot = original
            if overwrite:
                try:
                    snapshot, _ = self.backend.read_text(path)
                except BackendError as exc:
                    log.warning("could not re-read %s before overwrite: %s", path, exc)
            if snapshot is not None:
                self.backups.add(path, snapshot.encode("utf-8"), "config")
        return self.backend.write_text(
            path, contents, expect_mtime=expect_mtime, overwrite=overwrite
        )


def _meta_from_dict(d: dict):
    from .models import JarMeta

    known = {f for f in JarMeta.__dataclass_fields__}  # type: ignore[attr-defined]
    return JarMeta(**{k: v for k, v in d.items() if k in known})
