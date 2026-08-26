from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from mobile_agent.apps import AppRegistry
from mobile_agent.cache import WorkflowCache
from mobile_agent.config import Settings
from mobile_agent.models import (
    Action,
    ActionKind,
    ActionResult,
    ConfirmationRequest,
    DeviceRef,
    InstalledApp,
    Plan,
    RiskLevel,
    TaskRequest,
    TaskStatus,
    UiSnapshot,
)
from mobile_agent.risk import RiskPolicy
from mobile_agent.runtime import TaskRunner, TaskStore


class StubPlanner:
    def __init__(self, plan: Plan, replacement: Plan | None = None) -> None:
        self.plan_value = plan
        self.replacement = replacement
        self.replan_calls = 0

    async def plan(self, request: TaskRequest) -> Plan:
        del request
        return self.plan_value

    async def replan(
        self,
        request: TaskRequest,
        plan: Plan,
        failed_action: Action,
        snapshot: UiSnapshot,
        error: str,
    ) -> Plan | None:
        del request, plan, failed_action, snapshot, error
        self.replan_calls += 1
        return self.replacement


class SequenceExecutor:
    def __init__(self, outcomes: list[bool], *, delay_s: float = 0) -> None:
        self.outcomes = list(outcomes)
        self.delay_s = delay_s
        self.calls: list[str] = []
        self.closed = False

    async def execute(self, action: Action, device: DeviceRef) -> ActionResult:
        del device
        self.calls.append(action.id)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        ok = self.outcomes.pop(0) if self.outcomes else True
        return ActionResult(
            action_id=action.id,
            ok=ok,
            latency_ms=1,
            backend="fake",
            message="ok" if ok else "boom",
        )

    async def snapshot(self, device: DeviceRef) -> UiSnapshot:
        del device
        return UiSnapshot(state_hash="screen-1")

    async def list_apps(self, device: DeviceRef) -> list[InstalledApp]:
        del device
        return []

    async def close(self) -> None:
        self.closed = True


async def wait_for_status(
    runner: TaskRunner,
    task_id: str,
    statuses: set[TaskStatus],
):
    async with asyncio.timeout(3):
        while True:
            task = await runner.store.get(task_id)
            if task.status in statuses:
                return task
            await asyncio.sleep(0.01)


async def make_runner(
    tmp_path: Path,
    planner: StubPlanner,
    executor: SequenceExecutor,
) -> TaskRunner:
    settings = Settings(database_path=tmp_path / "agent.db")
    cache = WorkflowCache(settings.database_path, min_successes=1)
    await cache.initialize()
    return TaskRunner(
        planner=planner,
        executor=executor,
        registry=AppRegistry(settings.profiles_file),
        risk_policy=RiskPolicy(require_confirmation=True),
        cache=cache,
        store=TaskStore(),
    )


def request() -> TaskRequest:
    return TaskRequest(
        instruction="执行测试动作",
        device=DeviceRef(serial="emulator-5554"),
        context={"ui_state_hash": "initial"},
    )


@pytest.mark.asyncio
async def test_executor_failure_preserves_original_error(tmp_path: Path) -> None:
    action = Action(
        kind=ActionKind.WAIT,
        description="会失败的动作",
        retries=0,
        duration_ms=0,
    )
    planner = StubPlanner(
        Plan(
            intent="test",
            objective="failure path",
            steps=[action],
            max_replans=0,
        )
    )
    executor = SequenceExecutor([False])
    runner = await make_runner(tmp_path, planner, executor)

    try:
        submitted = await runner.submit(request())
        task = await wait_for_status(runner, submitted.task_id, {TaskStatus.FAILED})
        assert task.error is not None
        assert "boom" in task.error
        assert "NameError" not in task.error
        assert [result.action_id for result in task.results] == [action.id]
    finally:
        await runner.close()


