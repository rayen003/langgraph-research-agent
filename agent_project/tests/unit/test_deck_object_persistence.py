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


def test_deck_context_outline_and_result_form_version_lineage(isolated_db):
    from graphs.workflows.deck.persistence import (
        persist_approved_outline,
        persist_deck_context,
        persist_deck_result,
    )

    state = {
        "thread_id": "thread-1",
        "session_id": "session-1",
        "brief": {"title": "Apple valuation", "audience": "ic"},
        "sources": [{
            "type": "dcf_output",
            "object_id": "dcf_run:thread-1",
            "version_id": "dcf_run:thread-1:v000001",
            "payload_path": "/tmp/dcf_output.json",
        }],
    }
    context = persist_deck_context(state)
    state["deck_context_version_id"] = context["version_id"]
    outline = persist_approved_outline(
        state,
        outline={"title": "Apple valuation", "slides": []},
        actor_id="user:ray",
        decision="approve",
    )
    state["approved_outline_version_id"] = outline["version_id"]
    result = persist_deck_result(
        state,
        payload={"brief": state["brief"], "slides": [], "pptx_path": "/tmp/apple.pptx"},
        deck_output_path="/tmp/deck_output.json",
    )

    assert context["source_version_ids"] == ["dcf_run:thread-1:v000001"]
    assert outline["source_version_ids"] == [context["version_id"]]
    assert result["source_version_ids"] == [context["version_id"], outline["version_id"]]
    assert result["payload"]["approved_outline_version_id"] == outline["version_id"]
    assert isolated_db.list_object_actions("deck_outline:thread-1")[-1]["actor_id"] == "user:ray"

