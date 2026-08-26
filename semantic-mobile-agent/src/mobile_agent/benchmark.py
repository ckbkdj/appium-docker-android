from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from typing import Any

from .device import AdbClient, BridgeClient


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return ordered[index]


async def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    adb = AdbClient(args.serial, args.adb_path)
    bridge = BridgeClient(
        adb=adb,
        port=args.port,
        socket_name=args.socket_name,
        token=args.token,
        connect_timeout_s=args.connect_timeout,
        command_timeout_s=args.command_timeout,
    )
    await bridge.connect()
    try:
        for _ in range(args.warmup):
            await bridge.request(args.command)
        samples: list[float] = []
        failures = 0
        for _ in range(args.samples):
            started = time.perf_counter_ns()
            try:
                await bridge.request(args.command)
            except Exception:
                failures += 1
                continue
            samples.append((time.perf_counter_ns() - started) / 1_000_000)
        if not samples:
            raise RuntimeError("Every benchmark request failed")
        return {
            "serial": args.serial,
            "command": args.command,
            "samples": len(samples),
            "failures": failures,
            "min_ms": round(min(samples), 3),
            "mean_ms": round(statistics.fmean(samples), 3),
            "p50_ms": round(percentile(samples, 0.50), 3),
            "p95_ms": round(percentile(samples, 0.95), 3),
            "p99_ms": round(percentile(samples, 0.99), 3),
            "max_ms": round(max(samples), 3),
            "target_p95_under_500ms": percentile(samples, 0.95) <= 500.0,
        }
    finally:
        await bridge.close()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Measure warm Accessibility Bridge request/response latency."
    )
    result.add_argument("--serial", required=True, help="ADB serial, e.g. emulator-5554")
    result.add_argument("--port", type=int, default=27183)
    result.add_argument("--token")
    result.add_argument("--samples", type=int, default=100)
    result.add_argument("--warmup", type=int, default=10)
    result.add_argument("--command", choices=["ping", "snapshot"], default="ping")
    result.add_argument("--adb-path", default="adb")
    result.add_argument("--socket-name", default="semantic_mobile_agent")
    result.add_argument("--connect-timeout", type=float, default=1.0)
    result.add_argument("--command-timeout", type=float, default=3.0)
    return result


def main() -> None:
    args = parser().parse_args()
    if args.samples < 1 or args.warmup < 0:
        raise SystemExit("samples must be >= 1 and warmup must be >= 0")
    print(json.dumps(asyncio.run(run_benchmark(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
