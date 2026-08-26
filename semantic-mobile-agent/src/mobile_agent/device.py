from __future__ import annotations

import asyncio
import json
import re
import shlex
import time
from typing import Any, Protocol

from .apps import AppRegistry
from .config import Settings
from .models import (
    Action,
    ActionKind,
    ActionResult,
    DeviceRef,
    InstalledApp,
    Selector,
    UiSnapshot,
)
from .ui import SelectorEngine, parse_appium_xml, snapshot_from_bridge


class DeviceError(RuntimeError):
    pass


class ActionExecutor(Protocol):
    async def execute(self, action: Action, device: DeviceRef) -> ActionResult: ...

    async def snapshot(self, device: DeviceRef) -> UiSnapshot: ...

    async def list_apps(self, device: DeviceRef) -> list[InstalledApp]: ...

    async def close(self) -> None: ...


class AdbClient:
    def __init__(self, serial: str, adb_path: str = "adb") -> None:
        self.serial = serial
        self.adb_path = adb_path

    async def run(self, *args: str, timeout_s: float = 12.0) -> str:
        process = await asyncio.create_subprocess_exec(
            self.adb_path,
            "-s",
            self.serial,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
        except TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise DeviceError(f"ADB command timed out: {shlex.join(args)}") from exc
        if process.returncode != 0:
            message = stderr.decode("utf-8", errors="replace").strip()
            raise DeviceError(f"ADB failed ({process.returncode}): {message}")
        return stdout.decode("utf-8", errors="replace").strip()

    async def shell(self, *args: str, timeout_s: float = 12.0) -> str:
        return await self.run("shell", *args, timeout_s=timeout_s)

    async def ping(self) -> None:
        state = await self.run("get-state", timeout_s=3.0)
        if state.strip() != "device":
            raise DeviceError(f"Device {self.serial} is not ready: {state}")

    async def forward(self, local_port: int, socket_name: str) -> None:
        await self.run(
            "forward",
            f"tcp:{local_port}",
            f"localabstract:{socket_name}",
            timeout_s=4.0,
        )

    async def launch(self, package: str, activity: str | None = None) -> None:
        if activity:
            await self.shell("am", "start", "-n", f"{package}/{activity}", timeout_s=8.0)
            return
        await self.shell(
            "monkey",
            "-p",
            package,
            "-c",
            "android.intent.category.LAUNCHER",
            "1",
            timeout_s=8.0,
        )

    async def deep_link(self, uri: str, package: str | None = None) -> None:
        args = ["am", "start", "-a", "android.intent.action.VIEW", "-d", uri]
        if package:
            args.extend(["-p", package])
        await self.shell(*args, timeout_s=8.0)

    async def tap(self, x: int, y: int) -> None:
        await self.shell("input", "tap", str(x), str(y), timeout_s=3.0)

    async def input_text(self, text: str) -> None:
        if any(ord(char) > 127 for char in text):
            raise DeviceError("ADB input text is not reliable for non-ASCII; enable Bridge or Appium")
        encoded = text.replace("%", "%25").replace(" ", "%s")
        await self.shell("input", "text", encoded, timeout_s=5.0)

    async def key(self, keycode: int) -> None:
        await self.shell("input", "keyevent", str(keycode), timeout_s=3.0)

    async def swipe(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        duration_ms: int,
    ) -> None:
        await self.shell(
            "input",
            "swipe",
            str(x1),
            str(y1),
            str(x2),
            str(y2),
            str(duration_ms),
            timeout_s=4.0,
        )

    async def current_window(self) -> tuple[str, str]:
        output = await self.shell("dumpsys", "window", "windows", timeout_s=6.0)
        for pattern in [
            r"mCurrentFocus=.*?\s([\w.]+)/([\w.$]+)",
            r"mFocusedApp=.*?\s([\w.]+)/([\w.$]+)",
        ]:
            match = re.search(pattern, output)
            if match:
                return match.group(1), match.group(2)
        return "", ""

    async def page_source(self) -> str:
        command = (
            "uiautomator dump /sdcard/semantic-agent-window.xml >/dev/null "
            "&& cat /sdcard/semantic-agent-window.xml"
        )
        return await self.shell("sh", "-c", command, timeout_s=12.0)

    async def list_apps(self) -> list[InstalledApp]:
        output = await self.shell("pm", "list", "packages", "-3", timeout_s=10.0)
        packages = [line.removeprefix("package:").strip() for line in output.splitlines()]
        return [InstalledApp(label=package, package=package, source="adb-package") for package in packages]


class BridgeClient:
    def __init__(
        self,
        adb: AdbClient,
        port: int,
        socket_name: str,
        token: str | None,
        connect_timeout_s: float,
        command_timeout_s: float,
    ) -> None:
        self.adb = adb
        self.port = port
        self.socket_name = socket_name
        self.token = token
        self.connect_timeout_s = connect_timeout_s
        self.command_timeout_s = command_timeout_s
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._next_id = 1

    async def connect(self) -> None:
        if self._writer is not None and not self._writer.is_closing():
            return
        await self.adb.forward(self.port, self.socket_name)
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", self.port),
                timeout=self.connect_timeout_s,
            )
        except (OSError, TimeoutError) as exc:
            raise DeviceError(
                "Accessibility Bridge is unavailable; install/enable it or use Appium fallback"
            ) from exc
        self._reader = reader
        self._writer = writer

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            await self._writer.wait_closed()
        self._reader = None
        self._writer = None

    async def request(self, command: str, **payload: Any) -> dict[str, Any]:
        async with self._lock:
            await self.connect()
            assert self._reader is not None
            assert self._writer is not None
            request_id = self._next_id
            self._next_id += 1
            body = {"id": request_id, "cmd": command, **payload}
            if self.token:
                body["token"] = self.token
            self._writer.write(
                (json.dumps(body, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
            )
            try:
                await asyncio.wait_for(self._writer.drain(), timeout=self.command_timeout_s)
                line = await asyncio.wait_for(
                    self._reader.readline(), timeout=self.command_timeout_s
                )
            except (OSError, TimeoutError) as exc:
                await self.close()
                raise DeviceError(f"Bridge command timed out or disconnected: {command}") from exc
            if not line:
                await self.close()
                raise DeviceError("Bridge disconnected")
            response = json.loads(line)
            if int(response.get("id", -1)) != request_id:
                raise DeviceError("Bridge response id mismatch")
            if not response.get("ok", False):
                raise DeviceError(str(response.get("error", "Bridge command failed")))
            result = response.get("result")
            return result if isinstance(result, dict) else {"value": result}

    async def snapshot(self) -> UiSnapshot:
        return snapshot_from_bridge(await self.request("snapshot"))

    async def tap(
        self,
        *,
        node_id: int | None = None,
        x: int | None = None,
        y: int | None = None,
    ) -> None:
        await self.request("tap", nodeId=node_id, x=x, y=y)

    async def input_text(self, text: str, node_id: int | None = None) -> None:
        await self.request("input", text=text, nodeId=node_id)

    async def gesture(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None:
        await self.request(
            "gesture", x1=x1, y1=y1, x2=x2, y2=y2, durationMs=duration_ms
        )

    async def global_action(self, action: str) -> None:
        await self.request("global", action=action)

    async def launch(self, package: str) -> None:
        await self.request("launch", package=package)

    async def list_apps(self) -> list[InstalledApp]:
        result = await self.request("apps")
        return [InstalledApp.model_validate(app) for app in result.get("apps", [])]


class AppiumSession:
    def __init__(self, device: DeviceRef, default_url: str | None) -> None:
        self.device = device
        self.url = device.appium_url or default_url
        self._driver: Any = None
        self._lock = asyncio.Lock()

    async def _get_driver(self) -> Any:
        if not self.url:
            raise DeviceError("Appium URL is not configured")
        async with self._lock:
            if self._driver is not None:
                return self._driver
            try:
                from appium import webdriver
                from appium.options.android import UiAutomator2Options
            except ImportError as exc:
                raise DeviceError("Install semantic-mobile-agent[appium] for Appium fallback") from exc
            capabilities: dict[str, Any] = {
                "platformName": "Android",
                "automationName": "UiAutomator2",
                "udid": self.device.serial,
                "noReset": True,
                "newCommandTimeout": 180,
            }
            if self.device.platform_version:
                capabilities["platformVersion"] = self.device.platform_version
            options = UiAutomator2Options().load_capabilities(capabilities)
            self._driver = await asyncio.to_thread(
                webdriver.Remote, command_executor=self.url, options=options
            )
            return self._driver

    async def close(self) -> None:
        if self._driver is not None:
            driver = self._driver
            self._driver = None
            await asyncio.to_thread(driver.quit)

    async def snapshot(self) -> UiSnapshot:
        driver = await self._get_driver()

        def read_state() -> tuple[str, str, str]:
            return (
                driver.page_source,
                driver.current_package or "",
                driver.current_activity or "",
            )

        xml, package, activity = await asyncio.to_thread(read_state)
        return parse_appium_xml(xml, package=package, activity=activity)

    async def activate(self, package: str) -> None:
        driver = await self._get_driver()
        await asyncio.to_thread(driver.activate_app, package)

    async def tap(self, x: int, y: int) -> None:
        driver = await self._get_driver()
        await asyncio.to_thread(
            driver.execute_script, "mobile: clickGesture", {"x": x, "y": y}
        )

    async def input_text(self, selector: Selector, text: str) -> None:
        driver = await self._get_driver()
        element = await asyncio.to_thread(self._find_element_sync, driver, selector)
        if element is None:
            element = await asyncio.to_thread(lambda: driver.switch_to.active_element)
        await asyncio.to_thread(element.clear)
        await asyncio.to_thread(element.send_keys, text)

    @staticmethod
    def _find_element_sync(driver: Any, selector: Selector) -> Any:
        try:
            from appium.webdriver.common.appiumby import AppiumBy
        except ImportError:
            return None
        locators: list[tuple[str, str]] = []
        if selector.resource_id:
            locators.append((AppiumBy.ID, selector.resource_id))
        if selector.content_desc:
            locators.append((AppiumBy.ACCESSIBILITY_ID, selector.content_desc))
        if selector.text:
            escaped = selector.text.replace('"', '\\"')
            locators.append(
                (AppiumBy.ANDROID_UIAUTOMATOR, f'new UiSelector().text("{escaped}")')
            )
        if selector.text_contains:
            escaped = selector.text_contains.replace('"', '\\"')
            locators.append(
                (
                    AppiumBy.ANDROID_UIAUTOMATOR,
                    f'new UiSelector().textContains("{escaped}")',
                )
            )
        for term in selector.candidate_texts:
            escaped = term.replace('"', '\\"')
            locators.append(
                (
                    AppiumBy.ANDROID_UIAUTOMATOR,
                    f'new UiSelector().textContains("{escaped}")',
                )
            )
        for by, value in locators:
            try:
                return driver.find_element(by, value)
            except Exception:
                continue
        return None

    async def key(self, keycode: int) -> None:
        driver = await self._get_driver()
        await asyncio.to_thread(driver.press_keycode, keycode)

    async def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None:
        del duration_ms
        driver = await self._get_driver()
        await asyncio.to_thread(
            driver.execute_script,
            "mobile: dragGesture",
            {"startX": x1, "startY": y1, "endX": x2, "endY": y2, "speed": 2500},
        )


class DryRunExecutor:
    async def execute(self, action: Action, device: DeviceRef) -> ActionResult:
        del device
        return ActionResult(
            action_id=action.id,
            ok=True,
            latency_ms=0.0,
            backend="dry-run",
            message=f"would execute: {action.description}",
        )

    async def snapshot(self, device: DeviceRef) -> UiSnapshot:
        del device
        return UiSnapshot(state_hash="dry-run")

    async def list_apps(self, device: DeviceRef) -> list[InstalledApp]:
        del device
        return []

    async def close(self) -> None:
        return None


class HybridExecutor:
    KEYCODES = {"HOME": 3, "BACK": 4, "ENTER": 66, "SEARCH": 84, "TAB": 61, "ESCAPE": 111}

    def __init__(self, registry: AppRegistry, settings: Settings) -> None:
        self.registry = registry
        self.settings = settings
        self.selector_engine = SelectorEngine()
        self._bridge: dict[str, BridgeClient] = {}
        self._appium: dict[str, AppiumSession] = {}

    def _adb(self, device: DeviceRef) -> AdbClient:
        return AdbClient(device.serial, self.settings.adb_path)

    @staticmethod
    def _derived_port(serial: str) -> int:
        return 27000 + (sum(serial.encode()) % 1000)

    def _bridge_client(self, device: DeviceRef) -> BridgeClient:
        if device.serial not in self._bridge:
            self._bridge[device.serial] = BridgeClient(
                self._adb(device),
                device.bridge_port or self._derived_port(device.serial),
                self.settings.bridge_socket_name,
                device.bridge_token,
                self.settings.bridge_connect_timeout_s,
                self.settings.bridge_command_timeout_s,
            )
        return self._bridge[device.serial]

    def _appium_session(self, device: DeviceRef) -> AppiumSession:
        if device.serial not in self._appium:
            self._appium[device.serial] = AppiumSession(device, self.settings.appium_url)
        return self._appium[device.serial]

    async def close(self) -> None:
        await asyncio.gather(
            *(client.close() for client in self._bridge.values()),
            *(session.close() for session in self._appium.values()),
            return_exceptions=True,
        )

    async def snapshot(self, device: DeviceRef) -> UiSnapshot:
        errors: list[str] = []
        try:
            return await self._bridge_client(device).snapshot()
        except Exception as exc:
            errors.append(f"bridge={exc}")
        try:
            return await self._appium_session(device).snapshot()
        except Exception as exc:
            errors.append(f"appium={exc}")
        try:
            adb = self._adb(device)
            package, activity = await adb.current_window()
            return parse_appium_xml(await adb.page_source(), package=package, activity=activity)
        except Exception as exc:
            errors.append(f"adb={exc}")
        raise DeviceError("Unable to capture UI: " + "; ".join(errors))

    async def list_apps(self, device: DeviceRef) -> list[InstalledApp]:
        try:
            apps = await self._bridge_client(device).list_apps()
        except Exception:
            apps = await self._adb(device).list_apps()
        self.registry.merge_installed(apps)
        return apps

    async def execute(self, action: Action, device: DeviceRef) -> ActionResult:
        start = time.perf_counter()
        backend = "unknown"
        details: dict[str, Any] = {}
        try:
            backend, details = await self._dispatch(action, device)
            dispatch_ms = (time.perf_counter() - start) * 1000
            if action.wait_after_ms:
                await asyncio.sleep(action.wait_after_ms / 1000)
            if action.expected_texts:
                snapshot = await self.snapshot(device)
                haystack = "\n".join(
                    f"{node.text}\n{node.content_desc}" for node in snapshot.nodes
                ).casefold()
                if not any(term.casefold() in haystack for term in action.expected_texts):
                    raise DeviceError(
                        f"Expected UI text not found: {', '.join(action.expected_texts)}"
                    )
            return ActionResult(
                action_id=action.id,
                ok=True,
                latency_ms=dispatch_ms,
                backend=backend,
                message="ok",
                details=details,
            )
        except Exception as exc:
            return ActionResult(
                action_id=action.id,
                ok=False,
                latency_ms=(time.perf_counter() - start) * 1000,
                backend=backend,
                message=str(exc),
                details=details,
            )

    async def _dispatch(self, action: Action, device: DeviceRef) -> tuple[str, dict[str, Any]]:
        if action.kind is ActionKind.WAIT:
            await asyncio.sleep(action.duration_ms / 1000)
            return "local", {}
        if action.kind is ActionKind.FINISH:
            return "local", {}
        if action.kind is ActionKind.ASSERT_UI:
            assert action.selector is not None
            snapshot = await self.snapshot(device)
            node = self.selector_engine.best(snapshot, action.selector)
            if node is None:
                raise DeviceError("UI assertion failed")
            return "local-selector", {"node_id": node.node_id, "state": snapshot.state_hash}

        if action.kind is ActionKind.LAUNCH_APP:
            package = self._resolve_package(action)
            if not package:
                raise DeviceError(f"No package resolved for app: {action.app}")
            try:
                await self._bridge_client(device).launch(package)
                return "bridge", {"package": package}
            except Exception:
                try:
                    await self._adb(device).launch(package)
                    return "adb", {"package": package}
                except Exception:
                    await self._appium_session(device).activate(package)
                    return "appium", {"package": package}

        if action.kind is ActionKind.DEEP_LINK:
            assert action.uri is not None
            await self._adb(device).deep_link(action.uri, action.package)
            return "adb", {"uri": action.uri}

        if action.kind in {ActionKind.TAP, ActionKind.TAP_TEXT}:
            assert action.selector is not None
            selector = action.selector
            if selector.is_coordinate:
                assert selector.x is not None and selector.y is not None
                return await self._tap_coordinates(device, selector.x, selector.y)
            snapshot = await self.snapshot(device)
            node = self.selector_engine.best(snapshot, selector)
            if node is None:
                raise DeviceError(f"No UI node matched selector: {selector.model_dump(exclude_none=True)}")
            try:
                await self._bridge_client(device).tap(node_id=node.node_id)
                return "bridge", {"node_id": node.node_id, "state": snapshot.state_hash}
            except Exception:
                x, y = node.center
                try:
                    await self._adb(device).tap(x, y)
                    return "adb", {"x": x, "y": y, "state": snapshot.state_hash}
                except Exception:
                    await self._appium_session(device).tap(x, y)
                    return "appium", {"x": x, "y": y, "state": snapshot.state_hash}

        if action.kind is ActionKind.INPUT_TEXT:
            assert action.selector is not None and action.text is not None
            snapshot = await self.snapshot(device)
            node = self.selector_engine.best(snapshot, action.selector)
            node_id = node.node_id if node else None
            try:
                await self._bridge_client(device).input_text(action.text, node_id=node_id)
                return "bridge", {"node_id": node_id, "state": snapshot.state_hash}
            except Exception:
                try:
                    await self._appium_session(device).input_text(action.selector, action.text)
                    return "appium", {"node_id": node_id, "state": snapshot.state_hash}
                except Exception:
                    if node:
                        await self._adb(device).tap(*node.center)
                    await self._adb(device).input_text(action.text)
                    return "adb", {"node_id": node_id, "state": snapshot.state_hash}

        if action.kind is ActionKind.KEY:
            key = (action.text or "").upper()
            if key not in self.KEYCODES:
                raise DeviceError(f"Unsupported key: {key}")
            if key in {"HOME", "BACK"}:
                try:
                    await self._bridge_client(device).global_action(key.lower())
                    return "bridge", {"key": key}
                except Exception:
                    pass
            try:
                await self._adb(device).key(self.KEYCODES[key])
                return "adb", {"key": key}
            except Exception:
                await self._appium_session(device).key(self.KEYCODES[key])
                return "appium", {"key": key}

        if action.kind is ActionKind.SWIPE:
            snapshot = await self.snapshot(device)
            width = snapshot.width or 1080
            height = snapshot.height or 1920
            x1, y1, x2, y2 = self._swipe_points(action.direction or "up", width, height)
            try:
                await self._bridge_client(device).gesture(x1, y1, x2, y2, action.duration_ms)
                return "bridge", {"points": [x1, y1, x2, y2]}
            except Exception:
                try:
                    await self._adb(device).swipe(x1, y1, x2, y2, action.duration_ms)
                    return "adb", {"points": [x1, y1, x2, y2]}
                except Exception:
                    await self._appium_session(device).swipe(
                        x1, y1, x2, y2, action.duration_ms
                    )
                    return "appium", {"points": [x1, y1, x2, y2]}

        raise DeviceError(f"Unsupported action kind: {action.kind}")

    async def _tap_coordinates(
        self, device: DeviceRef, x: int, y: int
    ) -> tuple[str, dict[str, Any]]:
        try:
            await self._bridge_client(device).tap(x=x, y=y)
            return "bridge", {"x": x, "y": y}
        except Exception:
            try:
                await self._adb(device).tap(x, y)
                return "adb", {"x": x, "y": y}
            except Exception:
                await self._appium_session(device).tap(x, y)
                return "appium", {"x": x, "y": y}

    def _resolve_package(self, action: Action) -> str | None:
        if action.package:
            return action.package
        if action.app:
            profile = self.registry.resolve(action.app)
            if profile:
                return self.registry.resolve_package(profile)
        return None

    @staticmethod
    def _swipe_points(direction: str, width: int, height: int) -> tuple[int, int, int, int]:
        cx, cy = width // 2, height // 2
        horizontal = int(width * 0.32)
        vertical = int(height * 0.32)
        points = {
            "up": (cx, cy + vertical, cx, cy - vertical),
            "down": (cx, cy - vertical, cx, cy + vertical),
            "left": (cx + horizontal, cy, cx - horizontal, cy),
            "right": (cx - horizontal, cy, cx + horizontal, cy),
        }
        if direction not in points:
            raise DeviceError(f"Unsupported swipe direction: {direction}")
        return points[direction]
