from __future__ import annotations

from mobile_agent.models import Selector, UiNode, UiSnapshot
from mobile_agent.ui import SelectorEngine, compress_snapshot, compute_state_hash, parse_appium_xml


def test_appium_xml_is_normalized_and_semantically_selected() -> None:
    xml = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
    <hierarchy rotation="0">
      <node index="0" text="" resource-id="com.demo:id/search" class="android.widget.EditText"
            package="com.demo" content-desc="搜索地点" clickable="true" enabled="true"
            focused="false" bounds="[20,100][1060,220]" displayed="true" />
      <node index="1" text="打车" resource-id="com.demo:id/ride" class="android.widget.TextView"
            package="com.demo" content-desc="" clickable="true" enabled="true"
            focused="false" bounds="[20,300][300,430]" displayed="true" />
    </hierarchy>"""
    snapshot = parse_appium_xml(xml, package="com.demo", activity=".MainActivity")
    selected = SelectorEngine().best(
        snapshot,
        Selector(candidate_texts=["打车", "出行"], clickable=True),
    )

    assert selected is not None
    assert selected.text == "打车"
    compact = compress_snapshot(snapshot, max_nodes=10, max_chars=2000)
    assert compact["package"] == "com.demo"
    assert compact["state"] == snapshot.state_hash
    assert any(node["t"] == "打车" for node in compact["nodes"])


def test_state_hash_is_stable_but_changes_with_meaningful_ui() -> None:
    first = UiSnapshot(
        package="com.demo",
        nodes=[
            UiNode(
                node_id=1,
                text="确认呼叫",
                clickable=True,
                bounds=(10, 10, 200, 100),
            )
        ],
    )
    same = first.model_copy(deep=True)
    changed = first.model_copy(deep=True)
    changed.nodes[0].text = "等待司机接单"

    assert compute_state_hash(first) == compute_state_hash(same)
    assert compute_state_hash(first) != compute_state_hash(changed)
