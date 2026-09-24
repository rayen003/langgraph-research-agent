from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage


@pytest.fixture()
def isolated_db(monkeypatch: pytest.MonkeyPatch):
    import storage

    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(storage, "DB_PATH", Path(tmp) / "agent.db")
        storage.init_db()
        yield storage


class _Response:
    def __init__(self, content: str):
        self.content = content


class _Router:
    def __init__(self, payloads: list[str]):
        self.payloads = iter(payloads)

    def invoke(self, _messages):
        return _Response(next(self.payloads))


def test_current_route_replaces_per_turn_while_history_appends(monkeypatch, isolated_db):
    import file as main_graph

    monkeypatch.setattr(main_graph, "intent_llm", _Router([
        '{"route_level":"direct","playbook_id":null,"selected_workflow":null,"selected_case_type":null,"confidence":0.9,"latency_class":"instant","max_tool_calls":0,"creates_objects":false,"needs_confirmation":false,"object_policy":"none","reason":"simple answer"}',
        '{"route_level":"workflow","playbook_id":"dcf_fcff","selected_workflow":"dcf","selected_case_type":null,"confidence":0.95,"latency_class":"long","max_tool_calls":0,"creates_objects":true,"needs_confirmation":true,"object_policy":"artifact","reason":"full valuation"}',
    ]))
    state = {
        "thread_id": "thread-1",
        "session_id": "session-1",
        "mode": "auto",
        "messages": [HumanMessage(id="message-1", content="What is WACC?")],
    }
    first = main_graph.semantic_router_node(state)
    first_turn_id = first["current_turn"]["turn_id"]
    state.update(first)
    state["messages"] = [*state["messages"], HumanMessage(id="message-2", content="Build a full DCF")]
    second = main_graph.semantic_router_node(state)

    assert first["current_turn"]["route"]["route_level"] == "direct"
    assert second["current_turn"]["route"]["route_level"] == "workflow"
    assert second["current_turn"]["turn_id"] != first_turn_id
    assert "route_history" not in second

    records = isolated_db.list_route_records(thread_id="thread-1")
    assert [record["turn_id"] for record in records] == [first_turn_id, second["current_turn"]["turn_id"]]
    assert [record["decision"]["route_level"] for record in records] == ["direct", "workflow"]


def test_same_turn_route_record_is_idempotent(isolated_db):
    payload = {"route_level": "direct", "confidence": 0.8, "reason": "answer"}
    isolated_db.record_route_decision(
        route_id="retry-generated-a-different-id",
        turn_id="turn-1",
        thread_id="thread-1",
        session_id="session-1",
        user_id=None,
        decision=payload,
        model="router",
    )
    isolated_db.record_route_decision(
        route_id="route:thread-1:turn-1",
        turn_id="turn-1",
        thread_id="thread-1",
        session_id="session-1",
        user_id=None,
        decision={**payload, "confidence": 0.9},
        model="router",
    )

    records = isolated_db.list_route_records(thread_id="thread-1")
    assert len(records) == 1
    assert records[0]["decision"]["confidence"] == 0.9
