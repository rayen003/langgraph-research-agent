"""Versioned durable boundaries for deck inputs, outline approval, and output."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from storage import upsert_workspace_object


def _thread_id(state: dict[str, Any]) -> str:
    explicit = str(state.get("thread_id") or "").strip()
    if explicit:
        return explicit
    from utils import get_run_dir

    return get_run_dir().name


def _title(state: dict[str, Any]) -> str:
    return str((state.get("brief") or {}).get("title") or "Presentation deck")


def _source_lineage(sources: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    object_ids: list[str] = []
    version_ids: list[str] = []
    for source in sources:
        object_id = source.get("object_id")
        version_id = source.get("version_id")
        if object_id and object_id not in object_ids:
            object_ids.append(str(object_id))
        if version_id and version_id not in version_ids:
            version_ids.append(str(version_id))
    return object_ids, version_ids


def persist_deck_context(state: dict[str, Any]) -> dict[str, Any]:
    thread_id = _thread_id(state)
    title = _title(state)
    sources = list(state.get("sources") or [])
    source_object_ids, source_version_ids = _source_lineage(sources)
    return upsert_workspace_object({
        "object_id": f"deck_context:{thread_id}",
        "object_type": "deck_context",
        "schema_ref": "workflows.deck.DeckContext",
        "schema_version": "0.1",
        "title": f"{title} context",
        "status": "complete",
        "session_id": state.get("session_id") or "",
        "thread_id": thread_id,
        "run_id": thread_id,
        "created_by": "workflow:deck",
        "updated_by": "workflow:deck",
        "source_object_ids": source_object_ids,
        "source_version_ids": source_version_ids,
        "tags": ["deck", "context"],
        "summary": f"Deck brief and {len(sources)} selected source(s).",
        "search_text": f"{title} deck context sources",
        "payload": {"brief": state.get("brief") or {}, "sources": sources},
    })


def persist_approved_outline(
    state: dict[str, Any],
    *,
    outline: dict[str, Any],
    actor_id: str,
    decision: str,
) -> dict[str, Any]:
    thread_id = _thread_id(state)
    context_version_id = state.get("deck_context_version_id")
    return upsert_workspace_object(
        {
            "object_id": f"deck_outline:{thread_id}",
            "object_type": "deck_outline",
            "schema_ref": "workflows.deck.DeckOutline",
            "schema_version": "0.1",
            "title": f"{_title(state)} approved outline",
            "status": "complete",
            "session_id": state.get("session_id") or "",
            "thread_id": thread_id,
            "run_id": thread_id,
            "created_by": actor_id,
            "updated_by": actor_id,
            "source_object_ids": [f"deck_context:{thread_id}"] if context_version_id else [],
            "source_version_ids": [context_version_id] if context_version_id else [],
            "tags": ["deck", "outline", "approved"],
            "summary": f"Approved outline with {len(outline.get('slides') or [])} slides.",
            "search_text": f"{_title(state)} approved deck outline",
            "payload": {
                "outline": outline,
                "decision": decision,
                "deck_context_version_id": context_version_id,
            },
        },
        action_type="approved" if decision == "approve" else "edited",
        action_metadata={"decision": decision},
    )


def persist_deck_result(
    state: dict[str, Any],
    *,
    payload: dict[str, Any],
    deck_output_path: str | Path,
) -> dict[str, Any]:
    thread_id = _thread_id(state)
    context_version_id = state.get("deck_context_version_id")
    outline_version_id = state.get("approved_outline_version_id")
    source_versions = [value for value in (context_version_id, outline_version_id) if value]
    enriched = {
        **payload,
        "deck_context_version_id": context_version_id,
        "approved_outline_version_id": outline_version_id,
        "deck_output_path": str(deck_output_path),
    }
    artifacts = [
        str(path) for path in (
            payload.get("pptx_path"), payload.get("pdf_path"),
            payload.get("html_path"), deck_output_path,
        ) if path
    ]
    return upsert_workspace_object({
        "object_id": f"deck:{thread_id}",
        "object_type": "deck",
        "schema_ref": "workspace.deck",
        "schema_version": "0.1",
        "title": _title(state),
        "status": "complete",
        "session_id": state.get("session_id") or "",
        "thread_id": thread_id,
        "run_id": state.get("deck_run_id") or thread_id,
        "created_by": "workflow:deck",
        "updated_by": "workflow:deck",
        "source_object_ids": [
            object_id for object_id, version_id in (
                (f"deck_context:{thread_id}", context_version_id),
                (f"deck_outline:{thread_id}", outline_version_id),
            ) if version_id
        ],
        "source_version_ids": source_versions,
        "artifact_paths": artifacts,
        "tags": ["deck", "presentation"],
        "summary": f"{len(payload.get('slides') or [])} slides",
        "search_text": f"{_title(state)} deck presentation slides",
        "payload": enriched,
    })

