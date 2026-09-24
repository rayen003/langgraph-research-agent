from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from langgraph.types import Command


class _StructuredMemoLLM:
    def with_structured_output(self, schema, method=None):
        self.schema = schema
        return self

    def invoke(self, _messages):
        return self.schema(
            title="Apple investment memo",
            executive_summary="Apple has durable cash generation but valuation constrains upside.",
            sections={
                "Investment thesis": "Services mix and installed base support resilience.",
                "Valuation": "Completed DCF implies $195 per share.",
                "Catalysts": "Services growth and product cycle.",
                "Risks": "Multiple compression and weaker hardware demand.",
            },
            recommendation="Watchlist pending a better entry point.",
            source_version_ids=["dcf:v1"],
            confidence=0.8,
        )


@pytest.fixture()
def isolated_memo_runtime(monkeypatch: pytest.MonkeyPatch):
    import storage
    import utils

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        monkeypatch.setattr(storage, "DB_PATH", root / "agent.db")
        monkeypatch.setattr(storage, "RUNS_DIR", root)
        monkeypatch.setattr(utils, "RUNS_DIR", root)
        storage.init_db()
        utils.set_thread_id("memo-thread")
        yield storage, root


def test_memo_graph_interrupt_edit_resume_and_version_lineage(monkeypatch, isolated_memo_runtime) -> None:
    storage, root = isolated_memo_runtime
    import graphs.workflows.memo.graph as memo_graph
    import utils

    monkeypatch.setattr(memo_graph, "ChatOpenAI", lambda **_kwargs: _StructuredMemoLLM())
    events: list[dict] = []
    utils.set_ui_event_handler(events.append)
    config = {"configurable": {"thread_id": "memo-thread_memo"}}
    first = memo_graph.memo_workflow_app.invoke({
        "thread_id": "memo-thread",
        "session_id": "session-1",
        "sources": [{
            "type": "workspace_object",
            "title": "Apple DCF",
            "summary": "DCF output",
            "content": "Implied value $195 per share.",
            "object_id": "dcf_run:memo-thread",
            "version_id": "dcf_run:memo-thread:v000001",
        }],
        "brief": {"title": "Apple investment memo", "company_name": "Apple", "hitl_mode": "review"},
    }, config=config)

    assert first["__interrupt__"][0].value["type"] == "memo_draft_review"
    edited = dict(first["draft"])
    edited["recommendation"] = "Reject at current price; revisit below intrinsic value."
    completed = memo_graph.memo_workflow_app.invoke(Command(resume={
        "action": "edit", "actor_id": "user:e2e", "draft": edited,
    }), config=config)

    output = completed["output"]
    assert output["status"] == "complete"
    assert "Reject at current price" in output["markdown"]
    assert Path(output["markdown_path"]).is_file()
    context = storage.get_workspace_object("memo_context:memo-thread")
    draft = storage.get_workspace_object("memo_draft:memo-thread")
    memo = storage.get_workspace_object("memo:memo-thread")
    assert context["source_version_ids"] == ["dcf_run:memo-thread:v000001"]
    assert draft["source_version_ids"] == [context["version_id"]]
    assert memo["source_version_ids"] == [context["version_id"], draft["version_id"]]
    assert storage.list_object_actions("memo_draft:memo-thread")[-1]["actor_id"] == "user:e2e"
    assert output["memo_version_id"] == memo["version_id"]
    activities = [event for event in events if event.get("type") == "activity"]
    names = {event.get("name") for event in activities}
    assert {
        "workflow:memo",
        "workflow:memo:validate_inputs",
        "workflow:memo:generate_draft",
        "workflow:memo:review_draft",
        "workflow:memo:finalize_memo",
    } <= names
    assert any(
        event.get("name") == "workflow:memo:review_draft"
        and event.get("status") == "awaiting_input"
        and event.get("review_ref", {}).get("type") == "memo_draft_review"
        for event in activities
    )
    assert any(
        event.get("name") == "workflow:memo" and event.get("status") == "completed"
        for event in activities
    )
    utils.set_ui_event_handler(None)


def test_memo_input_resolver_hydrates_selected_workspace_object(monkeypatch) -> None:
    from graphs.workflows.memo import inputs

    monkeypatch.setattr(inputs, "get_workspace_object", lambda object_id: {
        "object_id": object_id,
        "version_id": f"{object_id}:v1",
        "title": "Apple DCF",
        "summary": "Implied value $195.",
        "payload": {"valuation": {"implied_share_price": 195}},
        "source_refs": [],
    })
    sources, brief = inputs.resolve_memo_inputs([], {"title": "Apple memo"}, workflow_context={
        "company_name": "Apple Inc.",
        "user_intent": "Write IC memo",
        "selected_artifact_ids": ["dcf:aapl"],
    })

    assert sources[0]["object_id"] == "dcf:aapl"
    assert sources[0]["version_id"] == "dcf:aapl:v1"
    assert "implied_share_price" in sources[0]["content"]
    assert brief["company_name"] == "Apple Inc."


def test_memo_tool_is_exposed_to_main_agent() -> None:
    import graphs.conversational as conversational
    import tools

    assert "run_memo_workflow" in {tool.name for tool in tools.ALL_TOOLS}
    assert "run_memo_workflow" in conversational.CHAT_TOOLS_BY_NAME


def test_memo_tool_returns_semantic_review_summary(monkeypatch) -> None:
    import graphs.workflows.memo as memo_workflow
    import tools

    monkeypatch.setattr(memo_workflow, "resolve_memo_inputs", lambda sources, brief, workflow_context=None: (
        sources or [{"type": "manual_text", "title": "Source", "content": "Evidence"}],
        brief,
    ))
    monkeypatch.setattr(memo_workflow, "run_memo_workflow_sync", lambda **_kwargs: {
        "__memo_hitl__": True,
        "workflow": "memo",
        "type": "memo_draft_review",
        "workflow_id": "memo",
        "draft": {"title": "Apple memo"},
        "sources": [],
    })
    monkeypatch.setattr(tools, "emit_ui_event", lambda _event: None)

    raw = tools.run_memo_workflow.invoke({
        "brief": {"title": "Apple memo"},
        "sources": [{"type": "manual_text", "title": "Source", "content": "Evidence"}],
    })
    payload = json.loads(raw)

    assert payload["summary"] == "Memo draft ready for review"
    assert payload["status"] == "waiting"
    assert payload["next_actions"] == ["approve", "edit", "reject"]
