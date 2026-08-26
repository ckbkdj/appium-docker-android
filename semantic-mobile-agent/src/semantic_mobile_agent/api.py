from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import Field

from . import __version__
from .engine import TaskEngine, TaskNotFound
from .models import ConfirmationRequest, TaskRequest, TaskView
from .runtime import get_engine, get_settings

LOGGER = logging.getLogger(__name__)


class ExecuteRequest(TaskRequest):
    timeout_s: float = Field(default=60, ge=0.1, le=3600)


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    engine = get_engine()
    try:
        yield
    finally:
        await engine.close()


app = FastAPI(
    title="Semantic Mobile Agent",
    version=__version__,
    description=(
        "Low-latency semantic Android control with deterministic micro-policies, "
        "optional LLM recovery, Appium/ADB/Accessibility execution and confirmation gates."
    ),
    lifespan=lifespan,
)

Engine = Annotated[TaskEngine, Depends(get_engine)]


@app.get("/healthz")
async def healthz() -> dict[str, object]:
    settings = get_settings()
    return {
        "ok": True,
        "version": __version__,
        "drivers": {
            "bridge": settings.bridge_enabled,
            "appium": settings.appium_enabled,
            "adb": True,
        },
        "llm_configured": bool(settings.llm_model),
        "confirmation_required": settings.require_confirmation,
    }


@app.get("/v1/devices")
async def devices(engine: Engine) -> list[dict[str, str]]:
    try:
        return [
            {
                "serial": item.serial,
                "state": item.state,
                "model": item.model,
                "product": item.product,
                "transport_id": item.transport_id,
            }
            for item in await engine.devices_list()
        ]
    except Exception as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.post("/v1/plan")
async def plan(request: TaskRequest, engine: Engine) -> dict[str, object]:
    try:
        compiled = await engine.plan_only(request)
        return compiled.model_dump(mode="json")
    except Exception as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/v1/tasks", response_model=TaskView, status_code=202)
async def create_task(request: TaskRequest, engine: Engine) -> TaskView:
    return await engine.create(request)


@app.post("/v1/execute", response_model=TaskView)
async def execute(request: ExecuteRequest, engine: Engine) -> TaskView:
    task_request = TaskRequest.model_validate(request.model_dump(exclude={"timeout_s"}))
    return await engine.execute(task_request, timeout_s=request.timeout_s)


@app.get("/v1/tasks/{task_id}", response_model=TaskView)
async def task_status(task_id: str, engine: Engine) -> TaskView:
    try:
        return await engine.get(task_id)
    except TaskNotFound as error:
        raise HTTPException(status_code=404, detail="task not found") from error


@app.post("/v1/tasks/{task_id}/confirm", response_model=TaskView)
async def confirm_task(
    task_id: str,
    confirmation: ConfirmationRequest,
    engine: Engine,
) -> TaskView:
    try:
        return await engine.confirm(task_id, confirmation)
    except TaskNotFound as error:
        raise HTTPException(status_code=404, detail="task not found") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.delete("/v1/tasks/{task_id}", response_model=TaskView)
async def cancel_task(
    task_id: str,
    engine: Engine,
    reason: str = Query(default="cancelled by caller", max_length=300),
) -> TaskView:
    try:
        return await engine.cancel(task_id, reason)
    except TaskNotFound as error:
        raise HTTPException(status_code=404, detail="task not found") from error


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "semantic_mobile_agent.api:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.casefold(),
    )


if __name__ == "__main__":
    main()
