"""Persist reusable turn outputs as compact workspace objects."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from storage import upsert_workspace_object

logger = logging.getLogger(__name__)


class EntityRef(BaseModel):
    kind: str
    name: str
    ticker: str | None = None


class WorkspaceObjectDraft(BaseModel):
    object_type: str = Field(description="Short stable object type, for example news_digest or research_finding")
    title: str
    summary: str
    entity_refs: list[EntityRef] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)


def _writer_llm() -> Any:
    model = os.getenv("AGENT_MEMORY_WRITER_MODEL", "gpt-4o-mini")
    return ChatOpenAI(
        model=model,
        api_key=os.getenv("OPENAI_API_KEY"),
        timeout=20,
        max_retries=0,
    ).with_structured_output(
        WorkspaceObjectDraft, method="function_calling"
    )


def _latest_user_text(messages: list[BaseMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return str(message.content or "")
    return ""


def _latest_answer(messages: list[BaseMessage]) -> str:
    for message in reversed(messages):
        if message.__class__.__name__ == "AIMessage" and isinstance(message.content, str):
            return message.content
    return ""


def persist_turn_object(state: dict[str, Any]) -> dict[str, Any] | None:
    """Extract and persist one reusable object; return stored object or None."""
    route = state.get("route_decision") or {}
    playbook = state.get("selected_playbook") or {}
    policy = playbook.get("object_policy") or {}
    should_persist = bool(
        route.get("creates_objects")
        or policy.get("creates_objects")
        or route.get("route_level") in {"tool", "small_task"}
    )
    if not should_persist or not state.get("session_id"):
        return None

    user_text = _latest_user_text(state.get("messages") or [])
    answer = _latest_answer(state.get("messages") or [])
    if not user_text or not answer:
        return None

    cards = [
        *(state.get("memory_context") or {}).get("retrieved_object_cards", []),
        *(state.get("memory_context") or {}).get("expanded_dependencies", []),
    ]
    workflow_context = state.get("approved_workflow_context") or {}
    selected_artifact_ids = set(workflow_context.get("selected_artifact_ids") or [])
    cards.extend(
        artifact
        for artifact in workflow_context.get("available_artifacts") or []
        if artifact.get("object_id") in selected_artifact_ids
    )
    cards_by_id = {
        str(card.get("object_id")): card
        for card in cards
        if isinstance(card, dict) and card.get("object_id")
    }
    cards = list(cards_by_id.values())
    prompt = {
        "task": "Convert latest research answer into one reusable workspace object.",
        "rules": [
            "Use only facts present in conversation and answer.",
            "Return concise metadata, never invent sources or numbers.",
            "Entity refs must identify companies with kind=company, name, and ticker when known.",
        ],
        "conversation": [
            {"role": message.__class__.__name__, "content": str(message.content)[:3000]}
            for message in (state.get("messages") or [])[-8:]
        ],
        "answer": answer[:6000],
        "prior_object_cards": cards,
    }
    # Micro news answers already contain persisted source pointers and do not
    # need another model call to choose object metadata. Keep response latency
    # bounded while still creating a reusable workspace object.
    if playbook.get("id") == "latest_company_news":
        draft = WorkspaceObjectDraft(
            object_type=str(policy.get("default_object_type") or "news_digest"),
            title=f"{user_text[:80].strip()} news digest",
            summary=answer[:1200],
            tags=["news", "current"],
            confidence=0.6,
        )
    else:
        try:
            draft = _writer_llm().invoke(json.dumps(prompt, ensure_ascii=False))
            if not isinstance(draft, WorkspaceObjectDraft):
                draft = WorkspaceObjectDraft.model_validate(draft)
        except Exception:  # noqa: BLE001
            logger.exception("memory writer failed")
            return None

    object_id = "turn:" + hashlib.sha256(
        f"{state['session_id']}\n{user_text}".encode("utf-8")
    ).hexdigest()[:16]
    stored = upsert_workspace_object({
        "object_id": object_id,
        "object_type": draft.object_type,
        "schema_ref": "workspace_object",
        "schema_version": "0.1",
        "title": draft.title,
        "status": "complete",
        "session_id": state.get("session_id"),
        "source_object_ids": [card.get("object_id") for card in cards if card.get("object_id")],
        "source_version_ids": [card.get("version_id") for card in cards if card.get("version_id")],
        "entity_refs": [entity.model_dump() for entity in draft.entity_refs],
        "tags": draft.tags,
        "confidence": draft.confidence,
        "created_by": "memory_writer",
        "updated_by": "memory_writer",
        "summary": draft.summary,
        "search_text": " ".join([draft.title, draft.summary, *draft.tags]),
        "payload": {"answer": answer, "request": user_text},
    })
    return stored
