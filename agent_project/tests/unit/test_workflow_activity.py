from __future__ import annotations

from workflow_activity import emit_workflow, emit_workflow_step


def test_workflow_step_separates_summary_audit_and_review(monkeypatch) -> None:
    events: list[dict] = []
    monkeypatch.setattr("workflow_activity.emit_activity", lambda **event: events.append(event))

    payload = {
        "summary_line": "Draft ready for review",
        "detail": {
            "outputs": {"section_count": 4},
            "object_refs": ["memo:v1"],
        },
        "review_ref": {"type": "memo_draft_review", "workflow_id": "memo"},
        "internal_flag": True,
    }
    emit_workflow_step(
        workflow="memo",
        step="review_draft",
        status="awaiting_input",
        parent_step_id="turn-1",
        payload=payload,
    )

    event = events[-1]
    assert event["name"] == "workflow:memo:review_draft"
    assert event["status"] == "awaiting_input"
    assert event["summary"] == "Draft ready for review"
    assert event["detail"]["outputs"]["section_count"] == 4
    assert event["review_ref"]["type"] == "memo_draft_review"
    assert event["meta"] == {"internal_flag": True}
    assert payload["summary_line"] == "Draft ready for review"


def test_workflow_root_uses_same_contract(monkeypatch) -> None:
    events: list[dict] = []
    monkeypatch.setattr("workflow_activity.emit_activity", lambda **event: events.append(event))

    emit_workflow(
        workflow="memo",
        parent_step_id="turn-1",
        status="complete",
        payload={
            "summary_line": "Memo completed",
            "detail": {"artifact_refs": ["memo.md"]},
        },
    )

    event = events[-1]
    assert event["kind"] == "workflow"
    assert event["name"] == "workflow:memo"
    assert event["summary"] == "Memo completed"
    assert event["detail"]["artifact_refs"] == ["memo.md"]
    assert event["ended_at"] is not None
