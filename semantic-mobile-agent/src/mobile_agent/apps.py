from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

from .models import InstalledApp


class AppProfile(BaseModel):
    name: str
    aliases: list[str] = Field(default_factory=list)
    package_candidates: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    entry_texts: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("aliases", mode="before")
    @classmethod
    def normalize_aliases(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("aliases must be a list")
        return [str(item) for item in value]

    @property
    def all_names(self) -> list[str]:
        return [self.name, *self.aliases]


class AppRegistry:
    def __init__(self, profiles_path: Path) -> None:
        self.profiles_path = profiles_path
        self._profiles: dict[str, AppProfile] = {}
        self._alias_index: dict[str, str] = {}
        self._installed: dict[str, InstalledApp] = {}
        self.reload()

    @staticmethod
    def normalize(value: str) -> str:
        return re.sub(r"[\s\-_/·.]+", "", value).casefold()

    def reload(self) -> None:
        raw: dict[str, Any] = yaml.safe_load(self.profiles_path.read_text(encoding="utf-8")) or {}
        self._profiles.clear()
        self._alias_index.clear()
        for item in raw.get("apps", []):
            profile = AppProfile.model_validate(item)
            self._profiles[profile.name] = profile
            for alias in profile.all_names:
                self._alias_index[self.normalize(alias)] = profile.name

    def merge_installed(self, apps: list[InstalledApp]) -> None:
        self._installed = {app.package: app for app in apps}
        for app in apps:
            normalized = self.normalize(app.label)
            if normalized and normalized not in self._alias_index:
                dynamic_name = app.label
                if dynamic_name not in self._profiles:
                    self._profiles[dynamic_name] = AppProfile(
                        name=dynamic_name,
                        package_candidates=[app.package],
                        capabilities=["open", "generic"],
                    )
                self._alias_index[normalized] = dynamic_name

    def match_in_text(self, text: str) -> AppProfile | None:
        normalized = self.normalize(text)
        matches: list[tuple[int, AppProfile]] = []
        for alias, name in self._alias_index.items():
            if alias and alias in normalized:
                matches.append((len(alias), self._profiles[name]))
        if not matches:
            return None
        matches.sort(key=lambda item: item[0], reverse=True)
        return matches[0][1]

    def resolve(self, name_or_alias: str) -> AppProfile | None:
        normalized = self.normalize(name_or_alias)
        direct = self._alias_index.get(normalized)
        if direct:
            return self._profiles[direct]
        return self.match_in_text(name_or_alias)

    def resolve_package(self, profile: AppProfile) -> str | None:
        for package in profile.package_candidates:
            if package in self._installed:
                return package
        return profile.package_candidates[0] if profile.package_candidates else None

    def list_profiles(self) -> list[AppProfile]:
        return sorted(self._profiles.values(), key=lambda profile: profile.name)

    def compact_catalog(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        matched = self.match_in_text(query)
        selected: list[AppProfile] = []
        if matched is not None:
            selected.append(matched)
        query_norm = self.normalize(query)
        for profile in self.list_profiles():
            if profile in selected:
                continue
            alias_hit = any(self.normalize(alias) in query_norm for alias in profile.all_names)
            if alias_hit or len(selected) < min(limit, 8):
                selected.append(profile)
            if len(selected) >= limit:
                break
        return [
            {
                "name": profile.name,
                "aliases": profile.aliases,
                "package": self.resolve_package(profile),
                "capabilities": profile.capabilities,
                "entry_texts": profile.entry_texts,
            }
            for profile in selected
        ]
