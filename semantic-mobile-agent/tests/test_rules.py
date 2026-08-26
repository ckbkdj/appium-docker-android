from __future__ import annotations

import pytest

from mobile_agent.apps import AppRegistry
from mobile_agent.config import Settings
from mobile_agent.models import DeviceRef, InstalledApp, TaskRequest
from mobile_agent.planner import HybridPlanner


@pytest.mark.asyncio
async def test_ride_plan_stops_before_final_call(tmp_path) -> None:
    settings = Settings(database_path=tmp_path / "agent.db")
    registry = AppRegistry(settings.profiles_file)
    planner = HybridPlanner(registry, settings)

    plan = await planner.plan(
        TaskRequest(
            instruction="使用美团打车去首都机场",
            device=DeviceRef(serial="emulator-5554"),
        )
    )

    assert plan.intent == "ride_hailing"
    assert plan.app == "美团"
    assert any(step.text == "首都机场" for step in plan.steps)
    assert plan.steps[-1].requires_confirmation is True


@pytest.mark.asyncio
async def test_message_plan_stops_before_send(tmp_path) -> None:
    settings = Settings(database_path=tmp_path / "agent.db")
    registry = AppRegistry(settings.profiles_file)
    planner = HybridPlanner(registry, settings)

    plan = await planner.plan(
        TaskRequest(
            instruction="用微信给张三发消息：十分钟后到",
            device=DeviceRef(serial="emulator-5554"),
        )
    )

    assert plan.intent == "send_message"
    assert any(step.text == "张三" for step in plan.steps)
    assert any(step.text == "十分钟后到" for step in plan.steps)
    assert plan.steps[-1].requires_confirmation is True


@pytest.mark.asyncio
async def test_runtime_discovered_app_can_be_opened(tmp_path) -> None:
    settings = Settings(database_path=tmp_path / "agent.db")
    registry = AppRegistry(settings.profiles_file)
    registry.merge_installed(
        [InstalledApp(label="测试云机工具", package="com.example.cloudtool")]
    )
    planner = HybridPlanner(registry, settings)

    plan = await planner.plan(
        TaskRequest(
            instruction="打开测试云机工具",
            device=DeviceRef(serial="10.0.0.8:5555"),
        )
    )

    assert plan.intent == "open_app"
    assert plan.steps[0].package == "com.example.cloudtool"
