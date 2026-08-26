from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


_NORMALIZE_RE = re.compile(r"[\s\-_.·•]+")


def normalize_app_name(value: str) -> str:
    return _NORMALIZE_RE.sub("", value.strip().casefold())


class AppSpec(BaseModel):
    name: str
    package: str
    aliases: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    launch_activity: str | None = None
    intents: dict[str, Any] = Field(default_factory=dict)

    @property
    def all_names(self) -> list[str]:
        return [self.name, *self.aliases, self.package]


class AppRegistry:
    """Seed catalog plus runtime-discovered launcher apps."""

    def __init__(self, catalog_path: Path | str | None = None) -> None:
        self._apps: list[AppSpec] = []
        self._index: dict[str, AppSpec] = {}
        if catalog_path and Path(catalog_path).exists():
            self.load(Path(catalog_path))

    def load(self, path: Path) -> None:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for item in raw.get("apps", []):
            self.register(AppSpec.model_validate(item))

    def register(self, app: AppSpec) -> None:
        current = self._index.get(normalize_app_name(app.package))
        if current:
            return
        self._apps.append(app)
        for name in app.all_names:
            self._index[normalize_app_name(name)] = app

    def merge_discovered(self, apps: list[dict[str, str]]) -> None:
        for item in apps:
            package = item.get("package", "").strip()
            label = item.get("label", "").strip() or package
            if package:
                self.register(AppSpec(name=label, package=package, aliases=[]))

    def resolve(self, query: str | None) -> AppSpec | None:
        if not query:
            return None
        normalized = normalize_app_name(query)
        exact = self._index.get(normalized)
        if exact:
            return exact
        candidates: list[tuple[int, AppSpec]] = []
        for app in self._apps:
            for name in app.all_names:
                n = normalize_app_name(name)
                if normalized in n or n in normalized:
                    candidates.append((min(len(n), len(normalized)), app))
        return max(candidates, key=lambda x: x[0])[1] if candidates else None

    def detect_in_text(self, text: str) -> AppSpec | None:
        normalized = normalize_app_name(text)
        matches: list[tuple[int, AppSpec]] = []
        for app in self._apps:
            for name in app.all_names:
                n = normalize_app_name(name)
                if n and n in normalized:
                    matches.append((len(n), app))
        return max(matches, key=lambda x: x[0])[1] if matches else None

    def prompt_catalog(self, limit: int = 80) -> list[dict[str, str]]:
        return [
            {"name": app.name, "package": app.package, "aliases": ",".join(app.aliases[:4])}
            for app in self._apps[:limit]
        ]

    def __len__(self) -> int:
        return len(self._apps)
