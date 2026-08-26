from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher
from typing import Any

from .models import Bounds, Locator, UINode, UISnapshot

_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)]\[(\d+),(\d+)]")
_SPACE_RE = re.compile(r"\s+")


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").casefold().strip()
    return _SPACE_RE.sub("", value)


def parse_bounds(value: str | list[int] | dict[str, int] | None) -> Bounds:
    if isinstance(value, dict):
        return Bounds.model_validate(value)
    if isinstance(value, list) and len(value) == 4:
        return Bounds(left=value[0], top=value[1], right=value[2], bottom=value[3])
    match = _BOUNDS_RE.fullmatch(value or "")
    if not match:
        return Bounds(left=0, top=0, right=0, bottom=0)
    left, top, right, bottom = (int(x) for x in match.groups())
    return Bounds(left=left, top=top, right=right, bottom=bottom)


def _bool(value: Any) -> bool:
    return str(value).casefold() == "true" if not isinstance(value, bool) else value


def parse_uiautomator_xml(xml: str, *, source: str = "adb") -> UISnapshot:
    root = ET.fromstring(xml)
    nodes: list[UINode] = []
    package = ""

    def walk(element: ET.Element, path: str) -> None:
        nonlocal package
        attrs = element.attrib
        node_package = attrs.get("package", "")
        package = package or node_package
        class_name = attrs.get("class", "")
        node = UINode(
            path=path,
            text=attrs.get("text", "")[:300],
            description=attrs.get("content-desc", "")[:300],
            resource_id=attrs.get("resource-id", "")[:300],
            class_name=class_name,
            package=node_package,
            clickable=_bool(attrs.get("clickable", False)),
            editable="EditText" in class_name or _bool(attrs.get("editable", False)),
            scrollable=_bool(attrs.get("scrollable", False)),
            enabled=not attrs.get("enabled") or _bool(attrs.get("enabled", True)),
            selected=_bool(attrs.get("selected", False)),
            checked=_bool(attrs.get("checked", False)),
            focused=_bool(attrs.get("focused", False)),
            bounds=parse_bounds(attrs.get("bounds")),
        )
        if node.bounds.area > 0:
            nodes.append(node)
        for index, child in enumerate(list(element)):
            walk(child, f"{path}/{index}")

    walk(root, "0")
    snapshot = UISnapshot(package=package, source=source, nodes=nodes)
    snapshot.state_hash = state_signature(snapshot)
    return snapshot


def parse_bridge_snapshot(payload: dict[str, Any]) -> UISnapshot:
    nodes = []
    for item in payload.get("nodes", []):
        class_name = str(item.get("class_name") or item.get("class") or "")
        nodes.append(
            UINode(
                path=str(item.get("path", "0")),
                text=str(item.get("text", ""))[:300],
                description=str(item.get("description") or item.get("desc") or "")[:300],
                resource_id=str(item.get("resource_id") or item.get("id") or "")[:300],
                class_name=class_name,
                package=str(item.get("package", "")),
                clickable=bool(item.get("clickable", False)),
                editable=bool(item.get("editable", False)) or "EditText" in class_name,
                scrollable=bool(item.get("scrollable", False)),
                enabled=bool(item.get("enabled", True)),
                selected=bool(item.get("selected", False)),
                checked=bool(item.get("checked", False)),
                focused=bool(item.get("focused", False)),
                bounds=parse_bounds(item.get("bounds")),
            )
        )
    snapshot = UISnapshot(
        package=str(payload.get("package", "")),
        activity=str(payload.get("activity", "")),
        width=int(payload.get("width", 0) or 0),
        height=int(payload.get("height", 0) or 0),
        source="bridge",
        nodes=nodes,
    )
    snapshot.state_hash = state_signature(snapshot)
    return snapshot


