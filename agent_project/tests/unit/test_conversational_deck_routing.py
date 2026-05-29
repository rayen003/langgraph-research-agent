"""Regression tests for deck workflow routing in chat."""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-placeholder")

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import agent_project.graphs.conversational as conversational


class _DeckToolCallLLM:
    calls = 0

    def invoke(self, history):
        self.calls += 1
        system = next(m for m in history if m.type == "system")
        assert "run_deck_workflow now" in str(system.content)
        return AIMessage(
            content="",
            tool_calls=[{
                "name": "run_deck_workflow",
                "args": {
                    "brief": {"title": "AAPL — DCF Investment Case", "audience": "ic"},
                },
                "id": "tool-deck-1",
            }],
        )


class _DeckHitlTool:
    def invoke(self, _args):
        return (
            "⛔ STOP — DO NOT CALL MORE TOOLS. Present the draft outline for user review.\n\n"
            "## Draft Deck Outline (3 slides)\n\n"
            "1. **Executive Summary** `executive_summary`"
        )


def test_build_deck_nudge_when_user_asks_for_deck_after_dcf(monkeypatch, tmp_path):
    dcf_pointer = json.dumps({
        "tool_name": "run_dcf_workflow",
        "tool_result_id": "run_dcf_workflow_abc",
        "summary": "# DCF Valuation: AAPL",
    })

    monkeypatch.setattr(
        conversational,
        "_extract_dcf_payload_from_history",
        lambda _history: {"ticker": "AAPL", "sensitivity_chart": None},
    )

    nudge = conversational._build_deck_workflow_nudge([
        HumanMessage(content="let's build a deck from this dcf analysis"),
        ToolMessage(content=dcf_pointer, tool_call_id="tool-1"),
    ])

    assert nudge is not None
    assert "run_deck_workflow now" in nudge
    assert "Do NOT pass `sources`" in nudge
    assert "AAPL" in nudge


def test_chat_node_calls_deck_tool_when_user_requests_deck(monkeypatch, tmp_path):
    import utils as utils_mod

    events: list[dict] = []
    fake_llm = _DeckToolCallLLM()
    run_id = f"test-deck-route-{tmp_path.name}"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    (run_dir / "dcf_output.json").write_text(
        json.dumps({"ticker": "AAPL", "valuation": {}, "assumptions": {}}),
        encoding="utf-8",
    )

    monkeypatch.setattr(utils_mod, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(conversational, "chat_agent_llm", fake_llm)
    monkeypatch.setattr(
        conversational,
        "_extract_dcf_payload_from_history",
        lambda _history: {"ticker": "AAPL", "sensitivity_chart": None},
    )
    monkeypatch.setattr(
        conversational,
        "CHAT_TOOLS_BY_NAME",
        {"run_deck_workflow": _DeckHitlTool()},
    )
    monkeypatch.setattr(conversational, "emit_ui_event", lambda event: events.append(dict(event)))
    monkeypatch.setattr(conversational.agent_log, "chat_start", lambda: 0.0)
    monkeypatch.setattr(conversational.agent_log, "chat_done", lambda *_a, **_k: None)

    utils_mod.set_thread_id(run_id)
    result = conversational._chat_node_inner({
        "messages": [HumanMessage(content="build a deck from this dcf analysis")],
        "session_id": "test-session",
    })

    assert fake_llm.calls == 1
    assert "Draft Deck Outline" in result["messages"][-1].content
    assert not any(e.get("type") == "chat_complete" for e in events)
