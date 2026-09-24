#!/usr/bin/env -S uv run python
"""Thread E2E for memory -> workflow routing.

Default test is deterministic. Opt-in live test calls the real LLM:

    uv run python -m pytest agent_project/tests/e2e/test_thread_memory_workflow_e2e.py -q -s --agent-trace
    uv run python -m pytest agent_project/tests/e2e/test_thread_memory_workflow_e2e.py -q -s --agent-trace --run-live-agent-e2e
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = PROJECT_ROOT / ".env"


class _ThreadAwareRouterLLM:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def invoke(self, messages):
        prompt = messages[0].content
        self.prompts.append(prompt)
        latest_human = prompt.rsplit("human:", 1)[-1]
        if "latest news for said company" in latest_human:
            assert "Apple Inc." in prompt
            payload = {
                "route_level": "tool",
                "playbook_id": "latest_company_news",
                "selected_workflow": None,
                "selected_case_type": None,
                "confidence": 0.93,
                "latency_class": "short",
                "max_tool_calls": 2,
                "creates_objects": False,
                "needs_confirmation": False,
                "object_policy": "ephemeral",
                "reason": "current-news follow-up resolves company from thread memory",
            }
        elif "Create a DCF for said company" in latest_human:
            assert "Apple Inc." in prompt
            payload = {
                "route_level": "workflow",
                "playbook_id": "dcf_fcff",
                "selected_workflow": "dcf",
                "selected_case_type": None,
                "confidence": 0.95,
                "latency_class": "long",
                "max_tool_calls": 0,
                "creates_objects": True,
                "needs_confirmation": True,
                "object_policy": "artifact",
                "reason": "follow-up valuation request resolves company from thread memory",
            }
        elif "Build a deck from that DCF context" in latest_human:
            assert "Apple Inc." in prompt
            payload = {
                "route_level": "workflow",
                "playbook_id": "deck",
                "selected_workflow": "deck",
                "selected_case_type": None,
                "confidence": 0.94,
                "latency_class": "long",
                "max_tool_calls": 0,
                "creates_objects": True,
                "needs_confirmation": True,
                "object_policy": "artifact",
                "reason": "deck request should use deck workflow and prior DCF object",
            }
        else:
            payload = {
                "route_level": "direct",
                "playbook_id": None,
                "selected_workflow": None,
                "selected_case_type": None,
                "confidence": 0.9,
                "latency_class": "instant",
                "max_tool_calls": 0,
                "creates_objects": False,
                "needs_confirmation": False,
                "object_policy": "none",
                "reason": "company overview answer",
            }
        return AIMessage(content=json.dumps(payload))


class _ScriptedAgentLLM:
    def __init__(self) -> None:
        self.history_lengths: list[int] = []
        self.tool_call_names: list[str] = []

    def invoke(self, history):
        self.history_lengths.append(len(history))
        tool_messages = [message for message in history if getattr(message, "type", "") == "tool"]
        tool_names = {getattr(message, "name", "") for message in tool_messages}
        if "retrieve_tool_result" in tool_names:
            return AIMessage(content=(
                "Apple news check: recent coverage points to Services resilience, iPhone cycle scrutiny, "
                "and AI-capex discipline. Valuation implication: keep revenue growth and margin assumptions "
                "explicit rather than baking news flow into terminal value."
            ))
        if "search_web" in tool_names:
            self.tool_call_names.append("retrieve_tool_result")
            return AIMessage(
                content="",
                tool_calls=[{
                    "id": "call_retrieve_aapl_news",
                    "name": "retrieve_tool_result",
                    "args": {"tool_result_id": "tool_result:aapl_news"},
                }],
            )

        latest_user = ""
        for message in reversed(history):
            if isinstance(message, HumanMessage):
                latest_user = str(message.content or "")
                break

        if "latest news for said company" in latest_user:
            if "search_web" not in tool_names:
                self.tool_call_names.append("search_web")
                return AIMessage(
                    content="",
                    tool_calls=[{
                        "id": "call_search_aapl_news",
                        "name": "search_web",
                        "args": {"query": "latest Apple Inc AAPL company news"},
                    }],
                )

        return AIMessage(content=(
            "Apple Inc. (AAPL) is a listed consumer technology company. "
            "Core revenue comes from iPhone, Mac, iPad, wearables, and Services. "
            "For valuation work, key drivers are revenue growth, services mix, gross margin, "
            "buybacks, tax rate, reinvestment needs, and terminal growth."
        ))


class _FakeTool:
    def __init__(self, name: str, result: dict[str, Any]) -> None:
        self.name = name
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def invoke(self, args: dict[str, Any]) -> str:
        self.calls.append(args)
        return json.dumps(self.result, ensure_ascii=False)


def _ai_text(state: dict[str, Any]) -> str:
    return str(state["messages"][-1].content)


def _interrupt_payload(state: dict[str, Any]) -> dict[str, Any]:
    return state["__interrupt__"][0].value


def _workflow_interrupt_summary(state: dict[str, Any]) -> dict[str, Any]:
    payload = _interrupt_payload(state)
    context = payload.get("context", {})
    memory_context = context.get("memory_context", {})
    return {
        "workflow_id": payload.get("workflow_id"),
        "company_name": context.get("company_name"),
        "selected_artifact_ids": context.get("selected_artifact_ids") or [],
        "memory_object_ids": [
            card.get("object_id")
            for card in memory_context.get("retrieved_object_cards", [])
        ],
    }


def _trace_payload(
    *,
    session_id: str,
    thread_id: str,
    first: dict[str, Any],
    second: dict[str, Any],
    events: list[dict[str, Any]],
    router_prompts: list[str] | None = None,
    first_user: str = "Tell me about Apple Inc.",
    second_user: str = "Create a DCF for said company",
) -> dict[str, Any]:
    interrupt_payload = _interrupt_payload(second)
    route_events = [event for event in events if event.get("type") == "route_decision"]
    prompt_memory_check = None
    if router_prompts is not None:
        prompt_memory_check = len(router_prompts) >= 2 and "Apple Inc." in router_prompts[-1]
    return {
        "session_id": session_id,
        "thread_id": thread_id,
        "turns": [
            {
                "user": first_user,
                "assistant": _ai_text(first),
            },
            {
                "user": second_user,
                "workflow_interrupt": {
                    "workflow_id": interrupt_payload.get("workflow_id"),
                    "company_name": interrupt_payload.get("context", {}).get("company_name"),
                    "object_ids": [
                        card.get("object_id")
                        for card in (
                            interrupt_payload.get("context", {})
                            .get("memory_context", {})
                            .get("retrieved_object_cards", [])
                        )
                    ],
                },
            },
        ],
        "route_decisions": route_events,
        "router_prompt_contains_thread_memory": prompt_memory_check,
    }


def _event_slice(events: list[dict[str, Any]], start: int) -> list[dict[str, Any]]:
    return [dict(event) for event in events[start:]]


def _write_object(storage, events: list[dict[str, Any]], payload: dict[str, Any]) -> dict[str, Any]:
    storage.upsert_workspace_object(payload)
    event = {
        "type": "object_write",
        "object_id": payload.get("object_id"),
        "object_type": payload.get("object_type"),
        "title": payload.get("title"),
        "source_object_ids": payload.get("source_object_ids") or [],
    }
    events.append(event)
    return event


@pytest.fixture()
def isolated_db(monkeypatch: pytest.MonkeyPatch):
    import storage

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "agent.db"
        monkeypatch.setattr(storage, "DB_PATH", db_path)
        monkeypatch.setattr(storage, "RUNS_DIR", Path(tmp))
        storage.init_db()
        yield storage


@pytest.mark.skip(reason="Replaced by live LLM thread E2E; kept as a trace fixture during migration.")
@pytest.mark.e2e
def test_thread_followup_company_info_then_dcf_routes_with_memory(
    request: pytest.FixtureRequest,
    monkeypatch,
    isolated_db,
    agent_thread_trace,
):
    import file as main_graph
    import graphs.conversational as conversational
    import utils
    from lg_compat import Command
    from server import _sync_dcf_workspace_object
    from tools import resume_dcf_workflow_after_hitl

    session_id = f"session-{uuid4().hex[:8]}"
    thread_id = f"thread-{uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": thread_id}}
    events: list[dict] = []
    router = _ThreadAwareRouterLLM()
    chat_llm = _ScriptedAgentLLM()
    search_web = _FakeTool("search_web", {
        "tool_result_id": "tool_result:aapl_news",
        "summary": "Found Apple news around Services, iPhone demand, and AI capex discipline.",
    })
    retrieve_tool_result = _FakeTool("retrieve_tool_result", {
        "summary": "Apple news digest: Services resilience, iPhone cycle scrutiny, AI capex discipline.",
        "sources": [
            {"name": "Reuters", "date": "2026-09-01"},
            {"name": "Apple Investor Relations", "date": "2026-08-28"},
        ],
    })
    tool_map = dict(conversational.CHAT_TOOLS_BY_NAME)
    tool_map["search_web"] = search_web
    tool_map["retrieve_tool_result"] = retrieve_tool_result

    utils.set_ui_event_handler(lambda event: events.append(dict(event)))
    request.addfinalizer(lambda: utils.set_ui_event_handler(None))
    monkeypatch.setattr(main_graph, "intent_llm", router)
    monkeypatch.setattr(main_graph, "emit_ui_event", lambda event: events.append(dict(event)))
    monkeypatch.setattr(conversational, "llm", chat_llm)
    monkeypatch.setattr(conversational, "_chat_llm_for_policy", lambda _policy: chat_llm)
    monkeypatch.setattr(conversational, "CHAT_TOOLS_BY_NAME", tool_map)
    monkeypatch.setattr(conversational, "emit_ui_event", lambda event: events.append(dict(event)))
    monkeypatch.setattr(conversational.agent_log, "chat_start", lambda: 0.0)
    monkeypatch.setattr(conversational.agent_log, "chat_done", lambda *_args, **_kwargs: None)

    turns: list[dict[str, Any]] = []

    start = len(events)
    first_input = "Tell me about Apple Inc."
    first = main_graph.app.invoke(
        {
            "messages": [HumanMessage(content=first_input)],
            "mode": "auto",
            "session_id": session_id,
        },
        config=config,
    )
    assert "Apple Inc. (AAPL)" in _ai_text(first)
    assert "Services" in _ai_text(first)

    company_object = _write_object(isolated_db, events, {
        "object_id": "company_brief:aapl",
        "object_type": "company_brief",
        "title": "Apple Inc. company brief",
        "status": "complete",
        "session_id": session_id,
        "thread_id": thread_id,
        "summary": _ai_text(first),
        "search_text": "Apple Inc AAPL listed consumer technology company DCF valuation",
        "tags": ["apple", "aapl", "company_brief"],
        "entity_refs": [{"kind": "company", "name": "Apple Inc", "ticker": "AAPL"}],
        "payload": {"body": _ai_text(first)},
    })
    turns.append({
        "input": first_input,
        "output": _ai_text(first),
        "events": _event_slice(events, start),
        "objects_written": [company_object],
    })

    start = len(events)
    news_input = "Get latest news for said company"
    news = main_graph.app.invoke(
        {
            "messages": [HumanMessage(content=news_input)],
            "mode": "auto",
            "session_id": session_id,
        },
        config=config,
    )
    assert "Apple news check" in _ai_text(news)
    assert search_web.calls == [{"query": "latest Apple Inc AAPL company news"}]
    assert retrieve_tool_result.calls == [{"tool_result_id": "tool_result:aapl_news"}]
    news_object = _write_object(isolated_db, events, {
        "object_id": "news_digest:aapl:latest",
        "object_type": "news_digest",
        "title": "Apple Inc. latest news digest",
        "status": "complete",
        "session_id": session_id,
        "thread_id": thread_id,
        "summary": _ai_text(news),
        "search_text": f"Apple Inc AAPL latest news valuation DCF deck {_ai_text(news)}",
        "tags": ["apple", "aapl", "news"],
        "entity_refs": [{"kind": "company", "name": "Apple Inc", "ticker": "AAPL"}],
        "source_object_ids": ["company_brief:aapl"],
        "source_refs": [{"source_id": "tool_result:aapl_news", "kind": "web_search"}],
        "payload": {"body": _ai_text(news)},
    })
    turns.append({
        "input": news_input,
        "output": _ai_text(news),
        "events": _event_slice(events, start),
        "objects_written": [news_object],
    })

    start = len(events)
    dcf_input = "Create a DCF for said company"
    second = main_graph.app.invoke(
        {
            "messages": [HumanMessage(content=dcf_input)],
            "mode": "auto",
            "session_id": session_id,
        },
        config=config,
    )

    assert "__interrupt__" in second
    interrupt_payload = second["__interrupt__"][0].value
    assert interrupt_payload["workflow_id"] == "dcf"
    assert interrupt_payload["context"]["company_name"].rstrip(".") == "Apple Inc"
    assert news_object["object_id"] in {
        card["object_id"]
        for card in interrupt_payload["context"]["memory_context"]["retrieved_object_cards"]
    }
    dcf_object = _write_object(isolated_db, events, {
        "object_id": "dcf_run:aapl:base",
        "object_type": "dcf_run",
        "title": "Apple Inc. DCF setup",
        "status": "draft",
        "session_id": session_id,
        "thread_id": thread_id,
        "summary": "DCF workflow setup reached for Apple Inc. using company brief and news context.",
        "search_text": "Apple Inc AAPL DCF valuation deck assumptions revenue margin WACC terminal growth",
        "tags": ["apple", "aapl", "dcf"],
        "entity_refs": [{"kind": "company", "name": "Apple Inc", "ticker": "AAPL"}],
        "source_object_ids": ["company_brief:aapl", "news_digest:aapl:latest"],
        "payload": {"workflow_interrupt": _workflow_interrupt_summary(second)},
    })
    turns.append({
        "input": dcf_input,
        "workflow_interrupt": _workflow_interrupt_summary(second),
        "events": _event_slice(events, start),
        "objects_written": [dcf_object],
    })

    start = len(events)
    deck_input = "Build a deck from that DCF context"
    deck = main_graph.app.invoke(
        {
            "messages": [HumanMessage(content=deck_input)],
            "mode": "auto",
            "session_id": session_id,
        },
        config=config,
    )
    assert "__interrupt__" in deck
    deck_payload = _interrupt_payload(deck)
    assert deck_payload["workflow_id"] == "deck"
    assert "dcf_run:aapl:base" in deck_payload["context"]["selected_artifact_ids"]
    turns.append({
        "input": deck_input,
        "workflow_interrupt": _workflow_interrupt_summary(deck),
        "events": _event_slice(events, start),
        "objects_written": [],
    })

    execution_steps = [event for event in events if event.get("type") == "execution_step"]
    assert [event["stage"] for event in execution_steps if event["stage"] in {
        "semantic_router",
        "build_memory_context",
        "route_lane",
        "work_controller",
    }][-4:] == [
        "semantic_router",
        "build_memory_context",
        "route_lane",
        "work_controller",
    ]
    assert any(event.get("next_node") == "work_controller" for event in execution_steps)
    assert any(event.get("object_ids") == ["company_brief:aapl"] for event in execution_steps)
    assert any(event.get("type") == "activity" and event.get("name") == "search_web" for event in events)
    assert any(event.get("type") == "activity" and event.get("name") == "retrieve_tool_result" for event in events)
    assert len(router.prompts) == 4
    assert chat_llm.history_lengths
    assert chat_llm.tool_call_names == ["search_web", "retrieve_tool_result"]

    agent_thread_trace("Thread E2E: routing + memory + tools + objects + workflows", turns)


def _real_openai_key() -> str:
    load_dotenv(ENV_PATH, override=True)
    env_key = os.getenv("OPENAI_API_KEY", "")
    if env_key and env_key != "sk-test-placeholder":
        return env_key
    return ""


def _runtime_trace_summary(events: list[dict[str, Any]]) -> str:
    completed = [
        event for event in events
        if event.get("type") == "trace_span" and event.get("status") in {"completed", "error"}
    ]
    completed.sort(key=lambda event: float(event.get("duration_ms") or 0), reverse=True)
    lines = ["Runtime trace (slowest first):"]
    for event in completed[:20]:
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        model = f" model={metadata.get('model')}" if metadata.get("model") else ""
        lines.append(
            f"  {str(event.get('category') or '?'):9} "
            f"{str(event.get('name') or '?'):32} "
            f"{float(event.get('duration_ms') or 0):9.1f}ms{model}"
        )
    return "\n".join(lines)


@pytest.mark.e2e
@pytest.mark.llm
def test_live_simple_current_news_returns_in_bounded_time(
    request: pytest.FixtureRequest,
    pytestconfig: pytest.Config,
    isolated_db,
):
    """Production smoke test: simple news query must not become a long workflow."""
    if not (pytestconfig.getoption("--run-live-agent-e2e") or os.getenv("RUN_LIVE_AGENT_E2E")):
        pytest.skip("Pass --run-live-agent-e2e or set RUN_LIVE_AGENT_E2E=1 to call live LLMs.")
    if not _real_openai_key():
        pytest.skip("Set real OPENAI_API_KEY to run live agent E2E.")
    if not os.getenv("EXA_API_KEY"):
        pytest.skip("Set EXA_API_KEY to run live current-news E2E.")

    import file as main_graph
    import tracing
    import utils

    events: list[dict[str, Any]] = []

    def capture_event(event: dict[str, Any]) -> None:
        events.append({**dict(event), "_observed_monotonic": time.monotonic()})

    utils.set_ui_event_handler(capture_event)
    request.addfinalizer(lambda: utils.set_ui_event_handler(None))

    thread_id = f"live-news-{uuid4().hex[:8]}"
    utils.set_thread_id(thread_id)
    trace_id = f"trace-{uuid4().hex[:12]}"
    root_span_id = f"run-{uuid4().hex[:12]}"
    tracing.set_trace_context(trace_id, root_span_id)
    started_wall = time.time()
    started = time.monotonic()
    result = main_graph.app.invoke(
        {
            "messages": [HumanMessage(content="Whar are apples latest news?")],
            "trace_id": trace_id,
            "root_span_id": root_span_id,
            "mode": "auto",
            "session_id": f"session-{thread_id}",
        },
        config={
            "configurable": {"thread_id": thread_id},
            "callbacks": [tracing.TraceCallbackHandler(trace_id, root_span_id)],
        },
    )
    elapsed = time.monotonic() - started
    answer = _ai_text(result)
    trace_summary = _runtime_trace_summary(events)
    print(
        f"\nRoute:\n{json.dumps(result.get('current_turn', {}).get('route', {}), indent=2)}\n"
        f"\nAnswer ({elapsed:.2f}s):\n{answer}\n\n{trace_summary}\n"
    )

    trace_events = [event for event in events if event.get("type") == "trace_span"]
    trace_starts = [float(event["started_at"]) for event in trace_events if event.get("started_at")]
    completed_models = [
        event for event in trace_events
        if event.get("category") == "model" and event.get("status") == "completed"
    ]
    assert len(completed_models) <= 2, f"simple news query used {len(completed_models)} model calls"
    router_spans = [
        event for event in trace_events
        if event.get("name") == "semantic_router" and event.get("status") == "completed"
    ]
    assert router_spans and float(router_spans[-1].get("duration_ms") or 0) < 5_000
    route_events = [event for event in events if event.get("type") == "route_decision"]
    tool_events = [
        event for event in events
        if event.get("type") == "activity" and event.get("name") == "search_web"
    ]
    activity_ids = {
        event.get("activity_id")
        for event in tool_events
        if event.get("activity_id")
    }
    first_answer_token = next(
        (
            float(event["_observed_monotonic"])
            for event in events
            if event.get("type") == "chat_token" and event.get("token")
        ),
        None,
    )
    failures = []
    if elapsed >= 12:
        failures.append(f"total latency {elapsed:.1f}s >= 12s")
    if not answer.strip():
        failures.append("empty answer")
    if len(answer.split()) < 20:
        failures.append("answer is not substantive")
    if "I found these relevant sources" in answer:
        failures.append("answer exposed raw source-list fallback")
    if any(term in answer.lower() for term in ("apple orchard", "apple growers", "apple-picking")):
        failures.append("company query was interpreted as fruit industry news")
    if not re.search(r"\[[^\]]+, [^\]]+\]\(https?://", answer):
        failures.append("answer missing named, dated source citation")
    if "could not generate a final answer" in answer.lower() or "wasn't able to retrieve" in answer.lower():
        failures.append("answer reported retrieval failure")
    if not any(event.get("type") == "chat_complete" for event in events):
        failures.append("missing chat_complete event")
    if not trace_starts:
        failures.append("run emitted no diagnostic spans")
    elif min(trace_starts) - started_wall >= 1:
        failures.append("no observable progress during first second")
    if len(completed_models) > 2:
        failures.append(f"used {len(completed_models)} model calls; budget is 2")
    if not router_spans:
        failures.append("missing completed semantic_router span")
    elif float(router_spans[-1].get("duration_ms") or 0) >= 5_000:
        failures.append(
            f"router latency {float(router_spans[-1].get('duration_ms') or 0) / 1000:.1f}s >= 5s"
        )
    if not route_events or route_events[-1].get("route_level") not in {"tool", "small_task"}:
        failures.append("news query did not select tool/small_task route")
    if route_events and route_events[-1].get("selected_workflow") is not None:
        failures.append(f"news query escalated to workflow={route_events[-1].get('selected_workflow')}")
    if len(activity_ids) != 1:
        failures.append(f"expected one web search activity, got {activity_ids}")
    if first_answer_token is None:
        failures.append("news synthesis emitted no streamed answer tokens")
    elif first_answer_token - started >= 7:
        failures.append(f"first answer token latency {first_answer_token - started:.1f}s >= 7s")

    assert not failures, "\n".join(["Simple-news contract failed:", *[f"- {item}" for item in failures], trace_summary])

    recall_event_start = len(events)
    recall = main_graph.app.invoke(
        {
            "messages": [HumanMessage(content="what did I just ask you?")],
            "trace_id": trace_id,
            "root_span_id": root_span_id,
            "mode": "auto",
            "session_id": f"session-{thread_id}",
        },
        config={
            "configurable": {"thread_id": thread_id},
            "callbacks": [tracing.TraceCallbackHandler(trace_id, root_span_id)],
        },
    )
    recall_answer = _ai_text(recall)
    recall_events = events[recall_event_start:]
    recall_routes = [event for event in recall_events if event.get("type") == "route_decision"]
    recall_searches = [
        event for event in recall_events
        if event.get("type") == "activity" and event.get("name") == "search_web"
    ]
    assert recall_routes and recall_routes[-1]["route_level"] == "direct"
    assert recall_routes[-1].get("playbook_id") is None
    assert not recall_searches, "context-recall turn incorrectly called search_web"
    assert all(term in recall_answer.lower() for term in ("latest", "apple", "news"))
    assert recall["turn_context"]["previous_user_turn"] == "Whar are apples latest news?"

    financial_event_start = len(events)
    financial = main_graph.app.invoke(
        {
            "messages": [HumanMessage(content=(
                "Can you try to find apple's financial perfomence for this year, and plot it"
            ))],
            "trace_id": trace_id,
            "root_span_id": root_span_id,
            "mode": "auto",
            "session_id": f"session-{thread_id}",
        },
        config={
            "configurable": {"thread_id": thread_id},
            "callbacks": [tracing.TraceCallbackHandler(trace_id, root_span_id)],
        },
    )
    financial_events = events[financial_event_start:]
    financial_route = financial["current_turn"]["route"]
    financial_tools = {
        event.get("name")
        for event in financial_events
        if event.get("type") == "activity" and event.get("status") == "completed"
    }
    financial_diagnostics = [
        {
            "name": event.get("name") or event.get("stage"),
            "status": event.get("status"),
            "summary": event.get("summary"),
            "error": event.get("error"),
        }
        for event in financial_events
        if event.get("type") in {"execution_step", "activity"}
    ]
    print(
        "\nFinancial/chart turn:\n"
        f"route={json.dumps(financial_route, ensure_ascii=False)}\n"
        f"result_refs={financial['current_turn'].get('result_refs', [])}\n"
        f"artifact_refs={financial['current_turn'].get('artifact_refs', [])}\n"
        f"artifact_paths={financial['current_turn'].get('artifact_paths', [])}\n"
        f"events={json.dumps(financial_diagnostics, ensure_ascii=False, default=str, indent=2)}\n"
    )
    assert financial_route["route_level"] == "small_task"
    assert financial_route.get("playbook_id") != "latest_company_news"
    assert "chart" in financial_route["requested_outputs"]
    assert "get_company_financials" in financial_tools
    assert "render_financial_chart" in financial_tools
    assert "get_stock_price_history" not in financial_tools
    assert "render_stock_price_chart" not in financial_tools
    assert financial["current_turn"]["artifact_refs"]
    assert financial["current_turn"]["artifact_paths"]
    assert all(Path(path).exists() for path in financial["current_turn"]["artifact_paths"])
    assert financial["current_turn"]["response"]["status"] == "complete"
    assert "Incomplete requested outputs" not in _ai_text(financial)

    stock_event_start = len(events)
    stock = main_graph.app.invoke(
        {
            "messages": [HumanMessage(content=(
                "Look up Apple's stock-price evolution for the last five years and plot it. "
                "Use actual market prices."
            ))],
            "trace_id": trace_id,
            "root_span_id": root_span_id,
            "mode": "auto",
            "session_id": f"session-{thread_id}",
        },
        config={
            "configurable": {"thread_id": thread_id},
            "callbacks": [tracing.TraceCallbackHandler(trace_id, root_span_id)],
        },
    )
    stock_events = events[stock_event_start:]
    stock_tools = {
        event.get("name")
        for event in stock_events
        if event.get("type") == "activity" and event.get("status") == "completed"
    }
    assert "get_stock_price_history" in stock_tools
    assert "render_stock_price_chart" in stock_tools
    assert "execute_python" not in stock_tools
    assert stock["current_turn"]["artifact_refs"]
    assert stock["current_turn"]["artifact_paths"]
    assert all(Path(path).exists() for path in stock["current_turn"]["artifact_paths"])
    assert stock["current_turn"]["response"]["status"] == "complete"
    assert "Incomplete requested outputs" not in _ai_text(stock)


@pytest.mark.e2e
@pytest.mark.llm
def test_live_financial_request_chains_result_ref_into_chart(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    pytestconfig: pytest.Config,
    isolated_db,
):
    """Real models must compose data acquisition and chart rendering."""
    api_key = _real_openai_key()
    if not (pytestconfig.getoption("--run-live-agent-e2e") or os.getenv("RUN_LIVE_AGENT_E2E")):
        pytest.skip("Pass --run-live-agent-e2e or set RUN_LIVE_AGENT_E2E=1 to call live LLMs.")
    if not api_key:
        pytest.skip("Set real OPENAI_API_KEY to run live agent E2E.")

    from langchain_openai import ChatOpenAI
    import file as main_graph
    import graphs.conversational as conversational
    import utils

    model_name = os.getenv("AGENT_E2E_MODEL", "gpt-4o-mini")
    model = ChatOpenAI(model=model_name, api_key=api_key, timeout=45, max_retries=0)
    financials = _FakeTool("get_company_financials", {
        "tool_result_id": "financials_live_123",
        "tool_name": "get_company_financials",
        "summary": "Loaded five annual Apple periods",
        "result_schema": "company_financial_history.v1",
        "ticker": "AAPL",
        "coverage": ["revenue", "net_income"],
        "periods": ["2021", "2022", "2023", "2024", "2025"],
    })
    chart = _FakeTool("render_financial_chart", {
        "tool_result_id": "chart_live_123",
        "tool_name": "render_financial_chart",
        "summary": "Rendered Apple revenue and net income chart",
        "artifact_paths": ["/tmp/apple-live-financials.png"],
        "object_version_ids": ["chart:apple-live:v000001"],
    })
    tool_map = dict(conversational.CHAT_TOOLS_BY_NAME)
    tool_map.update({financials.name: financials, chart.name: chart})
    events: list[dict[str, Any]] = []

    utils.set_ui_event_handler(lambda event: events.append(dict(event)))
    request.addfinalizer(lambda: utils.set_ui_event_handler(None))
    monkeypatch.setattr(main_graph, "intent_llm", model)
    monkeypatch.setattr(main_graph, "emit_ui_event", lambda event: events.append(dict(event)))
    monkeypatch.setattr(conversational, "llm", model)
    monkeypatch.setattr(conversational, "chat_agent_llm", model.bind_tools(conversational.CHAT_TOOLS))
    monkeypatch.setattr(conversational, "CHAT_TOOLS_BY_NAME", tool_map)
    monkeypatch.setattr(conversational, "emit_ui_event", lambda event: events.append(dict(event)))

    thread_id = f"live-financial-chart-{uuid4().hex[:8]}"
    result = main_graph.app.invoke({
        "messages": [HumanMessage(content=(
            "Find Apple's financial performance over the last five fiscal years and "
            "generate a graph comparing revenue and net income."
        ))],
        "mode": "auto",
        "session_id": f"session-{thread_id}",
    }, config={"configurable": {"thread_id": thread_id}})

    route = result["current_turn"]["route"]
    assert "chart" in route["requested_outputs"]
    assert route["route_level"] == "small_task"
    assert len(financials.calls) == 1, "agent did not acquire structured financial data"
    assert len(chart.calls) == 1, "agent stopped before producing requested chart"
    assert chart.calls[0]["input_result_id"] == "financials_live_123"
    assert result["current_turn"]["result_refs"] == ["financials_live_123", "chart_live_123"]
    assert result["current_turn"]["artifact_refs"] == ["chart:apple-live:v000001"]
    assert result["current_turn"]["artifact_paths"] == ["/tmp/apple-live-financials.png"]
    assert result["current_turn"]["response"]["status"] == "complete"
    assert "Incomplete requested outputs" not in _ai_text(result)


@pytest.mark.e2e
@pytest.mark.llm
def test_live_minimal_analyst_loop_writes_grounded_report_then_uses_thread_memory(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    pytestconfig: pytest.Config,
    isolated_db,
):
    """Real controller must compose structured data into prose, then retain context."""
    api_key = _real_openai_key()
    if not (pytestconfig.getoption("--run-live-agent-e2e") or os.getenv("RUN_LIVE_AGENT_E2E")):
        pytest.skip("Pass --run-live-agent-e2e or set RUN_LIVE_AGENT_E2E=1 to call live LLMs.")
    if not api_key:
        pytest.skip("Set real OPENAI_API_KEY to run live agent E2E.")

    from langchain_openai import ChatOpenAI
    import file as main_graph
    import graphs.conversational as conversational
    import utils

    model_name = os.getenv("AGENT_E2E_MODEL", os.getenv("ORCHESTRATOR_MODEL", "gpt-4.1"))
    model = ChatOpenAI(model=model_name, api_key=api_key, timeout=60, max_retries=0)
    financial_payloads = {
        "financials_aapl_2025": {
            "kind": "tool_result",
            "payload": {
                "tool_name": "get_company_financials",
                "result_schema": "company_financial_history.v1",
                "ticker": "AAPL",
                "period": "annual",
                "result": {
                    "ticker": "AAPL",
                    "currency": "USD",
                    "series": [{"fiscal_year": "2025", "revenue": 416_161_000_000, "net_income": 112_010_000_000}],
                },
            },
        },
        "financials_nvda_2025": {
            "kind": "tool_result",
            "payload": {
                "tool_name": "get_company_financials",
                "result_schema": "company_financial_history.v1",
                "ticker": "NVDA",
                "period": "annual",
                "result": {
                    "ticker": "NVDA",
                    "currency": "USD",
                    "series": [{"fiscal_year": "2025", "revenue": 130_497_000_000, "net_income": 72_880_000_000}],
                },
            },
        },
    }

    class _FinancialTool:
        name = "get_company_financials"

        def __init__(self) -> None:
            self.calls: list[dict] = []

        def invoke(self, args):
            self.calls.append(dict(args))
            ticker = str(args.get("ticker") or "").upper()
            result_id = "financials_aapl_2025" if ticker == "AAPL" else "financials_nvda_2025"
            return json.dumps({
                "tool_result_id": result_id,
                "tool_name": self.name,
                "summary": f"Loaded FY2025 financials for {ticker}",
                "result_schema": "company_financial_history.v1",
                "ticker": ticker,
                "periods": ["2025"],
            })

    financials = _FinancialTool()
    tool_map = dict(conversational.CHAT_TOOLS_BY_NAME)
    tool_map[financials.name] = financials
    events: list[dict[str, Any]] = []

    utils.set_ui_event_handler(lambda event: events.append(dict(event)))
    request.addfinalizer(lambda: utils.set_ui_event_handler(None))
    monkeypatch.setattr(main_graph, "intent_llm", model)
    monkeypatch.setattr(main_graph, "emit_ui_event", lambda event: events.append(dict(event)))
    monkeypatch.setattr(main_graph, "persist_turn_object", lambda _state: None)
    monkeypatch.setattr(conversational, "llm", model)
    monkeypatch.setattr(conversational, "chat_agent_llm", model.bind_tools(conversational.CHAT_TOOLS))
    monkeypatch.setattr(conversational, "CHAT_TOOLS_BY_NAME", tool_map)
    monkeypatch.setattr(conversational, "resolve_ref", lambda ref_id: financial_payloads[ref_id])
    monkeypatch.setattr(conversational, "emit_ui_event", lambda event: events.append(dict(event)))

    thread_id = f"live-analyst-loop-{uuid4().hex[:8]}"
    session_id = f"session-{thread_id}"
    config = {"configurable": {"thread_id": thread_id}}
    report = main_graph.app.invoke({
        "messages": [HumanMessage(content=(
            "Write an analyst report comparing Apple and NVIDIA financial performance for fiscal 2025. "
            "Use structured company financial data. I want prose, not a chart."
        ))],
        "mode": "auto",
        "session_id": session_id,
    }, config=config)
    report_text = _ai_text(report)

    assert report["current_turn"]["route"]["route_level"] == "small_task"
    assert "report" in report["current_turn"]["required_outputs"]
    assert {str(call.get("ticker")).upper() for call in financials.calls} == {"AAPL", "NVDA"}
    assert "AAPL" in report_text.upper() and "NVDA" in report_text.upper()
    assert re.search(r"\d", report_text)
    assert "Detailed analysis showing" not in report_text
    assert report["current_turn"]["response"]["status"] == "complete"
    assert any(event.get("stage") == "hydrate_result_evidence" for event in events)
    assert any(
        event.get("stage") == "validate_answer" and event.get("status") == "completed"
        for event in events
    )

    calls_after_report = len(financials.calls)
    follow_up = main_graph.app.invoke({
        "messages": [HumanMessage(content="Which of those two had the higher 2025 net margin, and why?")],
        "mode": "auto",
        "session_id": session_id,
    }, config=config)
    follow_up_text = _ai_text(follow_up)

    assert "NVDA" in follow_up_text.upper() or "NVIDIA" in follow_up_text
    assert len(financials.calls) == calls_after_report, "follow-up repeated completed data acquisition"


class _MultiLevelRouterLLM:
    def invoke(self, messages):
        prompt = str(messages[0].content)
        latest = prompt.rsplit("human:", 1)[-1]
        if "CASE_REQUEST" in latest:
            return AIMessage(content=json.dumps({
                "route_level": "case",
                "playbook_id": None,
                "selected_workflow": None,
                "selected_case_type": "equity_research",
                "confidence": 0.97,
                "latency_class": "background",
                "max_tool_calls": 8,
                "creates_objects": True,
                "needs_confirmation": False,
                "object_policy": "case",
                "reason": "multiple deliverables and dependent workstreams",
            }))
        return AIMessage(content=json.dumps({
            "route_level": "direct",
            "playbook_id": None,
            "selected_workflow": None,
            "selected_case_type": None,
            "confidence": 0.96,
            "latency_class": "instant",
            "max_tool_calls": 0,
            "creates_objects": False,
            "needs_confirmation": False,
            "object_policy": "none",
            "reason": "single contextual answer",
        }))


class _CasePlannerLLM:
    def __init__(self, plan: dict[str, Any]) -> None:
        self.plan = plan
        self.inputs: list[dict[str, Any]] = []

    def invoke(self, messages):
        self.inputs.append(json.loads(str(messages[-1].content)))
        return AIMessage(content=json.dumps(self.plan))


class _CaseWorkerLLM:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def bind_tools(self, _tools):
        return self

    def invoke(self, messages):
        payload = json.loads(str(messages[-1].content))
        task = payload["task"]
        self.calls.append(task["task_id"])
        dependency_ids = [result["task_id"] for result in payload.get("dependency_results") or []]
        return AIMessage(content=(
            f"Completed {task['task_id']} using dependencies {dependency_ids}. "
            "Apple evidence remains linked to [doc:apple-10k:p31:c4]."
        ))


class _CaseSynthesisLLM:
    def invoke(self, messages):
        payload = json.loads(str(messages[-1].content))
        task_ids = [result["task_id"] for result in payload["case_task_results"]]
        return AIMessage(content=f"Final case output from tasks: {', '.join(task_ids)}.")


def _case_plan(scenario: str) -> dict[str, Any]:
    company = {
        "task_id": "company",
        "capability_id": "company_research",
        "objective": "Reuse Apple company evidence and refresh thesis",
        "dependency_task_ids": [],
        "required_output_types": ["company_profile", "evidence_bundle"],
    }
    if scenario == "memo_without_dcf":
        return {
            "objective": "Create Apple investment memo without valuation",
            "deliverables": ["investment_memo"],
            "tasks": [
                company,
                {
                    "task_id": "industry",
                    "capability_id": "industry_research",
                    "objective": "Assess Apple industry and demand context",
                    "dependency_task_ids": [],
                    "required_output_types": ["industry_analysis", "evidence_bundle"],
                },
                {
                    "task_id": "memo",
                    "capability_id": "run_memo_workflow",
                    "objective": "Write investment memo",
                    "dependency_task_ids": ["company", "industry"],
                    "required_output_types": ["memo"],
                },
            ],
        }
    return {
        "objective": "Research Apple, value it, and write investment memo",
        "deliverables": ["valuation", "investment_memo"],
        "tasks": [
            company,
            {
                "task_id": "financials",
                "capability_id": "financial_analysis",
                "objective": "Analyze Apple financial statements",
                "dependency_task_ids": [],
                "required_output_types": ["financial_analysis", "evidence_bundle"],
            },
            {
                "task_id": "dcf",
                "capability_id": "run_dcf_workflow",
                "objective": "Produce DCF valuation",
                "dependency_task_ids": ["company", "financials"],
                "required_output_types": ["valuation_result"],
            },
            {
                "task_id": "memo",
                "capability_id": "run_memo_workflow",
                "objective": "Write valuation-aware investment memo",
                "dependency_task_ids": ["company", "dcf"],
                "required_output_types": ["memo"],
            },
        ],
    }


@pytest.mark.contract
@pytest.mark.parametrize("scenario", ["memo_without_dcf", "valuation_then_memo"])
def test_case_dag_contract_with_scripted_model_boundaries(
    scenario,
    request: pytest.FixtureRequest,
    monkeypatch,
    isolated_db,
    agent_thread_trace,
):
    """Exercise route transition, durable memory retrieval, DAG fan-out/join, and versions."""
    import case_orchestration
    import evidence_memory
    import file as main_graph
    import graphs.conversational as conversational
    import utils

    session_id = f"case-session-{scenario}-{uuid4().hex[:6]}"
    thread_id = f"case-thread-{scenario}-{uuid4().hex[:6]}"
    config = {"configurable": {"thread_id": thread_id}}
    events: list[dict[str, Any]] = []
    turns: list[dict[str, Any]] = []
    worker_calls: list[str] = []
    planner = _CasePlannerLLM(_case_plan(scenario))
    worker = _CaseWorkerLLM(worker_calls)
    synthesis = _CaseSynthesisLLM()

    utils.set_thread_id(thread_id)
    utils.set_ui_event_handler(lambda event: events.append(dict(event)))
    request.addfinalizer(lambda: utils.set_ui_event_handler(None))
    monkeypatch.setattr(main_graph, "intent_llm", _MultiLevelRouterLLM())
    monkeypatch.setattr(main_graph, "emit_ui_event", lambda event: events.append(dict(event)))
    monkeypatch.setattr(conversational, "_chat_llm_for_policy", lambda _policy: _ScriptedAgentLLM())
    monkeypatch.setattr(conversational, "emit_ui_event", lambda event: events.append(dict(event)))
    monkeypatch.setattr(evidence_memory, "_chunk_refs", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(case_orchestration, "ChatOpenAI", lambda **_kwargs: worker)
    real_plan_case = case_orchestration.plan_case
    real_synthesize_case = case_orchestration.synthesize_case
    monkeypatch.setattr(main_graph, "plan_case", lambda state: real_plan_case(state, planner_llm=planner))
    monkeypatch.setattr(
        case_orchestration,
        "synthesize_case",
        lambda state, planner_llm=None: real_synthesize_case(state, planner_llm=synthesis),
    )

    def persist_seed_memory(state):
        answer = _ai_text(state)
        stored = isolated_db.upsert_workspace_object({
            "object_id": f"company_brief:{session_id}:aapl",
            "object_type": "company_brief",
            "schema_ref": "tests.CompanyBrief",
            "schema_version": "0.1",
            "title": "Apple company brief",
            "status": "complete",
            "session_id": session_id,
            "thread_id": thread_id,
            "created_by": "agent:e2e",
            "updated_by": "agent:e2e",
            "summary": answer,
            "search_text": f"Apple AAPL revenue services margins investment memo valuation {answer}",
            "entity_refs": [{"kind": "company", "name": "Apple Inc.", "ticker": "AAPL"}],
            "payload": {"answer": answer},
        })
        evidence_memory.persist_financial_fact(
            session_id=session_id,
            thread_id=thread_id,
            subject_id="AAPL",
            predicate="revenue",
            value=391.0,
            value_text="Apple reported revenue of $391 billion",
            fiscal_period="FY2024",
            confidence=0.96,
            fact_status="verified",
            evidence_refs=[{
                "evidence_id": "doc:apple-10k:p31:c4",
                "document_id": "apple-10k",
                "document_version_id": "apple-10k:v1",
                "citation_id": "doc:apple-10k:p31:c4",
                "filename": "apple-10k.pdf",
                "page": 31,
                "chunk_index": 4,
                "text_span": "Apple reported revenue of $391 billion",
            }],
        )
        return stored

    monkeypatch.setattr(main_graph, "persist_turn_object", persist_seed_memory)

    first_start = len(events)
    first = main_graph.app.invoke({
        "messages": [HumanMessage(id=f"{scenario}-intro", content="Give concise Apple valuation context")],
        "mode": "auto",
        "session_id": session_id,
        "thread_id": thread_id,
    }, config=config)
    assert first["current_turn"]["route"]["route_level"] == "direct"
    assert first.get("case_plan") is None
    turns.append({
        "input": "Give concise Apple valuation context",
        "output": _ai_text(first),
        "events": _event_slice(events, first_start),
        "objects_written": isolated_db.list_workspace_objects(session_id=session_id),
    })

    case_start = len(events)
    case_query = (
        "CASE_REQUEST: Create an Apple investment memo from prior context; no valuation needed"
        if scenario == "memo_without_dcf"
        else "CASE_REQUEST: Research Apple, build a DCF, then create an investment memo"
    )
    result = main_graph.app.invoke({
        "messages": [HumanMessage(id=f"{scenario}-case", content=case_query)],
        "mode": "auto",
        "session_id": session_id,
        "thread_id": thread_id,
    }, config=config)

    task_specs = result["case_plan"]["tasks"]
    task_ids = [task["task_id"] for task in task_specs]
    assert set(result["case_task_results"]) == set(task_ids)
    assert all(task["status"] == "completed" for task in result["case_task_results"].values())
    assert result["case_status"] == "complete"
    assert result["pending_task_packets"] == []
    assert result["current_turn"]["delegation"]["status"] == "complete"
    assert result["current_turn"]["response"]["case_version_id"]
    assert result["evidence_pack"]["fact_refs"][0]["payload"]["predicate"] == "revenue"
    assert planner.inputs[0]["workspace_objects"], "planner did not receive retrieved thread memory"
    assert planner.inputs[0]["evidence_pack"]["fact_refs"], "planner did not receive durable facts"
    case_events = _event_slice(events, case_start)
    task_events = [
        event for event in case_events
        if event.get("type") == "execution_step" and event.get("stage") == "execute_case_task"
    ]
    assert len(task_events) == len(task_ids)
    assert any(
        event.get("type") == "execution_step"
        and event.get("stage") == "build_memory_context"
        and event.get("object_ids")
        for event in case_events
    )

    if scenario == "memo_without_dcf":
        assert "dcf" not in task_ids
        assert set(worker_calls[:2]) == {"company", "industry"}
        assert worker_calls[-1] == "memo"
    else:
        assert worker_calls.index("dcf") > worker_calls.index("company")
        assert worker_calls.index("dcf") > worker_calls.index("financials")
        assert worker_calls.index("memo") > worker_calls.index("dcf")

    route_records = isolated_db.list_route_records(thread_id=thread_id)
    assert [record["decision"]["route_level"] for record in route_records] == ["direct", "case"]
    case_objects = isolated_db.list_workspace_objects(session_id=session_id, object_type="research_case")
    result_objects = isolated_db.list_workspace_objects(session_id=session_id, object_type="task_result")
    assert case_objects[0]["status"] == "complete"
    assert len(result_objects) == len(task_ids)
    assert all(
        any(
            source.get("source_id") == "doc:apple-10k:p31:c4"
            or source.get("citation_id") == "doc:apple-10k:p31:c4"
            for source in obj.get("source_refs") or []
        )
        for obj in result_objects
    )
    assert len(isolated_db.list_workspace_object_versions(case_objects[0]["object_id"])) == 2

    turns.append({
        "input": case_query,
        "output": _ai_text(result),
        "events": case_events,
        "objects_written": [*result_objects, *case_objects],
        "dag": {
            "case_id": result["case_id"],
            "status": result["case_status"],
            "tasks": [{
                "task_id": task["task_id"],
                "capability_id": task["capability_id"],
                "dependencies": task["dependency_task_ids"],
                "status": result["case_task_results"][task["task_id"]]["status"],
            } for task in task_specs],
        },
    })
    agent_thread_trace(f"Case E2E: {scenario}", turns)


@pytest.mark.e2e
@pytest.mark.llm
def test_live_thread_company_research_dcf_hitl_then_memo(
    request: pytest.FixtureRequest,
    pytestconfig: pytest.Config,
    monkeypatch,
    isolated_db,
    agent_thread_trace,
):
    if not (pytestconfig.getoption("--run-live-agent-e2e") or os.getenv("RUN_LIVE_AGENT_E2E")):
        pytest.skip("Pass --run-live-agent-e2e or set RUN_LIVE_AGENT_E2E=1 to call live LLMs.")
    api_key = _real_openai_key()
    if not api_key:
        pytest.skip("Set real OPENAI_API_KEY to run live agent E2E.")

    from langchain_openai import ChatOpenAI
    import file as main_graph
    import graphs.conversational as conversational
    import utils
    from lg_compat import Command
    from server import _sync_dcf_workspace_object
    from tools import resume_dcf_workflow_after_hitl

    model = os.getenv("AGENT_E2E_MODEL", "gpt-4o-mini")
    events: list[dict] = []
    turns: list[dict[str, Any]] = []
    search_web = _FakeTool("search_web", {
        "tool_result_id": "tool_result:live_aapl_news",
        "summary": "Found Apple news around Services, iPhone demand, and AI investment.",
    })
    retrieve_tool_result = _FakeTool("retrieve_tool_result", {
        "summary": "Apple news digest: Services resilience, iPhone demand debate, AI investment discipline.",
        "sources": [
            {"name": "Reuters", "date": "2026-09-01"},
            {"name": "Apple Investor Relations", "date": "2026-08-28"},
        ],
    })
    tool_map = dict(conversational.CHAT_TOOLS_BY_NAME)
    tool_map["search_web"] = search_web
    tool_map["retrieve_tool_result"] = retrieve_tool_result

    utils.set_ui_event_handler(lambda event: events.append(dict(event)))
    request.addfinalizer(lambda: utils.set_ui_event_handler(None))
    monkeypatch.setattr(main_graph, "intent_llm", ChatOpenAI(model=model, api_key=api_key, timeout=30))
    monkeypatch.setattr(main_graph, "emit_ui_event", lambda event: events.append(dict(event)))
    monkeypatch.setattr(conversational, "llm", ChatOpenAI(model=model, api_key=api_key, timeout=60))
    monkeypatch.setattr(conversational, "chat_agent_llm", conversational.llm.bind_tools(conversational.CHAT_TOOLS))
    monkeypatch.setattr(conversational, "CHAT_TOOLS_BY_NAME", tool_map)
    monkeypatch.setattr(conversational, "emit_ui_event", lambda event: events.append(dict(event)))

    session_id = f"live-session-{uuid4().hex[:8]}"
    thread_id = f"live-thread-{uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": thread_id}}
    utils.set_thread_id(thread_id)
    first_user = (
        "Tell me about Apple Inc. for valuation context. "
        "Use your general company knowledge; no current news needed."
    )

    start = len(events)
    first = main_graph.app.invoke(
        {
            "messages": [HumanMessage(content=first_user)],
            "mode": "auto",
            "session_id": session_id,
        },
        config=config,
    )
    first_answer = _ai_text(first)
    assert "Apple" in first_answer

    turns.append({
        "input": first_user,
        "output": first_answer,
        "events": _event_slice(events, start),
        "objects_written": isolated_db.list_workspace_objects(session_id=session_id),
    })

    start = len(events)
    news_user = (
        "Get latest news for said company. Use search_web first, then retrieve_tool_result, "
        "then answer in valuation-relevant bullets."
    )
    news = main_graph.app.invoke(
        {
            "messages": [HumanMessage(content=news_user)],
            "mode": "auto",
            "session_id": session_id,
        },
        config=config,
    )
    news_answer = _ai_text(news)
    assert "Apple" in news_answer or "Services" in news_answer or "iPhone" in news_answer
    assert search_web.calls, "live chat LLM did not call search_web"
    assert retrieve_tool_result.calls, "live chat LLM did not call retrieve_tool_result"
    news_objects = isolated_db.list_workspace_objects(session_id=session_id)
    assert news_objects, "graph did not persist live research output"
    news_object = news_objects[0]
    assert news_object["created_by"] == "memory_writer"
    assert news_object["payload"]["answer"] == news_answer
    turns.append({
        "input": news_user,
        "output": news_answer,
        "events": _event_slice(events, start),
        "objects_written": [news_object],
    })

    start = len(events)
    dcf_user = "Create a DCF for said company"
    dcf = main_graph.app.invoke(
        {
            "messages": [HumanMessage(content=dcf_user)],
            "mode": "auto",
            "session_id": session_id,
        },
        config=config,
    )

    assert "__interrupt__" in dcf
    interrupt_payload = _interrupt_payload(dcf)
    assert interrupt_payload["workflow_id"] == "dcf"
    assert interrupt_payload["context"]["company_name"].rstrip(".") == "Apple Inc"
    assert news_object["object_id"] in {
        card["object_id"]
        for card in interrupt_payload["context"]["memory_context"]["retrieved_object_cards"]
    }
    turns.append({
        "input": dcf_user,
        "workflow_interrupt": _workflow_interrupt_summary(dcf),
        "interrupt": interrupt_payload,
        "events": _event_slice(events, start),
        "objects_written": [],
    })

    start = len(events)
    context_resume = {
        "action": "approve",
        "approved": True,
        "context": interrupt_payload["context"],
    }
    context_approved = main_graph.app.invoke(Command(resume=context_resume), config=config)
    assumption_events = [event for event in events[start:] if event.get("type") == "dcf_assumptions_review"]
    assert assumption_events, "agent did not call real DCF workflow after context approval"
    assumption_interrupt = assumption_events[-1]
    proposed = assumption_interrupt.get("assumptions") or {}
    assert {"revenue_growth", "fcff_margin", "wacc", "terminal_growth"}.issubset(proposed)
    turns.append({
        "input": "Approve DCF workflow context",
        "output": _ai_text(context_approved),
        "interrupt": assumption_interrupt,
        "resume_payload": context_resume,
        "events": _event_slice(events, start),
        "objects_written": [],
    })

    edited = {
        "wacc": min(float(proposed["wacc"]) + 0.005, 0.24),
        "terminal_growth": max(float(proposed["terminal_growth"]) - 0.002, -0.04),
    }
    assumption_resume = {"action": "edit", "assumptions": edited}
    start = len(events)
    completed_payload, _pointer, completed_report = resume_dcf_workflow_after_hitl(
        resume_payload=assumption_resume,
        thread_id=thread_id,
        args={
            "ticker": "AAPL",
            "horizon_years": 5,
            "workflow_context": interrupt_payload["context"],
        },
    )
    final_assumptions = completed_payload.get("assumptions") or {}
    final_provenance = completed_payload.get("assumption_provenance") or {}
    assert final_assumptions["wacc"] == pytest.approx(edited["wacc"])
    assert final_assumptions["terminal_growth"] == pytest.approx(edited["terminal_growth"])
    assert final_assumptions["revenue_growth"] == pytest.approx(float(proposed["revenue_growth"]))
    assert final_assumptions["fcff_margin"] == pytest.approx(float(proposed["fcff_margin"]))
    assert final_provenance["wacc"]["user_edited"] is True
    assert isinstance((completed_payload.get("valuation") or {}).get("implied_share_price"), (int, float))
    assert completed_payload.get("result_path")
    synced_object = _sync_dcf_workspace_object(
        thread_id,
        session_id,
        result_path=completed_payload["result_path"],
    )
    assert synced_object is not None
    dcf_objects = isolated_db.list_workspace_objects(session_id=session_id, object_type="dcf_run")
    assert dcf_objects, "completed DCF was not persisted as workspace object"
    dcf_object = dcf_objects[0]
    assert dcf_object["payload"]["implied_share_price"] == completed_payload["valuation"]["implied_share_price"]
    turns.append({
        "input": "Accept remaining suggestions; edit WACC and terminal growth",
        "output": completed_report,
        "interrupt": assumption_interrupt,
        "resume_payload": assumption_resume,
        "events": _event_slice(events, start),
        "objects_written": [dcf_object],
    })

    start = len(events)
    memo_user = (
        "Write an investment committee memo for Apple using the completed DCF and prior research. "
        "Include thesis, valuation, catalysts, risks, and recommendation. Do not create a deck."
    )
    memo = main_graph.app.invoke(
        {
            "messages": [HumanMessage(content=memo_user)],
            "mode": "auto",
            "session_id": session_id,
        },
        config=config,
    )
    assert "__interrupt__" in memo
    memo_interrupt = _interrupt_payload(memo)
    memo_summary = _workflow_interrupt_summary(memo)
    assert memo_summary["workflow_id"] == "memo"
    assert dcf_object["object_id"] in memo_summary["selected_artifact_ids"]
    turns.append({
        "input": memo_user,
        "workflow_interrupt": memo_summary,
        "interrupt": memo_interrupt,
        "events": _event_slice(events, start),
        "objects_written": [],
    })

    start = len(events)
    memo_context_resume = {
        "action": "approve",
        "approved": True,
        "context": memo_interrupt["context"],
    }
    memo_started = main_graph.app.invoke(Command(resume=memo_context_resume), config=config)
    memo_review_events = [event for event in events[start:] if event.get("type") == "memo_draft_review"]
    assert memo_review_events, "agent did not call memo workflow after context approval"
    memo_review = memo_review_events[-1]
    draft = memo_review.get("draft") or {}
    assert draft.get("executive_summary")
    assert draft.get("recommendation")

    from graphs.workflows.memo import memo_workflow_app

    memo_graph_config = {
        "configurable": {"thread_id": f"{utils.get_run_dir().name}_memo"},
        "recursion_limit": 30,
    }
    edited_draft = dict(draft)
    edited_draft["recommendation"] = f"Analyst-reviewed: {draft['recommendation']}"
    memo_completed = memo_workflow_app.invoke(Command(resume={
        "action": "edit",
        "actor_id": "user:e2e",
        "draft": edited_draft,
    }), config=memo_graph_config)
    memo_output = memo_completed["output"]
    memo_answer = memo_output["markdown"]
    assert len(memo_answer) >= 500, "live model did not produce a substantive memo"
    assert any(term in memo_answer.lower() for term in ("thesis", "recommendation"))
    assert "valuation" in memo_answer.lower()
    assert "risk" in memo_answer.lower()

    memo_object = isolated_db.get_workspace_object(f"memo:{thread_id}")
    assert memo_object, "memo workflow output was not persisted"
    memo_context_object = isolated_db.get_workspace_object(f"memo_context:{thread_id}")
    assert memo_context_object
    assert dcf_object["object_id"] in memo_context_object.get("source_object_ids", [])
    assert dcf_object["version_id"] in memo_context_object.get("source_version_ids", [])
    assert memo_output["memo_version_id"] == memo_object["version_id"]
    assert isolated_db.list_object_actions(f"memo_draft:{thread_id}")[-1]["actor_id"] == "user:e2e"
    turns.append({
        "input": "Approve memo context, then edit and approve memo draft",
        "output": memo_answer,
        "interrupt": memo_review,
        "resume_payload": {"action": "edit", "actor_id": "user:e2e"},
        "events": _event_slice(events, start),
        "objects_written": [memo_context_object, memo_object],
    })

    route_records = isolated_db.list_route_records(thread_id=thread_id)
    route_levels = [record["decision"]["route_level"] for record in route_records]
    assert route_levels[-2:] == ["workflow", "workflow"]
    assert route_records[-1]["decision"]["selected_workflow"] == "memo"

    agent_thread_trace("Live Thread E2E: research -> DCF HITL -> investment memo", turns)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-s", "--agent-trace", *sys.argv[1:]]))
