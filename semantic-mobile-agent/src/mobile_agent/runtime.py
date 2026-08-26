from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from functools import partial
from typing import Any

from .apps import AppRegistry
from .cache import WorkflowCache
from .device import ActionExecutor, DryRunExecutor
from .models import (
    Action,
    ActionKind,
    ActionResult,
    ConfirmationRequest,
    Plan,
    RiskLevel,
    TaskRecord,
    TaskRequest,
    TaskStatus,
    UiSnapshot,
    utc_now,
)
from .planner import Planner
from .risk import RiskPolicy


class TaskNotFound(KeyError):
    pass


class TaskConflict(RuntimeError):
    pass


class TaskStore:
    """In-memory active task store with idempotency and bounded retention."""

    def __init__(self, retention: int = 1000) -> None:
        self.retention = max(10, retention)
        self._tasks: dict[str, TaskRecord] = {}
        self._idempotency: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def add(self, task: TaskRecord) -> tuple[TaskRecord, bool]:
        async with self._lock:
            key = task.request.idempotency_key
            if key and key in self._idempotency:
                existing = self._tasks.get(self._idempotency[key])
                if existing is not None:
                    return existing, False
            self._tasks[task.task_id] = task
            if key:
                self._idempotency[key] = task.task_id
            await self._prune_locked()
            return task, True

    async def get(self, task_id: str) -> TaskRecord:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise TaskNotFound(task_id)
            return task

    async def mutate(
        self,
        task_id: str,
        mutator: Callable[[TaskRecord], Any],
    ) -> TaskRecord:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise TaskNotFound(task_id)
            mutator(task)
            task.updated_at = utc_now()
            return task

    async def _prune_locked(self) -> None:
        if len(self._tasks) <= self.retention:
            return
        terminal = [
            task
            for task in self._tasks.values()
            if task.status
            in {
                TaskStatus.SUCCEEDED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }
        ]
        terminal.sort(key=lambda task: task.updated_at)
        for task in terminal[: max(0, len(self._tasks) - self.retention)]:
            self._tasks.pop(task.task_id, None)
            key = task.request.idempotency_key
            if key and self._idempotency.get(key) == task.task_id:
                self._idempotency.pop(key, None)


