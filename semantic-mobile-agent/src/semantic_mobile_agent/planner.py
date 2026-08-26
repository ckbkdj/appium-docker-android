from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI

from .apps import AppRegistry, AppSpec
from .config import Settings
from .models import (
    ActionKind,
    Locator,
    PrimitiveAction,
    RiskLevel,
    TaskPlan,
    UISnapshot,
)
from .ui import compact_ui

LOGGER = logging.getLogger(__name__)

_RIDE_RE = re.compile(r"(?:打车|叫车|叫辆车|网约车|出租车)(?:去|到|前往)?(?P<target>.+)?")
_NAV_RE = re.compile(r"(?:导航|带我去|路线|怎么去)(?:到|去|前往)?(?P<target>.+)?")
_SEARCH_RE = re.compile(r"(?:搜索|搜一下|查找|找一下|查一下)(?P<target>.+)")
_MESSAGE_RE = re.compile(
    r"(?:给|向)(?P<contact>.+?)(?:发|发送)(?:一条|个)?(?:消息|微信|短信)?[：:\s]*(?P<body>.+)"
)
_FOOD_RE = re.compile(r"(?:点|订|叫)(?:一份|个|些)?(?P<food>.+?)(?:外卖|送到|配送)?$")
_OPEN_RE = re.compile(r"(?:打开|启动|进入|运行)(?P<app>.+?)(?:app|应用)?$")


def _clean_target(value: str | None) -> str:
    if not value:
        return ""
    value = value.strip(" ，,。.!！?？:：")
    value = re.sub(r"^(?:去|到|前往|一下|帮我|请)", "", value).strip()
    return value


def goal_key(instruction: str) -> str:
    normalized = re.sub(r"\s+", "", instruction.casefold())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]


def _semantic(value: str, *alternatives: str) -> Locator:
    return Locator(strategy="semantic", value=value, alternatives=list(alternatives))


@dataclass(slots=True)
class PlanContext:
    instruction: str
    app: AppSpec | None


