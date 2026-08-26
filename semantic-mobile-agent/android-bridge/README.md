# Android Accessibility Bridge

A small privileged helper that exposes the active Android accessibility tree and a fixed set of control operations to the Semantic Mobile Agent.

## Security properties

- The manifest does **not** request `android.permission.INTERNET`.
- The accessibility service is not exported.
- The server uses `LocalServerSocket("semantic_mobile_agent")`, not a TCP listener.
- A host can reach it only after explicitly creating an ADB forward for the selected device.
- Every command requires the device-local random token configured in the launcher activity.
- It has no command for shell execution, file access, screenshots, passwords, OTPs, payment PINs, or CAPTCHA handling.

Accessibility is a powerful permission. Install this APK only on an authorized test/cloud phone, and disable/remove it when the device leaves the pool.

## Build

The project expects JDK 17, Android SDK 35, and a recent Gradle compatible with Android Gradle Plugin 8.7.3:

```bash
cd android-bridge
gradle :app:assembleDebug
```

The APK will be under `app/build/outputs/apk/debug/`.

## Install and configure

```bash
adb -s emulator-5554 install -r app/build/outputs/apk/debug/app-debug.apk
adb -s emulator-5554 shell am start \
  -n ai.semantic.mobile.bridge/.MainActivity
```

1. Keep or replace the generated random token and save it.
2. Tap **Open accessibility settings**.
3. Enable **Semantic Mobile Bridge**.
4. Return to the app and verify the running status.

The host then creates a unique forward for this device:

```bash
adb -s emulator-5554 forward \
  tcp:27183 localabstract:semantic_mobile_agent
```

Pass the same serial, local port, and token in `DeviceRef`.

## Protocol smoke test

```bash
TOKEN='replace-with-device-token'
printf '%s\n' \
  '{"id":1,"cmd":"ping","token":"'"$TOKEN"'"}' \
  | nc 127.0.0.1 27183
```

Expected shape:

```json
{"id":1,"ok":true,"result":{"service":"semantic-mobile-bridge","running":true}}
```

## Commands

- `ping`
- `snapshot`
- `tap` by current snapshot `nodeId` or coordinates
- `input` through accessibility `ACTION_SET_TEXT`
- `gesture`
- `global` for back/home/recents/notifications
- `launch` by package
- `apps` for discoverable Launcher applications

Node IDs are rebuilt on each snapshot. Always take a fresh snapshot before semantic node actions; do not persist IDs across screens.

## Operational guidance

- Use one forwarded host port per active device.
- Never expose ADB or the forwarded port beyond the trusted host namespace.
- Rotate the token when a device changes tenant or owner.
- Clear App data or uninstall the APK before returning a device to an unmanaged pool.
- The service returns accessible UI content, which can be sensitive; do not log full snapshots in production.
