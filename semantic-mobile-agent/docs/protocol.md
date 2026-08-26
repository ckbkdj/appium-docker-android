# Protocols

## REST task protocol

### Create a task

`POST /v1/tasks`

```json
{
  "instruction": "使用美团打车去首都机场",
  "device": {
    "serial": "emulator-5554",
    "bridge_port": 27183,
    "bridge_token": "device-local-token",
    "appium_url": "http://127.0.0.1:4723"
  },
  "context": {
    "destination": "北京首都国际机场 T3 航站楼"
  },
  "idempotency_key": "user-42:conversation-9:turn-18",
  "dry_run": false,
  "max_steps": 30
}
```

The API returns `202` and a `TaskRecord`. The host polls `GET /v1/tasks/{task_id}`. A task is terminal only in `succeeded`, `failed`, or `cancelled`.

### Confirmation

When status is `awaiting_confirmation`, the response contains the exact `pending_action` and `confirmation_reason`. The host must display that current action and call:

```json
{
  "approved": true,
  "note": "User confirmed the displayed final ride call"
}
```

at `POST /v1/tasks/{task_id}/confirm`. Approval is single-use and resumes only that task's current action.

### Idempotency

The same non-empty `idempotency_key` returns the original active/retained task instead of creating another task. The key must represent one user intent, not a whole conversation or a reusable business type.

## Plan schema

A plan has an objective, intent, optional target app, confidence, assumptions, a bounded list of actions, and an expected result. Only typed actions are executable.

```json
{
  "intent": "ride_hailing",
  "objective": "Prepare a ride to the airport",
  "app": "美团",
  "source": "rule",
  "confidence": 0.94,
  "steps": [
    {
      "kind": "launch_app",
      "description": "打开美团",
      "package": "com.sankuai.meituan"
    },
    {
      "kind": "tap_text",
      "description": "确认呼叫车辆",
      "selector": {"candidate_texts": ["确认呼叫", "立即叫车"]},
      "risk": "high",
      "requires_confirmation": true
    }
  ]
}
```

Allowed action kinds are `launch_app`, `deep_link`, `tap`, `tap_text`, `input_text`, `swipe`, `key`, `wait`, `assert_ui`, `ask_user`, and `finish`. The runtime currently executes every kind except `ask_user`; user-dependent cases should be represented as a confirmation or returned to the host before execution.

## Accessibility Bridge protocol

The host creates an ADB forward:

```bash
adb -s emulator-5554 forward tcp:27183 localabstract:semantic_mobile_agent
```

The client then opens `127.0.0.1:27183`. Messages are one UTF-8 JSON object per line. The connection is persistent and requests are serialized by the client.

Request:

```json
{"id":1,"cmd":"snapshot","token":"device-local-token"}
```

Success:

```json
{"id":1,"ok":true,"result":{"package":"com.demo","nodes":[]}}
```

Failure:

```json
{"id":1,"ok":false,"error":"unauthorized"}
```

Commands:

| Command | Required fields | Result |
|---|---|---|
| `ping` | none | service status |
| `snapshot` | none | normalized active-window nodes |
| `tap` | `nodeId` or `x`,`y` | performed flag |
| `input` | `text`, optional `nodeId` | performed flag |
| `gesture` | `x1`,`y1`,`x2`,`y2`,`durationMs` | performed flag |
| `global` | `action`: back/home/recents/notifications | performed flag |
| `launch` | `package` | performed flag |
| `apps` | none | Launcher app labels/packages/activities |

Node IDs are ephemeral and valid only for the most recent snapshot on that connection. A client must not save a node ID across UI changes.

## Error rules

- A model-generated unknown action is rejected by schema validation.
- A missing device serial is rejected before execution.
- Selector misses produce a failed `ActionResult`; the runtime may perform only a bounded replan.
- Credentials, OTP, CAPTCHA, identity verification, or payment-password screens require user takeover; they are not protocol inputs.
