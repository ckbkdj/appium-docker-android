from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Action, ActionKind, RiskLevel


@dataclass(frozen=True)
class RiskDecision:
    level: RiskLevel
    requires_confirmation: bool
    reason: str


class RiskPolicy:
    _critical = re.compile(r"(付款|支付|确认支付|转账|提交订单|立即购买|下单并支付|借款|提现吗)")
    _high = re.compile(
        r"(呼叫|叫车|确认叫车|发送|发消息|发布|删除|注销|拨打|确认订单|预订|提交|抢票)"
    )
    _medium = re.compile(r"(加入购物车|选择车型|选择座位|填写手机号|授权|同意|登录)")

    def __init__(self, require_confirmation: bool = True) -> None:
        self.require_confirmation = require_confirmation

    def evaluate(self, action: Action) -> RiskDecision:
        if action.requires_confirmation:
            return RiskDecision(
                action.risk if action.risk is not RiskLevel.NONE else RiskLevel.HIGH,
                True,
                "动作计划已显式标记为需要确认",
            )
        text = " ".join(
            filter(
                None,
                [
                    action.description,
                    action.text,
                    action.selector.text if action.selector else None,
                    action.selector.text_contains if action.selector else None,
                    " ".join(action.selector.candidate_texts) if action.selector else None,
                ],
            )
        )
        if self._critical.search(text):
            return RiskDecision(RiskLevel.CRITICAL, self.require_confirmation, "涉及支付或资金动作")
        if self._high.search(text):
            return RiskDecision(RiskLevel.HIGH, self.require_confirmation, "涉及不可逆外部动作")
        if self._medium.search(text):
            return RiskDecision(RiskLevel.MEDIUM, False, "涉及授权、登录或交易准备")
        if action.kind in {ActionKind.LAUNCH_APP, ActionKind.WAIT, ActionKind.ASSERT_UI}:
            return RiskDecision(RiskLevel.NONE, False, "只读或导航动作")
        return RiskDecision(action.risk, False, "普通界面操作")
