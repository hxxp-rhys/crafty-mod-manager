"""Shared data structures for Crafty Mod Manager."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


# --------------------------------------------------------------------------- #
#  Remote filesystem
# --------------------------------------------------------------------------- #
@dataclass
class RemoteEntry:
    """A file or directory on the remote server, relative to the server root."""

    name: str
    path: str  # POSIX-style, relative to the server root ("" == root)
    is_dir: bool
    size: int = 0
    size_text: str = ""
    modified: Optional[float] = None  # epoch seconds, when known
    modified_text: str = ""

    @property
    def ext(self) -> str:
        return self.name.rsplit(".", 1)[-1].lower() if "." in self.name else ""


@dataclass
class ServerRef:
    """A server exposed by a backend."""

    id: str
    name: str
    path: str = ""
    server_type: str = ""
    running: Optional[bool] = None
    raw: dict = field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover - display helper
        return self.name or self.id


# --------------------------------------------------------------------------- #
#  Mods
# --------------------------------------------------------------------------- #
@dataclass
class JarMeta:
    """Metadata scraped out of a mod jar."""

    mod_id: str = ""
    name: str = ""
    version: str = ""
    loader: str = ""  # fabric / forge / neoforge / quilt / unknown
    mc_versions: list[str] = field(default_factory=list)
    authors: list[str] = field(default_factory=list)
    description: str = ""
    depends: list[str] = field(default_factory=list)
    error: str = ""


@dataclass
class ModEntry:
    """One jar in the server's mods folder, plus everything we know about it."""

    filename: str  # e.g. "sodium-fabric-0.5.8.jar" or "...jar.disabled"
    path: str  # relative path on the server
    size: int = 0
    modified: Optional[float] = None

    sha1: str = ""
    sha512: str = ""
    murmur2: int = 0

    meta: JarMeta = field(default_factory=JarMeta)

    # Resolved identity from an online provider
    source: str = ""  # "modrinth" | "curseforge" | ""
    project_id: str = ""
    project_slug: str = ""
    project_name: str = ""
    version_id: str = ""
    version_number: str = ""
    page_url: str = ""

    # Update check results
    latest_version_id: str = ""
    latest_version_number: str = ""
    latest_filename: str = ""
    latest_url: str = ""
    latest_date: str = ""

    pinned: bool = False
    hash_known: bool = False

    @property
    def disabled(self) -> bool:
        return self.filename.lower().endswith(".disabled")

    @property
    def base_filename(self) -> str:
        return self.filename[: -len(".disabled")] if self.disabled else self.filename

    @property
    def display_name(self) -> str:
        return (
            self.project_name
            or self.meta.name
            or self.meta.mod_id
            or self.base_filename
        )

    @property
    def current_version(self) -> str:
        return self.version_number or self.meta.version or "?"

    @property
    def update_available(self) -> bool:
        if self.pinned or not self.latest_version_id:
            return False
        return self.latest_version_id != self.version_id

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


# --------------------------------------------------------------------------- #
#  Provider search results
# --------------------------------------------------------------------------- #
@dataclass
class ProjectHit:
    source: str
    project_id: str
    slug: str
    title: str
    description: str = ""
    downloads: int = 0
    author: str = ""
    icon_url: str = ""
    categories: list[str] = field(default_factory=list)
    page_url: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class VersionInfo:
    source: str
    version_id: str
    project_id: str
    name: str
    version_number: str
    filename: str
    download_url: str
    size: int = 0
    game_versions: list[str] = field(default_factory=list)
    loaders: list[str] = field(default_factory=list)
    release_type: str = ""  # release / beta / alpha
    date_published: str = ""
    sha1: str = ""
    sha512: str = ""
    dependencies: list[dict] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover - display helper
        rt = f" [{self.release_type}]" if self.release_type else ""
        mc = f" · MC {', '.join(self.game_versions[:4])}" if self.game_versions else ""
        return f"{self.version_number}{rt}{mc}"