def state_signature(snapshot: UISnapshot) -> str:
    rows = [snapshot.package, snapshot.activity]
    for node in snapshot.nodes:
        if not (node.clickable or node.editable or node.scrollable or node.text or node.description):
            continue
        rows.append(
            "|".join(
                (
                    normalize_text(node.text)[:80],
                    normalize_text(node.description)[:80],
                    node.resource_id.rsplit("/", 1)[-1][:80],
                    node.class_name.rsplit(".", 1)[-1],
                    "1" if node.clickable else "0",
                    "1" if node.editable else "0",
                    str(node.bounds.left // 16),
                    str(node.bounds.top // 16),
                )
            )
        )
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()[:20]


def compact_ui(snapshot: UISnapshot, max_nodes: int = 80, max_chars: int = 8000) -> str:
    priority: list[tuple[int, UINode]] = []
    for node in snapshot.nodes:
        score = 0
        score += 8 if node.focused else 0
        score += 6 if node.editable else 0
        score += 4 if node.clickable else 0
        score += 2 if node.scrollable else 0
        score += 2 if node.text else 0
        score += 1 if node.description else 0
        if score:
            priority.append((score, node))
    priority.sort(key=lambda item: (-item[0], item[1].bounds.top, item[1].bounds.left))
    lines = [
        f"screen package={snapshot.package} activity={snapshot.activity} hash={snapshot.state_hash} "
        f"size={snapshot.width}x{snapshot.height}"
    ]
    for _, node in priority[:max_nodes]:
        flags = "".join(
            flag
            for enabled, flag in (
                (node.clickable, "C"),
                (node.editable, "E"),
                (node.scrollable, "S"),
                (node.focused, "F"),
            )
            if enabled
        )
        line = (
            f"{node.path} [{flags or '-'}] text={node.text!r} desc={node.description!r} "
            f"id={node.resource_id!r} class={node.class_name.rsplit('.', 1)[-1]} "
            f"b={node.bounds.left},{node.bounds.top},{node.bounds.right},{node.bounds.bottom}"
        )
        if sum(len(x) + 1 for x in lines) + len(line) > max_chars:
            break
        lines.append(line)
    return "\n".join(lines)


def _token_overlap(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    aset = set(a)
    bset = set(b)
    return len(aset & bset) / max(1, len(aset | bset))


def node_score(node: UINode, locator: Locator) -> float:
    value = normalize_text(locator.value)
    alternatives = [value, *(normalize_text(x) for x in locator.alternatives)]
    texts = {
        "text": normalize_text(node.text),
        "description": normalize_text(node.description),
        "resource_id": normalize_text(node.resource_id),
        "role": normalize_text(node.class_name.rsplit(".", 1)[-1]),
    }
    if locator.strategy == "focused":
        return 1.0 if node.focused else 0.0
    if locator.strategy == "path":
        return 1.0 if node.path == locator.value else 0.0
    if locator.strategy in texts:
        candidate = texts[locator.strategy]
        if not candidate:
            return 0.0
        return max(1.0 if candidate == target else 0.0 for target in alternatives)
    if locator.strategy == "text_contains":
        candidate = texts["text"] + texts["description"]
        return max(0.95 if target and target in candidate else 0.0 for target in alternatives)

    label = "".join((texts["text"], texts["description"], texts["resource_id"], texts["role"]))
    best = 0.0
    for target in alternatives:
        if not target:
            continue
        exact = 1.0 if target in label else 0.0
        sequence = SequenceMatcher(None, target, label).ratio()
        overlap = _token_overlap(target, label)
        best = max(best, exact, sequence * 0.65 + overlap * 0.35)
    best += 0.08 if node.clickable else 0
    best += 0.08 if node.editable and any(x in value for x in ("输入", "搜索", "目的", "地址")) else 0
    return min(1.0, best)


def rank_nodes(
    snapshot: UISnapshot,
    locator: Locator,
    *,
    min_score: float = 0.42,
) -> list[tuple[float, UINode]]:
    scored = [(node_score(node, locator), node) for node in snapshot.nodes if node.enabled]
    scored = [item for item in scored if item[0] >= min_score]
    scored.sort(key=lambda item: (-item[0], item[1].bounds.area, item[1].bounds.top))
    return scored


def snapshot_to_json(snapshot: UISnapshot) -> str:
    return json.dumps(snapshot.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
