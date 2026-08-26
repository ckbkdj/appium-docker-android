from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import shlex
import time
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .models import (
    ActionKind,
    ActionResult,
    Locator,
    PrimitiveAction,
    UISnapshot,
)
from .ui import parse_bridge_snapshot, parse_uiautomator_xml, rank_nodes

LOGGER = logging.getLogger(__name__)


class DriverError(RuntimeError):
    pass


class ElementNotFound(DriverError):
    pass


def _elapsed_ms(start: float) -> int:
    return max(0, int((time.perf_counter() - start) * 1000))


class ADBClient:
    def __init__(self, adb_path: str = "adb") -> None:
        self.adb_path = adb_path

    async def run(
        self,
        serial: str | None,
        *args: str,
        timeout: float = 12,
        check: bool = True,
    ) -> tuple[int, bytes, bytes]:
        command = [self.adb_path]
        if serial:
            command.extend(["-s", serial])
        command.extend(args)
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            process.kill()
            await process.communicate()
            raise DriverError(f"adb timed out: {shlex.join(command)}") from None
        code = process.returncode or 0
        if check and code:
            raise DriverError(
                f"adb failed ({code}): {shlex.join(command)}: "
                f"{stderr.decode(errors='replace').strip()}"
            )
        return code, stdout, stderr

    async def shell(self, serial: str, *args: str, timeout: float = 12) -> str:
        _, stdout, _ = await self.run(serial, "shell", *args, timeout=timeout)
        return stdout.decode(errors="replace").strip()

    async def forward(self, serial: str, local_port: int, abstract_socket: str) -> None:
        await self.run(
            serial,
            "forward",
            f"tcp:{local_port}",
            f"localabstract:{abstract_socket}",
            timeout=8,
        )

    async def remove_forward(self, serial: str, local_port: int) -> None:
        await self.run(
            serial,
            "forward",
            "--remove",
            f"tcp:{local_port}",
            timeout=5,
            check=False,
        )

    async def launch(self, serial: str, package: str) -> None:
        # `monkey` resolves the launchable activity without maintaining a brittle
        # activity-name catalog and works on both emulators and remote ADB devices.
        output = await self.shell(
            serial,
            "monkey",
            "-p",
            package,
            "-c",
            "android.intent.category.LAUNCHER",
            "1",
            timeout=15,
        )
        if "No activities found" in output or "monkey aborted" in output.casefold():
            raise DriverError(f"no launchable activity for package {package}")

    async def tap(self, serial: str, x: int, y: int) -> None:
        await self.shell(serial, "input", "tap", str(x), str(y), timeout=5)

    async def swipe(
        self,
        serial: str,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        duration_ms: int,
    ) -> None:
        await self.shell(
            serial,
            "input",
            "swipe",
            str(x1),
            str(y1),
            str(x2),
            str(y2),
            str(duration_ms),
            timeout=6,
        )

    async def key(self, serial: str, key: str) -> None:
        mapping = {
            "BACK": "4",
            "HOME": "3",
            "ENTER": "66",
            "SEARCH": "84",
            "RECENTS": "187",
            "DEL": "67",
            "DELETE": "67",
            "TAB": "61",
        }
        value = mapping.get(key.upper(), key)
        await self.shell(serial, "input", "keyevent", value, timeout=5)

    async def input_text(self, serial: str, text: str) -> None:
        if any(ord(character) > 127 for character in text):
            raise DriverError(
                "ADB input text is not reliable for non-ASCII text; install/enable the "
                "Accessibility bridge or Appium for Chinese input"
            )
        escaped = text.replace("%", "%25").replace(" ", "%s")
        escaped = escaped.replace("&", "\\&").replace("<", "\\<").replace(">", "\\>")
        await self.shell(serial, "input", "text", escaped, timeout=8)

    async def snapshot(self, serial: str) -> UISnapshot:
        # `uiautomator dump` is the slow fallback. Normal operation should use the
        # bridge's in-memory accessibility tree or a persistent Appium session.
        remote = "/sdcard/semantic-mobile-agent-window.xml"
        await self.shell(serial, "uiautomator", "dump", remote, timeout=10)
        xml = await self.shell(serial, "cat", remote, timeout=8)
        if "<hierarchy" not in xml:
            raise DriverError("uiautomator did not return a hierarchy")
        snapshot = parse_uiautomator_xml(xml, source="adb")
        with contextlib.suppress(Exception):
            activity = await self.shell(
                serial,
                "dumpsys",
                "activity",
                "activities",
                timeout=5,
            )
            match = re.search(r"(?:topResumedActivity|mResumedActivity).*? ([\w.]+)/([\w.$]+)", activity)
            if match:
                snapshot.package = match.group(1)
                snapshot.activity = match.group(2)
        return snapshot

    async def installed_apps(self, serial: str) -> list[dict[str, str]]:
        output = await self.shell(serial, "pm", "list", "packages", "-3", timeout=15)
        apps = []
        for line in output.splitlines():
            if line.startswith("package:"):
                package = line.removeprefix("package:").strip()
                if package:
                    apps.append({"package": package, "label": package})
        return apps


