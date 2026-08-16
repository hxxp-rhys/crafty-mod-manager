"""Common surface for mod platforms (Modrinth, CurseForge)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from ..models import ProjectHit, VersionInfo


class ProviderError(Exception):
    pass


class RateLimited(ProviderError):
    pass


class ModProvider(ABC):
    name: str = "base"
    label: str = "Base"
    needs_key: bool = False

    @property
    def available(self) -> bool:
        return True

    @abstractmethod
    def search(
        self,
        query: str,
        loader: str = "",
        mc_version: str = "",
        offset: int = 0,
        limit: int = 30,
        project_type: str = "mod",
    ) -> list[ProjectHit]: ...

    @abstractmethod
    def versions(
        self, project_id: str, loader: str = "", mc_version: str = ""
    ) -> list[VersionInfo]: ...

    @abstractmethod
    def download(self, version: VersionInfo) -> bytes: ...

    def identify(self, mods: list) -> dict:
        """Map local jars to platform projects. Returns {filename: (ProjectHit-ish
        dict, VersionInfo)}. Optional - default is 'cannot identify'."""
        return {}

    def latest_for(
        self, mods: list, loader: str = "", mc_version: str = ""
    ) -> dict:
        """Return {filename: VersionInfo} for mods that have a newer build."""
        return {}
