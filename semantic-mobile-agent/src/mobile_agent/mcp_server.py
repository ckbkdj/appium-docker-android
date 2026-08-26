from __future__ import annotations

import os
from typing import Any

import httpx

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover - optional dependency
    raise RuntimeError("Install semantic-mobile-agent[mcp] to use the MCP server") from exc


API_URL = os.getenv("MOBILE_AGENT_API_URL", "http://127.0.0.1:8080").rstrip("/")
API_TOKEN = os.getenv("MOBILE_AGENT_API_TOKEN")
mcp = FastMCP("semantic-mobile-agent")


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if API_TOKEN:
        headers["Authorization"] = f"Bearer {API_TOKEN}"
    return headers


async def _request(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.request(
            method,
            f"{API_URL}{path}",
            headers=_headers(),
            json=payload,
        )
    if response.is_error:
        detail = response.text[:2000]
        raise RuntimeError(f"Mobile Agent API {response.status_code}: {detail}")
    if response.status_code == 204:
        return None
    return response.json()


def _device(
    serial: str,
    appium_url: str | None,
    bridge_port: int | None,
    bridge_token: str | None,
) -> dict[str, Any]:
    return {
        "serial": serial,
        "appium_url": appium_url,
        "bridge_port": bridge_port,
        "bridge_token": bridge_token,
    }


@mcp.tool()
async def mobile_plan(
    instruction: str,
    serial: str,
    appium_url: str | None = None,
    bridge_port: int | None = None,
    bridge_token: str | None = None,
    context: dict[str, Any] | None = None,
    max_steps: int = 30,
) -> dict[str, Any]:
    """Compile natural language into a safe Android action plan without executing it."""
    return await _request(
        "POST",
        "/v1/plan",
        {
            "instruction": instruction,
            "device": _device(serial, appium_url, bridge_port, bridge_token),
            "context": context or {},
            "max_steps": max_steps,
            "dry_run": True,
        },
    )


@mcp.tool()
async def mobile_execute(
    instruction: str,
    serial: str,
    appium_url: str | None = None,
    bridge_port: int | None = None,
    bridge_token: str | None = None,
    context: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    dry_run: bool = False,
    max_steps: int = 30,
) -> dict[str, Any]:
    """Start an Android task. Poll mobile_task_status until it reaches a terminal or confirmation state."""
    return await _request(
        "POST",
        "/v1/tasks",
        {
            "instruction": instruction,
            "device": _device(serial, appium_url, bridge_port, bridge_token),
            "context": context or {},
            "idempotency_key": idempotency_key,
            "dry_run": dry_run,
            "max_steps": max_steps,
        },
    )


@mcp.tool()
async def mobile_task_status(task_id: str) -> dict[str, Any]:
    """Read task progress, step results, and any pending user-confirmation action."""
    return await _request("GET", f"/v1/tasks/{task_id}")


@mcp.tool()
async def mobile_confirm(
    task_id: str,
    approved: bool,
    note: str | None = None,
) -> dict[str, Any]:
    """Approve or reject exactly the pending irreversible action shown in task status."""
    return await _request(
        "POST",
        f"/v1/tasks/{task_id}/confirm",
        {"approved": approved, "note": note},
    )


@mcp.tool()
async def mobile_cancel(task_id: str) -> dict[str, Any]:
    """Cancel a queued, running, or confirmation-waiting task."""
    return await _request("POST", f"/v1/tasks/{task_id}/cancel")


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
