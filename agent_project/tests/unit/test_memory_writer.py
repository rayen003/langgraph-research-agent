from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage


class _DraftLLM:
    def invoke(self, _prompt):
        from memory_writer import WorkspaceObjectDraft

        return WorkspaceObjectDraft(
            object_type="investment_memo",
            title="Apple investment memo",
            summary="Memo based on completed Apple DCF.",
            entity_refs=[{"kind": "company", "name": "Apple Inc.", "ticker": "AAPL"}],
            tags=["apple", "memo"],
        )


def test_persisted_workflow_output_keeps_selected_artifact_version_lineage(monkeypatch) -> None:
    import memory_writer

    writes = []
    monkeypatch.setattr(memory_writer, "_writer_llm", lambda: _DraftLLM())
    monkeypatch.setattr(
        memory_writer,
        "upsert_workspace_object",
        lambda payload: writes.append(payload) or {**payload, "version_id": "memo:v1"},
    )

    stored = memory_writer.persist_turn_object({
        "messages": [
            HumanMessage(content="Write an investment memo from the DCF"),
            AIMessage(content="## Thesis\nApple memo body."),
        ],
        "session_id": "session-1",
        "route_decision": {"route_level": "workflow", "creates_objects": True},
        "selected_playbook": {},
        "memory_context": {"retrieved_object_cards": [], "expanded_dependencies": []},
        "approved_workflow_context": {
            "workflow_id": "memo",
            "selected_artifact_ids": ["dcf_run:aapl"],
            "available_artifacts": [{
                "object_id": "dcf_run:aapl",
                "version_id": "dcf_run:aapl:v3",
                "object_type": "dcf_run",
                "title": "Apple DCF",
                "summary": "Implied value $195 per share.",
            }],
        },
    })

    assert stored is not None
    assert writes[0]["source_object_ids"] == ["dcf_run:aapl"]
    assert writes[0]["source_version_ids"] == ["dcf_run:aapl:v3"]

