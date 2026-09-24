"""Hybrid retrieval and append-only finance fact persistence."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from domain.evidence import EvidencePack, EvidenceRef, FinancialFact


def _tokens(value: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9][a-z0-9._-]+", value.lower()) if len(token) > 1}


def _score(query: str, value: str) -> int:
    return len(_tokens(query) & _tokens(value))


def _logical_fact_id(subject_id: str, predicate: str, fiscal_period: str) -> str:
    key = f"{subject_id}|{predicate}|{fiscal_period}".lower()
    return f"fact:{hashlib.sha256(key.encode('utf-8')).hexdigest()[:20]}"


def persist_financial_fact(
    *,
    session_id: str,
    thread_id: str | None,
    subject_id: str,
    predicate: str,
    value: Any,
    value_text: str,
    fiscal_period: str = "",
    confidence: float = 0.7,
    fact_status: str = "extracted",
    evidence_refs: list[dict[str, Any]] | None = None,
    created_by: str = "agent:fact_extractor",
) -> dict[str, Any]:
    """Persist logical fact as versioned workspace object; unchanged retries are idempotent."""
    from storage import get_workspace_object, upsert_workspace_object  # noqa: PLC0415

    object_id = _logical_fact_id(subject_id, predicate, fiscal_period)
    refs = [EvidenceRef.model_validate(ref) for ref in (evidence_refs or [])]
    fact = FinancialFact(
        object_id=object_id,
        title=f"{subject_id} {predicate} {fiscal_period}".strip(),
        summary=value_text or f"{predicate}: {value}",
        schema_ref="domain.evidence.FinancialFact",
        schema_version="0.1",
        created_by=created_by,
        updated_by=created_by,
        confidence=confidence,
        subject_id=subject_id,
        predicate=predicate,
        value=value,
        value_text=value_text,
        fiscal_period=fiscal_period,
        fact_status=fact_status,
        evidence_refs=refs,
        source_refs=[
            {
                "source_id": ref.citation_id,
                "source_type": "document",
                "title": ref.filename or ref.document_id,
                "object_id": ref.document_version_id,
                "period": fiscal_period or None,
            }
            for ref in refs
        ],
        entity_refs=[{"kind": "company", "name": subject_id, "ticker": subject_id}],
        search_text=f"{subject_id} {predicate} {fiscal_period} {value_text}",
    )
    workspace = fact.to_workspace_object(session_id=session_id, thread_id=thread_id)
    current = get_workspace_object(object_id)
    if current:
        current_payload = current.get("payload") or {}
        semantic_keys = {
            "subject_id", "predicate", "value", "value_text", "fiscal_period",
            "fact_status", "actual_or_estimate", "evidence_refs",
            "supersedes_fact_version_id", "contradicts_fact_version_ids",
        }
        if all(current_payload.get(key) == workspace["payload"].get(key) for key in semantic_keys):
            return current
    return upsert_workspace_object(workspace, action_type="fact_extracted")


def _fact_refs(query: str, session_id: str, limit: int) -> list[dict[str, Any]]:
    from storage import list_workspace_objects  # noqa: PLC0415

    facts = list_workspace_objects(session_id=session_id, object_type="financial_fact", limit=200)
    ranked = sorted(
        ((_score(query, str(fact.get("search_text") or fact.get("summary") or "")), fact) for fact in facts),
        key=lambda item: (item[0], str(item[1].get("updated_at") or "")),
        reverse=True,
    )
    return [
        {
            "fact_id": fact.get("object_id"),
            "fact_version_id": fact.get("version_id"),
            "summary": fact.get("summary"),
            "confidence": fact.get("confidence"),
            "payload": fact.get("payload"),
            "source_refs": fact.get("source_refs") or [],
        }
        for score, fact in ranked[:limit]
        if score > 0
    ]


def _kg_refs(query: str, session_id: str, limit: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from storage import list_kg_edges, list_kg_nodes  # noqa: PLC0415

    nodes = list_kg_nodes(session_id=session_id)
    ranked = sorted(
        ((_score(query, f"{node.get('ticker')} {node.get('field')} {json.dumps(node.get('value'), default=str)}"), node) for node in nodes),
        key=lambda item: (item[0], float(item[1].get("updated_at") or 0)),
        reverse=True,
    )
    selected = [node for score, node in ranked[:limit] if score > 0]
    selected_ids = {str(node.get("id")) for node in selected}
    edges = [
        edge for edge in list_kg_edges(session_id=session_id)
        if str(edge.get("src_id")) in selected_ids or str(edge.get("tgt_id")) in selected_ids
    ][:limit]
    return selected, edges


def _chunk_refs(query: str, session_id: str, limit: int, doc_ids: list[str] | None) -> list[dict[str, Any]]:
    if not session_id:
        return []
    try:
        from documents import _make_doc_citation_id, hybrid_search  # noqa: PLC0415

        results = hybrid_search(query, session_id, n_results=limit, doc_ids=doc_ids)
    except Exception:
        return []
    chunks: list[dict[str, Any]] = []
    for index, result in enumerate(results):
        metadata = result.get("metadata") or {}
        citation_id = _make_doc_citation_id(metadata, index)
        doc_id = str(metadata.get("doc_id") or "unknown")
        chunks.append({
            "evidence_id": citation_id,
            "document_id": doc_id,
            "document_version_id": f"{doc_id}:v1",
            "citation_id": citation_id,
            "filename": metadata.get("filename") or "",
            "page": metadata.get("page"),
            "chunk_index": metadata.get("chunk_index", index),
            "text_span": str(result.get("text") or "")[:2400],
            "score_source": "dense_bm25_rrf",
        })
    return chunks


def build_evidence_pack(
    state: dict[str, Any],
    *,
    include_chunks: bool | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    """Fuse versioned facts, KG entities/relations, and document chunks."""
    messages = state.get("messages") or []
    query = ""
    for message in reversed(messages):
        if message.__class__.__name__ == "HumanMessage" and isinstance(message.content, str):
            query = message.content
            break
    session_id = str(state.get("session_id") or "")
    route = ((state.get("current_turn") or {}).get("route") or state.get("route_decision") or {})
    route_level = str(route.get("route_level") or "direct")
    if include_chunks is None:
        include_chunks = route_level in {"small_task", "workflow", "case"} or bool(
            re.search(r"\b(document|filing|contract|pdf|uploaded|statement|clause)\b", query, re.IGNORECASE)
        )
    approved_context = state.get("approved_workflow_context") or {}
    doc_ids = approved_context.get("selected_doc_ids") or None
    facts = _fact_refs(query, session_id, limit)
    entities, relationships = _kg_refs(query, session_id, limit)
    chunks = _chunk_refs(query, session_id, limit, doc_ids) if include_chunks else []
    coverage = []
    if facts:
        coverage.append("durable_facts")
    if entities:
        coverage.append("entity_graph")
    if chunks:
        coverage.append("document_evidence")
    return EvidencePack(
        query=query,
        entity_refs=entities,
        fact_refs=facts,
        chunk_refs=chunks,
        relationship_refs=relationships,
        coverage=coverage,
        missing=[] if coverage else ["No relevant durable evidence found"],
        contradictions=[],
    ).model_dump()


def format_evidence_pack_prompt(pack: dict[str, Any] | None) -> str:
    if not pack or not any(pack.get(key) for key in ("fact_refs", "chunk_refs", "entity_refs")):
        return ""
    return (
        "\n\n## Retrieved evidence pack\n"
        "Use only relevant evidence. Preserve exact citation_id values for document-supported claims. "
        "Fact summaries are claims, not substitutes for their evidence. Report contradictions.\n"
        f"{json.dumps(pack, ensure_ascii=False, default=str)}"
    )
