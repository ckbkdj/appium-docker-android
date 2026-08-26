from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Any

import httpx

from .apps import AppProfile, AppRegistry
from .config import Settings
from .models import (
    Action,
    ActionKind,
    Plan,
    RiskLevel,
    Selector,
    TaskRequest,
    UiSnapshot,
)
from .ui import compress_snapshot


class Planner(ABC):
    @abstractmethod
    async def plan(self, request: TaskRequest) -> Plan:
        raise NotImplementedError

    async def replan(
        self,
        request: TaskRequest,
        plan: Plan,
        failed_action: Action,
        snapshot: UiSnapshot,
        error: str,
    ) -> Plan | None:
        return None


class RulePlanner:
    def __init__(self, registry: AppRegistry, settings: Settings) -> None:
        self.registry = registry
        self.settings = settings

    def try_plan(self, request: TaskRequest) -> Plan | None:
        instruction = request.instruction.strip()
        profile = self.registry.match_in_text(instruction)

        if self._is_ride(instruction):
            profile = profile or self.registry.resolve("高德地图") or self.registry.resolve("美团")
            destination = self._destination(instruction, request.context)
            if profile and destination:
                return self._ride_plan(profile, destination)

        if self._is_message(instruction) and profile:
            contact, message = self._message_parts(instruction, request.context)
            if contact and message:
                return self._message_plan(profile, contact, message)

        if self._is_navigation(instruction):
            profile = profile or self.registry.resolve("高德地图")
            destination = self._destination(instruction, request.context)
            if profile and destination:
                return self._navigation_plan(profile, destination)

        if self._is_food(instruction):
            profile = profile or self.registry.resolve("美团")
            query = self._food_query(instruction, request.context)
            if profile:
                return self._food_plan(profile, query)

        search = self._search_parts(instruction)
        if search and profile:
            return self._search_plan(profile, search)

        if profile and re.search(r"(打开|启动|进入|运行)", instruction):
            return self._open_plan(profile)

        return None

    @staticmethod
    def _is_ride(text: str) -> bool:
        return bool(re.search(r"(打车|叫车|叫辆车|网约车|出租车)", text))

    @staticmethod
    def _is_navigation(text: str) -> bool:
        return bool(re.search(r"(导航|路线|怎么走|带我去)", text)) and not RulePlanner._is_ride(text)

    @staticmethod
    def _is_message(text: str) -> bool:
        return bool(re.search(r"(发消息|发送消息|发微信|告诉.+说|跟.+说)", text))

    @staticmethod
    def _is_food(text: str) -> bool:
        return bool(re.search(r"(外卖|点餐|点一份|买吃的|订餐)", text))

    @staticmethod
    def _destination(text: str, context: dict[str, Any]) -> str | None:
        explicit = context.get("destination")
        if isinstance(explicit, str) and explicit.strip():
            return explicit.strip()
        patterns = [
            r"(?:打车|叫车|叫辆车|导航|带我)(?:去|到|前往)\s*([^，。；;]+)",
            r"(?:去|到|前往)\s*([^，。；;]+?)(?:打车|导航)?$",
            r"从.+?(?:去|到)\s*([^，。；;]+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                value = re.sub(r"(吧|一下|附近)$", "", match.group(1).strip())
                if value:
                    return value
        return None

    @staticmethod
    def _search_parts(text: str) -> str | None:
        patterns = [
            r"(?:在.+?(?:里|上))?搜索\s*([^，。；;]+)",
            r"(?:在.+?(?:里|上))?查找\s*([^，。；;]+)",
            r"搜一下\s*([^，。；;]+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1).strip()
        return None

    @staticmethod
    def _message_parts(text: str, context: dict[str, Any]) -> tuple[str | None, str | None]:
        contact = context.get("contact") if isinstance(context.get("contact"), str) else None
        message = context.get("message") if isinstance(context.get("message"), str) else None
        if contact and message:
            return contact.strip(), message.strip()
        patterns = [
            r"给\s*(.+?)\s*(?:发消息|发送消息|发微信)[:：]?\s*(.+)$",
            r"(?:告诉|跟)\s*(.+?)\s*说[:：]?\s*(.+)$",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1).strip(), match.group(2).strip()
        return None, None

    @staticmethod
    def _food_query(text: str, context: dict[str, Any]) -> str | None:
        query = context.get("query")
        if isinstance(query, str) and query.strip():
            return query.strip()
        match = re.search(r"(?:点一份|点餐|外卖|买)\s*([^，。；;]+)", text)
        return match.group(1).strip() if match else None

    def _package(self, profile: AppProfile) -> str | None:
        return self.registry.resolve_package(profile)

    def _launch(self, profile: AppProfile) -> Action:
        return Action(
            kind=ActionKind.LAUNCH_APP,
            description=f"打开{profile.name}",
            app=profile.name,
            package=self._package(profile),
            timeout_ms=5000,
            retries=1,
        )

    def _open_plan(self, profile: AppProfile) -> Plan:
        return Plan(
            intent="open_app",
            objective=f"打开{profile.name}",
            app=profile.name,
            source="rule",
            confidence=0.99,
            steps=[self._launch(profile)],
            expected_result=f"{profile.name}处于前台",
            max_replans=self.settings.default_max_replans,
        )

    def _search_plan(self, profile: AppProfile, query: str) -> Plan:
        return Plan(
            intent="search",
            objective=f"在{profile.name}中搜索{query}",
            app=profile.name,
            source="rule",
            confidence=0.92,
            steps=[
                self._launch(profile),
                Action(
                    kind=ActionKind.TAP_TEXT,
                    description="进入搜索",
                    app=profile.name,
                    selector=Selector(candidate_texts=["搜索", "搜一搜", "查找"], clickable=True),
                    retries=2,
                ),
                Action(
                    kind=ActionKind.INPUT_TEXT,
                    description=f"输入搜索词：{query}",
                    app=profile.name,
                    selector=Selector(editable=True, clickable=False),
                    text=query,
                    retries=1,
                ),
                Action(
                    kind=ActionKind.KEY,
                    description="提交搜索",
                    app=profile.name,
                    text="ENTER",
                ),
            ],
            expected_result=f"展示与{query}相关的结果",
            max_replans=self.settings.default_max_replans,
        )

    def _ride_plan(self, profile: AppProfile, destination: str) -> Plan:
        entry = profile.entry_texts.get("ride", ["打车", "打车出行", "出行"])
        return Plan(
            intent="ride_hailing",
            objective=f"使用{profile.name}准备前往{destination}的叫车订单",
            app=profile.name,
            source="rule",
            confidence=0.94,
            assumptions=["默认使用设备当前定位作为起点", "最终呼叫车辆前必须由用户确认"],
            steps=[
                self._launch(profile),
                Action(
                    kind=ActionKind.TAP_TEXT,
                    description="进入打车入口",
                    app=profile.name,
                    selector=Selector(candidate_texts=entry, clickable=True),
                    retries=2,
                    timeout_ms=5000,
                ),
                Action(
                    kind=ActionKind.TAP_TEXT,
                    description="打开目的地输入框",
                    app=profile.name,
                    selector=Selector(
                        candidate_texts=["你要去哪儿", "输入目的地", "去哪儿", "目的地"],
                        clickable=True,
                    ),
                    retries=2,
                ),
                Action(
                    kind=ActionKind.INPUT_TEXT,
                    description=f"输入目的地：{destination}",
                    app=profile.name,
                    selector=Selector(editable=True, clickable=False),
                    text=destination,
                ),
                Action(
                    kind=ActionKind.TAP_TEXT,
                    description=f"选择目的地搜索结果：{destination}",
                    app=profile.name,
                    selector=Selector(text_contains=destination, clickable=True),
                    retries=2,
                    timeout_ms=5000,
                ),
                Action(
                    kind=ActionKind.TAP_TEXT,
                    description="确认呼叫车辆",
                    app=profile.name,
                    selector=Selector(
                        candidate_texts=["确认呼叫", "立即叫车", "同时呼叫", "呼叫"],
                        clickable=True,
                    ),
                    risk=RiskLevel.HIGH,
                    requires_confirmation=True,
                    retries=1,
                    timeout_ms=5000,
                ),
            ],
            expected_result=f"已在最终确认前准备好前往{destination}的叫车订单",
            max_replans=self.settings.default_max_replans,
        )

    def _navigation_plan(self, profile: AppProfile, destination: str) -> Plan:
        return Plan(
            intent="navigation",
            objective=f"使用{profile.name}导航到{destination}",
            app=profile.name,
            source="rule",
            confidence=0.92,
            steps=[
                self._launch(profile),
                Action(
                    kind=ActionKind.TAP_TEXT,
                    description="打开地图搜索框",
                    app=profile.name,
                    selector=Selector(candidate_texts=["搜索地点", "搜索", "去哪儿"], clickable=True),
                    retries=2,
                ),
                Action(
                    kind=ActionKind.INPUT_TEXT,
                    description=f"输入目的地：{destination}",
                    app=profile.name,
                    selector=Selector(editable=True, clickable=False),
                    text=destination,
                ),
                Action(
                    kind=ActionKind.TAP_TEXT,
                    description=f"选择地点：{destination}",
                    app=profile.name,
                    selector=Selector(text_contains=destination, clickable=True),
                    retries=2,
                ),
                Action(
                    kind=ActionKind.TAP_TEXT,
                    description="进入路线规划",
                    app=profile.name,
                    selector=Selector(candidate_texts=["路线", "导航", "到这去"], clickable=True),
                    retries=2,
                ),
                Action(
                    kind=ActionKind.TAP_TEXT,
                    description="开始导航",
                    app=profile.name,
                    selector=Selector(candidate_texts=["开始导航", "开始", "导航"], clickable=True),
                ),
            ],
            expected_result=f"开始前往{destination}的导航",
            max_replans=self.settings.default_max_replans,
        )

    def _message_plan(self, profile: AppProfile, contact: str, message: str) -> Plan:
        return Plan(
            intent="send_message",
            objective=f"通过{profile.name}向{contact}发送消息",
            app=profile.name,
            source="rule",
            confidence=0.9,
            assumptions=["发送前必须由用户确认消息内容和收件人"],
            steps=[
                self._launch(profile),
                Action(
                    kind=ActionKind.TAP_TEXT,
                    description="打开联系人搜索",
                    app=profile.name,
                    selector=Selector(candidate_texts=["搜索", "通讯录", "联系人"], clickable=True),
                    retries=2,
                ),
                Action(
                    kind=ActionKind.INPUT_TEXT,
                    description=f"输入联系人：{contact}",
                    app=profile.name,
                    selector=Selector(editable=True, clickable=False),
                    text=contact,
                ),
                Action(
                    kind=ActionKind.TAP_TEXT,
                    description=f"选择联系人：{contact}",
                    app=profile.name,
                    selector=Selector(text_contains=contact, clickable=True),
                    retries=2,
                ),
                Action(
                    kind=ActionKind.INPUT_TEXT,
                    description=f"输入消息：{message}",
                    app=profile.name,
                    selector=Selector(editable=True, clickable=False),
                    text=message,
                ),
                Action(
                    kind=ActionKind.TAP_TEXT,
                    description=f"向{contact}发送消息：{message}",
                    app=profile.name,
                    selector=Selector(candidate_texts=["发送", "Send"], clickable=True),
                    risk=RiskLevel.HIGH,
                    requires_confirmation=True,
                ),
            ],
            expected_result=f"消息发送给{contact}",
            max_replans=self.settings.default_max_replans,
        )

    def _food_plan(self, profile: AppProfile, query: str | None) -> Plan:
        steps = [
            self._launch(profile),
            Action(
                kind=ActionKind.TAP_TEXT,
                description="进入外卖或美食入口",
                app=profile.name,
                selector=Selector(candidate_texts=["外卖", "美食", "点餐"], clickable=True),
                retries=2,
            ),
        ]
        if query:
            steps.extend(
                [
                    Action(
                        kind=ActionKind.TAP_TEXT,
                        description="打开餐品搜索",
                        app=profile.name,
                        selector=Selector(candidate_texts=["搜索", "搜美食", "搜商家"], clickable=True),
                        retries=2,
                    ),
                    Action(
                        kind=ActionKind.INPUT_TEXT,
                        description=f"输入餐品：{query}",
                        app=profile.name,
                        selector=Selector(editable=True, clickable=False),
                        text=query,
                    ),
                    Action(
                        kind=ActionKind.KEY,
                        description="提交餐品搜索",
                        app=profile.name,
                        text="ENTER",
                    ),
                ]
            )
        return Plan(
            intent="food_search",
            objective=f"在{profile.name}中查找{query or '外卖'}",
            app=profile.name,
            source="rule",
            confidence=0.88,
            assumptions=["只导航和搜索，不自动提交订单或付款"],
            steps=steps,
            expected_result="展示可选商家或餐品",
            max_replans=self.settings.default_max_replans,
        )


class LLMPlanner:
    def __init__(self, registry: AppRegistry, settings: Settings) -> None:
        self.registry = registry
        self.settings = settings

    @property
    def available(self) -> bool:
        return bool(self.settings.llm_base_url and self.settings.llm_model)

    async def plan(self, request: TaskRequest, snapshot: UiSnapshot | None = None) -> Plan:
        if not self.available:
            raise RuntimeError("LLM planner is not configured")
        prompt = self._prompt(request, compress_snapshot(snapshot) if snapshot else None)
        raw = await self._call(prompt)
        plan = self._parse_plan(raw)
        return plan.model_copy(
            update={"source": "llm", "max_replans": self.settings.default_max_replans}
        )

    async def replan(
        self,
        request: TaskRequest,
        plan: Plan,
        failed_action: Action,
        snapshot: UiSnapshot,
        error: str,
    ) -> Plan:
        prompt = {
            "role": "mobile_replanner",
            "instruction": request.instruction,
            "objective": plan.objective,
            "failed_action": failed_action.model_dump(mode="json"),
            "error": error[:1000],
            "current_ui": compress_snapshot(
                snapshot,
                max_nodes=self.settings.ui_max_nodes,
                max_chars=self.settings.ui_max_chars,
            ),
            "requirements": self._requirements(),
            "output": "Return one JSON Plan object only; include only remaining actions.",
        }
        raw = await self._call(json.dumps(prompt, ensure_ascii=False, separators=(",", ":")))
        replanned = self._parse_plan(raw)
        return replanned.model_copy(update={"source": "llm-replan"})

    def _prompt(self, request: TaskRequest, compact_ui: dict[str, Any] | None) -> str:
        payload = {
            "role": "semantic_android_planner",
            "instruction": request.instruction,
            "host_context": request.context,
            "apps": self.registry.compact_catalog(request.instruction),
            "current_ui": compact_ui,
            "requirements": self._requirements(),
            "allowed_actions": [kind.value for kind in ActionKind],
            "output": "Return exactly one JSON Plan object. Do not include markdown or hidden reasoning.",
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _requirements() -> list[str]:
        return [
            "Prefer a short deterministic plan; do not repeatedly reopen the same page.",
            "Use selectors and visible labels before coordinates.",
            "Never request, read, or transmit passwords, OTPs, payment PINs, or CAPTCHA answers.",
            "Payment, placing an order, final ride call, sending a message, publishing, deleting, or calling must set requires_confirmation=true and risk=high or critical.",
            "Stop at ask_user for credentials, OTP, CAPTCHA, identity verification, or ambiguous irreversible choices.",
            "Do not invent package names when an app package is absent from the catalog.",
        ]

    async def _call(self, prompt: str) -> str:
        assert self.settings.llm_base_url is not None
        assert self.settings.llm_model is not None
        base = self.settings.llm_base_url.rstrip("/")
        headers = {"Content-Type": "application/json"}
        if self.settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.llm_api_key}"

        if self.settings.llm_api_style == "responses":
            url = f"{base}/responses"
            payload: dict[str, Any] = {
                "model": self.settings.llm_model,
                "input": prompt,
                "temperature": self.settings.llm_temperature,
                "max_output_tokens": self.settings.llm_max_output_tokens,
            }
        else:
            url = f"{base}/chat/completions"
            payload = {
                "model": self.settings.llm_model,
                "messages": [
                    {"role": "system", "content": "Compile intent into safe Android action JSON."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": self.settings.llm_temperature,
                "max_tokens": self.settings.llm_max_output_tokens,
                "response_format": {"type": "json_object"},
            }

        async with httpx.AsyncClient(timeout=self.settings.llm_timeout_s) as client:
            response = await client.post(url, headers=headers, json=payload)
            if response.status_code == 400 and "response_format" in payload:
                payload.pop("response_format", None)
                response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
        if self.settings.llm_api_style == "responses":
            return self._responses_text(data)
        return str(data["choices"][0]["message"]["content"])

    @staticmethod
    def _responses_text(data: dict[str, Any]) -> str:
        if isinstance(data.get("output_text"), str):
            return data["output_text"]
        chunks: list[str] = []
        for item in data.get("output", []):
            for content in item.get("content", []):
                text = content.get("text")
                if isinstance(text, str):
                    chunks.append(text)
        if not chunks:
            raise ValueError("Responses API result did not contain text")
        return "".join(chunks)

    @staticmethod
    def _parse_plan(raw: str) -> Plan:
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL)
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("LLM did not return a JSON object")
        return Plan.model_validate(json.loads(text[start : end + 1]))


class HybridPlanner(Planner):
    def __init__(self, registry: AppRegistry, settings: Settings) -> None:
        self.registry = registry
        self.settings = settings
        self.rules = RulePlanner(registry, settings)
        self.llm = LLMPlanner(registry, settings)

    async def plan(self, request: TaskRequest) -> Plan:
        deterministic = self.rules.try_plan(request)
        if deterministic is not None:
            return deterministic
        if self.llm.available:
            return await self.llm.plan(request)
        profile = self.registry.match_in_text(request.instruction)
        if profile:
            return Plan(
                intent="generic_open",
                objective=request.instruction,
                app=profile.name,
                source="rule-fallback",
                confidence=0.55,
                assumptions=["未配置LLM，因此只安全地打开目标应用"],
                steps=[self.rules._launch(profile)],
                expected_result="目标应用处于前台，复杂后续步骤需要LLM或新增适配器",
            )
        raise ValueError("无法从指令识别目标应用或确定性操作；请配置LLM规划器")

    async def replan(
        self,
        request: TaskRequest,
        plan: Plan,
        failed_action: Action,
        snapshot: UiSnapshot,
        error: str,
    ) -> Plan | None:
        if not self.llm.available:
            return None
        return await self.llm.replan(request, plan, failed_action, snapshot, error)
