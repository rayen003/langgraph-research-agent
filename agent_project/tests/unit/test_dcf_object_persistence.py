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


def test_dcf_objects_form_exact_version_lineage(isolated_db):
    from graphs.workflows.dcf.persistence import (
        persist_approved_assumptions,
        persist_dcf_result,
        persist_workflow_context,
    )

    state = {
        "thread_id": "thread-1",
        "session_id": "session-1",
        "ticker": "AAPL",
        "kg_run_id": "kg-run-1",
        "workflow_context": {"company_name": "Apple", "focus_areas": ["valuation"]},
    }
    context = persist_workflow_context(state)
    state["workflow_context_version_id"] = context["version_id"]
    assumptions = persist_approved_assumptions(
        state,
        assumptions={"wacc": 0.09, "terminal_growth": 0.025},
        provenance={"wacc": {"approved_by": "user"}},
        actor_id="user:ray",
        decision="edit",
    )
    state["approved_assumptions_version_id"] = assumptions["version_id"]
    result = persist_dcf_result(
        state,
        payload={
            "ticker": "AAPL",
            "valuation": {"implied_share_price": 210.0, "current_price": 190.0},
            "model_validity": "valid",
        },
        result_path="/tmp/dcf_output.json",
    )

    assert assumptions["source_version_ids"] == [context["version_id"]]
    assert result["source_version_ids"] == [context["version_id"], assumptions["version_id"]]
    assert result["payload"]["approved_assumptions_version_id"] == assumptions["version_id"]
    assert isolated_db.get_workspace_object_version("dcf_run:thread-1", 1)["version_id"] == result["version_id"]

