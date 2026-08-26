# Deployment

## Prerequisites

- Python 3.11 or newer.
- Android platform-tools (`adb`) available to the service account.
- A host-controlled Android device serial or TCP ADB address.
- Appium 2 with UiAutomator2 when Appium fallback is required.
- The optional Android Accessibility Bridge for low-latency semantic nodes and reliable Unicode input.
- An optional OpenAI-compatible LLM endpoint for tasks not covered by deterministic rules.

## Native installation

```bash
git clone https://github.com/ckbkdj/appium-docker-android.git
cd appium-docker-android/semantic-mobile-agent
python -m venv .venv
source .venv/bin/activate
pip install -e '.[all]'
cp .env.example .env
mobile-agent-api
```

Check the service:

```bash
curl http://127.0.0.1:8080/healthz
```

## LLM configuration

Any endpoint implementing compatible Chat Completions or Responses semantics can be used:

```dotenv
MOBILE_AGENT_LLM_BASE_URL=http://127.0.0.1:18080/v1
MOBILE_AGENT_LLM_API_KEY=replace-me
MOBILE_AGENT_LLM_MODEL=Qwen3.8-27B
MOBILE_AGENT_LLM_API_STYLE=chat_completions
```

Use temperature zero and structured JSON output. Keep the planner close to the service network-wise; slow first-pass planning does not affect warm action dispatch but does affect new-task latency.

## Appium fallback

Install and start Appium outside or alongside this service:

```bash
npm install -g appium
appium driver install uiautomator2
appium --address 127.0.0.1 --port 4723
```

Set `MOBILE_AGENT_APPIUM_URL`. Appium sessions are lazily created and pooled per host-provided device serial.

## Accessibility Bridge

Build `android-bridge/`, install the APK on the controlled phone, open the app once to configure a random token, and enable the service in Android Accessibility settings. The Bridge has no INTERNET permission and listens only on an Android abstract local socket.

The host reserves a local port per device and forwards it explicitly:

```bash
adb -s emulator-5554 forward tcp:27183 localabstract:semantic_mobile_agent
```

Do not reuse one forwarded port for multiple active devices.

## Docker Compose

```bash
mkdir -p data
docker compose up -d --build
docker compose logs -f semantic-mobile-agent
```

The example uses host networking because Appium and ADB forwarding usually run on the host. Adjust device and `.android` mounts to the deployment platform; do not expose the ADB daemon publicly.

## Remote cloud phones

The host should perform the equivalent of:

```bash
adb connect 10.0.0.8:5555
adb -s 10.0.0.8:5555 get-state
```

Only after the host verifies ownership/lease should it pass `10.0.0.8:5555` to the agent. The service does not scan the LAN or choose the first available emulator.

## API authentication

Set a long random `MOBILE_AGENT_API_TOKEN`. Every endpoint except `/healthz` then requires:

```http
Authorization: Bearer <token>
```

Terminate TLS at a trusted reverse proxy when the API crosses a host boundary. The Bridge token is separate and scoped to the phone-side local socket.

## Process and data layout

- Run one API process when using the default in-memory active task store.
- SQLite stores successful path statistics, not durable task orchestration.
- For horizontal scaling, replace `TaskStore` with Redis/PostgreSQL and add distributed device locks before running multiple API replicas.
- Persist `data/mobile-agent.db`, protect it as operational metadata, and rotate/remove it when test devices or tenants change.

## Production checklist

- Unique device lease and unique Bridge forward per active task/device.
- API token and TLS.
- Real App/version/region/account-state regression tests.
- Explicit user confirmation UI and audit record.
- Log redaction for message bodies, addresses, contacts, and tokens.
- Request rate limits, task timeouts, and operator takeover.
- No automated OTP/CAPTCHA/payment-password handling.
- Measured p50/p95 on the actual cloud-phone network rather than a development emulator.
