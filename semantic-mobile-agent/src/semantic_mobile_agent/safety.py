from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

import yaml

from .models import ActionKind, PrimitiveAction, RiskLevel

_RISK_ORDER = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}


def max_risk(*values: RiskLevel) -> RiskLevel:
    return max(values, key=_RISK_ORDER.__getitem__)


class SafetyPolicy:
    """Classifies commitment actions independently from the model's own label.

    The model may raise risk, but it cannot lower a risk inferred by deterministic
    patterns. Navigation, typing and browsing remain automatic; irreversible or
    externally committing operations stop before the final click.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.critical_patterns: list[re.Pattern[str]] = []
        self.high_risk_patterns: list[re.Pattern[str]] = []
        if path and Path(path).exists():
            self.load(Path(path))

    def load(self, path: Path) -> None:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        self.critical_patterns = [
            re.compile(pattern, re.IGNORECASE) for pattern in raw.get("critical_patterns", [])
        ]
        self.high_risk_patterns = [
            re.compile(pattern, re.IGNORECASE) for pattern in raw.get("high_risk_patterns", [])
        ]

    @staticmethod
    def _action_text(action: PrimitiveAction) -> str:
        locator = action.locator
        pieces: Iterable[str | None] = (
            action.app,
            action.package,
            action.text,
            action.expected,
            action.confirmation_message,
            locator.value if locator else None,
            " ".join(locator.alternatives) if locator else None,
            str(action.metadata.get("purpose", "")),
            str(action.metadata.get("label", "")),
        )
        return " ".join(piece for piece in pieces if piece)

    def classify(self, action: PrimitiveAction) -> RiskLevel:
        text = self._action_text(action)
        inferred = RiskLevel.LOW

        if any(pattern.search(text) for pattern in self.critical_patterns):
            inferred = RiskLevel.CRITICAL
        elif any(pattern.search(text) for pattern in self.high_risk_patterns):
            inferred = RiskLevel.HIGH
        elif action.kind in {ActionKind.SET_TEXT, ActionKind.SWIPE, ActionKind.KEY}:
            inferred = RiskLevel.MEDIUM

        # Opening an app, waiting, asserting and finishing are never escalated by an
        # untrusted model label alone. Click/tap may retain an explicit higher label.
        if action.kind in {
            ActionKind.OPEN_APP,
            ActionKind.WAIT,
            ActionKind.ASSERT,
            ActionKind.FINISH,
        }:
            explicit = RiskLevel.LOW
        else:
            explicit = action.risk

        return max_risk(inferred, explicit)

    def normalize(self, action: PrimitiveAction) -> PrimitiveAction:
        risk = self.classify(action)
        update: dict[str, object] = {"risk": risk}
        if risk in {RiskLevel.HIGH, RiskLevel.CRITICAL} and not action.confirmation_message:
            label = self._action_text(action).strip() or action.kind.value
            update["confirmation_message"] = f"即将执行高风险操作：{label}。是否继续？"
        return action.model_copy(update=update)

    def needs_confirmation(
        self,
        action: PrimitiveAction,
        *,
        require_confirmation: bool,
        allow_unsafe: bool,
        allowed_risks: set[RiskLevel] | None = None,
    ) -> bool:
        if allow_unsafe or not require_confirmation:
            return False
        risk = self.classify(action)
        if allowed_risks and risk in allowed_risks:
            return False
        return risk in {RiskLevel.HIGH, RiskLevel.CRITICAL}
