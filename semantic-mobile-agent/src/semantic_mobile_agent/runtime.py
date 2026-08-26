from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from .apps import AppRegistry
from .cache import ActionCache
from .config import Settings
from .device import DeviceResolver
from .drivers import HybridDriver
from .engine import TaskEngine
from .planner import Planner
from .safety import SafetyPolicy


def _resolve_project_path(path: Path) -> Path:
    if path.is_absolute() or path.exists():
        return path
    # Source/editable installs keep config beside pyproject.toml. Container images
    # copy it to /app/config and therefore resolve from the working directory.
    source_root = Path(__file__).resolve().parents[2]
    candidate = source_root / path
    return candidate if candidate.exists() else path


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.app_catalog_path = _resolve_project_path(settings.app_catalog_path)
    settings.safety_path = _resolve_project_path(settings.safety_path)
    settings.prepare()
    return settings


@lru_cache(maxsize=1)
def get_engine() -> TaskEngine:
    settings = get_settings()
    registry = AppRegistry(settings.app_catalog_path)
    planner = Planner(settings, registry)
    safety = SafetyPolicy(settings.safety_path)
    cache = ActionCache(settings.database_path)
    driver = HybridDriver(settings)
    devices = DeviceResolver(settings.adb_path, settings.default_device)
    return TaskEngine(
        settings=settings,
        registry=registry,
        planner=planner,
        safety=safety,
        cache=cache,
        driver=driver,
        devices=devices,
    )