class TaskRunner:
    def __init__(
        self,
        planner: Planner,
        executor: ActionExecutor,
        registry: AppRegistry,
        risk_policy: RiskPolicy,
        cache: WorkflowCache,
        store: TaskStore,
    ) -> None:
        self.planner = planner
        self.executor = executor
        self.registry = registry
        self.risk_policy = risk_policy
        self.cache = cache
        self.store = store
        self._jobs: dict[str, asyncio.Task[None]] = {}
        self._confirm_events: dict[str, asyncio.Event] = {}
        self._confirm_decisions: dict[str, ConfirmationRequest] = {}
        self._closed = False

    async def plan_only(self, request: TaskRequest) -> Plan:
        plan = await self.planner.plan(request)
        return self._bounded_plan(plan, request.max_steps)

    async def submit(self, request: TaskRequest) -> TaskRecord:
        if self._closed:
            raise RuntimeError("Task runner is closed")
        record = TaskRecord(request=request)
        stored, created = await self.store.add(record)
        if not created:
            return stored
        job = asyncio.create_task(self._run(record.task_id), name=f"mobile-task-{record.task_id}")
        self._jobs[record.task_id] = job
        job.add_done_callback(
            lambda _job, task_id=record.task_id: self._jobs.pop(task_id, None)
        )
        return record

    async def confirm(self, task_id: str, request: ConfirmationRequest) -> TaskRecord:
        task = await self.store.get(task_id)
        if task.status is not TaskStatus.AWAITING_CONFIRMATION or task.pending_action is None:
            raise TaskConflict("Task is not awaiting confirmation")
        self._confirm_decisions[task_id] = request
        event = self._confirm_events.get(task_id)
        if event is None:
            raise TaskConflict("Confirmation waiter is unavailable")
        event.set()
        return task

    async def cancel(self, task_id: str) -> TaskRecord:
        task = await self.store.get(task_id)
        if task.status in {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            return task

        def mark_cancelled(record: TaskRecord) -> None:
            record.status = TaskStatus.CANCELLED
            record.error = "Task cancelled"
            record.pending_action = None
            record.confirmation_reason = None
            record.finished_at = utc_now()

        task = await self.store.mutate(task_id, mark_cancelled)
        event = self._confirm_events.get(task_id)
        if event is not None:
            event.set()
        job = self._jobs.get(task_id)
        if job is not None and job is not asyncio.current_task():
            job.cancel()
        return task

    async def close(self) -> None:
        self._closed = True
        jobs = list(self._jobs.values())
        for job in jobs:
            job.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
        await self.executor.close()

    async def _run(self, task_id: str) -> None:
        task = await self.store.get(task_id)
        started = time.perf_counter()
        initial_state = self._provided_state(task.request)
        try:
            await self.store.mutate(task_id, self._mark_planning)
            if initial_state is None and not task.request.dry_run:
                initial_state = await self._safe_state(task.request)

            plan: Plan | None = None
            profile = self.registry.match_in_text(task.request.instruction)
            app_name = profile.name if profile else None
            if task.request.allow_cached_path:
                plan = await self.cache.get(task.request.instruction, app_name, initial_state)
            if plan is None:
                plan = await self.planner.plan(task.request)
            plan = self._bounded_plan(plan, task.request.max_steps)

            await self.store.mutate(task_id, partial(self._start_running, plan=plan))
            succeeded = await self._execute_plan(task_id, initial_state)
            task = await self.store.get(task_id)
            if not succeeded or task.status is TaskStatus.CANCELLED:
                return
            total_latency_ms = (time.perf_counter() - started) * 1000
            await self.cache.record_success(
                task.request.instruction,
                task.plan.app if task.plan else app_name,
                initial_state,
                task.plan,
                total_latency_ms,
            )
            await self.store.mutate(task_id, self._mark_succeeded)
        except asyncio.CancelledError:
            task = await self.store.get(task_id)
            if task.status is not TaskStatus.CANCELLED:
                await self.store.mutate(task_id, self._mark_cancelled)
            raise
        except Exception as exc:
            error_message = str(exc)
            task = await self.store.get(task_id)
            if task.plan is not None:
                await self.cache.record_failure(
                    task.request.instruction,
                    task.plan.app,
                    initial_state,
                    task.plan,
                )
            await self.store.mutate(
                task_id,
                partial(self._mark_failed, message=error_message),
            )
        finally:
            self._confirm_events.pop(task_id, None)
            self._confirm_decisions.pop(task_id, None)

    async def _execute_plan(self, task_id: str, initial_state: str | None) -> bool:
        del initial_state
        replans = 0
        while True:
            task = await self.store.get(task_id)
            if task.plan is None:
                raise RuntimeError("Task does not have a plan")
            plan = task.plan
            while task.current_step < len(plan.steps):
                if task.status is TaskStatus.CANCELLED:
                    return False
                action = plan.steps[task.current_step]

                if action.kind is ActionKind.ASK_USER:
                    handoff = action.model_copy(
                        update={
                            "requires_confirmation": True,
                            "risk": RiskLevel.HIGH,
                        }
                    )
                    approved = await self._await_confirmation(
                        task_id,
                        handoff,
                        action.description or "需要用户在手机上手动处理后继续",
                    )
                    if not approved:
                        await self.store.mutate(task_id, self._mark_cancelled_by_user)
                        return False
                    result = ActionResult(
                        action_id=action.id,
                        ok=True,
                        latency_ms=0,
                        backend="user-handoff",
                        message="user completed manual handoff",
                    )
                    await self.store.mutate(
                        task_id,
                        partial(self._append_result, result=result),
                    )
                    await self.store.mutate(task_id, self._advance_step)
                    task = await self.store.get(task_id)
                    continue

                decision = self.risk_policy.evaluate(action)
                action = action.model_copy(
                    update={
                        "risk": decision.level,
                        "requires_confirmation": action.requires_confirmation
                        or decision.requires_confirmation,
                    }
                )
                if action.requires_confirmation:
                    approved = await self._await_confirmation(task_id, action, decision.reason)
                    if not approved:
                        await self.store.mutate(task_id, self._mark_cancelled_by_user)
                        return False

                executor: ActionExecutor = (
                    DryRunExecutor() if task.request.dry_run else self.executor
                )
                result = await self._execute_with_retries(executor, action, task.request)
                await self.store.mutate(
                    task_id,
                    partial(self._append_result, result=result),
                )
                if result.ok:
                    await self.store.mutate(task_id, self._advance_step)
                    task = await self.store.get(task_id)
                    continue

                if replans >= plan.max_replans:
                    raise RuntimeError(
                        f"Action failed after retries: {action.description}: {result.message}"
                    )
                snapshot = await self._safe_snapshot(task.request)
                if snapshot is None:
                    raise RuntimeError(
                        "Action failed and UI could not be captured: "
                        f"{action.description}: {result.message}"
                    )
                replacement = await self.planner.replan(
                    task.request,
                    plan,
                    action,
                    snapshot,
                    result.message,
                )
                if replacement is None:
                    raise RuntimeError(
                        "Action failed and no replanner is available: "
                        f"{action.description}: {result.message}"
                    )
                replans += 1
                replacement = self._bounded_plan(replacement, task.request.max_steps)
                await self.store.mutate(
                    task_id,
                    partial(self._install_replan, plan=replacement),
                )
                break
            else:
                return True

    async def _execute_with_retries(
        self,
        executor: ActionExecutor,
        action: Action,
        request: TaskRequest,
    ) -> ActionResult:
        retry_safe = action.metadata.get("retry_safe") is True
        max_attempts = action.retries + 1
        if not retry_safe and (
            action.requires_confirmation
            or action.risk in {RiskLevel.HIGH, RiskLevel.CRITICAL}
        ):
            max_attempts = 1

        last: ActionResult | None = None
        for attempt in range(max_attempts):
            started = time.perf_counter()
            try:
                last = await asyncio.wait_for(
                    executor.execute(action, request.device),
                    timeout=max(0.1, action.timeout_ms / 1000),
                )
            except TimeoutError:
                last = ActionResult(
                    action_id=action.id,
                    ok=False,
                    latency_ms=(time.perf_counter() - started) * 1000,
                    backend="timeout",
                    message=f"action timed out after {action.timeout_ms} ms",
                )
            last.details["attempt"] = attempt + 1
            last.details["max_attempts"] = max_attempts
            if last.ok:
                return last
            if attempt + 1 < max_attempts:
                await asyncio.sleep(min(0.15 * (attempt + 1), 0.5))
        assert last is not None
        return last

    async def _await_confirmation(
        self,
        task_id: str,
        action: Action,
        reason: str,
    ) -> bool:
        event = asyncio.Event()
        self._confirm_events[task_id] = event

        def mark_waiting(record: TaskRecord) -> None:
            record.status = TaskStatus.AWAITING_CONFIRMATION
            record.pending_action = action
            record.confirmation_reason = reason

        await self.store.mutate(task_id, mark_waiting)
        await event.wait()
        task = await self.store.get(task_id)
        if task.status is TaskStatus.CANCELLED:
            return False
        decision = self._confirm_decisions.pop(task_id, None)
        self._confirm_events.pop(task_id, None)

        def resume(record: TaskRecord) -> None:
            record.status = TaskStatus.RUNNING
            record.pending_action = None
            record.confirmation_reason = None

        await self.store.mutate(task_id, resume)
        return bool(decision and decision.approved)

    async def _safe_state(self, request: TaskRequest) -> str | None:
        snapshot = await self._safe_snapshot(request)
        return snapshot.state_hash if snapshot is not None else None

    async def _safe_snapshot(self, request: TaskRequest) -> UiSnapshot | None:
        try:
            return await self.executor.snapshot(request.device)
        except Exception:
            return None

    @staticmethod
    def _provided_state(request: TaskRequest) -> str | None:
        state = request.context.get("ui_state_hash")
        return state if isinstance(state, str) and state else None

    @staticmethod
    def _bounded_plan(plan: Plan, max_steps: int) -> Plan:
        if len(plan.steps) <= max_steps:
            return plan
        return plan.model_copy(
            update={
                "steps": plan.steps[:max_steps],
                "assumptions": [
                    *plan.assumptions,
                    f"计划超过宿主限制，已截断为{max_steps}步",
                ],
            }
        )

    @staticmethod
    def _mark_planning(record: TaskRecord) -> None:
        record.status = TaskStatus.PLANNING

    @staticmethod
    def _start_running(record: TaskRecord, *, plan: Plan) -> None:
        record.plan = plan
        record.status = TaskStatus.RUNNING
        record.current_step = 0

    @staticmethod
    def _append_result(record: TaskRecord, *, result: ActionResult) -> None:
        record.results.append(result)

    @staticmethod
    def _advance_step(record: TaskRecord) -> None:
        record.current_step += 1

    @staticmethod
    def _install_replan(record: TaskRecord, *, plan: Plan) -> None:
        record.plan = plan
        record.current_step = 0
        record.status = TaskStatus.RUNNING
        record.pending_action = None
        record.confirmation_reason = None

    @staticmethod
    def _mark_succeeded(record: TaskRecord) -> None:
        record.status = TaskStatus.SUCCEEDED
        record.pending_action = None
        record.confirmation_reason = None
        record.finished_at = utc_now()

    @staticmethod
    def _mark_failed(record: TaskRecord, message: str) -> None:
        record.status = TaskStatus.FAILED
        record.error = message[:4000]
        record.pending_action = None
        record.confirmation_reason = None
        record.finished_at = utc_now()

    @staticmethod
    def _mark_cancelled(record: TaskRecord) -> None:
        record.status = TaskStatus.CANCELLED
        record.error = record.error or "Task cancelled"
        record.pending_action = None
        record.confirmation_reason = None
        record.finished_at = utc_now()

    @staticmethod
    def _mark_cancelled_by_user(record: TaskRecord) -> None:
        record.status = TaskStatus.CANCELLED
        record.error = "User rejected the pending action"
        record.pending_action = None
        record.confirmation_reason = None
        record.finished_at = utc_now()
