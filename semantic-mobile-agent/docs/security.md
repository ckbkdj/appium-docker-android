# Security model

## Trust boundaries

The system has four distinct trust zones:

1. The user-facing host/Longxia session.
2. The semantic mobile-agent API and planner.
3. A device-specific ADB/Appium connection.
4. The phone-side Accessibility Bridge.

A device serial, Bridge port, and task must remain bound to the same authenticated user/device lease for their entire lifetime. The mobile agent deliberately does not discover a replacement device when the supplied device is unavailable.

## Main risks and controls

### Wrong-device execution

**Risk:** a shared host has several emulators and an action lands on another user's phone.

**Controls:** host-resolved serial is mandatory; clients and pools are keyed by serial; the host must hold a device lease and allocate unique ADB forwarding ports; no “first device” fallback exists.

### Prompt-generated unsafe commands

**Risk:** an LLM emits arbitrary shell or unvalidated Appium code.

**Controls:** the planner can return only the typed `Plan`/`Action` schema. The executor implements a fixed action enum and never evaluates code or passes model text to a shell command interpreter.

### Irreversible actions

**Risk:** a false selector sends a message, places an order, pays, calls a car, or deletes data.

**Controls:** risk policy, model prompt rules, explicit `requires_confirmation`, exact pending-action display, single-use approval, idempotency keys, bounded retries, and no automatic confirmation by the mobile agent.

### Credentials and sensitive challenges

The system must not request, read, store, infer, or transmit passwords, payment PINs, SMS/email one-time codes, CAPTCHA answers, identity-document images, or biometric secrets. Login, CAPTCHA, real-name verification, and payment-authentication screens require visible user takeover.

### Accessibility privilege

Android Accessibility can observe and act on the active UI. Treat the Bridge APK as a privileged component:

- It declares no INTERNET permission.
- It listens on `LocalServerSocket`, not a TCP listener.
- External access requires an explicit ADB forward selected by the host.
- Requests can require a device-local token.
- Disable the service and remove ADB authorization when a device leaves the pool.
- Do not install it on an unrelated personal device without the owner's informed consent.

### API exposure

Set `MOBILE_AGENT_API_TOKEN`, use TLS at the network boundary, restrict the listen interface/firewall, and rate-limit task creation. Do not expose Appium or ADB directly to the public Internet. MCP stdio should run under the same local user or a restricted service account.

### Data retention

UI snapshots may contain names, addresses, chat previews, purchase details, or account balances. This implementation keeps current snapshots in memory and stores only plan/cache metadata in SQLite, but application logs and upstream LLM requests can still leak content if configured carelessly.

Production deployments should:

- avoid logging full snapshots, message bodies, tokens, and addresses;
- use a private or contractually suitable planner endpoint;
- set retention limits and tenant-separated databases where required;
- encrypt disks/backups;
- avoid screenshots unless a task truly requires visual analysis;
- provide a user-visible activity/audit record.

## Selector safety

Prefer exact resource IDs or visible semantic labels over coordinates. Before an irreversible action, the host should show app, description, recipient/destination/item/amount where available. A confirmation only approves the current pending action; changing a critical parameter requires a new plan/task.

## Unsupported behavior

The project is not intended to:

- bypass anti-bot, CAPTCHA, device integrity, login, or payment controls;
- create fake engagement, spam, harassment, account farming, or unauthorized transactions;
- operate devices/accounts without authorization;
- conceal automation from an application or platform;
- perform high-volume transactions through UI automation where an authorized API is required.

## Incident response

On unexpected behavior: cancel the task, revoke the device lease, remove the ADB forward, stop Appium/Bridge access, preserve redacted action results, and invalidate any API/Bridge token that might have been exposed. Failed workflows should remain excluded from cache reuse until reviewed and revalidated.
