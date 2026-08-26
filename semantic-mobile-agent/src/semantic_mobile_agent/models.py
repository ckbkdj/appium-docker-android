from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class TaskStatus(StrEnum):
    QUEUED = "queued"
    PLANNING = "planning"
    RUNNING = "running"
    WAITING_CONFIRMATION = "waiting_confirmation"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ActionKind(StrEnum):
    OPEN_APP = "open_app"
    CLICK = "click"
    TAP = "tap"
    SET_TEXT = "set_text"
    SWIPE = "swipe"
    KEY = "key"
    WAIT = "wait"
    ASSERT = "assert"
    FINISH = "finish"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Locator(BaseModel):
    strategy: Literal[
        "resource_id",
        "text",
        "text_contains",
        "description",
        "role",
        "path",
        "semantic",
        "focused",
    ] = "semantic"
    value: str = ""
    alternatives: list[str] = Field(default_factory=list)
    index: int = 0


class PrimitiveAction(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    kind: ActionKind
    package: str | None = None
    app: str | None = None
    locator: Locator | None = None
    text: str | None = None
    x: int | None = None
    y: int | None = None
    x1: int | None = None
    y1: int | None = None
    x2: int | None = None
    y2: int | None = None
    key: str | None = None
    duration_ms: int = 250
    timeout_ms: int = 3000
    expected: str | None = None
    risk: RiskLevel = RiskLevel.LOW
    confirmation_message: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_required_fields(self) -> PrimitiveAction:
        if self.kind == ActionKind.OPEN_APP and not (self.package or self.app):
            raise ValueError("open_app requires package or app")
        if self.kind == ActionKind.TAP and (self.x is None or self.y is None):
            raise ValueError("tap requires x and y")
        if self.kind == ActionKind.SET_TEXT and self.text is None:
            raise ValueError("set_text requires text")
        if self.kind == ActionKind.SWIPE and None in (self.x1, self.y1, self.x2, self.y2):
            raise ValueError("swipe requires x1/y1/x2/y2")
        if self.kind == ActionKind.KEY and not self.key:
            raise ValueError("key requires key")
        return self


class TaskPlan(BaseModel):
    goal: str
    intent: str
    app: str | None = None
    package: str | None = None
    confidence: float = Field(default=0.5, ge=0, le=1)
    steps: list[PrimitiveAction] = Field(default_factory=list)
    max_steps: int = Field(default=30, ge=1, le=100)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskRequest(BaseModel):
    instruction: str = Field(min_length=1, max_length=4000)
    device: str | None = None
    locale: str = "zh-CN"
    context: dict[str, Any] = Field(default_factory=dict)
    dry_run: bool = False
    allow_risks: list[RiskLevel] = Field(default_factory=list)


class ConfirmationRequest(BaseModel):
    approved: bool
    note: str | None = None


class Bounds(BaseModel):
    left: int
    top: int
    right: int
    bottom: int

    @property
    def center(self) -> tuple[int, int]:
        return ((self.left + self.right) // 2, (self.top + self.bottom) // 2)

    @property
    def area(self) -> int:
        return max(0, self.right - self.left) * max(0, self.bottom - self.top)


class UINode(BaseModel):
    path: str
    text: str = ""
    description: str = ""
    resource_id: str = ""
    class_name: str = ""
    package: str = ""
    clickable: bool = False
    editable: bool = False
    scrollable: bool = False
    enabled: bool = True
    selected: bool = False
    checked: bool = False
    focused: bool = False
    bounds: Bounds

    @property
    def label(self) -> str:
        return " ".join(x for x in (self.text, self.description, self.resource_id) if x).strip()


class UISnapshot(BaseModel):
    package: str = ""
    activity: str = ""
    width: int = 0
    height: int = 0
    source: str = "unknown"
    captured_at_ms: int = Field(default_factory=lambda: int(time.time() * 1000))
    state_hash: str = ""
    nodes: list[UINode] = Field(default_factory=list)


class ActionResult(BaseModel):
    action_id: str
    success: bool
    driver: str
    latency_ms: int
    message: str = ""
    before_state: str = ""
    after_state: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskEvent(BaseModel):
    at_ms: int = Field(default_factory=lambda: int(time.time() * 1000))
    type: str
    message: str
    action: PrimitiveAction | None = None
    result: ActionResult | None = None


class TaskView(BaseModel):
    id: str
    instruction: str
    device: str | None = None
    status: TaskStatus
    plan: TaskPlan | None = None
    cursor: int = 0
    pending_action: PrimitiveAction | None = None
    error: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    events: list[TaskEvent] = Field(default_factory=list)
    created_at_ms: int = Field(default_factory=lambda: int(time.time() * 1000))
    updated_at_ms: int = Field(default_factory=lambda: int(time.time() * 1000))
