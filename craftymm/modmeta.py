"""Read mod identity out of a .jar and compute the hashes the mod platforms use.

Supported descriptors:
  * ``fabric.mod.json``                 (Fabric)
  * ``quilt.mod.json``                  (Quilt)
  * ``META-INF/neoforge.mods.toml``     (NeoForge 1.20.5+)
  * ``META-INF/mods.toml``              (Forge 1.13+ / early NeoForge)
  * ``mcmod.info``                      (Forge 1.12 and older)
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import zipfile
from typing import Any, Optional

from .models import JarMeta

log = logging.getLogger(__name__)

try:  # Python 3.11+
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as _toml  # type: ignore[no-redef]
    except ModuleNotFoundError:
        _toml = None  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
#  Hashes
# --------------------------------------------------------------------------- #
def sha1_of(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def sha512_of(data: bytes) -> str:
    return hashlib.sha512(data).hexdigest()


_CF_SKIP = frozenset((9, 10, 13, 32))  # \t \n \r space


def murmur2_of(data: bytes) -> int:
    """CurseForge file fingerprint: MurmurHash2 (32-bit, seed 1) over the file
    with tabs, newlines, carriage returns and spaces removed."""
    buf = bytes(b for b in data if b not in _CF_SKIP)
    return _murmur2(buf, 1)


def _murmur2(data: bytes, seed: int = 1) -> int:
    m = 0x5BD1E995
    r = 24
    length = len(data)
    h = (seed ^ length) & 0xFFFFFFFF
    i = 0
    while length >= 4:
        k = int.from_bytes(data[i : i + 4], "little")
        k = (k * m) & 0xFFFFFFFF
        k ^= k >> r
        k = (k * m) & 0xFFFFFFFF
        h = (h * m) & 0xFFFFFFFF
        h ^= k
        i += 4
        length -= 4
    if length == 3:
        h ^= data[i + 2] << 16
    if length >= 2:
        h ^= data[i + 1] << 8
    if length >= 1:
        h ^= data[i]
        h = (h * m) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * m) & 0xFFFFFFFF
    h ^= h >> 15
    return h & 0xFFFFFFFF


# --------------------------------------------------------------------------- #
#  Descriptor parsing
# --------------------------------------------------------------------------- #
# Greedy name so the *last* version-looking token wins: in
# "SomeMod-fabric-1.19.2-2.0.1.jar" the version is 2.0.1, not "1.19.2-2.0.1".
# The trailing group swallows loader/platform tags like "-forge" or "-universal"
# so "ferritecore-6.0.1-forge.jar" still yields 6.0.1.
_TAIL_TAG = (
    r"(?:[-_](?:forge|fabric|neoforge|quilt|rift|liteloader|"
    r"mc\d[\w.]*|all|universal|release|server|client|dev|sources|api)){0,3}"
)
_VERSION_IN_NAME = re.compile(
    r"^(?P<name>.+)[-_]"
    r"(?P<version>v?\d+(?:\.[A-Za-z0-9]+)*(?:[+][A-Za-z0-9.\-]+)?)"
    + _TAIL_TAG
    + r"\.jar$",
    re.IGNORECASE,
)
_MC_IN_NAME = re.compile(r"(?:^|[-_])(?:mc)?(1\.\d{1,2}(?:\.\d{1,2})?)(?:$|[-_])", re.I)
_LOOKS_LIKE_MC = re.compile(r"^v?1\.\d{1,2}(\.\d{1,2})?$")
_NAME_NOISE = re.compile(
    r"[-_](?:forge|fabric|neoforge|quilt|mc)?\d[\w.]*$|[-_](?:forge|fabric|neoforge|quilt)$",
    re.IGNORECASE,
)


def _as_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, dict):
        return [str(x) for x in v.values() if isinstance(x, (str, int))]
    if isinstance(v, (list, tuple)):
        out: list[str] = []
        for x in v:
            if isinstance(x, str):
                out.append(x)
            elif isinstance(x, dict):
                for key in ("name", "id", "value"):
                    if key in x:
                        out.append(str(x[key]))
                        break
        return out
    return [str(v)]


def parse_jar(data: bytes, filename: str = "") -> JarMeta:
    """Best-effort metadata extraction. Never raises."""
    meta = JarMeta()
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())

            if "fabric.mod.json" in names:
                _from_fabric(zf.read("fabric.mod.json"), meta)
                meta.loader = "fabric"
            elif "quilt.mod.json" in names:
                _from_quilt(zf.read("quilt.mod.json"), meta)
                meta.loader = "quilt"

            for toml_name, loader in (
                ("META-INF/neoforge.mods.toml", "neoforge"),
                ("META-INF/mods.toml", "forge"),
            ):
                if toml_name in names and not meta.mod_id:
                    _from_mods_toml(zf.read(toml_name), meta, zf)
                    meta.loader = meta.loader or loader
                    break

            if not meta.mod_id and "mcmod.info" in names:
                _from_mcmod_info(zf.read("mcmod.info"), meta)
                meta.loader = meta.loader or "forge"

            # A jar can ship both fabric.mod.json and mods.toml (multi-loader).
            if "fabric.mod.json" in names and (
                "META-INF/mods.toml" in names or "META-INF/neoforge.mods.toml" in names
            ):
                meta.loader = "multi"
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        meta.error = f"Unreadable jar: {exc}"
    except Exception as exc:  # pragma: no cover - defensive
        meta.error = f"Metadata parse failed: {exc}"

    _fill_from_filename(meta, filename)
    return meta


def _match_version(base: str):
    """Match name+version, preferring a real mod version over a trailing
    Minecraft version ("Xaeros_Minimap_24.2.0_Fabric_1.20.jar" -> 24.2.0)."""
    m = _VERSION_IN_NAME.match(base)
    if m and _LOOKS_LIKE_MC.match(m.group("version")):
        better = _VERSION_IN_NAME.match(m.group("name") + ".jar")
        if better and not _LOOKS_LIKE_MC.match(better.group("version")):
            return better
    return m


def _fill_from_filename(meta: JarMeta, filename: str) -> None:
    base = filename[: -len(".disabled")] if filename.lower().endswith(".disabled") else filename
    if not meta.name or not meta.version:
        m = _match_version(base)
        if m:
            if not meta.name:
                raw = m.group("name")
                # Trim trailing "-1.20.1" / "-fabric" noise from the display name.
                for _ in range(3):
                    trimmed = _NAME_NOISE.sub("", raw)
                    if trimmed == raw or not trimmed:
                        break
                    raw = trimmed
                meta.name = raw.replace("_", " ").strip() or m.group("name")
            meta.version = meta.version or m.group("version")
    if not meta.name and base:
        meta.name = base.rsplit(".", 1)[0]
    if not meta.mc_versions:
        found = _MC_IN_NAME.findall(base)
        if found:
            meta.mc_versions = sorted(set(found))
    if not meta.loader:
        low = base.lower()
        for token, loader in (
            ("fabric", "fabric"),
            ("neoforge", "neoforge"),
            ("quilt", "quilt"),
            ("forge", "forge"),
        ):
            if token in low:
                meta.loader = loader
                break
        else:
            meta.loader = "unknown"


def _from_fabric(raw: bytes, meta: JarMeta) -> None:
    d = json.loads(raw.decode("utf-8", "replace"))
    meta.mod_id = str(d.get("id") or "")
    meta.name = str(d.get("name") or "")
    meta.version = str(d.get("version") or "")
    meta.description = str(d.get("description") or "").strip()
    meta.authors = _as_list(d.get("authors"))
    depends = d.get("depends") or {}
    if isinstance(depends, dict):
        meta.depends = [k for k in depends if k not in ("fabricloader", "java")]
        mc = depends.get("minecraft")
        if mc:
            meta.mc_versions = _as_list(mc)


def _from_quilt(raw: bytes, meta: JarMeta) -> None:
    d = json.loads(raw.decode("utf-8", "replace"))
    ql = d.get("quilt_loader") or {}
    meta.mod_id = str(ql.get("id") or "")
    meta.version = str(ql.get("version") or "")
    md = ql.get("metadata") or {}
    meta.name = str(md.get("name") or "")
    meta.description = str(md.get("description") or "").strip()
    contributors = md.get("contributors") or {}
    meta.authors = list(contributors.keys()) if isinstance(contributors, dict) else []
    deps = ql.get("depends") or []
    meta.depends = [
        str(x.get("id")) for x in deps if isinstance(x, dict) and x.get("id")
    ]


def _from_mods_toml(raw: bytes, meta: JarMeta, zf: zipfile.ZipFile) -> None:
    if _toml is None:  # pragma: no cover
        meta.error = "tomllib/tomli unavailable - cannot read mods.toml"
        return
    text = raw.decode("utf-8", "replace")
    try:
        d = _toml.loads(text)
    except Exception:
        # mods.toml occasionally ships invalid TOML; fall back to regex.
        d = {}
        mid = re.search(r'modId\s*=\s*"([^"]+)"', text)
        ver = re.search(r'version\s*=\s*"([^"]+)"', text)
        nam = re.search(r'displayName\s*=\s*"([^"]+)"', text)
        if mid:
            meta.mod_id = mid.group(1)
        if ver:
            meta.version = ver.group(1)
        if nam:
            meta.name = nam.group(1)
        return

    mods = d.get("mods") or []
    if mods:
        m0 = mods[0]
        meta.mod_id = str(m0.get("modId") or "")
        meta.name = str(m0.get("displayName") or "")
        meta.version = str(m0.get("version") or "")
        meta.description = str(m0.get("description") or "").strip()
        authors = m0.get("authors")
        meta.authors = _as_list(authors)

    # version = "${file.jarVersion}" -> read the real value from the manifest
    if meta.version.startswith("${"):
        meta.version = _manifest_version(zf) or meta.version

    deps = d.get("dependencies") or {}
    if isinstance(deps, dict):
        for _mod, entries in deps.items():
            if isinstance(entries, list):
                for e in entries:
                    if isinstance(e, dict):
                        dep = str(e.get("modId") or "")
                        if dep and dep not in ("minecraft", "forge", "neoforge"):
                            meta.depends.append(dep)
                        if dep == "minecraft":
                            rng = str(e.get("versionRange") or "")
                            found = re.findall(r"1\.\d{1,2}(?:\.\d{1,2})?", rng)
                            if found:
                                meta.mc_versions = sorted(set(found))
    meta.depends = sorted(set(meta.depends))


def _manifest_version(zf: zipfile.ZipFile) -> str:
    try:
        mf = zf.read("META-INF/MANIFEST.MF").decode("utf-8", "replace")
    except KeyError:
        return ""
    m = re.search(r"Implementation-Version:\s*(.+)", mf)
    return m.group(1).strip() if m else ""


def _from_mcmod_info(raw: bytes, meta: JarMeta) -> None:
    try:
        d = json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return
    if isinstance(d, dict):
        d = d.get("modList") or []
    if not isinstance(d, list) or not d:
        return
    m0 = d[0]
    meta.mod_id = str(m0.get("modid") or "")
    meta.name = str(m0.get("name") or "")
    meta.version = str(m0.get("version") or "")
    meta.description = str(m0.get("description") or "").strip()
    meta.authors = _as_list(m0.get("authorList") or m0.get("authors"))
    mcv = m0.get("mcversion")
    if mcv:
        meta.mc_versions = _as_list(mcv)
