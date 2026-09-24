from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage


@pytest.fixture()
def isolated_db(monkeypatch: pytest.MonkeyPatch):
    import storage

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "agent.db"
        monkeypatch.setattr(storage, "DB_PATH", db_path)
        monkeypatch.setattr(storage, "RUNS_DIR", Path(tmp))
        storage.init_db()
        yield storage


def _route(route_level: str, **overrides) -> dict:
    payload = {
        "route_level": route_level,
        "playbook_id": None,
        "selected_workflow": None,
        "selected_case_type": None,
        "confidence": 0.9,
        "latency_class": "short",
        "max_tool_calls": 0,
        "creates_objects": False,
        "needs_confirmation": False,
        "object_policy": "none",
        "reason": "test",
    }
    payload.update(overrides)
    return payload


def _state(route_level: str, *, session_id: str = "s1", query: str = "latest Apple news", **route_overrides) -> dict:
    return {
        "messages": [HumanMessage(content=query)],
        "session_id": session_id,
        "route_decision": _route(route_level, **route_overrides),
        "selected_playbook": {"id": route_overrides.get("playbook_id") or "base", "kind": "base"},
    }


def test_direct_memory_context_uses_thread_and_session_summary_only(isolated_db) -> None:
    storage = isolated_db
    storage.set_session_memory("s1", "Prior work: Apple valuation completed last week.")

    from memory_context import build_memory_context

    context = build_memory_context(_state("direct", query="what is WACC?"))

    assert context["memory_policy"] == "thread_only"
    assert context["memory_summary"] == "Prior work: Apple valuation completed last week."
    assert context["retrieved_object_cards"] == []
    assert context["expanded_dependencies"] == []


def test_tool_memory_context_retrieves_relevant_cards_without_payload(isolated_db) -> None:
    storage = isolated_db
    storage.upsert_workspace_object({
        "object_id": "dcf_run:apple",
        "object_type": "dcf_run",
        "title": "Apple DCF run",
        "status": "complete",
        "session_id": "s1",
        "summary": "AAPL implied share price and WACC.",
        "search_text": "Apple AAPL DCF valuation WACC",
        "tags": ["apple", "aapl", "dcf"],
        "payload": {"large": "x" * 10_000},
    })
    storage.upsert_workspace_object({
        "object_id": "memo:tesla",
        "object_type": "memo",
        "title": "Tesla memo",
        "status": "complete",
        "session_id": "s1",
        "summary": "TSLA delivery update.",
        "search_text": "Tesla TSLA deliveries",
        "tags": ["tesla"],
        "payload": {"large": "y" * 10_000},
    })

    from memory_context import build_memory_context

    context = build_memory_context(_state("tool", query="latest Apple valuation context"))

    assert context["memory_policy"] == "cards"
    assert [card["object_id"] for card in context["retrieved_object_cards"]] == ["dcf_run:apple"]
    assert "payload" not in context["retrieved_object_cards"][0]


def test_workflow_memory_context_expands_dependencies_as_cards(isolated_db) -> None:
    storage = isolated_db
    storage.upsert_workspace_object({
        "object_id": "uploaded_document:aapl_10k",
        "object_type": "uploaded_document",
        "title": "Apple 10-K",
        "status": "complete",
        "session_id": "s1",
        "summary": "Apple annual filing.",
        "search_text": "Apple AAPL 10-K revenue margins",
        "payload": {"full_text": "x" * 20_000},
    })
    storage.upsert_workspace_object({
        "object_id": "dcf_run:apple",
        "object_type": "dcf_run",
        "title": "Apple DCF run",
        "status": "complete",
        "session_id": "s1",
        "summary": "DCF sourced from Apple 10-K.",
        "search_text": "Apple AAPL DCF valuation",
        "source_object_ids": ["uploaded_document:aapl_10k"],
        "kg_node_ids": ["AAPL::fundamentals::revenue"],
        "artifact_paths": ["/runs/thread_1/dcf-report.pdf"],
        "source_refs": [{"source_id": "doc:aapl_10k", "source_type": "document", "title": "Apple 10-K"}],
        "payload": {"valuation": {"implied_share_price": 210}},
    })

    from memory_context import build_memory_context

    context = build_memory_context(_state(
        "workflow",
        query="build Apple DCF",
        playbook_id="dcf_fcff",
        selected_workflow="dcf",
    ))

    assert context["memory_policy"] == "expanded"
    assert [card["object_id"] for card in context["retrieved_object_cards"]] == ["dcf_run:apple"]
    assert [card["object_id"] for card in context["expanded_dependencies"]] == ["uploaded_document:aapl_10k"]
    assert context["kg_node_ids"] == ["AAPL::fundamentals::revenue"]
    assert context["artifact_paths"] == ["/runs/thread_1/dcf-report.pdf"]
    assert context["source_refs"][0]["source_id"] == "doc:aapl_10k"
    assert "payload" not in context["expanded_dependencies"][0]


