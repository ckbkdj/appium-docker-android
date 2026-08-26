# Application adapters

## Why there is no hard-coded “Top 500” selector table

Application package names, labels, experiments, screen layouts, languages, regions, login states, and accessibility quality change continuously. A large static coordinate or XPath catalog would look complete but fail quickly and create unsafe clicks. This project treats the seed catalog as hints and obtains real installed applications and UI nodes from the target phone.

## Coverage layers

### 1. Launcher discovery

After the Bridge is enabled, call:

```bash
curl -X POST http://127.0.0.1:8080/v1/apps/refresh \
  -H 'Content-Type: application/json' \
  -d '{"serial":"emulator-5554","bridge_port":27183}'
```

The registry merges all discoverable Launcher activities. This allows “打开某某 App” for applications absent from `apps.yaml`.

### 2. Generic semantic controls

The selector engine ranks visible enabled nodes by:

- exact/contains text;
- accessibility description;
- resource ID;
- class type;
- clickable/editable/focused state;
- valid bounds and depth.

Selectors should use user-visible concepts and candidate labels. Coordinates are a last resort and should not be persisted across devices.

### 3. Seed profiles

`src/mobile_agent/data/apps.yaml` stores:

```yaml
- name: 美团
  aliases: [美团外卖, meituan]
  package_candidates: [com.sankuai.meituan]
  capabilities: [open, search, food, ride]
  entry_texts:
    ride: [打车, 打车/租车, 美团打车, 出行]
```

A profile should contain only stable public UI hints. Do not put credentials, private deep links, device IDs, ad identifiers, or captured user data in the catalog.

### 4. Successful-path cache

The runtime can reuse a path after repeated success for the same normalized instruction, app, and initial UI state. A failure increments the failure count and prevents blind reuse. This is workflow learning, not a license to skip confirmation gates.

## Adding a new application

1. Install the production app version on a test device.
2. Refresh Launcher discovery and confirm the real package/activity.
3. Test the generic open/search flow first.
4. Add aliases and capabilities to `apps.yaml` only when useful.
5. Add stable entry labels to `entry_texts`.
6. Create a deterministic micro-policy only for a common, well-defined intent.
7. Add fixture-based tests from normalized UI nodes; avoid storing screenshots containing personal data.
8. Run the task under logged-out, logged-in, first-run, permission-dialog, and A/B layout states.
9. Keep final order/payment/send/call/delete actions behind confirmation.

## Adapter quality levels

| Level | Meaning |
|---|---|
| Discovery | App can be found and opened from its real Launcher entry. |
| Generic | LLM/rules can operate accessible text/edit fields with semantic selectors. |
| Seeded | Package aliases, capabilities, and stable entry labels are known. |
| Deterministic | A tested micro-policy covers a specific high-frequency intent. |
| Production-certified | Version/region/account-state regression suite passes on target cloud phones. |

The repository's seed list provides discovery hints and several deterministic intent families. It is not a claim that every listed app/version is production-certified.

## Prefer official interfaces where available

For irreversible or high-volume operations, use an official app deep link, API, A2A skill, or MCP integration when the provider offers one. UI automation remains useful for visible user-driven flows and fallback, but it should not replace a stable authorized transaction interface.
