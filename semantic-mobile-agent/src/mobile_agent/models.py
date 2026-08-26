from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class RiskLevel(StrEnum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ActionKind(StrEnum):
    LAUNCH_APP = "launch_app"
    DEEP_LINK = "deep_link"
    TAP = "tap"
    TAP_TEXT = "tap_text"
    INPUT_TEXT = "input_text"
    SWIPE = "swipe"
    KEY = "key"
    WAIT = "wait"
    ASSERT_UI = "assert_ui"
    ASK_USER = "ask_user"
    FINISH = "finish"


class TaskStatus(StrEnum):
    QUEUED = "queued"
    PLANNING = "planning"
    RUNNING = "running"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DeviceRef(BaseModel):
    """The host resolves the actual device; the agent only consumes this reference."""

    serial: str = Field(min_length=1, description="ADB serial or TCP address")
    appium_url: str | None = None
    bridge_port: int | None = Field(default=None, ge=1024, le=65535)
    bridge_token: str | None = Field(default=None, repr=False)
    platform_version: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Selector(BaseModel):
    text: str | None = None
    text_contains: str | None = None
    candidate_texts: list[str] = Field(default_factory=list)
    resource_id: str | None = None
    content_desc: str | None = None
    class_name: str | None = None
    clickable: bool | None = True
    editable: bool | None = None
    node_id: int | None = None
    x: int | None = None
    y: int | None = None

    @property
    def is_coordinate(self) -> bool:
        return self.x is not None and self.y is not None


class Action(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    kind: ActionKind
    description: str
    app: str | None = None
    package: str | None = None
    selector: Selector | None = None
    text: str | None = None
    uri: str | None = None
    direction: str | None = None
    duration_ms: int = Field(default=250, ge=0, le=30_000)
    wait_after_ms: int = Field(default=120, ge=0, le=30_000)
    timeout_ms: int = Field(default=3000, ge=100, le=120_000)
    retries: int = Field(default=1, ge=0, le=5)
    risk: RiskLevel = RiskLevel.NONE
    requires_confirmation: bool = False
    expected_texts: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_payload(self) -> Action:
        if self.kind in {ActionKind.TAP, ActionKind.TAP_TEXT, ActionKind.INPUT_TEXT}:
            if self.selector is None:
                raise ValueError(f"{self.kind} requires selector")
        if self.kind is ActionKind.INPUT_TEXT and self.text is None:
            raise ValueError("input_text requires text")
        if self.kind is ActionKind.DEEP_LINK and self.uri is None:
            raise ValueError("deep_link requires uri")
        return self


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(default_factory=lambda: uuid4().hex)
    intent: str
    objective: str
    app: str | None = None
    source: str = "rule"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    assumptions: list[str] = Field(default_factory=list)
    steps: list[Action] = Field(default_factory=list, max_length=60)
    expected_result: str | None = None
    max_replans: int = Field(default=2, ge=0, le=5)


class TaskRequest(BaseModel):
    instruction: str = Field(min_length=1, max_length=4000)
    device: DeviceRef
    context: dict[str, Any] = Field(default_factory=dict)
    dry_run: bool = False
    max_steps: int = Field(default=30, ge=1, le=60)
    allow_cached_path: bool = True
    auto_confirm_low_risk: bool = True
    idempotency_key: str | None = Field(default=None, max_length=200)


class PlanRequest(TaskRequest):
    include_ui: bool = False


class ConfirmationRequest(BaseModel):
    approved: bool
    note: str | None = Field(default=None, max_length=1000)


class UiNode(BaseModel):
    node_id: int
    text: str = ""
    content_desc: str = ""
    resource_id: str = ""
    class_name: str = ""
    package: str = ""
    clickable: bool = False
    editable: bool = False
    enabled: bool = True
    focused: bool = False
    visible: bool = True
    depth: int = 0
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.bounds
        return ((left + right) // 2, (top + bottom) // 2)


class UiSnapshot(BaseModel):
    package: str = ""
    activity: str = ""
    rotation: int = 0
    width: int = 0
    height: int = 0
    nodes: list[UiNode] = Field(default_factory=list)
    captured_at: datetime = Field(default_factory=utc_now)
    state_hash: str = ""


class ActionResult(BaseModel):
    action_id: str
    ok: bool
    latency_ms: float
    backend: str
    message: str = ""
    before_state: str | None = None
    after_state: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class TaskRecord(BaseModel):
    task_id: str = Field(default_factory=lambda: uuid4().hex)
    status: TaskStatus = TaskStatus.QUEUED
    request: TaskRequest
    plan: Plan | None = None
    current_step: int = 0
    results: list[ActionResult] = Field(default_factory=list)
    pending_action: Action | None = None
    confirmation_reason: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None


class InstalledApp(BaseModel):
    label: str
    package: str
    activity: str | None = None
    source: str = "device"
