from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from typing import Any

from .models import Selector, UiNode, UiSnapshot

_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)]\[(\d+),(\d+)]")


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())


def parse_appium_xml(xml: str, *, package: str = "", activity: str = "") -> UiSnapshot:
    root = ET.fromstring(xml)
    nodes: list[UiNode] = []
    stack: list[tuple[ET.Element, int]] = [(root, 0)]
    next_id = 0
    while stack:
        element, depth = stack.pop()
        attrs = element.attrib
        match = _BOUNDS_RE.fullmatch(attrs.get("bounds", ""))
        bounds = tuple(map(int, match.groups())) if match else (0, 0, 0, 0)
        nodes.append(
            UiNode(
                node_id=next_id,
                text=_clean(attrs.get("text")),
                content_desc=_clean(attrs.get("content-desc")),
                resource_id=_clean(attrs.get("resource-id")),
                class_name=_clean(attrs.get("class")),
                package=_clean(attrs.get("package")),
                clickable=attrs.get("clickable") == "true",
                editable="EditText" in attrs.get("class", ""),
                enabled=attrs.get("enabled", "true") == "true",
                focused=attrs.get("focused") == "true",
                visible=attrs.get("displayed", "true") == "true",
                depth=depth,
                bounds=bounds,
            )
        )
        next_id += 1
        for child in reversed(list(element)):
            stack.append((child, depth + 1))
    snapshot = UiSnapshot(package=package, activity=activity, nodes=nodes)
    snapshot.state_hash = compute_state_hash(snapshot)
    return snapshot


def compute_state_hash(snapshot: UiSnapshot) -> str:
    stable: list[tuple[Any, ...]] = []
    for node in snapshot.nodes:
        if not node.visible:
            continue
        if not any((node.text, node.content_desc, node.resource_id, node.clickable, node.editable)):
            continue
        stable.append(
            (
                node.text[:80],
                node.content_desc[:80],
                node.resource_id[-100:],
                node.class_name.rsplit(".", 1)[-1],
                node.clickable,
                node.editable,
                tuple(round(value / 8) for value in node.bounds),
            )
        )
    payload = json.dumps(
        [snapshot.package, snapshot.activity, stable],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.blake2s(payload.encode("utf-8"), digest_size=12).hexdigest()


def compress_snapshot(
    snapshot: UiSnapshot,
    *,
    max_nodes: int = 180,
    max_chars: int = 9000,
) -> dict[str, Any]:
    ranked = sorted(
        (node for node in snapshot.nodes if node.visible and node.enabled),
        key=lambda node: (
            not node.focused,
            not node.editable,
            not node.clickable,
            not bool(node.text or node.content_desc),
            node.depth,
        ),
    )
    compact_nodes: list[dict[str, Any]] = []
    consumed = 0
    for node in ranked[:max_nodes]:
        item = {
            "id": node.node_id,
            "t": node.text[:120],
            "d": node.content_desc[:120],
            "r": node.resource_id[-120:],
            "c": node.class_name.rsplit(".", 1)[-1],
            "b": node.bounds,
            "click": node.clickable,
            "edit": node.editable,
            "focus": node.focused,
        }
        encoded = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        if consumed + len(encoded) > max_chars:
            break
        compact_nodes.append(item)
        consumed += len(encoded)
    return {
        "package": snapshot.package,
        "activity": snapshot.activity,
        "state": snapshot.state_hash or compute_state_hash(snapshot),
        "nodes": compact_nodes,
    }


def snapshot_from_bridge(payload: dict[str, Any]) -> UiSnapshot:
    nodes = [
        UiNode(
            node_id=int(item.get("id", index)),
            text=_clean(item.get("text")),
            content_desc=_clean(item.get("desc")),
            resource_id=_clean(item.get("resourceId")),
            class_name=_clean(item.get("className")),
            package=_clean(item.get("package")),
            clickable=bool(item.get("clickable", False)),
            editable=bool(item.get("editable", False)),
            enabled=bool(item.get("enabled", True)),
            focused=bool(item.get("focused", False)),
            visible=bool(item.get("visible", True)),
            depth=int(item.get("depth", 0)),
            bounds=tuple(item.get("bounds", [0, 0, 0, 0])),
        )
        for index, item in enumerate(payload.get("nodes", []))
    ]
    snapshot = UiSnapshot(
        package=str(payload.get("package", "")),
        activity=str(payload.get("activity", "")),
        rotation=int(payload.get("rotation", 0)),
        width=int(payload.get("width", 0)),
        height=int(payload.get("height", 0)),
        nodes=nodes,
    )
    snapshot.state_hash = compute_state_hash(snapshot)
    return snapshot


class SelectorEngine:
    def rank(self, snapshot: UiSnapshot, selector: Selector) -> list[tuple[float, UiNode]]:
        candidates: list[tuple[float, UiNode]] = []
        expected_terms = [term.casefold() for term in selector.candidate_texts if term]
        for node in snapshot.nodes:
            if not node.visible or not node.enabled:
                continue
            if selector.clickable is True and not node.clickable and not node.editable:
                continue
            if selector.editable is True and not node.editable:
                continue
            score = self._score(node, selector, expected_terms)
            if score > 0:
                candidates.append((score, node))
        candidates.sort(key=lambda item: (item[0], -item[1].depth), reverse=True)
        return candidates

    def best(self, snapshot: UiSnapshot, selector: Selector) -> UiNode | None:
        if selector.node_id is not None:
            for node in snapshot.nodes:
                if node.node_id == selector.node_id:
                    return node
        ranked = self.rank(snapshot, selector)
        return ranked[0][1] if ranked else None

    @staticmethod
    def _score(node: UiNode, selector: Selector, expected_terms: Iterable[str]) -> float:
        score = 0.0
        text = node.text.casefold()
        desc = node.content_desc.casefold()
        resource_id = node.resource_id.casefold()
        class_name = node.class_name.casefold()

        if selector.text:
            target = selector.text.casefold()
            if text == target or desc == target:
                score += 120
            elif target in text or target in desc:
                score += 72
            else:
                return 0.0
        if selector.text_contains:
            target = selector.text_contains.casefold()
            if target in text or target in desc:
                score += 82
            else:
                return 0.0
        if expected_terms:
            hits = [term for term in expected_terms if term in text or term in desc]
            if not hits:
                return 0.0
            score += 60 + max(len(term) for term in hits)
        if selector.resource_id:
            target = selector.resource_id.casefold()
            if resource_id == target or resource_id.endswith(target):
                score += 110
            else:
                return 0.0
        if selector.content_desc:
            target = selector.content_desc.casefold()
            if desc == target:
                score += 110
            elif target in desc:
                score += 65
            else:
                return 0.0
        if selector.class_name:
            target = selector.class_name.casefold()
            if class_name.endswith(target):
                score += 30
            else:
                return 0.0
        if node.focused:
            score += 8
        if node.clickable:
            score += 6
        if node.editable:
            score += 6
        left, top, right, bottom = node.bounds
        area = max(0, right - left) * max(0, bottom - top)
        if area <= 0:
            score -= 50
        elif area > 1_000_000:
            score -= 8
        return score
