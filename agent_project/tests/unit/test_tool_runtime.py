"""Universal ToolNode execution contract tests."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Annotated, TypedDict

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from domain.tool_execution import ToolCachePolicy, ToolProgress, ToolResult, ToolRetryPolicy, ToolSpec
from tool_runtime import (
    CapabilityDispatcher,
    InMemoryToolCache,
    ToolExecutionMiddleware,
    ToolLaneLimits,
    ToolRegistry,
    write_tool_progress,
)


def _message(*calls: tuple[str, str, dict]) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": name, "id": call_id, "args": args, "type": "tool_call"}
            for name, call_id, args in calls
        ],
    )


def _results(output: dict) -> list[ToolResult]:
    messages = [message for message in output["messages"] if isinstance(message, ToolMessage)]
    assert messages
    return [ToolResult.model_validate_json(str(message.content)) for message in messages]


def test_dispatcher_enforces_surface_and_uses_middleware() -> None:
    @tool
    def chat_only(value: str) -> str:
        """Return supplied value."""
        return value

    events: list[dict] = []
    registry = ToolRegistry([
        ToolSpec(tool_id="chat_only", title="Chat only", exposed_in=["chat"]),
    ])
    dispatcher = CapabilityDispatcher(
        [chat_only],
        registry,
        middleware=ToolExecutionMiddleware(registry, event_sink=events.append),
    )
    call = {"id": "call-1", "name": "chat_only", "args": {"value": "ok"}, "type": "tool_call"}

    accepted = dispatcher.invoke([call], surface="chat")
    rejected = dispatcher.invoke([call], surface="research")

    accepted_result = ToolResult.model_validate_json(str(accepted[0].content))
    assert accepted_result.output == "ok"
    assert rejected[0].status == "error"
    assert "not exposed" in str(rejected[0].content)
    assert any(event["type"] == "tool_invocation" for event in events)
    assert any(event["type"] == "tool_result" for event in events)


class _RuntimeState(TypedDict, total=False):
    messages: Annotated[list, add_messages]
    thread_id: str
    session_id: str
    user_id: str | None
    tool_execution_context: dict


def _graph(tool_node):
    builder = StateGraph(_RuntimeState)
    builder.add_node("tools", tool_node)
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)
    return builder.compile()


def test_tool_spec_rejects_unsafe_retry_and_cache_policies() -> None:
    with pytest.raises(ValueError, match="cannot retry"):
        ToolSpec(
            tool_id="writer",
            title="Writer",
            execution_kind="write",
            idempotent=False,
            retry_policy=ToolRetryPolicy(max_attempts=2),
        )
    with pytest.raises(ValueError, match="cannot use execution cache"):
        ToolSpec(
            tool_id="writer",
            title="Writer",
            execution_kind="write",
            idempotent=False,
            cache_policy=ToolCachePolicy(enabled=True, ttl_seconds=30),
        )


def test_registry_requires_spec_for_every_tool() -> None:
    @tool
    def lookup(value: str) -> str:
        """Look up value."""
        return value

    middleware = ToolExecutionMiddleware(ToolRegistry())
    with pytest.raises(ValueError, match="lookup"):
        middleware.create_tool_node([lookup])


def test_registry_rejects_unknown_fallback_tool() -> None:
    @tool
    def lookup(value: str) -> str:
        """Look up value."""
        return value

    spec = ToolSpec(
        tool_id="lookup",
        title="Lookup",
        fallback_tool_ids=["missing_provider"],
    )
    middleware = ToolExecutionMiddleware(ToolRegistry([spec]))
    with pytest.raises(ValueError, match="missing_provider"):
        middleware.create_tool_node([lookup])


def test_current_catalog_covers_every_chat_tool() -> None:
    from graphs.conversational import CHAT_TOOLS
    from tool_catalog import build_current_tool_registry, capability_ids_for_surface

    registry = build_current_tool_registry()
    registry.validate_tools(CHAT_TOOLS)
    assert {tool.name for tool in CHAT_TOOLS} == set(capability_ids_for_surface("chat"))


def test_tool_node_normalizes_result_and_emits_lifecycle() -> None:
    @tool
    def lookup(value: str) -> str:
        """Look up value."""
        return json.dumps({"summary": f"found {value}", "value": value})

    events: list[dict] = []
    spec = ToolSpec(tool_id="lookup", title="Lookup", timeout_ms=1_000)
    node = _graph(ToolExecutionMiddleware(ToolRegistry([spec]), event_sink=events.append).create_tool_node([lookup]))

    output = node.invoke({
        "messages": [_message(("lookup", "call-1", {"value": "AAPL"}))],
        "thread_id": "thread-1",
        "session_id": "session-1",
    })

    result = _results(output)[0]
    assert result.status == "success"
    assert result.summary == "found AAPL"
    assert result.output["value"] == "AAPL"
    invocation_events = [event["invocation"] for event in events if event["type"] == "tool_invocation"]
    assert [event["status"] for event in invocation_events] == ["running", "completed"]
    assert invocation_events[-1]["thread_id"] == "thread-1"
    assert invocation_events[-1]["result_id"] == result.result_id


def test_structured_tool_payload_never_becomes_activity_summary() -> None:
    payload = {
        "__memo_hitl__": True,
        "workflow": "memo",
        "type": "memo_draft_review",
        "status": "waiting",
        "draft": {"title": "Apple memo"},
    }

    summary = ToolExecutionMiddleware._summary(payload, json.dumps(payload))

    assert summary == "Memo draft review · waiting"
    assert not summary.startswith("{")
    assert "__memo_hitl__" not in summary


def test_sensitive_arguments_are_redacted_but_hash_uses_raw_values() -> None:
    @tool
    def execute_secret(code: str) -> str:
        """Execute secret input."""
        return "done"

    events: list[dict] = []
    spec = ToolSpec(
        tool_id="execute_secret",
        title="Secret execution",
        sensitive_arg_names=["code"],
    )
    node = _graph(ToolExecutionMiddleware(ToolRegistry([spec]), event_sink=events.append).create_tool_node(
        [execute_secret]
    ))

    node.invoke({"messages": [_message(("execute_secret", "call-1", {"code": "alpha"}))]})
    node.invoke({"messages": [_message(("execute_secret", "call-2", {"code": "beta"}))]})

    started = [
        event["invocation"]
        for event in events
        if event["type"] == "tool_invocation" and event["invocation"]["status"] == "running"
    ]
    assert [event["arguments"] for event in started] == [
        {"code": "[REDACTED]"},
        {"code": "[REDACTED]"},
    ]
    assert started[0]["arguments_hash"] != started[1]["arguments_hash"]


def test_json_error_payload_becomes_error_result_and_is_not_cached() -> None:
    calls = 0

    @tool
    async def failed_lookup(value: str) -> str:
        """Return provider failure payload."""
        nonlocal calls
        calls += 1
        return json.dumps({"error": f"provider unavailable for {value}", "error_type": "ProviderError"})

    spec = ToolSpec(
        tool_id="failed_lookup",
        title="Failed lookup",
        cache_policy=ToolCachePolicy(enabled=True, ttl_seconds=60),
    )
    node = _graph(ToolExecutionMiddleware(ToolRegistry([spec]), event_sink=lambda _event: None).create_tool_node(
        [failed_lookup]
    ))

    async def run() -> tuple[ToolResult, ToolResult]:
        first = _results(await node.ainvoke({
            "messages": [_message(("failed_lookup", "call-1", {"value": "x"}))],
        }))[0]
        second = _results(await node.ainvoke({
            "messages": [_message(("failed_lookup", "call-2", {"value": "x"}))],
        }))[0]
        return first, second

    first, second = asyncio.run(run())
    assert calls == 2
    assert first.status == second.status == "error"
    assert first.error_type == "ProviderError"
    assert second.cached is False


def test_event_sink_failure_does_not_fail_tool() -> None:
    @tool
    def lookup() -> str:
        """Return value."""
        return "ok"

    def broken_sink(_event: dict) -> None:
        raise RuntimeError("telemetry unavailable")

    spec = ToolSpec(tool_id="lookup", title="Lookup")
    node = _graph(ToolExecutionMiddleware(ToolRegistry([spec]), event_sink=broken_sink).create_tool_node([lookup]))

    result = _results(node.invoke({"messages": [_message(("lookup", "call-1", {}))]}))[0]
    assert result.status == "success"
    assert result.output == "ok"


def test_tool_node_executes_independent_async_calls_in_parallel() -> None:
    @tool
    async def fetch_a(value: str) -> str:
        """Fetch source A."""
        await asyncio.sleep(0.08)
        return json.dumps({"summary": f"A:{value}"})

    @tool
    async def fetch_b(value: str) -> str:
        """Fetch source B."""
        await asyncio.sleep(0.08)
        return json.dumps({"summary": f"B:{value}"})

    registry = ToolRegistry([
        ToolSpec(tool_id="fetch_a", title="Fetch A", timeout_ms=500),
        ToolSpec(tool_id="fetch_b", title="Fetch B", timeout_ms=500),
    ])
    node = _graph(ToolExecutionMiddleware(registry, event_sink=lambda _event: None).create_tool_node([fetch_a, fetch_b]))

    async def run() -> tuple[float, dict]:
        started = time.monotonic()
        output = await node.ainvoke({"messages": [_message(
            ("fetch_a", "call-a", {"value": "x"}),
            ("fetch_b", "call-b", {"value": "y"}),
        )]})
        return time.monotonic() - started, output

    elapsed, output = asyncio.run(run())
    assert elapsed < 0.14
    assert {result.summary for result in _results(output)} == {"A:x", "B:y"}


def test_concurrency_key_serializes_shared_provider_calls() -> None:
    @tool
    async def provider_a() -> str:
        """Call shared provider A."""
        await asyncio.sleep(0.06)
        return "A"

    @tool
    async def provider_b() -> str:
        """Call shared provider B."""
        await asyncio.sleep(0.06)
        return "B"

    specs = [
        ToolSpec(tool_id="provider_a", title="Provider A", timeout_ms=500, concurrency_key="provider"),
        ToolSpec(tool_id="provider_b", title="Provider B", timeout_ms=500, concurrency_key="provider"),
    ]
    node = _graph(ToolExecutionMiddleware(ToolRegistry(specs), event_sink=lambda _event: None).create_tool_node(
        [provider_a, provider_b]
    ))

    async def run() -> float:
        started = time.monotonic()
        await node.ainvoke({"messages": [_message(
            ("provider_a", "call-a", {}),
            ("provider_b", "call-b", {}),
        )]})
        return time.monotonic() - started

    assert asyncio.run(run()) >= 0.11


def test_cache_reuses_successful_result_with_new_invocation_identity() -> None:
    calls = 0

    @tool
    async def cached_lookup(value: str) -> str:
        """Return cacheable value."""
        nonlocal calls
        calls += 1
        return json.dumps({"summary": f"value {value}", "value": value})

    spec = ToolSpec(
        tool_id="cached_lookup",
        title="Cached lookup",
        timeout_ms=500,
        cache_policy=ToolCachePolicy(enabled=True, ttl_seconds=60),
    )
    middleware = ToolExecutionMiddleware(
        ToolRegistry([spec]), cache=InMemoryToolCache(), event_sink=lambda _event: None,
    )
    node = _graph(middleware.create_tool_node([cached_lookup]))

    async def run() -> tuple[ToolResult, ToolResult]:
        first = _results(await node.ainvoke({
            "messages": [_message(("cached_lookup", "call-1", {"value": "x"}))],
        }))[0]
        second = _results(await node.ainvoke({
            "messages": [_message(("cached_lookup", "call-2", {"value": "x"}))],
        }))[0]
        return first, second

    first, second = asyncio.run(run())
    assert calls == 1
    assert first.invocation_id != second.invocation_id
    assert second.cached is True
    assert second.output == first.output


def test_retry_policy_retries_matching_exception_only() -> None:
    calls = 0

    @tool
    async def flaky_lookup() -> str:
        """Fail once, then succeed."""
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("temporary")
        return json.dumps({"summary": "recovered"})

    spec = ToolSpec(
        tool_id="flaky_lookup",
        title="Flaky lookup",
        timeout_ms=500,
        retry_policy=ToolRetryPolicy(
            max_attempts=2,
            initial_backoff_ms=1,
            retryable_exceptions=["ValueError"],
        ),
    )
    node = _graph(ToolExecutionMiddleware(ToolRegistry([spec]), event_sink=lambda _event: None).create_tool_node([flaky_lookup]))

    result = _results(asyncio.run(node.ainvoke({
        "messages": [_message(("flaky_lookup", "call-1", {}))],
    })))[0]
    assert calls == 2
    assert result.status == "success"
    assert result.attempt_count == 2


def test_async_deadline_returns_normalized_timeout() -> None:
    @tool
    async def slow_lookup() -> str:
        """Return too slowly."""
        await asyncio.sleep(0.1)
        return "late"

    spec = ToolSpec(tool_id="slow_lookup", title="Slow lookup", timeout_ms=20, first_event_slo_ms=10)
    node = _graph(ToolExecutionMiddleware(ToolRegistry([spec]), event_sink=lambda _event: None).create_tool_node([slow_lookup]))

    started = time.monotonic()
    result = _results(asyncio.run(node.ainvoke({
        "messages": [_message(("slow_lookup", "call-1", {}))],
    })))[0]
    elapsed = time.monotonic() - started

    assert elapsed < 0.08
    assert result.status == "timeout"
    assert result.error_type == "TimeoutError"
    assert result.retryable is True


def test_interactive_lane_is_not_blocked_by_background_capacity() -> None:
    @tool
    async def background_a() -> str:
        """Run background task A."""
        await asyncio.sleep(0.06)
        return "A"

    @tool
    async def background_b() -> str:
        """Run background task B."""
        await asyncio.sleep(0.06)
        return "B"

    @tool
    async def interactive_lookup() -> str:
        """Run interactive lookup."""
        await asyncio.sleep(0.01)
        return "interactive"

    specs = [
        ToolSpec(
            tool_id="background_a",
            title="Background A",
            execution_lane="background",
            timeout_ms=500,
        ),
        ToolSpec(
            tool_id="background_b",
            title="Background B",
            execution_lane="background",
            timeout_ms=500,
        ),
        ToolSpec(tool_id="interactive_lookup", title="Interactive lookup", timeout_ms=500),
    ]
    events: list[dict] = []
    middleware = ToolExecutionMiddleware(
        ToolRegistry(specs),
        event_sink=events.append,
        lane_limits=ToolLaneLimits(interactive=1, background=1),
    )
    node = _graph(middleware.create_tool_node([background_a, background_b, interactive_lookup]))

    async def run() -> float:
        started = time.monotonic()
        await node.ainvoke({"messages": [_message(
            ("background_a", "call-a", {}),
            ("background_b", "call-b", {}),
            ("interactive_lookup", "call-i", {}),
        )]})
        return time.monotonic() - started

    elapsed = asyncio.run(run())
    completed = [
        event["invocation"]
        for event in events
        if event["type"] == "tool_invocation" and event["invocation"]["status"] == "completed"
    ]
    assert elapsed >= 0.11
    assert completed[0]["tool_id"] == "interactive_lookup"
    assert completed[0]["metadata"]["execution_lane"] == "interactive"
    background_waits = [
        event["metadata"]["lane_queue_wait_ms"]
        for event in completed
        if event["metadata"]["execution_lane"] == "background"
    ]
    assert max(background_waits) >= 40


def test_langgraph_custom_stream_emits_partial_and_heartbeat_events() -> None:
    @tool
    async def streaming_lookup(value: str) -> str:
        """Stream partial lookup data."""
        write_tool_progress("First source received", partial={"source": "first", "value": value})
        await asyncio.sleep(0.035)
        return json.dumps({"summary": f"completed {value}"})

    spec = ToolSpec(
        tool_id="streaming_lookup",
        title="Streaming lookup",
        timeout_ms=500,
        supports_streaming=True,
        progress_interval_ms=10,
    )
    node = _graph(ToolExecutionMiddleware(
        ToolRegistry([spec]),
        event_sink=lambda _event: None,
    ).create_tool_node([streaming_lookup]))

    async def run() -> list[dict]:
        chunks = []
        async for chunk in node.astream(
            {"messages": [_message(("streaming_lookup", "call-1", {"value": "AAPL"}))]},
            stream_mode="custom",
        ):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(run())
    progress = [ToolProgress.model_validate(chunk["progress"]) for chunk in chunks]
    phases = [event.phase for event in progress]
    assert phases[0:2] == ["started", "attempt"]
    assert "partial" in phases
    assert "heartbeat" in phases
    assert phases[-1] == "completed"
    assert len({event.invocation_id for event in progress}) == 1
    assert [event.sequence for event in progress] == list(range(1, len(progress) + 1))
    partial = next(event for event in progress if event.phase == "partial")
    assert partial.partial == {"source": "first", "value": "AAPL"}