def test_build_memory_context_node_returns_state_and_emits_event(monkeypatch, isolated_db) -> None:
    storage = isolated_db
    storage.upsert_workspace_object({
        "object_id": "dcf_run:apple",
        "object_type": "dcf_run",
        "title": "Apple DCF run",
        "status": "complete",
        "session_id": "s1",
        "summary": "AAPL implied share price.",
        "search_text": "Apple AAPL DCF valuation",
        "payload": {"valuation": {"implied_share_price": 210}},
    })

    import file as main_graph

    events: list[dict] = []
    monkeypatch.setattr(main_graph, "emit_ui_event", events.append)

    result = main_graph.build_memory_context_node(_state("tool", query="Apple DCF context"))

    assert result["memory_context"]["retrieved_object_cards"][0]["object_id"] == "dcf_run:apple"
    assert events[-1]["stage"] == "build_memory_context"
    assert events[-1]["object_ids"] == ["dcf_run:apple"]


def test_memory_context_prompt_is_compact_and_excludes_payload() -> None:
    from memory_context import format_memory_context_prompt

    prompt = format_memory_context_prompt({
        "memory_policy": "cards",
        "memory_summary": "Prior Apple work.",
        "retrieved_object_cards": [{
            "object_id": "dcf_run:apple",
            "title": "Apple DCF",
            "payload": {"should_not": "appear"},
        }],
    })

    assert "## Retrieved memory context" in prompt
    assert "dcf_run:apple" in prompt
    assert "Prior Apple work." in prompt
    assert "should_not" not in prompt


class _CaptureLLM:
    def __init__(self):
        self.history = []

    def invoke(self, history):
        self.history = history
        return AIMessage(content="Memory-aware answer.")


def test_chat_prompt_receives_memory_context(monkeypatch) -> None:
    import graphs.conversational as conversational

    fake_llm = _CaptureLLM()
    monkeypatch.setattr(conversational, "_chat_llm_for_policy", lambda _policy: fake_llm)
    monkeypatch.setattr(conversational, "emit_ui_event", lambda _event: None)
    monkeypatch.setattr(conversational.agent_log, "chat_start", lambda: 0.0)
    monkeypatch.setattr(conversational.agent_log, "chat_done", lambda *_args, **_kwargs: None)

    conversational._chat_node_inner({
        "messages": [HumanMessage(content="use prior Apple DCF")],
        "session_id": "s1",
        "memory_context": {
            "memory_policy": "cards",
            "retrieved_object_cards": [{"object_id": "dcf_run:apple", "title": "Apple DCF"}],
        },
    })

    system_prompt = fake_llm.history[0].content
    assert "## Retrieved memory context" in system_prompt
    assert "dcf_run:apple" in system_prompt


def test_research_step_message_receives_memory_context() -> None:
    from graphs.research import build_step_message

    message = build_step_message(
        objective="Research Apple",
        step={"id": "step_1", "description": "Check prior valuation", "depends_on": []},
        review_feedback=None,
        plan_trajectory="- step_1",
        previous_step="none",
        next_step="none",
        context_stack_formatted="",
        memory_context={
            "memory_policy": "cards",
            "retrieved_object_cards": [{"object_id": "dcf_run:apple", "title": "Apple DCF"}],
        },
    )

    assert "## Retrieved memory context" in message
    assert "dcf_run:apple" in message


def test_workflow_context_builder_carries_memory_context(monkeypatch) -> None:
    import file as main_graph

    monkeypatch.setattr(main_graph, "_ready_docs", lambda _session_id: [])
    monkeypatch.setattr(main_graph, "_workspace_objects", lambda _session_id: [])

    context = main_graph._build_workflow_context({
        "messages": [HumanMessage(content="build Apple DCF")],
        "session_id": "s1",
        "memory_context": {
            "memory_policy": "expanded",
            "retrieved_object_cards": [{"object_id": "dcf_run:apple", "title": "Apple DCF"}],
        },
    }, "dcf")

    assert context["memory_context"]["memory_policy"] == "expanded"
    assert context["memory_context"]["retrieved_object_cards"][0]["object_id"] == "dcf_run:apple"
