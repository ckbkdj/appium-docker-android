from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from fastapi.responses import JSONResponse

from .apps import AppRegistry
from .cache import WorkflowCache
from .config import Settings
from .device import ActionExecutor, HybridExecutor
from .models import (
    ConfirmationRequest,
    DeviceRef,
    Plan,
    PlanRequest,
    TaskRecord,
    TaskRequest,
)
from .planner import HybridPlanner, Planner
from .risk import RiskPolicy
from .runtime import TaskConflict, TaskNotFound, TaskRunner, TaskStore


@dataclass
class Runtime:
    settings: Settings
    registry: AppRegistry
    cache: WorkflowCache
    planner: Planner
    executor: ActionExecutor
    store: TaskStore
    runner: TaskRunner


def create_runtime(
    settings: Settings | None = None,
    *,
    planner: Planner | None = None,
    executor: ActionExecutor | None = None,
) -> Runtime:
    settings = settings or Settings()
    registry = AppRegistry(settings.profiles_file)
    cache = WorkflowCache(settings.database_path, settings.cache_min_successes)
    planner = planner or HybridPlanner(registry, settings)
    executor = executor or HybridExecutor(registry, settings)
    store = TaskStore(settings.task_retention)
    runner = TaskRunner(
        planner=planner,
        executor=executor,
        registry=registry,
        risk_policy=RiskPolicy(settings.require_confirmation),
        cache=cache,
        store=store,
    )
    return Runtime(
        settings=settings,
        registry=registry,
        cache=cache,
        planner=planner,
        executor=executor,
        store=store,
        runner=runner,
    )


def create_app(
    settings: Settings | None = None,
    *,
    planner: Planner | None = None,
    executor: ActionExecutor | None = None,
) -> FastAPI:
    runtime = create_runtime(settings, planner=planner, executor=executor)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await runtime.cache.initialize()
        try:
            yield
        finally:
            await runtime.runner.close()

    app = FastAPI(
        title="Semantic Mobile Agent",
        version="0.1.0",
        description="Natural language to safe, structured Android operations.",
        lifespan=lifespan,
        default_response_class=JSONResponse,
    )
    app.state.runtime = runtime

    async def authorize(authorization: str | None = Header(default=None)) -> None:
        expected = runtime.settings.api_token
        if not expected:
            return
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing bearer token",
            )
        supplied = authorization.removeprefix("Bearer ").strip()
        if not hmac.compare_digest(supplied, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid bearer token",
            )

    auth = Depends(authorize)

    @app.exception_handler(TaskNotFound)
    async def task_not_found_handler(_request: Any, exc: TaskNotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": f"Task not found: {exc.args[0]}"})

    @app.exception_handler(TaskConflict)
    async def task_conflict_handler(_request: Any, exc: TaskConflict) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "ok": True,
            "service": "semantic-mobile-agent",
            "version": "0.1.0",
            "llm_configured": bool(
                runtime.settings.llm_base_url and runtime.settings.llm_model
            ),
        }

    @app.post("/v1/plan", response_model=Plan, dependencies=[auth])
    async def create_plan(request: PlanRequest) -> Plan:
        return await runtime.runner.plan_only(request)

    @app.post(
        "/v1/tasks",
        response_model=TaskRecord,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[auth],
    )
    async def create_task(request: TaskRequest) -> TaskRecord:
        return await runtime.runner.submit(request)

    @app.get("/v1/tasks/{task_id}", response_model=TaskRecord, dependencies=[auth])
    async def get_task(task_id: str) -> TaskRecord:
        return await runtime.store.get(task_id)

    @app.post(
        "/v1/tasks/{task_id}/confirm",
        response_model=TaskRecord,
        dependencies=[auth],
    )
    async def confirm_task(task_id: str, request: ConfirmationRequest) -> TaskRecord:
        return await runtime.runner.confirm(task_id, request)

    @app.post(
        "/v1/tasks/{task_id}/cancel",
        response_model=TaskRecord,
        dependencies=[auth],
    )
    async def cancel_task(task_id: str) -> TaskRecord:
        return await runtime.runner.cancel(task_id)

    @app.get("/v1/apps", dependencies=[auth])
    async def list_apps() -> dict[str, Any]:
        profiles = runtime.registry.list_profiles()
        return {
            "count": len(profiles),
            "apps": [profile.model_dump(mode="json") for profile in profiles],
        }

    @app.post("/v1/apps/refresh", dependencies=[auth])
    async def refresh_apps(device: DeviceRef) -> dict[str, Any]:
        apps = await runtime.executor.list_apps(device)
        runtime.registry.merge_installed(apps)
        return {
            "count": len(apps),
            "apps": [item.model_dump(mode="json") for item in apps],
        }

    @app.get("/v1/cache/stats", dependencies=[auth])
    async def cache_stats() -> dict[str, Any]:
        return await runtime.cache.stats()

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    return app


app = create_app()
