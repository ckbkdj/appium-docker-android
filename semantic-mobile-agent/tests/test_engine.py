from __future__ import annotations

from pathlib import Path

import pytest

from semantic_mobile_agent.apps import AppRegistry
from semantic_mobile_agent.cache import ActionCache
from semantic_mobile_agent.config import Settings
from semantic_mobile_agent.device import DeviceResolver
from semantic_mobile_agent.drivers import HybridDriver
from semantic_mobile_agent.engine import TaskEngine
from semantic_mobile_agent.models import TaskRequest, TaskStatus
from semantic_mobile_agent.planner import Planner
from semantic_mobile_agent.safety import SafetyPolicy

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_dry_run_succeeds_without_adb_device(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        app_catalog_path=PROJECT_ROOT / "config/apps.yaml",
        safety_path=PROJECT_ROOT / "config/safety.yaml",
        bridge_enabled=False,
        appium_enabled=False,
        llm_model=None,
    )
    settings.prepare()
    registry = AppRegistry(settings.app_catalog_path)
    planner = Planner(settings, registry)
    engine = TaskEngine(
        settings=settings,
        registry=registry,
        planner=planner,
        safety=SafetyPolicy(settings.safety_path),
        cache=ActionCache(settings.database_path),
        driver=HybridDriver(settings),
        devices=DeviceResolver(settings.adb_path),
    )

    try:
        task = await engine.execute(
            TaskRequest(instruction="使用美团打车去首都机场", dry_run=True),
            timeout_s=5,
        )
        assert task.status == TaskStatus.SUCCEEDED
        assert task.plan is not None
        assert task.plan.intent == "ride_hailing"
        assert task.result["dry_run"] is True
        assert task.device is None
    finally:
        await engine.close()
