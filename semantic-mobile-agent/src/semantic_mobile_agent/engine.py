from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import Counter
from typing import Any

from .apps import AppRegistry
from .cache import ActionCache
from .config import Settings
from .device import DeviceInfo, DeviceResolver
from .drivers import HybridDriver
from .models import (
    ActionKind,
    ConfirmationRequest,
    PrimitiveAction,
    RiskLevel,
    TaskEvent,
    TaskPlan,
    TaskRequest,
    TaskStatus,
    TaskView,
)
from .planner import Planner, goal_key
from .safety import SafetyPolicy

LOGGER = logging.getLogger(__name__)


class TaskNotFound(KeyError):
    pass


class TaskEngine:
    """Coordinates planning, execution, recovery, caching and risk confirmation."""

    def __init__(
        self,
        settings: Settings,
        registry: AppRegistry,
        planner: Planner,
        safety: SafetyPolicy,
        cache: ActionCache,
        driver: HybridDriver,
        devices: DeviceResolver,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.planner = planner
        self.safety = safety
        self.cache = cache
        self.driver = driver
        self.devices = devices
        self._tasks: dict[str, TaskView] = {}
        self._requests: dict[str, TaskRequest] = {}
        self._jobs: dict[str, asyncio.Task[None]] = {}
        self._approved_once: dict[str, set[str]] = {}
        self._replans: Counter[str] = Counter()
        self._loop_counts: dict[str, Counter[str]] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    async def _store(self, task: TaskView) -> None:
        task.updated_at_ms = self._now_ms()
        if len(task.events) > 200:
            task.events = task.events[-200:]
        async with self._lock:
            self._tasks[task.id] = task

    async def _event(
        self,
        task: TaskView,
        event_type: str,
        message: str,
        *,
        action: PrimitiveAction | None = None,
        result=None,
    ) -> None:
        task.events.append(
            TaskEvent(type=event_type, message=message, action=action, result=result)
        )
        await self._store(task)

    async def devices_list(self) -> list[DeviceInfo]:
        return await self.devices.list_devices()

    async def plan_only(self, request: TaskRequest) -> TaskPlan:
        return await self.planner.plan(request.instruction)

    async def create(self, request: TaskRequest, *, start: bool = True) -> TaskView:
        task_id = uuid.uuid4().hex
        task = TaskView(
            id=task_id,
            instruction=request.instruction,
            device=request.device,
            status=TaskStatus.QUEUED,
        )
        async with self._lock:
            self._tasks[task_id] = task
            self._requests[task_id] = request
            self._approved_once[task_id] = set()
            self._loop_counts[task_id] = Counter()
        if start:
            self._start_job(task_id)
        return task.model_copy(deep=True)

    def _start_job(self, task_id: str) -> None:
        current = self._jobs.get(task_id)
        if current and not current.done():
            return
        self._jobs[task_id] = asyncio.create_task(
            self._run(task_id), name=f"semantic-mobile-agent:{task_id}"
        )

    async def execute(self, request: TaskRequest, *, timeout_s: float | None = None) -> TaskView:
        task = await self.create(request)
        job = self._jobs[task.id]
        try:
            if timeout_s is None:
                await job
            else:
                await asyncio.wait_for(asyncio.shield(job), timeout=timeout_s)
        except TimeoutError:
            # The service task remains queryable and is not silently cancelled by an
            # HTTP client's shorter timeout.
            pass
        return await self.get(task.id)

    async def get(self, task_id: str) -> TaskView:
        async with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                raise TaskNotFound(task_id)
            return task.model_copy(deep=True)

    async def confirm(self, task_id: str, confirmation: ConfirmationRequest) -> TaskView:
        async with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                raise TaskNotFound(task_id)
            if task.status != TaskStatus.WAITING_CONFIRMATION or not task.pending_action:
                raise ValueError("task is not waiting for confirmation")
            pending = task.pending_action
            if confirmation.approved:
                self._approved_once.setdefault(task_id, set()).add(pending.id)
                task.pending_action = None
                task.status = TaskStatus.RUNNING
                task.events.append(
                    TaskEvent(
                        type="confirmation",
                        message=confirmation.note or "user approved pending action",
                        action=pending,
                    )
                )
            else:
                task.pending_action = None
                task.status = TaskStatus.CANCELLED
                task.result = {
                    "cancelled": True,
                    "reason": confirmation.note or "user rejected pending action",
                }
                task.events.append(
                    TaskEvent(
                        type="confirmation",
                        message=confirmation.note or "user rejected pending action",
                        action=pending,
                    )
                )
            task.updated_at_ms = self._now_ms()
        if confirmation.approved:
            self._start_job(task_id)
        return await self.get(task_id)

    async def cancel(self, task_id: str, reason: str = "cancelled") -> TaskView:
        async with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                raise TaskNotFound(task_id)
            task.status = TaskStatus.CANCELLED
            task.error = reason
            task.updated_at_ms = self._now_ms()
            job = self._jobs.get(task_id)
            if job and not job.done():
                job.cancel()
        return await self.get(task_id)

    async def _prepare(self, task: TaskView, request: TaskRequest) -> tuple[TaskView, str | None]:
        if task.plan is None:
            task.status = TaskStatus.PLANNING
            await self._event(task, "status", "planning task")

            serial: str | None = None
            if not request.dry_run:
                device = await self.devices.resolve(request.device)
                serial = device.serial
                task.device = serial
                try:
                    discovered = await self.driver.installed_apps(serial)
                    self.registry.merge_discovered(discovered)
                    await self._event(
                        task,
                        "device",
                        f"resolved device {serial}; discovered {len(discovered)} apps",
                    )
                except Exception as error:
                    LOGGER.warning("installed-app discovery failed for %s: %s", serial, error)
                    await self._event(
                        task,
                        "warning",
                        f"device resolved as {serial}; launcher app discovery unavailable: {error}",
                    )

            plan = await self.planner.plan(request.instruction)
            normalized_steps = [self.safety.normalize(step) for step in plan.steps]
            task.plan = plan.model_copy(update={"steps": normalized_steps})
            task.status = TaskStatus.RUNNING
            await self._event(
                task,
                "plan",
                f"compiled {len(normalized_steps)} actions using "
                f"{task.plan.metadata.get('source', 'llm')}",
            )

            if request.dry_run:
                task.status = TaskStatus.SUCCEEDED
                task.result = {
                    "dry_run": True,
                    "plan": task.plan.model_dump(mode="json"),
                }
                await self._event(task, "complete", "dry-run plan generated")
                return task, None
            return task, serial

        return task, task.device

    @staticmethod
    def _action_loop_key(state_hash: str, action: PrimitiveAction) -> str:
        locator = action.locator
        return "|".join(
            (
                state_hash,
                action.kind.value,
                locator.strategy if locator else "",
                locator.value if locator else "",
                action.text or "",
                action.key or "",
            )
        )

    async def _recover(
        self,
        task: TaskView,
        request: TaskRequest,
        serial: str,
        failed_action: PrimitiveAction,
        error: str,
    ) -> PrimitiveAction | None:
        if self._replans[task.id] >= self.settings.llm_max_replans:
            return None
        if not task.plan:
            return None
        try:
            snapshot = await self.driver.snapshot(serial, force=True)
        except Exception as snapshot_error:
            await self._event(
                task,
                "recovery_failed",
                f"could not inspect divergent screen: {snapshot_error}",
                action=failed_action,
            )
            return None

        self._replans[task.id] += 1
        recovered = await self.planner.llm.recover_action(
            goal=request.instruction,
            plan=task.plan,
            cursor=task.cursor,
            snapshot=snapshot,
            last_error=error,
        )
        if not recovered:
            return None
        recovered = self.safety.normalize(recovered)
        await self._event(
            task,
            "replan",
            f"screen-specific recovery action selected on state {snapshot.state_hash}",
            action=recovered,
        )
        return recovered

    async def _run(self, task_id: str) -> None:
        try:
            async with self._lock:
                task = self._tasks.get(task_id)
                request = self._requests.get(task_id)
            if not task or not request:
                return
            if task.status in {
                TaskStatus.SUCCEEDED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }:
                return

            task, serial = await self._prepare(task, request)
            if task.status == TaskStatus.SUCCEEDED or request.dry_run:
                return
            if not serial or not task.plan:
                raise RuntimeError("task has no resolved device or plan")

            allowed_risks = set(request.allow_risks)
            approved_once = self._approved_once.setdefault(task.id, set())
            executions = 0

            # A device cannot safely execute two independent tasks at the same time.
            # The lock is released while awaiting human confirmation and reacquired on resume.
            async with self.driver.device_lock(serial):
                while task.cursor < len(task.plan.steps):
                    if task.status == TaskStatus.CANCELLED:
                        return
                    if executions >= task.plan.max_steps:
                        raise RuntimeError(
                            f"execution exceeded plan.max_steps={task.plan.max_steps}"
                        )
                    executions += 1

                    planned_action = task.plan.steps[task.cursor]
                    action = self.safety.normalize(planned_action)

                    if self.safety.needs_confirmation(
                        action,
                        require_confirmation=self.settings.require_confirmation,
                        allow_unsafe=self.settings.allow_unsafe,
                        allowed_risks=allowed_risks,
                    ) and action.id not in approved_once:
                        task.status = TaskStatus.WAITING_CONFIRMATION
                        task.pending_action = action
                        await self._event(
                            task,
                            "confirmation_required",
                            action.confirmation_message or "confirmation required",
                            action=action,
                        )
                        return

                    if action.id in approved_once:
                        approved_once.remove(action.id)

                    state_hash = ""
                    package = task.plan.package or ""
                    cached_action: PrimitiveAction | None = None
                    if action.kind in {ActionKind.CLICK, ActionKind.SET_TEXT, ActionKind.KEY}:
                        try:
                            snapshot = await self.driver.snapshot(serial)
                            state_hash = snapshot.state_hash
                            package = snapshot.package or package
                            cached_action = await self.cache.get(
                                goal_key(request.instruction), package, state_hash
                            )
                        except Exception as error:
                            LOGGER.debug("pre-action state/cache lookup skipped: %s", error)

                    if cached_action:
                        cached_action = self.safety.normalize(cached_action)
                        # Never let a cached action bypass the current plan's stronger risk.
                        if list(RiskLevel).index(cached_action.risk) < list(RiskLevel).index(action.risk):
                            cached_action = cached_action.model_copy(update={"risk": action.risk})
                        action = cached_action.model_copy(
                            update={
                                "id": planned_action.id,
                                "confirmation_message": planned_action.confirmation_message
                                or cached_action.confirmation_message,
                            }
                        )
                        await self._event(
                            task,
                            "cache_hit",
                            f"reused successful action for UI state {state_hash}",
                            action=action,
                        )

                    loop_key = self._action_loop_key(state_hash, action)
                    loop_counts = self._loop_counts.setdefault(task.id, Counter())
                    loop_counts[loop_key] += 1
                    if state_hash and loop_counts[loop_key] > 2:
                        recovered = await self._recover(
                            task,
                            request,
                            serial,
                            action,
                            "same action repeated on the same UI state",
                        )
                        if not recovered:
                            raise RuntimeError("loop detected on unchanged UI state")
                        action = recovered

                    await self._event(task, "action", "dispatching action", action=action)
                    result = await self.driver.execute(serial, action)
                    await self._event(
                        task,
                        "action_result",
                        result.message,
                        action=action,
                        result=result,
                    )

                    if not result.success:
                        if state_hash:
                            await self.cache.record_failure(
                                goal_key(request.instruction), package, state_hash
                            )
                        recovered = await self._recover(
                            task,
                            request,
                            serial,
                            action,
                            result.message,
                        )
                        if recovered:
                            task.plan.steps.insert(task.cursor, recovered)
                            await self._store(task)
                            continue
                        raise RuntimeError(
                            f"action {task.cursor + 1}/{len(task.plan.steps)} failed: {result.message}"
                        )

                    if state_hash and action.kind in {
                        ActionKind.CLICK,
                        ActionKind.SET_TEXT,
                        ActionKind.KEY,
                    }:
                        await self.cache.record_success(
                            goal_key(request.instruction),
                            package,
                            state_hash,
                            action,
                            result.latency_ms,
                        )

                    task.cursor += 1
                    await self._store(task)
                    if action.kind == ActionKind.FINISH:
                        task.status = TaskStatus.SUCCEEDED
                        task.result = {
                            "message": result.message or "task complete",
                            "device": serial,
                            "actions_executed": task.cursor,
                            "replans": self._replans[task.id],
                        }
                        await self._event(task, "complete", "task completed successfully")
                        return
                    await self.driver.settle()

                task.status = TaskStatus.SUCCEEDED
                task.result = {
                    "message": "plan exhausted",
                    "device": serial,
                    "actions_executed": task.cursor,
                    "replans": self._replans[task.id],
                }
                await self._event(task, "complete", "task completed successfully")

        except asyncio.CancelledError:
            async with self._lock:
                task = self._tasks.get(task_id)
                if task and task.status != TaskStatus.CANCELLED:
                    task.status = TaskStatus.CANCELLED
                    task.error = "task coroutine cancelled"
                    task.updated_at_ms = self._now_ms()
            raise
        except Exception as error:
            LOGGER.exception("task %s failed", task_id)
            async with self._lock:
                task = self._tasks.get(task_id)
                if task:
                    task.status = TaskStatus.FAILED
                    task.error = str(error)
                    task.events.append(TaskEvent(type="error", message=str(error)))
                    task.updated_at_ms = self._now_ms()
        finally:
            current = asyncio.current_task()
            if self._jobs.get(task_id) is current:
                self._jobs.pop(task_id, None)

    async def close(self) -> None:
        jobs = [job for job in self._jobs.values() if not job.done()]
        for job in jobs:
            job.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
        await self.driver.close()
