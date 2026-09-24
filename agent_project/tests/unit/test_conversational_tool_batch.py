"""Conversational execution must use ToolNode concurrency for independent calls."""

from __future__ import annotations

import time

from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from domain.tool_execution import ToolSpec
from tool_runtime import CapabilityDispatcher, ToolExecutionMiddleware, ToolRegistry


def test_chat_executes_canonical_simple_calls_concurrently(monkeypatch) -> None:
    import graphs.conversational as conversational

    @tool
    def lookup_a(value: str) -> str:
        """Look up source A."""
        time.sleep(0.08)
        return f"A:{value}"

    @tool
    def lookup_b(value: str) -> str:
        """Look up source B."""
        time.sleep(0.08)
        return f"B:{value}"

    tools = [lookup_a, lookup_b]
    registry = ToolRegistry([
        ToolSpec(tool_id="lookup_a", title="Lookup A", timeout_ms=500, exposed_in=["chat"]),
        ToolSpec(tool_id="lookup_b", title="Lookup B", timeout_ms=500, exposed_in=["chat"]),
    ])
    dispatcher = CapabilityDispatcher(
        tools,
        registry,
        middleware=ToolExecutionMiddleware(registry, event_sink=lambda _event: None),
    )
    tool_map = {item.name: item for item in tools}
    monkeypatch.setattr(conversational, "CHAT_TOOLS_BY_NAME", tool_map)
    monkeypatch.setattr(conversational, "_CANONICAL_CHAT_TOOLS_BY_NAME", tool_map)
    monkeypatch.setattr(conversational, "_CAPABILITY_DISPATCHER", dispatcher)

    response = AIMessage(content="", tool_calls=[
        {"name": "lookup_a", "id": "call-a", "args": {"value": "x"}},
        {"name": "lookup_b", "id": "call-b", "args": {"value": "y"}},
    ])
    started = time.monotonic()
    messages = conversational._invoke_chat_tools(response)
    elapsed = time.monotonic() - started

    assert elapsed < 0.14
    assert [message.content for message in messages] == ["A:x", "B:y"]
    assert all(message.artifact and message.artifact.get("tool_result") for message in messages)