class MicroPolicy:
    """Fast deterministic compiler for frequent intents.

    It produces semantic actions rather than coordinates. The executor resolves
    those actions against the current accessibility tree, so one policy can work
    across screen sizes and many app versions.
    """

    def __init__(self, registry: AppRegistry) -> None:
        self.registry = registry

    def _open_step(self, app: AppSpec) -> PrimitiveAction:
        return PrimitiveAction(
            kind=ActionKind.OPEN_APP,
            app=app.name,
            package=app.package,
            metadata={"purpose": "open target application"},
        )

    def _detect_requested_app(self, instruction: str) -> AppSpec | None:
        app = self.registry.detect_in_text(instruction)
        if app:
            return app
        if any(word in instruction for word in ("打车", "叫车", "网约车")):
            return self.registry.resolve("美团") or self.registry.resolve("滴滴")
        if any(word in instruction for word in ("导航", "路线", "怎么去")):
            return self.registry.resolve("高德地图") or self.registry.resolve("百度地图")
        if "外卖" in instruction:
            return self.registry.resolve("美团") or self.registry.resolve("饿了么")
        return None

    def plan(self, instruction: str) -> TaskPlan | None:
        app = self._detect_requested_app(instruction)

        ride = _RIDE_RE.search(instruction)
        if ride:
            target = _clean_target(ride.group("target"))
            if not app:
                return None
            steps = [
                self._open_step(app),
                PrimitiveAction(
                    kind=ActionKind.CLICK,
                    locator=_semantic("打车", "打车出行", "叫车", "出租车", "网约车"),
                    timeout_ms=5000,
                    metadata={"purpose": "enter ride-hailing service"},
                ),
            ]
            if target:
                steps.extend(
                    [
                        PrimitiveAction(
                            kind=ActionKind.CLICK,
                            locator=_semantic(
                                "你要去哪儿",
                                "输入目的地",
                                "目的地",
                                "去哪儿",
                                "到哪里",
                            ),
                            timeout_ms=5000,
                            metadata={"purpose": "focus ride destination"},
                        ),
                        PrimitiveAction(
                            kind=ActionKind.SET_TEXT,
                            locator=Locator(
                                strategy="semantic",
                                value="目的地输入框",
                                alternatives=["输入目的地", "搜索地点", "你要去哪儿"],
                            ),
                            text=target,
                            metadata={"purpose": "enter ride destination"},
                        ),
                        PrimitiveAction(
                            kind=ActionKind.CLICK,
                            locator=Locator(
                                strategy="text_contains",
                                value=target,
                                alternatives=["第一个搜索结果", "推荐地点"],
                            ),
                            timeout_ms=6000,
                            metadata={"purpose": "select destination search result"},
                        ),
                    ]
                )
            steps.extend(
                [
                    PrimitiveAction(
                        kind=ActionKind.CLICK,
                        locator=_semantic(
                            "立即叫车",
                            "确认叫车",
                            "呼叫快车",
                            "呼叫出租车",
                            "确认呼叫",
                        ),
                        risk=RiskLevel.CRITICAL,
                        confirmation_message=(
                            f"已准备好使用{app.name}叫车去“{target}”，是否执行最终叫车操作？"
                            if target
                            else f"已进入{app.name}叫车页面，是否执行最终叫车操作？"
                        ),
                        timeout_ms=8000,
                        metadata={"purpose": "commit ride request"},
                    ),
                    PrimitiveAction(
                        kind=ActionKind.FINISH,
                        expected="ride request submitted",
                    ),
                ]
            )
            return TaskPlan(
                goal=instruction,
                intent="ride_hailing",
                app=app.name,
                package=app.package,
                confidence=0.94 if target else 0.78,
                steps=steps,
                metadata={"destination": target, "source": "micro_policy"},
            )

        navigation = _NAV_RE.search(instruction)
        if navigation:
            target = _clean_target(navigation.group("target"))
            if not app or not target:
                return None
            return TaskPlan(
                goal=instruction,
                intent="navigation",
                app=app.name,
                package=app.package,
                confidence=0.92,
                steps=[
                    self._open_step(app),
                    PrimitiveAction(
                        kind=ActionKind.CLICK,
                        locator=_semantic("搜索地点", "去哪儿", "搜索框", "目的地"),
                        timeout_ms=5000,
                    ),
                    PrimitiveAction(
                        kind=ActionKind.SET_TEXT,
                        locator=_semantic("搜索输入框", "搜索地点", "目的地"),
                        text=target,
                    ),
                    PrimitiveAction(
                        kind=ActionKind.CLICK,
                        locator=Locator(
                            strategy="text_contains",
                            value=target,
                            alternatives=["第一个搜索结果"],
                        ),
                        timeout_ms=6000,
                    ),
                    PrimitiveAction(
                        kind=ActionKind.CLICK,
                        locator=_semantic("路线", "导航", "开始导航"),
                        timeout_ms=5000,
                    ),
                    PrimitiveAction(kind=ActionKind.FINISH, expected="navigation started"),
                ],
                metadata={"destination": target, "source": "micro_policy"},
            )

        message = _MESSAGE_RE.search(instruction)
        if message and app:
            contact = _clean_target(message.group("contact"))
            body = message.group("body").strip()
            return TaskPlan(
                goal=instruction,
                intent="send_message",
                app=app.name,
                package=app.package,
                confidence=0.9,
                steps=[
                    self._open_step(app),
                    PrimitiveAction(
                        kind=ActionKind.CLICK,
                        locator=_semantic("搜索", "搜索联系人", "通讯录搜索"),
                    ),
                    PrimitiveAction(
                        kind=ActionKind.SET_TEXT,
                        locator=_semantic("搜索输入框", "搜索联系人"),
                        text=contact,
                    ),
                    PrimitiveAction(
                        kind=ActionKind.CLICK,
                        locator=Locator(strategy="text_contains", value=contact),
                    ),
                    PrimitiveAction(
                        kind=ActionKind.SET_TEXT,
                        locator=_semantic("消息输入框", "输入消息", "说点什么"),
                        text=body,
                    ),
                    PrimitiveAction(
                        kind=ActionKind.CLICK,
                        locator=_semantic("发送", "Send"),
                        risk=RiskLevel.HIGH,
                        confirmation_message=f"即将向“{contact}”发送：{body}。是否发送？",
                        metadata={"purpose": "send message"},
                    ),
                    PrimitiveAction(kind=ActionKind.FINISH, expected="message sent"),
                ],
                metadata={"contact": contact, "source": "micro_policy"},
            )

        search = _SEARCH_RE.search(instruction)
        if search and app:
            target = _clean_target(search.group("target"))
            if target:
                return TaskPlan(
                    goal=instruction,
                    intent="in_app_search",
                    app=app.name,
                    package=app.package,
                    confidence=0.86,
                    steps=[
                        self._open_step(app),
                        PrimitiveAction(
                            kind=ActionKind.CLICK,
                            locator=_semantic("搜索", "搜索框", "查找"),
                        ),
                        PrimitiveAction(
                            kind=ActionKind.SET_TEXT,
                            locator=_semantic("搜索输入框", "搜索", "查找"),
                            text=target,
                        ),
                        PrimitiveAction(kind=ActionKind.KEY, key="ENTER"),
                        PrimitiveAction(kind=ActionKind.FINISH, expected="search results visible"),
                    ],
                    metadata={"query": target, "source": "micro_policy"},
                )

        if app and ("外卖" in instruction or _FOOD_RE.search(instruction)):
            food_match = _FOOD_RE.search(instruction)
            food = _clean_target(food_match.group("food")) if food_match else ""
            steps = [
                self._open_step(app),
                PrimitiveAction(
                    kind=ActionKind.CLICK,
                    locator=_semantic("外卖", "美食外卖", "点外卖"),
                ),
            ]
            if food:
                steps.extend(
                    [
                        PrimitiveAction(
                            kind=ActionKind.CLICK,
                            locator=_semantic("搜索", "搜索商家或商品", "想吃什么"),
                        ),
                        PrimitiveAction(
                            kind=ActionKind.SET_TEXT,
                            locator=_semantic("搜索输入框", "搜索商家或商品"),
                            text=food,
                        ),
                        PrimitiveAction(kind=ActionKind.KEY, key="ENTER"),
                    ]
                )
            steps.append(PrimitiveAction(kind=ActionKind.FINISH, expected="food results visible"))
            return TaskPlan(
                goal=instruction,
                intent="food_delivery",
                app=app.name,
                package=app.package,
                confidence=0.84,
                steps=steps,
                metadata={"query": food, "source": "micro_policy"},
            )

        opened = _OPEN_RE.search(instruction)
        if opened:
            requested = self.registry.resolve(_clean_target(opened.group("app"))) or app
            if requested:
                return TaskPlan(
                    goal=instruction,
                    intent="open_app",
                    app=requested.name,
                    package=requested.package,
                    confidence=0.99,
                    steps=[
                        self._open_step(requested),
                        PrimitiveAction(kind=ActionKind.FINISH, expected="app opened"),
                    ],
                    metadata={"source": "micro_policy"},
                )
        return None


