"""Regression tests for the deterministic [DCF_APPROVED] rerun path.

The chat ReAct loop used to mishandle approved reruns — re-calling
run_dcf_workflow in assumption_review_mode=True (re-showing the HITL card) and
firing redundant/erroring calls, with the final report often never rendering.
`_direct_dcf_approval` intercepts the approval turn and runs the valuation once,
verbatim. These tests pin that contract without touching the LLM or network.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-placeholder")

from langchain_core.messages import AIMessage, HumanMessage

import agent_project.graphs.conversational as conversational
import agent_project.tools as tools
from agent_project.utils import set_thread_id


class _RecordingDcfTool:
    """Stand-in for the run_dcf_workflow tool: records args, returns a report pointer."""

    def __init__(self, report: str):
        self.report = report
        self.calls: list[dict] = []

    def invoke(self, args):
        self.calls.append(dict(args))
        return json.dumps({
            "tool_name": "run_dcf_workflow",
            "summary": self.report,
            "dcf_hitl": False,
        })


class _ExplodingLLM:
    def invoke(self, _history):
        raise AssertionError("approved rerun must not invoke the chat LLM")


def _approval_payload() -> str:
    return "[DCF_APPROVED]:" + json.dumps({
        "ticker": "AAPL",
        "horizon_years": 5,
        "all_assumptions": {
            "revenue_growth": 0.06,
            "fcff_margin": 0.20,
            "terminal_growth": 0.025,
            "tax_rate": 0.15,
            "wacc": 0.087,
        },
    })


def test_approved_rerun_runs_workflow_once_no_review(monkeypatch, tmp_path):
    """Approval turn → one workflow call with review_mode=False, report emitted."""
    events: list[dict] = []
    report = "# DCF Valuation: AAPL\n\n## Executive Summary\n\n- Model validity: VALID"
    tool = _RecordingDcfTool(report)

    monkeypatch.setattr(conversational, "chat_agent_llm", _ExplodingLLM())
    monkeypatch.setattr(conversational, "CHAT_TOOLS_BY_NAME", {"run_dcf_workflow": tool})
    monkeypatch.setattr(conversational, "emit_ui_event", lambda event: events.append(dict(event)))
    monkeypatch.setattr(conversational.agent_log, "chat_start", lambda: 0.0)
    monkeypatch.setattr(conversational.agent_log, "chat_done", lambda *_a, **_k: None)

    set_thread_id(f"test-dcf-rerun-{tmp_path.name}")
    result = conversational._chat_node_inner({
        "messages": [HumanMessage(content=_approval_payload())],
        "session_id": "test-session",
    })

    # Exactly one workflow call, in completion (non-review) mode, with overrides.
    assert len(tool.calls) == 1
    assert tool.calls[0]["assumption_review_mode"] is False
    assert tool.calls[0]["ticker"] == "AAPL"
    assert tool.calls[0]["horizon_years"] == 5
    assert tool.calls[0]["assumption_overrides"]["wacc"] == 0.087

    # Report surfaced verbatim, both as the message and the chat_complete event.
    assert result["messages"][-1].content == report
    complete = [e for e in events if e.get("type") == "chat_complete"]
    assert complete and complete[-1]["content"] == report


def test_approved_rerun_extracts_report_through_real_tool(monkeypatch, tmp_path):
    """End-to-end wiring: real run_dcf_workflow tool persists a pointer and the
    direct handler reads the full report back off disk (only the heavy compute
    is stubbed). Guards the persist→extract path the live UI depends on."""
    events: list[dict] = []
    report = (
        "# DCF Valuation: AAPL\n\n## Executive Summary\n\n"
        "- Implied share price: $212.40\n- Model validity: VALID"
    )

    # Stub the expensive compute and the summary renderer the tool persists.
    # summarize is looked up dynamically inside run_dcf_workflow, so patch where
    # the tool resolves it (the dcf package the tool imported it from).
    import agent_project.graphs.workflows.dcf as dcf_pkg
    monkeypatch.setattr(tools, "run_dcf_workflow_sync", lambda **_kw: {"ticker": "AAPL"})
    monkeypatch.setattr(tools, "summarize_dcf_payload", lambda _payload: report)
    monkeypatch.setattr(dcf_pkg, "summarize_dcf_payload", lambda _payload: report, raising=False)

    monkeypatch.setattr(conversational, "chat_agent_llm", _ExplodingLLM())
    monkeypatch.setattr(conversational, "emit_ui_event", lambda event: events.append(dict(event)))
    monkeypatch.setattr(conversational.agent_log, "chat_start", lambda: 0.0)
    monkeypatch.setattr(conversational.agent_log, "chat_done", lambda *_a, **_k: None)

    set_thread_id(f"test-dcf-rerun-real-{tmp_path.name}")
    result = conversational._chat_node_inner({
        "messages": [HumanMessage(content=_approval_payload())],
        "session_id": "test-session",
    })

    out = result["messages"][-1].content
    # Real path may append a presentation guide — match on the report core.
    assert out.startswith("# DCF Valuation: AAPL")
    complete = [e for e in events if e.get("type") == "chat_complete"]
    assert complete and complete[-1]["content"] == out


def test_non_approval_turn_returns_none(monkeypatch):
    """Ordinary chat turns fall through to the ReAct loop (handler returns None)."""
    assert conversational._direct_dcf_approval(
        [HumanMessage(content="what is the revenue for AAPL?")]
    ) is None


def test_malformed_approval_payload_returns_none(monkeypatch):
    """Bad JSON / missing overrides must not crash — fall through to ReAct loop."""
    assert conversational._direct_dcf_approval(
        [HumanMessage(content="[DCF_APPROVED]:{not json")]
    ) is None
    assert conversational._direct_dcf_approval(
        [HumanMessage(content="[DCF_APPROVED]:" + json.dumps({"ticker": "AAPL"}))]
    ) is None
