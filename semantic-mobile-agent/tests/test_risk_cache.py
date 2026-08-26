from __future__ import annotations

import pytest

from mobile_agent.cache import WorkflowCache
from mobile_agent.models import Action, ActionKind, Plan, Selector
from mobile_agent.risk import RiskPolicy


def test_irreversible_action_is_promoted_to_confirmation() -> None:
    policy = RiskPolicy(require_confirmation=True)
    payment = Action(
        kind=ActionKind.TAP_TEXT,
        description="提交订单并付款",
        selector=Selector(candidate_texts=["确认支付"]),
    )
    ordinary = Action(
        kind=ActionKind.TAP_TEXT,
        description="打开搜索",
        selector=Selector(candidate_texts=["搜索"]),
    )

    payment_decision = policy.evaluate(payment)
    ordinary_decision = policy.evaluate(ordinary)
    assert payment_decision.requires_confirmation is True
    assert payment_decision.level.value == "critical"
    assert ordinary_decision.requires_confirmation is False


@pytest.mark.asyncio
async def test_cache_requires_repeated_success_before_reuse(tmp_path) -> None:
    cache = WorkflowCache(tmp_path / "cache.db", min_successes=2)
    await cache.initialize()
    plan = Plan(
        intent="open_app",
        objective="打开美团",
        app="美团",
        confidence=0.9,
        steps=[Action(kind=ActionKind.WAIT, description="等待", duration_ms=1)],
    )

    assert await cache.get("打开美团", "美团", "state-a") is None
    await cache.record_success("打开美团", "美团", "state-a", plan, 100.0)
    assert await cache.get("打开美团", "美团", "state-a") is None
    await cache.record_success("打开美团", "美团", "state-a", plan, 80.0)
    cached = await cache.get("打开美团", "美团", "state-a")

    assert cached is not None
    assert cached.source == "cache"
    assert cached.confidence == 0.99