class LLMPlanner:
    """Optional OpenAI-compatible planner used only for novel or divergent screens."""

    def __init__(self, settings: Settings, registry: AppRegistry) -> None:
        self.settings = settings
        self.registry = registry
        self.client: AsyncOpenAI | None = None
        if settings.llm_model and (settings.llm_api_key or settings.llm_base_url):
            self.client = AsyncOpenAI(
                api_key=settings.llm_api_key or "local-no-key",
                base_url=settings.llm_base_url,
                timeout=settings.llm_timeout_s,
                max_retries=1,
            )

    @property
    def enabled(self) -> bool:
        return self.client is not None and bool(self.settings.llm_model)

    @staticmethod
    def _extract_json(value: str) -> dict[str, Any]:
        value = value.strip()
        if value.startswith("```"):
            value = re.sub(r"^```(?:json)?\s*", "", value)
            value = re.sub(r"\s*```$", "", value)
        start = value.find("{")
        end = value.rfind("}")
        if start < 0 or end < start:
            raise ValueError("model did not return a JSON object")
        return json.loads(value[start : end + 1])

    async def _complete(self, system: str, user: str, *, max_tokens: int) -> dict[str, Any]:
        if not self.client or not self.settings.llm_model:
            raise RuntimeError("LLM planner is not configured")
        response = await self.client.chat.completions.create(
            model=self.settings.llm_model,
            temperature=0,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        content = response.choices[0].message.content or ""
        return self._extract_json(content)

    async def plan(self, instruction: str) -> TaskPlan:
        catalog = self.registry.prompt_catalog(limit=90)
        schema = {
            "goal": instruction,
            "intent": "short_snake_case",
            "app": "display name or null",
            "package": "Android package or null",
            "confidence": 0.0,
            "steps": [
                {
                    "kind": "open_app|click|tap|set_text|swipe|key|wait|assert|finish",
                    "package": "optional",
                    "app": "optional",
                    "locator": {
                        "strategy": "semantic|text|text_contains|resource_id|description|role|path|focused",
                        "value": "visible concept",
                        "alternatives": [],
                    },
                    "text": "optional",
                    "key": "optional",
                    "risk": "low|medium|high|critical",
                    "confirmation_message": "required for final external commitment",
                    "metadata": {"purpose": "short description"},
                }
            ],
        }
        system = (
            "You compile a natural-language Android task into a compact JSON action plan. "
            "Return JSON only and never include chain-of-thought. Prefer semantic locators over coordinates. "
            "Open the requested app first. Stop at a confirmation gate before the final action that sends a "
            "message, places/pays for an order, requests a ride, transfers money, deletes data, publishes, "
            "authorizes access, or makes a booking. Mark that final click high or critical. Do not invent "
            "credentials, prices, addresses or selected items. Use at most 20 steps and include finish."
        )
        user = json.dumps(
            {"instruction": instruction, "available_apps": catalog, "output_schema": schema},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        raw = await self._complete(system, user, max_tokens=2200)
        raw.setdefault("goal", instruction)
        raw.setdefault("intent", "generic_ui_task")
        raw.setdefault("confidence", 0.5)
        raw.setdefault("steps", [])
        return TaskPlan.model_validate(raw)

    async def recover_action(
        self,
        *,
        goal: str,
        plan: TaskPlan,
        cursor: int,
        snapshot: UISnapshot,
        last_error: str,
    ) -> PrimitiveAction | None:
        if not self.enabled:
            return None
        system = (
            "Choose exactly one safe next Android UI action to advance the stated goal. Return JSON only, "
            "without reasoning. Use only controls present in the compact UI. Prefer text/resource-id/path "
            "semantic locators, never guess coordinates unless a visible node supplies them. Return "
            "{\"kind\":\"finish\"} only when the goal is visibly complete. Any final external commitment "
            "must be marked high or critical and include confirmation_message."
        )
        user = json.dumps(
            {
                "goal": goal,
                "intent": plan.intent,
                "planned_cursor": cursor,
                "last_error": last_error[-500:],
                "screen": compact_ui(snapshot),
                "action_schema": {
                    "kind": "click|set_text|swipe|key|wait|assert|finish",
                    "locator": {
                        "strategy": "semantic|text|text_contains|resource_id|description|role|path|focused",
                        "value": "...",
                        "alternatives": [],
                    },
                    "text": "optional",
                    "key": "optional",
                    "risk": "low|medium|high|critical",
                    "confirmation_message": "optional",
                    "metadata": {"purpose": "..."},
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        try:
            raw = await self._complete(system, user, max_tokens=700)
            return PrimitiveAction.model_validate(raw)
        except Exception:
            LOGGER.exception("single-step LLM recovery failed")
            return None


class Planner:
    def __init__(self, settings: Settings, registry: AppRegistry) -> None:
        self.registry = registry
        self.micro = MicroPolicy(registry)
        self.llm = LLMPlanner(settings, registry)

    async def plan(self, instruction: str) -> TaskPlan:
        plan = self.micro.plan(instruction)
        if plan:
            return plan
        if not self.llm.enabled:
            raise RuntimeError(
                "本地微策略无法确定该任务，且未配置 SMA_LLM_MODEL/SMA_LLM_BASE_URL。"
            )
        plan = await self.llm.plan(instruction)
        app = self.registry.resolve(plan.package) or self.registry.resolve(plan.app)
        if app:
            plan = plan.model_copy(update={"app": app.name, "package": app.package})
            normalized_steps = []
            for step in plan.steps:
                if step.kind == ActionKind.OPEN_APP and not step.package:
                    step = step.model_copy(update={"app": app.name, "package": app.package})
                normalized_steps.append(step)
            plan = plan.model_copy(update={"steps": normalized_steps})
        if not plan.steps:
            raise RuntimeError("planner returned an empty action plan")
        if plan.steps[-1].kind != ActionKind.FINISH:
            plan.steps.append(PrimitiveAction(kind=ActionKind.FINISH))
        return plan
