from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from .engine import TaskNotFound
from .models import ConfirmationRequest, RiskLevel, TaskRequest
from .runtime import get_engine

mcp = FastMCP(
    "semantic-mobile-agent",
    instructions=(
        "Use this server to control an Android cloud phone from natural-language goals. "
        "Pass the device serial supplied by the parent agent when more than one device exists. "
        "The server automatically executes low-risk navigation and pauses before payment, final ordering, "
        "final ride request, message sending, destructive, publishing and authorization actions."
    ),
)


def _risk_values(values: list[str] | None) -> list[RiskLevel]:
    if not values:
        return []
    return [RiskLevel(value) for value in values]


@mcp.tool()
async def mobile_execute(
    instruction: str,
    device: str | None = None,
    timeout_s: float = 60,
    context: dict[str, Any] | None = None,
    allow_risks: list[str] | None = None,
) -> dict[str, Any]:
    """Execute a semantic Android task and return completion or confirmation state.

    `device` accepts values such as `emulator-5554`, the common typo
    `emul-5554`, or `host:adb_port`. Do not approve high/critical risk merely to
    avoid a confirmation round trip; call `mobile_confirm` after presenting the
    exact pending action to the user.
    """

    engine = get_engine()
    request = TaskRequest(
        instruction=instruction,
        device=device,
        context=context or {},
        allow_risks=_risk_values(allow_risks),
    )
    task = await engine.execute(request, timeout_s=timeout_s)
    return task.model_dump(mode="json")


@mcp.tool()
async def mobile_plan(
    instruction: str,
    device: str | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile a task without touching a phone. Useful for preview and policy checks."""

    engine = get_engine()
    request = TaskRequest(
        instruction=instruction,
        device=device,
        context=context or {},
        dry_run=True,
    )
    plan = await engine.plan_only(request)
    return plan.model_dump(mode="json")


@mcp.tool()
async def mobile_task_status(task_id: str) -> dict[str, Any]:
    """Read the latest task state, events, cursor and pending confirmation action."""

    try:
        task = await get_engine().get(task_id)
    except TaskNotFound as error:
        return {"error": "task_not_found", "task_id": str(error)}
    return task.model_dump(mode="json")


@mcp.tool()
async def mobile_confirm(
    task_id: str,
    approved: bool,
    note: str | None = None,
) -> dict[str, Any]:
    """Approve or reject exactly the pending high-risk action, then resume or cancel."""

    try:
        task = await get_engine().confirm(
            task_id,
            ConfirmationRequest(approved=approved, note=note),
        )
    except TaskNotFound as error:
        return {"error": "task_not_found", "task_id": str(error)}
    except ValueError as error:
        return {"error": "invalid_task_state", "message": str(error)}
    return task.model_dump(mode="json")


@mcp.tool()
async def mobile_devices() -> list[dict[str, str]]:
    """List ADB-visible devices so the parent agent can choose the target phone."""

    return [
        {
            "serial": item.serial,
            "state": item.state,
            "model": item.model,
            "product": item.product,
            "transport_id": item.transport_id,
        }
        for item in await get_engine().devices_list()
    ]


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
