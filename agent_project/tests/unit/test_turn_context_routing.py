from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage


@pytest.fixture()
def isolated_db(monkeypatch: pytest.MonkeyPatch):
    import storage

    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(storage, "DB_PATH", Path(tmp) / "agent.db")
        storage.init_db()
        yield storage


def _route(*, level: str, playbook_id: str | None = None) -> dict:
    return {
        "route_level": level,
        "playbook_id": playbook_id,
        "selected_workflow": None,
        "selected_case_type": None,
        "confidence": 0.98,
        "latency_class": "short" if level == "tool" else "instant",
        "max_tool_calls": 1 if level == "tool" else 0,
        "creates_objects": False,
        "needs_confirmation": False,
        "object_policy": "ephemeral" if level == "tool" else "none",
        "speech_act": "fresh_lookup" if level == "tool" else "context_recall",
        "context_reference": "none" if level == "tool" else "previous_user_turn",
        "data_requirement": "fresh_external" if level == "tool" else "thread",
        "reason": "test route",
    }


class _ContextAwareRouter:
    def __init__(self) -> None:
        self.snapshots: list[dict] = []

    def invoke(self, messages):
        prompt = str(messages[0].content)
        marker = "Current turn context:\n"
        payload = json.loads(
            prompt.split(marker, 1)[1].split("\n\nRetrieved memory context:", 1)[0]
        )
        self.snapshots.append(payload)
        if len(self.snapshots) == 1:
            return AIMessage(content=json.dumps(_route(level="tool", playbook_id="latest_company_news")))
        assert payload["latest_user_turn"] == "Can you repeat my previous request?"
        assert payload["previous_user_turn"] == "What are Apple's latest news?"
        return AIMessage(content=json.dumps(_route(level="direct")))


class _RecallChatModel:
    def invoke(self, history):
        latest = next(
            message.content
            for message in reversed(history)
            if isinstance(message, HumanMessage)
        )
        if latest == "Can you repeat my previous request?":
            return AIMessage(content="You asked: What are Apple's latest news?")
        return AIMessage(content="Apple news summary.")


class _SearchTool:
    name = "search_web"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def invoke(self, args):
        self.calls.append(args)
        return json.dumps({"results": [{"title": "Apple update", "url": "https://example.com/apple"}]})


def test_turn_context_snapshot_separates_latest_and_prior_turns() -> None:
    from turn_context import build_turn_context_snapshot

    snapshot = build_turn_context_snapshot({
        "messages": [
            HumanMessage(id="u1", content="What are Apple's latest news?"),
            AIMessage(id="a1", content="Apple news summary."),
            HumanMessage(id="u2", content="Can you repeat my previous request?"),
        ],
        "session_id": "session-1",
    })

    assert snapshot.latest_user_turn == "Can you repeat my previous request?"
    assert snapshot.previous_user_turn == "What are Apple's latest news?"
    assert snapshot.previous_assistant_turn == "Apple news summary."
    assert [turn.role for turn in snapshot.recent_messages] == ["user", "assistant", "user"]


def test_turn_context_snapshot_includes_active_task_before_routing() -> None:
    from turn_context import build_turn_context_snapshot

    task = {
        "task_id": "assignment:apple",
        "goal": "Prepare Apple earnings memo",
        "assigned_to": "agent:research",
        "input_object_version_ids": ["filing:aapl:v000001"],
    }
    snapshot = build_turn_context_snapshot({
        "messages": [HumanMessage(content="Prepare Apple earnings memo")],
        "session_id": "workspace:finance",
        "task_context": task,
    })

    assert snapshot.active_task == task


def test_main_graph_builds_turn_context_before_semantic_router() -> None:
    import file as main_graph

    edges = set(main_graph.graph.edges)
    assert ("__start__", "build_turn_context") in edges
    assert ("build_turn_context", "build_memory_context") in edges
    assert ("build_memory_context", "semantic_router") in edges
    assert ("apply_runtime_policy", "build_memory_context") not in edges
    assert ("__start__", "semantic_router") not in edges


def test_same_thread_news_then_context_recall_does_not_search_again(
    monkeypatch,
    isolated_db,
) -> None:
    import file as main_graph
    import graphs.conversational as conversational

    router = _ContextAwareRouter()
    chat_model = _RecallChatModel()
    search = _SearchTool()
    tool_map = dict(conversational.CHAT_TOOLS_BY_NAME)
    tool_map["search_web"] = search

    monkeypatch.setattr(main_graph, "intent_llm", router)
    monkeypatch.setattr(main_graph, "emit_ui_event", lambda _event: None)
    monkeypatch.setattr(conversational, "_chat_llm_for_policy", lambda _policy: chat_model)
    monkeypatch.setattr(conversational, "_synthesize_news_answer", lambda *_args: "Apple news summary.")
    monkeypatch.setattr(conversational, "CHAT_TOOLS_BY_NAME", tool_map)
    monkeypatch.setattr(conversational, "emit_ui_event", lambda _event: None)
    monkeypatch.setattr(conversational.agent_log, "chat_start", lambda: 0.0)
    monkeypatch.setattr(conversational.agent_log, "chat_done", lambda *_args, **_kwargs: None)

    config = {"configurable": {"thread_id": "thread-context-regression"}}
    first = main_graph.app.invoke({
        "messages": [HumanMessage(content="What are Apple's latest news?")],
        "mode": "auto",
        "session_id": "session-context-regression",
    }, config=config)
    first_search_count = len(search.calls)

    second = main_graph.app.invoke({
        "messages": [HumanMessage(content="Can you repeat my previous request?")],
        "mode": "auto",
        "session_id": "session-context-regression",
    }, config=config)

    assert first["route_decision"]["playbook_id"] == "latest_company_news"
    assert first_search_count == 1
    assert second["route_decision"]["route_level"] == "direct"
    assert second["route_decision"]["playbook_id"] is None
    assert second["turn_context"]["previous_user_turn"] == "What are Apple's latest news?"
    assert len(search.calls) == first_search_count
    assert second["messages"][-1].content == "You asked: What are Apple's latest news?"
