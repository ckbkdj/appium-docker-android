# Benchmarking

## What the 200–500ms target means

The target is the time from sending an already compiled operation over an established local device session until the backend acknowledges that the operation was accepted/performed. It is **not** the total time for natural-language planning, opening an application, loading remote data, matching a driver, placing an order, or receiving a business confirmation.

The API records `ActionResult.latency_ms` before the optional post-action wait. This makes backend dispatch comparable without hiding application-render delays inside the metric.

## Bridge request benchmark

After installing/enabling the Bridge and forwarding a port:

```bash
adb -s emulator-5554 forward tcp:27183 localabstract:semantic_mobile_agent
mobile-agent-bench \
  --serial emulator-5554 \
  --port 27183 \
  --token "$BRIDGE_TOKEN" \
  --command ping \
  --warmup 20 \
  --samples 500
```

Use `--command snapshot` to measure semantic UI collection separately. The command prints minimum, mean, p50, p95, p99, maximum, failures, and whether p95 is under 500ms.

## End-to-end action benchmark

For each target cloud-phone class, collect at least these stages independently:

1. Host → API request latency.
2. New-task deterministic planning latency.
3. New-task LLM planning latency.
4. Bridge/ADB/Appium warm action dispatch.
5. UI snapshot acquisition.
6. Selector scoring.
7. App render/stability time after the action.
8. Replan latency and rate.
9. Full task completion and user-confirmation wait, reported separately.

Do not average all stages into one number; p95/p99 and failure rate matter more than a single mean.

## Recommended scenario matrix

- Local emulator and remote cloud phone.
- Bridge, ADB-only, and Appium-only paths.
- Idle and CPU-constrained phone.
- Wi-Fi/LAN and WAN ADB path.
- Chinese and English UI.
- Cold app, warm app, already-correct screen, and unexpected screen.
- First-run permission dialog, logged-out state, and normal logged-in test account.
- Different resolutions/densities and Android versions supported by the pool.

## Accuracy metrics

Speed without correctness is not useful. Track:

- task success rate;
- first-selector hit rate;
- replan rate;
- wrong-element click rate;
- confirmation-gate escape rate (must be zero);
- duplicate irreversible action rate (must be zero);
- median actions per successful task;
- LLM calls and input tokens per successful task;
- cache hit rate and cache failure after reuse.

## Acceptance guidance

A practical release gate for a certified app flow should include:

- p95 warm dispatch at or below the deployment target;
- zero confirmation bypasses in the regression suite;
- zero cross-device execution under concurrent device leases;
- bounded failure with no click loops;
- stable success across the supported app versions, regions, account states, and phone images;
- human takeover for login, OTP, CAPTCHA, identity, and payment authentication.

Store benchmark metadata with device model, Android version, app version, backend, network path, sample size, and commit SHA. A latency number without that context should not be treated as a production guarantee.