@pytest.mark.asyncio
async def test_failed_action_installs_and_executes_replan(tmp_path: Path) -> None:
    failed = Action(
        kind=ActionKind.WAIT,
        description="旧页面动作",
        retries=0,
        duration_ms=0,
    )
    recovered = Action(kind=ActionKind.FINISH, description="新页面完成")
    replacement = Plan(
        intent="test-replanned",
        objective="recover",
        steps=[recovered],
        max_replans=0,
    )
    planner = StubPlanner(
        Plan(
            intent="test",
            objective="initial",
            steps=[failed],
            max_replans=1,
        ),
        replacement,
    )
    executor = SequenceExecutor([False, True])
    runner = await make_runner(tmp_path, planner, executor)

    try:
        submitted = await runner.submit(request())
        task = await wait_for_status(runner, submitted.task_id, {TaskStatus.SUCCEEDED})
        assert planner.replan_calls == 1
        assert task.plan is not None
        assert task.plan.plan_id == replacement.plan_id
        assert executor.calls == [failed.id, recovered.id]
        assert [result.action_id for result in task.results] == [failed.id, recovered.id]
    finally:
        await runner.close()


@pytest.mark.asyncio
async def test_high_risk_action_is_not_automatically_retried(tmp_path: Path) -> None:
    action = Action(
        kind=ActionKind.WAIT,
        description="提交不可逆动作",
        retries=3,
        duration_ms=0,
        risk=RiskLevel.HIGH,
        requires_confirmation=True,
    )
    planner = StubPlanner(Plan(intent="test", objective="risk", steps=[]))
    executor = SequenceExecutor([False, True, True, True])
    runner = await make_runner(tmp_path, planner, executor)

    try:
        result = await runner._execute_with_retries(executor, action, request())
        assert result.ok is False
        assert len(executor.calls) == 1
        assert result.details["max_attempts"] == 1
    finally:
        await runner.close()


@pytest.mark.asyncio
async def test_low_risk_action_retries_and_honors_timeout(tmp_path: Path) -> None:
    planner = StubPlanner(Plan(intent="test", objective="retry", steps=[]))
    executor = SequenceExecutor([False, True])
    runner = await make_runner(tmp_path, planner, executor)
    retry_action = Action(
        kind=ActionKind.WAIT,
        description="可安全重试",
        retries=2,
        duration_ms=0,
    )

    try:
        result = await runner._execute_with_retries(executor, retry_action, request())
        assert result.ok is True
        assert len(executor.calls) == 2
        assert result.details["attempt"] == 2

        slow_executor = SequenceExecutor([True], delay_s=0.2)
        timeout_action = Action(
            kind=ActionKind.WAIT,
            description="超时动作",
            retries=0,
            duration_ms=0,
            timeout_ms=100,
        )
        timed_out = await runner._execute_with_retries(
            slow_executor,
            timeout_action,
            request(),
        )
        assert timed_out.ok is False
        assert timed_out.backend == "timeout"
        assert "100 ms" in timed_out.message
    finally:
        await runner.close()


@pytest.mark.asyncio
async def test_ask_user_is_manual_handoff_not_device_action(tmp_path: Path) -> None:
    handoff = Action(
        kind=ActionKind.ASK_USER,
        description="请在手机上完成验证码后确认继续",
    )
    finish = Action(kind=ActionKind.FINISH, description="完成")
    planner = StubPlanner(
        Plan(
            intent="manual-handoff",
            objective="wait for user",
            steps=[handoff, finish],
            max_replans=0,
        )
    )
    executor = SequenceExecutor([True])
    runner = await make_runner(tmp_path, planner, executor)

    try:
        submitted = await runner.submit(request())
        waiting = await wait_for_status(
            runner,
            submitted.task_id,
            {TaskStatus.AWAITING_CONFIRMATION},
        )
        assert waiting.pending_action is not None
        assert waiting.pending_action.kind is ActionKind.ASK_USER
        assert executor.calls == []

        await runner.confirm(
            submitted.task_id,
            ConfirmationRequest(approved=True, note="人工处理完成"),
        )
        task = await wait_for_status(runner, submitted.task_id, {TaskStatus.SUCCEEDED})
        assert executor.calls == [finish.id]
        assert task.results[0].backend == "user-handoff"
        assert task.results[0].action_id == handoff.id
    finally:
        await runner.close()
