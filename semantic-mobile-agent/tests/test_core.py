from __future__ import annotations

from pathlib import Path

import pytest

from semantic_mobile_agent.apps import AppRegistry
from semantic_mobile_agent.cache import ActionCache
from semantic_mobile_agent.device import DeviceResolver
from semantic_mobile_agent.models import (
    ActionKind,
    Locator,
    PrimitiveAction,
    RiskLevel,
)
from semantic_mobile_agent.planner import MicroPolicy
from semantic_mobile_agent.safety import SafetyPolicy
from semantic_mobile_agent.ui import parse_uiautomator_xml, rank_nodes

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_device_alias_normalization() -> None:
    assert DeviceResolver.normalize_hint("emul-5554") == "emulator-5554"
    assert DeviceResolver.normalize_hint("emulator:5556") == "emulator-5556"
    assert DeviceResolver.normalize_hint("adb://10.0.0.8:5555") == "10.0.0.8:5555"
    assert DeviceResolver.normalize_hint("auto") is None


def test_ui_parser_and_semantic_ranking() -> None:
    xml = """
    <hierarchy rotation="0">
      <node index="0" text="" resource-id="" class="android.widget.FrameLayout"
            package="com.example" content-desc="" clickable="false" enabled="true"
            bounds="[0,0][1080,2400]">
        <node index="0" text="你要去哪儿" resource-id="com.example:id/destination"
              class="android.widget.EditText" package="com.example" content-desc="输入目的地"
              clickable="true" enabled="true" focused="false" bounds="[50,200][1030,320]" />
        <node index="1" text="立即叫车" resource-id="com.example:id/submit"
              class="android.widget.Button" package="com.example" content-desc=""
              clickable="true" enabled="true" bounds="[50,2100][1030,2240]" />
      </node>
    </hierarchy>
    """
    snapshot = parse_uiautomator_xml(xml)
    matches = rank_nodes(
        snapshot,
        Locator(strategy="semantic", value="目的地输入框", alternatives=["你要去哪儿"]),
    )
    assert snapshot.package == "com.example"
    assert snapshot.state_hash
    assert matches
    assert matches[0][1].resource_id.endswith("destination")
    assert matches[0][1].editable is True


def test_meituan_ride_micro_policy_has_confirmation_gate() -> None:
    registry = AppRegistry(PROJECT_ROOT / "config/apps.yaml")
    plan = MicroPolicy(registry).plan("使用美团打车去首都机场")
    assert plan is not None
    assert plan.intent == "ride_hailing"
    assert plan.package == "com.sankuai.meituan"
    assert plan.metadata["destination"] == "首都机场"
    final_clicks = [step for step in plan.steps if step.kind == ActionKind.CLICK]
    assert final_clicks[-1].risk == RiskLevel.CRITICAL
    assert "首都机场" in (final_clicks[-1].confirmation_message or "")


def test_dynamic_app_registry_merge() -> None:
    registry = AppRegistry()
    registry.merge_discovered(
        [{"label": "内部测试应用", "package": "com.example.internal"}]
    )
    assert registry.resolve("内部测试应用") is not None
    assert registry.resolve("com.example.internal").package == "com.example.internal"


def test_safety_policy_escalates_final_commitment() -> None:
    policy = SafetyPolicy(PROJECT_ROOT / "config/safety.yaml")
    action = PrimitiveAction(
        kind=ActionKind.CLICK,
        locator=Locator(strategy="text", value="立即支付"),
        risk=RiskLevel.LOW,
    )
    normalized = policy.normalize(action)
    assert normalized.risk == RiskLevel.CRITICAL
    assert normalized.confirmation_message
    assert policy.needs_confirmation(
        normalized,
        require_confirmation=True,
        allow_unsafe=False,
    )


@pytest.mark.asyncio
async def test_sqlite_action_cache_round_trip(tmp_path: Path) -> None:
    cache = ActionCache(tmp_path / "cache.sqlite3")
    action = PrimitiveAction(
        kind=ActionKind.CLICK,
        locator=Locator(strategy="text", value="打车"),
    )
    assert await cache.get("goal", "com.example", "state") is None
    await cache.record_success("goal", "com.example", "state", action, 92)
    restored = await cache.get("goal", "com.example", "state")
    assert restored is not None
    assert restored.locator is not None
    assert restored.locator.value == "打车"
    await cache.record_failure("goal", "com.example", "state")
