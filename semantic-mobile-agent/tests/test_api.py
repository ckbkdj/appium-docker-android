from __future__ import annotations

import time

from fastapi.testclient import TestClient

from mobile_agent.api import create_app
from mobile_agent.config import Settings
from mobile_agent.device import DryRunExecutor


def test_api_task_pauses_at_ride_confirmation_and_can_be_rejected(tmp_path) -> None:
    settings = Settings(
        database_path=tmp_path / "api.db",
        api_token="test-token",
        require_confirmation=True,
    )
    app = create_app(settings, executor=DryRunExecutor())
    headers = {"Authorization": "Bearer test-token"}

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/v1/apps").status_code == 401
        response = client.post(
            "/v1/tasks",
            headers=headers,
            json={
                "instruction": "使用美团打车去首都机场",
                "device": {"serial": "emulator-5554"},
                "dry_run": True,
                "idempotency_key": "api-ride-test",
            },
        )
        assert response.status_code == 202
        task_id = response.json()["task_id"]

        status_payload = {}
        for _ in range(100):
            status_response = client.get(f"/v1/tasks/{task_id}", headers=headers)
            assert status_response.status_code == 200
            status_payload = status_response.json()
            if status_payload["status"] == "awaiting_confirmation":
                break
            time.sleep(0.01)

        assert status_payload["status"] == "awaiting_confirmation"
        assert status_payload["pending_action"]["requires_confirmation"] is True
        assert "呼叫" in status_payload["pending_action"]["description"]

        rejected = client.post(
            f"/v1/tasks/{task_id}/confirm",
            headers=headers,
            json={"approved": False, "note": "测试拒绝"},
        )
        assert rejected.status_code == 200

        for _ in range(100):
            final_payload = client.get(f"/v1/tasks/{task_id}", headers=headers).json()
            if final_payload["status"] == "cancelled":
                break
            time.sleep(0.01)
        assert final_payload["status"] == "cancelled"
