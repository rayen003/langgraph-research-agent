from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage


@pytest.fixture()
def isolated_storage(monkeypatch, tmp_path: Path):
    import storage

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "agent.db")
    storage.init_db()
    return storage


def _evidence(value_text: str = "Revenue was $100 million") -> list[dict]:
    return [{
        "evidence_id": "doc:filing:p4:c2",
        "document_id": "filing",
        "document_version_id": "filing:v1",
        "citation_id": "doc:filing:p4:c2",
        "filename": "10-k.pdf",
        "page": 4,
        "chunk_index": 2,
        "text_span": value_text,
    }]


def test_financial_fact_retries_are_idempotent_but_changed_values_append(isolated_storage):
    from evidence_memory import persist_financial_fact

    kwargs = {
        "session_id": "session-1",
        "thread_id": "thread-1",
        "subject_id": "ACME",
        "predicate": "revenue",
        "value": 100,
        "value_text": "Revenue was $100 million",
        "fiscal_period": "FY2025",
        "confidence": 0.95,
        "fact_status": "verified",
        "evidence_refs": _evidence(),
    }
    first = persist_financial_fact(**kwargs)
    retry = persist_financial_fact(**kwargs)
    changed = persist_financial_fact(**{**kwargs, "value": 110, "value_text": "Revenue was $110 million"})

    assert retry["version_id"] == first["version_id"]
    assert changed["version_id"] != first["version_id"]
    versions = isolated_storage.list_workspace_object_versions(first["object_id"])
    assert [version["payload"]["value"] for version in versions] == [100, 110]
    assert versions[0]["payload"]["evidence_refs"][0]["citation_id"] == "doc:filing:p4:c2"


def test_evidence_pack_fuses_facts_graph_and_chunks(monkeypatch, isolated_storage):
    import evidence_memory

    evidence_memory.persist_financial_fact(
        session_id="session-1", thread_id="thread-1", subject_id="ACME",
        predicate="revenue", value=100, value_text="ACME revenue was $100 million",
        fiscal_period="FY2025", confidence=0.95, fact_status="verified", evidence_refs=_evidence(),
    )
    monkeypatch.setattr(evidence_memory, "_kg_refs", lambda *_args, **_kwargs: ([{"id": "entity:acme"}], [{"relation": "reported_by"}]))
    monkeypatch.setattr(evidence_memory, "_chunk_refs", lambda *_args, **_kwargs: [{
        "citation_id": "doc:filing:p4:c2", "text_span": "ACME revenue was $100 million"
    }])
    pack = evidence_memory.build_evidence_pack({
        "messages": [HumanMessage(content="What was ACME revenue?")],
        "session_id": "session-1",
        "route_decision": {"route_level": "case"},
    })

    assert pack["fact_refs"][0]["payload"]["value"] == 100
    assert pack["entity_refs"][0]["id"] == "entity:acme"
    assert pack["chunk_refs"][0]["citation_id"] == "doc:filing:p4:c2"
    assert set(pack["coverage"]) == {"durable_facts", "entity_graph", "document_evidence"}


def test_document_source_version_is_immutable(isolated_storage):
    document = {
        "doc_id": "doc-1", "filename": "contract.pdf", "session_id": "session-1",
        "status": "processing", "chunk_count": 0, "page_count": 0,
        "created_at": 1.0,
    }
    isolated_storage.upsert_document(document)
    isolated_storage.upsert_document({**document, "status": "ready", "chunk_count": 12})

    versions = isolated_storage.list_document_versions("doc-1")
    assert len(versions) == 1
    assert versions[0]["version_id"] == "doc-1:v1"
    assert isolated_storage.get_document_version("doc-1:v1")["filename"] == "contract.pdf"
