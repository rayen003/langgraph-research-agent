from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
def isolated_db(monkeypatch: pytest.MonkeyPatch):
    import storage

    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(storage, "DB_PATH", Path(tmp) / "agent.db")
        storage.init_db()
        yield storage


def test_research_plan_steps_and_report_form_version_lineage(isolated_db):
    from graphs.research_persistence import (
        persist_approved_plan,
        persist_draft_plan,
        persist_research_context,
        persist_research_report,
        persist_step_result,
    )

    state = {
        "thread_id": "thread-1",
        "session_id": "session-1",
        "objective": "Assess Apple services growth",
        "memory_context": {"retrieved_object_cards": []},
    }
    context = persist_research_context(state)
    state["research_context_version_id"] = context["version_id"]
    plan = {"plan_id": "plan-1", "query": state["objective"], "status": "draft", "steps": [
        {"id": "step_1", "description": "Collect evidence", "depends_on": [], "status": "pending"},
    ]}
    draft = persist_draft_plan(state, plan)
    approved = persist_approved_plan(state, {**plan, "status": "approved"}, actor_id="user:ray", decision="yes")
    state["approved_plan_version_id"] = approved["version_id"]
    step = persist_step_result(
        state,
        step={**plan["steps"][0], "status": "completed", "result": "Services grew."},
        tool_result_ids=["tool-1"],
    )
    state["step_result_version_ids"] = {"step_1": step["version_id"]}
    report = persist_research_report(
        state,
        content="# Apple\nServices grew.",
        report_path="/tmp/final_report.md",
    )

    assert draft["source_version_ids"] == [context["version_id"]]
    assert approved["version_number"] == 2
    assert step["source_version_ids"] == [approved["version_id"]]
    assert report["source_version_ids"] == [approved["version_id"], step["version_id"]]
    assert report["payload"]["approved_plan_version_id"] == approved["version_id"]
    assert isolated_db.list_object_actions("research_plan:thread-1")[-1]["actor_id"] == "user:ray"
    current_plan = isolated_db.get_workspace_object("research_plan:thread-1")
    assert current_plan["created_by"] == "workflow:research"
    assert current_plan["updated_by"] == "user:ray"