@dataclass(slots=True)
class _BridgeConnection:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    port: int
    lock: asyncio.Lock
    sequence: int = 0


class BridgeClient:
    """Persistent JSON-lines client for the optional on-device Accessibility bridge."""

    def __init__(self, settings: Settings, adb: ADBClient) -> None:
        self.settings = settings
        self.adb = adb
        self._connections: dict[str, _BridgeConnection] = {}
        self._connect_locks: dict[str, asyncio.Lock] = {}

    def _port_for(self, serial: str) -> int:
        offset = int(hashlib.sha256(serial.encode("utf-8")).hexdigest()[:4], 16) % 900
        return self.settings.bridge_port_base + offset

    async def _discard(self, serial: str) -> None:
        connection = self._connections.pop(serial, None)
        if connection:
            connection.writer.close()
            with contextlib.suppress(Exception):
                await connection.writer.wait_closed()
            with contextlib.suppress(Exception):
                await self.adb.remove_forward(serial, connection.port)

    async def _connect(self, serial: str) -> _BridgeConnection:
        current = self._connections.get(serial)
        if current and not current.writer.is_closing():
            return current
        lock = self._connect_locks.setdefault(serial, asyncio.Lock())
        async with lock:
            current = self._connections.get(serial)
            if current and not current.writer.is_closing():
                return current
            await self._discard(serial)
            port = self._port_for(serial)
            await self.adb.forward(serial, port, self.settings.bridge_socket)
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", port), timeout=1.5
                )
            except Exception:
                await self.adb.remove_forward(serial, port)
                raise
            connection = _BridgeConnection(
                reader=reader,
                writer=writer,
                port=port,
                lock=asyncio.Lock(),
            )
            self._connections[serial] = connection
            return connection

    async def request(
        self,
        serial: str,
        command: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float = 4,
        retry: bool = True,
    ) -> dict[str, Any]:
        try:
            connection = await self._connect(serial)
            async with connection.lock:
                connection.sequence += 1
                request_id = f"{serial}:{connection.sequence}"
                body = {"id": request_id, "command": command, **(payload or {})}
                if self.settings.bridge_token:
                    body["token"] = self.settings.bridge_token
                connection.writer.write(
                    (json.dumps(body, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
                        "utf-8"
                    )
                )
                await connection.writer.drain()
                line = await asyncio.wait_for(connection.reader.readline(), timeout=timeout)
                if not line:
                    raise DriverError("accessibility bridge closed the connection")
                response = json.loads(line.decode("utf-8"))
                if response.get("id") not in {None, request_id}:
                    raise DriverError("accessibility bridge response id mismatch")
                if not response.get("ok", False):
                    raise DriverError(str(response.get("error") or "bridge command failed"))
                return response
        except Exception:
            await self._discard(serial)
            if retry:
                return await self.request(
                    serial,
                    command,
                    payload,
                    timeout=timeout,
                    retry=False,
                )
            raise

    async def available(self, serial: str) -> bool:
        if not self.settings.bridge_enabled:
            return False
        try:
            await self.request(serial, "ping", timeout=1, retry=False)
            return True
        except Exception:
            return False

    async def snapshot(self, serial: str) -> UISnapshot:
        response = await self.request(serial, "snapshot", timeout=4)
        return parse_bridge_snapshot(response)

    async def click(self, serial: str, path: str) -> None:
        await self.request(serial, "click", {"path": path}, timeout=3)

    async def set_text(self, serial: str, path: str, text: str) -> None:
        await self.request(serial, "set_text", {"path": path, "text": text}, timeout=4)

    async def tap(self, serial: str, x: int, y: int) -> None:
        await self.request(serial, "tap", {"x": x, "y": y}, timeout=3)

    async def swipe(
        self,
        serial: str,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        duration_ms: int,
    ) -> None:
        await self.request(
            serial,
            "swipe",
            {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "duration_ms": duration_ms},
            timeout=4,
        )

    async def global_action(self, serial: str, action: str) -> None:
        await self.request(serial, "global", {"action": action}, timeout=3)

    async def open_app(self, serial: str, package: str) -> None:
        await self.request(serial, "open_app", {"package": package}, timeout=5)

    async def installed_apps(self, serial: str) -> list[dict[str, str]]:
        response = await self.request(serial, "installed_apps", timeout=8)
        return [item for item in response.get("apps", []) if isinstance(item, dict)]

    async def close(self) -> None:
        for serial in list(self._connections):
            await self._discard(serial)


class AppiumClient:
    """Lazy persistent UiAutomator2 sessions, one per Android device."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._drivers: dict[str, Any] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def _get_driver(self, serial: str) -> Any:
        driver = self._drivers.get(serial)
        if driver:
            return driver
        lock = self._locks.setdefault(serial, asyncio.Lock())
        async with lock:
            driver = self._drivers.get(serial)
            if driver:
                return driver

            def create() -> Any:
                from appium import webdriver
                from appium.options.android import UiAutomator2Options

                options = UiAutomator2Options()
                options.platform_name = "Android"
                options.automation_name = "UiAutomator2"
                options.udid = serial
                options.no_reset = True
                options.new_command_timeout = 300
                options.set_capability("appium:skipDeviceInitialization", True)
                options.set_capability("appium:disableWindowAnimation", True)
                return webdriver.Remote(self.settings.appium_url, options=options)

            driver = await asyncio.to_thread(create)
            self._drivers[serial] = driver
            return driver

    async def available(self, serial: str) -> bool:
        if not self.settings.appium_enabled:
            return False
        try:
            driver = await self._get_driver(serial)
            await asyncio.to_thread(lambda: driver.current_package)
            return True
        except Exception:
            await self.discard(serial)
            return False

    async def snapshot(self, serial: str) -> UISnapshot:
        driver = await self._get_driver(serial)

        def read() -> tuple[str, str, str, tuple[int, int]]:
            source = driver.page_source
            package = driver.current_package or ""
            activity = driver.current_activity or ""
            size = driver.get_window_size()
            return source, package, activity, (int(size["width"]), int(size["height"]))

        source, package, activity, size = await asyncio.to_thread(read)
        snapshot = parse_uiautomator_xml(source, source="appium")
        snapshot.package = package or snapshot.package
        snapshot.activity = activity
        snapshot.width, snapshot.height = size
        return snapshot

    async def set_focused_text(self, serial: str, text: str) -> None:
        driver = await self._get_driver(serial)

        def type_text() -> None:
            element = driver.switch_to.active_element
            with contextlib.suppress(Exception):
                element.clear()
            element.send_keys(text)

        await asyncio.to_thread(type_text)

    async def discard(self, serial: str) -> None:
        driver = self._drivers.pop(serial, None)
        if driver:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(driver.quit)

    async def close(self) -> None:
        for serial in list(self._drivers):
            await self.discard(serial)


class HybridDriver:
    """Accessibility bridge first, persistent Appium second, raw ADB last."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.adb = ADBClient(settings.adb_path)
        self.bridge = BridgeClient(settings, self.adb)
        self.appium = AppiumClient(settings)
        self._ui_cache: dict[str, tuple[float, UISnapshot]] = {}
        self._device_locks: dict[str, asyncio.Lock] = {}

    def device_lock(self, serial: str) -> asyncio.Lock:
        return self._device_locks.setdefault(serial, asyncio.Lock())

    def invalidate_snapshot(self, serial: str) -> None:
        self._ui_cache.pop(serial, None)

    async def snapshot(self, serial: str, *, force: bool = False) -> UISnapshot:
        now = time.monotonic()
        cached = self._ui_cache.get(serial)
        if not force and cached and (now - cached[0]) * 1000 <= self.settings.ui_cache_ttl_ms:
            return cached[1]

        snapshot: UISnapshot | None = None
        if self.settings.bridge_enabled:
            try:
                snapshot = await self.bridge.snapshot(serial)
            except Exception as error:
                LOGGER.debug("bridge snapshot unavailable for %s: %s", serial, error)
        if snapshot is None and self.settings.appium_enabled:
            try:
                snapshot = await self.appium.snapshot(serial)
            except Exception as error:
                LOGGER.debug("Appium snapshot unavailable for %s: %s", serial, error)
        if snapshot is None:
            snapshot = await self.adb.snapshot(serial)
        self._ui_cache[serial] = (time.monotonic(), snapshot)
        return snapshot

    async def installed_apps(self, serial: str) -> list[dict[str, str]]:
        if self.settings.bridge_enabled:
            try:
                return await self.bridge.installed_apps(serial)
            except Exception as error:
                LOGGER.debug("bridge app discovery unavailable for %s: %s", serial, error)
        return await self.adb.installed_apps(serial)

    @staticmethod
    def _resolve(snapshot: UISnapshot, locator: Locator | None):
        if locator is None:
            raise ElementNotFound("action has no locator")
        matches = rank_nodes(snapshot, locator)
        if not matches:
            raise ElementNotFound(
                f"no enabled node matched {locator.strategy}={locator.value!r} "
                f"on state={snapshot.state_hash}"
            )
        index = locator.index
        if index < 0 or index >= len(matches):
            raise ElementNotFound(
                f"locator index {index} outside {len(matches)} matched nodes for {locator.value!r}"
            )
        return matches[index][1]

    async def _bridge_or_adb_tap(self, serial: str, x: int, y: int) -> str:
        if self.settings.bridge_enabled:
            try:
                await self.bridge.tap(serial, x, y)
                return "bridge"
            except Exception as error:
                LOGGER.debug("bridge tap fallback for %s: %s", serial, error)
        await self.adb.tap(serial, x, y)
        return "adb"

    async def execute(self, serial: str, action: PrimitiveAction) -> ActionResult:
        start = time.perf_counter()
        before_state = ""
        driver_name = "local"
        message = ""
        try:
            if action.kind == ActionKind.OPEN_APP:
                package = action.package
                if not package:
                    raise DriverError("open_app action has no resolved package")
                if self.settings.bridge_enabled:
                    try:
                        await self.bridge.open_app(serial, package)
                        driver_name = "bridge"
                    except Exception as error:
                        LOGGER.debug("bridge open_app fallback for %s: %s", serial, error)
                        await self.adb.launch(serial, package)
                        driver_name = "adb"
                else:
                    await self.adb.launch(serial, package)
                    driver_name = "adb"
                self.invalidate_snapshot(serial)
                message = f"opened {package}"

            elif action.kind == ActionKind.CLICK:
                snapshot = await self.snapshot(serial)
                before_state = snapshot.state_hash
                node = self._resolve(snapshot, action.locator)
                if self.settings.bridge_enabled and snapshot.source == "bridge":
                    try:
                        await self.bridge.click(serial, node.path)
                        driver_name = "bridge"
                    except Exception as error:
                        LOGGER.debug("bridge node click fallback for %s: %s", serial, error)
                        x, y = node.bounds.center
                        driver_name = await self._bridge_or_adb_tap(serial, x, y)
                else:
                    x, y = node.bounds.center
                    driver_name = await self._bridge_or_adb_tap(serial, x, y)
                self.invalidate_snapshot(serial)
                message = f"clicked {node.label or node.path}"

            elif action.kind == ActionKind.TAP:
                assert action.x is not None and action.y is not None
                driver_name = await self._bridge_or_adb_tap(serial, action.x, action.y)
                self.invalidate_snapshot(serial)
                message = f"tapped {action.x},{action.y}"

            elif action.kind == ActionKind.SET_TEXT:
                snapshot = await self.snapshot(serial)
                before_state = snapshot.state_hash
                node = self._resolve(snapshot, action.locator)
                text = action.text or ""
                if self.settings.bridge_enabled and snapshot.source == "bridge":
                    try:
                        await self.bridge.set_text(serial, node.path, text)
                        driver_name = "bridge"
                    except Exception as error:
                        LOGGER.debug("bridge set_text fallback for %s: %s", serial, error)
                        x, y = node.bounds.center
                        await self._bridge_or_adb_tap(serial, x, y)
                        if self.settings.appium_enabled:
                            await self.appium.set_focused_text(serial, text)
                            driver_name = "appium"
                        else:
                            await self.adb.input_text(serial, text)
                            driver_name = "adb"
                else:
                    x, y = node.bounds.center
                    await self._bridge_or_adb_tap(serial, x, y)
                    if self.settings.appium_enabled:
                        try:
                            await self.appium.set_focused_text(serial, text)
                            driver_name = "appium"
                        except Exception:
                            await self.adb.input_text(serial, text)
                            driver_name = "adb"
                    else:
                        await self.adb.input_text(serial, text)
                        driver_name = "adb"
                self.invalidate_snapshot(serial)
                message = "text entered"

            elif action.kind == ActionKind.SWIPE:
                assert None not in (action.x1, action.y1, action.x2, action.y2)
                if self.settings.bridge_enabled:
                    try:
                        await self.bridge.swipe(
                            serial,
                            int(action.x1),
                            int(action.y1),
                            int(action.x2),
                            int(action.y2),
                            action.duration_ms,
                        )
                        driver_name = "bridge"
                    except Exception:
                        await self.adb.swipe(
                            serial,
                            int(action.x1),
                            int(action.y1),
                            int(action.x2),
                            int(action.y2),
                            action.duration_ms,
                        )
                        driver_name = "adb"
                else:
                    await self.adb.swipe(
                        serial,
                        int(action.x1),
                        int(action.y1),
                        int(action.x2),
                        int(action.y2),
                        action.duration_ms,
                    )
                    driver_name = "adb"
                self.invalidate_snapshot(serial)
                message = "swipe dispatched"

            elif action.kind == ActionKind.KEY:
                key = (action.key or "").upper()
                if self.settings.bridge_enabled and key in {"BACK", "HOME", "RECENTS"}:
                    try:
                        await self.bridge.global_action(serial, key)
                        driver_name = "bridge"
                    except Exception:
                        await self.adb.key(serial, key)
                        driver_name = "adb"
                else:
                    await self.adb.key(serial, key)
                    driver_name = "adb"
                self.invalidate_snapshot(serial)
                message = f"key {key} dispatched"

            elif action.kind == ActionKind.WAIT:
                await asyncio.sleep(max(0, action.duration_ms) / 1000)
                driver_name = "local"
                message = "wait complete"

            elif action.kind == ActionKind.ASSERT:
                snapshot = await self.snapshot(serial, force=True)
                before_state = snapshot.state_hash
                if action.locator:
                    self._resolve(snapshot, action.locator)
                elif action.expected:
                    text = " ".join(node.label for node in snapshot.nodes)
                    if action.expected.casefold() not in text.casefold():
                        raise DriverError(f"assertion not satisfied: {action.expected}")
                driver_name = snapshot.source
                message = "assertion satisfied"

            elif action.kind == ActionKind.FINISH:
                driver_name = "local"
                message = action.expected or "task complete"

            else:
                raise DriverError(f"unsupported action kind: {action.kind}")

            latency = _elapsed_ms(start)
            return ActionResult(
                action_id=action.id,
                success=True,
                driver=driver_name,
                latency_ms=latency,
                message=message,
                before_state=before_state,
                after_state="",
            )
        except Exception as error:
            return ActionResult(
                action_id=action.id,
                success=False,
                driver=driver_name,
                latency_ms=_elapsed_ms(start),
                message=str(error),
                before_state=before_state,
                after_state="",
                metadata={"error_type": type(error).__name__},
            )

    async def settle(self) -> None:
        if self.settings.action_settle_ms:
            await asyncio.sleep(self.settings.action_settle_ms / 1000)

    async def close(self) -> None:
        await self.bridge.close()
        await self.appium.close()
