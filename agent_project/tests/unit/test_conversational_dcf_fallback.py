"""Regression tests for DCF chat completion fallbacks."""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-placeholder")

from langchain_core.messages import AIMessage, ToolMessage

import agent_project.graphs.conversational as conversational
from agent_project.utils import set_thread_id


class _TimeoutLLM:
    def invoke(self, _history):
        raise TimeoutError("Request timed out.")


class _DcfToolCallThenFailLLM:
    calls = 0

    def invoke(self, _history):
        self.calls += 1
        if self.calls == 1:
            return AIMessage(
                content="",
                tool_calls=[{"name": "run_dcf_workflow", "args": {"ticker": "AAPL"}, "id": "tool-1"}],
            )
        raise AssertionError("DCF report should be emitted without a second LLM call")


class _DcfTool:
    def __init__(self, report: str):
        self.report = report

    def invoke(self, _args):
        return json.dumps({
            "tool_name": "run_dcf_workflow",
            "summary": self.report,
            "dcf_hitl": False,
        })


def test_chat_node_emits_dcf_report_when_post_dcf_llm_times_out(monkeypatch, tmp_path):
    """A completed DCF tool result should survive post-tool LLM timeout."""
    events: list[dict] = []
    report = "# DCF Valuation: AAPL\n\n## Executive Summary\n\n- Model validity: VALID"
    pointer = {
        "tool_name": "run_dcf_workflow",
        "summary": report,
        "dcf_hitl": False,
    }

    monkeypatch.setattr(conversational, "chat_agent_llm", _TimeoutLLM())
    monkeypatch.setattr(conversational, "emit_ui_event", lambda event: events.append(dict(event)))
    monkeypatch.setattr(conversational.agent_log, "chat_start", lambda: 0.0)
    monkeypatch.setattr(conversational.agent_log, "chat_done", lambda *_args, **_kwargs: None)

    set_thread_id(f"test-dcf-timeout-{tmp_path.name}")
    result = conversational._chat_node_inner({
        "messages": [ToolMessage(content=json.dumps(pointer), tool_call_id="tool-1")],
        "session_id": "test-session",
    })

    assert result["messages"][-1].content == report
    complete_events = [event for event in events if event.get("type") == "chat_complete"]
    assert complete_events
    assert complete_events[-1]["content"] == report


def test_chat_node_does_not_reprompt_llm_after_completed_dcf_tool(monkeypatch, tmp_path):
    """Completed DCF tools are terminal because the report is already final."""
    events: list[dict] = []
    report = "# DCF Valuation: AAPL\n\n## Executive Summary\n\n- Model validity: VALID"
    fake_llm = _DcfToolCallThenFailLLM()

    monkeypatch.setattr(conversational, "chat_agent_llm", fake_llm)
    monkeypatch.setattr(conversational, "CHAT_TOOLS_BY_NAME", {"run_dcf_workflow": _DcfTool(report)})
    monkeypatch.setattr(conversational, "emit_ui_event", lambda event: events.append(dict(event)))
    monkeypatch.setattr(conversational.agent_log, "chat_start", lambda: 0.0)
    monkeypatch.setattr(conversational.agent_log, "chat_done", lambda *_args, **_kwargs: None)

    set_thread_id(f"test-dcf-short-circuit-{tmp_path.name}")
    result = conversational._chat_node_inner({
        "messages": [],
        "session_id": "test-session",
    })

    assert fake_llm.calls == 1
    assert result["messages"][-1].content == report
    assert events[-1]["type"] == "chat_complete"
    assert events[-1]["content"] == report

