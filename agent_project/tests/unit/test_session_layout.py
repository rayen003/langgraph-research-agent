"""Tests for session sidebar layout persistence."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
def isolated_db(monkeypatch: pytest.MonkeyPatch):
    import storage

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "agent.db"
        monkeypatch.setattr(storage, "DB_PATH", db_path)
        monkeypatch.setattr(storage, "RUNS_DIR", Path(tmp))
        storage.init_db()
        yield storage


def test_session_layout_roundtrip(isolated_db):
    storage = isolated_db
    payload = {
        "groups": [
            {
                "id": "g_1",
                "name": "AAPL work",
                "color": "blue",
                "collapsed": False,
                "sort_order": 0,
                "created_at": "2026-05-31T12:00:00+00:00",
            }
        ],
        "sessions": [
            {
                "session_id": "s_1",
                "title_override": "Apple DCF",
                "pinned": True,
                "group_id": "g_1",
                "sort_order": 0,
                "updated_at": "2026-05-31T12:00:00+00:00",
            }
        ],
    }
    storage.replace_session_layout(**payload)
    out = storage.get_session_layout()
    assert len(out["groups"]) == 1
    assert out["groups"][0]["name"] == "AAPL work"
    assert len(out["sessions"]) == 1
    assert out["sessions"][0]["pinned"] is True
    assert out["sessions"][0]["group_id"] == "g_1"

    storage.replace_session_layout(groups=[], sessions=[])
    assert storage.get_session_layout() == {"groups": [], "sessions": []}


def test_workspace_object_roundtrip(isolated_db):
    storage = isolated_db
    obj = storage.upsert_workspace_object({
        "object_id": "dcf_run:thread_1",
        "object_type": "dcf_run",
        "title": "AMZN DCF run",
        "status": "complete",
        "session_id": "s_1",
        "thread_id": "thread_1",
        "source_object_ids": ["document_analysis:doc_1"],
        "kg_node_ids": ["AMZN::dcf_run::abc"],
        "artifact_paths": ["/runs/thread_1/dcf-report.pdf?inline=1"],
        "summary": "Implied $42.00",
        "schema_ref": "domain.cases.ValuationRunResult",
        "schema_version": "0.1",
        "search_text": "AMZN DCF valuation implied share price WACC",
        "tags": ["amzn", "dcf", "valuation"],
        "entity_refs": [{"kind": "company", "name": "Amazon", "ticker": "AMZN"}],
        "source_refs": [{
            "source_id": "api:fmp:amzn:income_statement",
            "source_type": "api",
            "title": "AMZN income statement",
            "provider": "FMP",
            "license_status": "approved",
        }],
        "confidence": 0.81,
        "quality": {"validation_status": "valid", "warnings": []},
        "case_id": "case_amzn",
        "task_id": "task_valuation",
        "run_id": "run_1",
        "created_by": "dcf_workflow",
        "updated_by": "dcf_workflow",
        "payload": {"ticker": "AMZN", "implied_share_price": 42.0},
    })

    assert obj["object_id"] == "dcf_run:thread_1"
    assert obj["source_object_ids"] == ["document_analysis:doc_1"]
    assert obj["schema_ref"] == "domain.cases.ValuationRunResult"
    assert obj["search_text"] == "AMZN DCF valuation implied share price WACC"
    assert obj["tags"] == ["amzn", "dcf", "valuation"]
    assert obj["entity_refs"][0]["ticker"] == "AMZN"
    assert obj["source_refs"][0]["provider"] == "FMP"
    assert obj["confidence"] == 0.81
    assert obj["quality"]["validation_status"] == "valid"
    assert obj["case_id"] == "case_amzn"
    assert obj["created_by"] == "dcf_workflow"
    assert obj["payload"]["ticker"] == "AMZN"

    listed = storage.list_workspace_objects(session_id="s_1")
    assert len(listed) == 1
    assert listed[0]["artifact_paths"] == ["/runs/thread_1/dcf-report.pdf?inline=1"]

    fetched = storage.get_workspace_object("dcf_run:thread_1")
    assert fetched is not None
    assert fetched["kg_node_ids"] == ["AMZN::dcf_run::abc"]


def test_workspace_object_updates_append_immutable_versions(isolated_db):
    storage = isolated_db
    base = {
        "object_id": "research:AAPL",
        "object_type": "research_finding",
        "title": "Apple finding",
        "status": "draft",
        "session_id": "s_1",
        "created_by": "agent:research",
        "updated_by": "agent:research",
        "summary": "Initial view",
        "payload": {"revenue_growth": 0.05},
    }

    first = storage.upsert_workspace_object(base)
    second = storage.upsert_workspace_object({
        **base,
        "status": "complete",
        "updated_by": "user:ray",
        "summary": "Reviewed view",
        "payload": {"revenue_growth": 0.07},
    })

    assert first["version_number"] == 1
    assert second["version_number"] == 2
    assert first["version_id"] != second["version_id"]

    versions = storage.list_workspace_object_versions("research:AAPL")
    assert [version["version_number"] for version in versions] == [1, 2]
    assert versions[0]["summary"] == "Initial view"
    assert versions[0]["payload"] == {"revenue_growth": 0.05}
    assert versions[1]["summary"] == "Reviewed view"
    assert versions[1]["payload"] == {"revenue_growth": 0.07}

    current = storage.get_workspace_object("research:AAPL")
    assert current is not None
    assert current["version_id"] == versions[1]["version_id"]
    assert current["version_number"] == 2

    actions = storage.list_object_actions("research:AAPL")
    assert [action["action_type"] for action in actions] == ["created", "updated"]
    assert actions[1]["actor_id"] == "user:ray"
    assert actions[1]["resulting_version_id"] == versions[1]["version_id"]


def test_workspace_object_archive_appends_version_without_deleting_history(isolated_db):
    storage = isolated_db
    storage.upsert_workspace_object({
        "object_id": "memo:1",
        "object_type": "memo",
        "title": "Investment memo",
        "status": "complete",
        "session_id": "s_1",
        "created_by": "agent:memo",
        "payload": {"body": "Original memo"},
    })

    storage.delete_workspace_object("memo:1", actor_id="user:ray")

    current = storage.get_workspace_object("memo:1")
    assert current is not None
    assert current["status"] == "archived"
    assert current["version_number"] == 2
    versions = storage.list_workspace_object_versions("memo:1")
    assert len(versions) == 2
    assert versions[0]["payload"] == {"body": "Original memo"}
    assert versions[1]["payload"] == {"body": "Original memo"}
    assert storage.list_object_actions("memo:1")[-1]["action_type"] == "archived"
