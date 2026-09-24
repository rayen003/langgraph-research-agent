"""Append-only persistence boundaries for memo workflow."""

from __future__ import annotations

from typing import Any

from storage import upsert_workspace_object


def _source_lineage(sources: list[dict[str, Any]]) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    object_ids = list(dict.fromkeys(str(s["object_id"]) for s in sources if s.get("object_id")))
    version_ids = list(dict.fromkeys(str(s["version_id"]) for s in sources if s.get("version_id")))
    refs = [ref for source in sources for ref in source.get("source_refs") or []]
    return object_ids, version_ids, refs


def persist_memo_context(state: dict[str, Any]) -> dict[str, Any]:
    object_ids, version_ids, refs = _source_lineage(state.get("sources") or [])
    thread_id = str(state["thread_id"])
    brief = state.get("brief") or {}
    return upsert_workspace_object({
        "object_id": f"memo_context:{thread_id}",
        "object_type": "memo_context",
        "schema_ref": "workflows.memo.MemoContext",
        "schema_version": "0.1",
        "title": f"{brief.get('title', 'Investment memo')} context",
        "status": "complete",
        "session_id": state.get("session_id") or "",
        "thread_id": thread_id,
        "created_by": "workflow:memo",
        "updated_by": "workflow:memo",
        "source_object_ids": object_ids,
        "source_version_ids": version_ids,
        "source_refs": refs,
        "summary": f"Memo brief with {len(state.get('sources') or [])} selected source(s).",
        "search_text": f"{brief.get('title', '')} memo context",
        "tags": ["memo", "context"],
        "payload": {"brief": brief, "sources": state.get("sources") or []},
    })


def persist_memo_draft(state: dict[str, Any], *, actor_id: str, decision: str) -> dict[str, Any]:
    thread_id = str(state["thread_id"])
    context_version = state.get("context_version_id")
    return upsert_workspace_object({
        "object_id": f"memo_draft:{thread_id}",
        "object_type": "memo_draft",
        "schema_ref": "workflows.memo.MemoDraft",
        "schema_version": "0.1",
        "title": str((state.get("draft") or {}).get("title") or "Investment memo draft"),
        "status": "complete",
        "session_id": state.get("session_id") or "",
        "thread_id": thread_id,
        "created_by": actor_id,
        "updated_by": actor_id,
        "source_object_ids": [f"memo_context:{thread_id}"],
        "source_version_ids": [context_version] if context_version else [],
        "summary": str((state.get("draft") or {}).get("executive_summary") or "")[:500],
        "search_text": f"{(state.get('draft') or {}).get('title', '')} approved memo draft",
        "tags": ["memo", "draft", "approved"],
        "payload": {"draft": state.get("draft") or {}, "decision": decision},
    }, action_type="edited" if decision == "edit" else "approved")


def persist_memo_result(state: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
    thread_id = str(state["thread_id"])
    source_versions = [v for v in (state.get("context_version_id"), state.get("approved_draft_version_id")) if v]
    return upsert_workspace_object({
        "object_id": f"memo:{thread_id}",
        "object_type": "memo",
        "schema_ref": "workflows.memo.MemoOutput",
        "schema_version": "0.1",
        "title": output["title"],
        "status": "complete",
        "session_id": state.get("session_id") or "",
        "thread_id": thread_id,
        "created_by": "workflow:memo",
        "updated_by": "workflow:memo",
        "source_object_ids": [f"memo_context:{thread_id}", f"memo_draft:{thread_id}"],
        "source_version_ids": source_versions,
        "source_refs": output.get("source_refs") or [],
        "artifact_paths": [output["markdown_path"]] if output.get("markdown_path") else [],
        "confidence": output.get("confidence"),
        "summary": str((state.get("draft") or {}).get("executive_summary") or "")[:500],
        "search_text": f"{output['title']} investment memo recommendation valuation risks",
        "tags": ["memo", "investment_committee"],
        "payload": output,
    })

