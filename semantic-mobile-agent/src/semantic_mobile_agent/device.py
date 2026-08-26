from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass


class DeviceResolutionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    serial: str
    state: str = "device"
    model: str = ""
    product: str = ""
    transport_id: str = ""


class DeviceResolver:
    def __init__(self, adb_path: str = "adb", default_device: str | None = None) -> None:
        self.adb_path = adb_path
        self.default_device = default_device

    @staticmethod
    def normalize_hint(hint: str | None) -> str | None:
        if hint is None:
            return None
        value = hint.strip()
        if not value or value.casefold() in {"auto", "default", "any"}:
            return None
        if value.startswith("adb://"):
            value = value[6:]
        value = re.sub(r"^emul-(\d+)$", r"emulator-\1", value, flags=re.IGNORECASE)
        value = re.sub(r"^emulator:(\d+)$", r"emulator-\1", value, flags=re.IGNORECASE)
        return value

    async def _run(self, *args: str, timeout: float = 8) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            self.adb_path,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            process.kill()
            await process.communicate()
            raise DeviceResolutionError(f"adb command timed out: {' '.join(args)}") from None
        return process.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")

    async def list_devices(self) -> list[DeviceInfo]:
        code, output, error = await self._run("devices", "-l")
        if code:
            raise DeviceResolutionError(error.strip() or "adb devices failed")
        devices: list[DeviceInfo] = []
        for raw in output.splitlines()[1:]:
            line = raw.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            serial, state = parts[0], parts[1]
            metadata: dict[str, str] = {}
            for token in parts[2:]:
                if ":" in token:
                    key, value = token.split(":", 1)
                    metadata[key] = value
            devices.append(
                DeviceInfo(
                    serial=serial,
                    state=state,
                    model=metadata.get("model", ""),
                    product=metadata.get("product", ""),
                    transport_id=metadata.get("transport_id", ""),
                )
            )
        return devices

    async def _connect_network_device(self, serial: str) -> None:
        if not re.fullmatch(r"(?:\[[0-9a-fA-F:]+]|[^:]+):\d{2,5}", serial):
            return
        code, output, error = await self._run("connect", serial, timeout=12)
        message = f"{output}\n{error}".casefold()
        if code or ("failed" in message and "already connected" not in message):
            raise DeviceResolutionError((output + error).strip() or f"cannot connect to {serial}")

    async def resolve(self, hint: str | None = None) -> DeviceInfo:
        normalized = self.normalize_hint(hint)
        normalized = normalized or self.normalize_hint(self.default_device)
        normalized = normalized or self.normalize_hint(os.getenv("ANDROID_SERIAL"))
        if normalized:
            await self._connect_network_device(normalized)

        devices = await self.list_devices()
        ready = [device for device in devices if device.state == "device"]
        if normalized:
            for device in devices:
                if device.serial == normalized:
                    if device.state != "device":
                        raise DeviceResolutionError(
                            f"device {normalized!r} is present but state={device.state!r}"
                        )
                    return device
            raise DeviceResolutionError(
                f"device {normalized!r} not found; available={[d.serial for d in devices]}"
            )
        if not ready:
            raise DeviceResolutionError("no ready Android device found")
        if len(ready) > 1:
            raise DeviceResolutionError(
                "multiple devices found; pass device explicitly: " + ", ".join(d.serial for d in ready)
            )
        return ready[0]
