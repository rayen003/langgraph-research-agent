"""Bounded, typed short-term context assembled before semantic routing."""

from __future__ import annotations

import json
from typing import Any, Literal

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from pydantic import BaseModel, ConfigDict, Field


class TurnMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str
    message_id: str | None = None


class WorkspaceCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    object_id: str
    object_type: str
    title: str
    summary: str = ""
    status: str | None = None
    entity_refs: list[dict[str, Any]] = Field(default_factory=list)
    source_object_ids: list[str] = Field(default_factory=list)
    updated_at: str | None = None


class TurnContextSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    latest_user_turn: str
    previous_user_turn: str | None = None
    previous_assistant_turn: str | None = None
    recent_messages: list[TurnMessage] = Field(default_factory=list)
    session_memory: str = ""
    workspace_candidates: list[WorkspaceCandidate] = Field(default_factory=list)
    active_entities: list[dict[str, Any]] = Field(default_factory=list)
    active_task: dict[str, Any] | None = None


def _text(message: BaseMessage) -> str:
    if isinstance(message.content, str):
        return message.content
    return json.dumps(message.content, ensure_ascii=False, default=str)


def _workspace_card(obj: dict[str, Any]) -> WorkspaceCandidate | None:
    object_id = str(obj.get("object_id") or "")
    object_type = str(obj.get("object_type") or "")
    title = str(obj.get("title") or "")
    if not object_id or not object_type or not title:
        return None
    return WorkspaceCandidate(
        object_id=object_id,
        object_type=object_type,
        title=title,
        summary=str(obj.get("summary") or "")[:1200],
        status=str(obj["status"]) if obj.get("status") is not None else None,
        entity_refs=[ref for ref in (obj.get("entity_refs") or []) if isinstance(ref, dict)],
        source_object_ids=[str(value) for value in (obj.get("source_object_ids") or [])],
        updated_at=str(obj["updated_at"]) if obj.get("updated_at") is not None else None,
    )


def build_turn_context_snapshot(state: dict[str, Any]) -> TurnContextSnapshot:
    """Build cheap pre-route context from checkpoint state and recent object metadata."""
    messages = [
        message
        for message in (state.get("messages") or [])
        if isinstance(message, (HumanMessage, AIMessage))
    ]
    user_messages = [message for message in messages if isinstance(message, HumanMessage)]
    assistant_messages = [message for message in messages if isinstance(message, AIMessage)]
    latest_user = _text(user_messages[-1]) if user_messages else ""
    previous_user = _text(user_messages[-2]) if len(user_messages) > 1 else None
    previous_assistant = _text(assistant_messages[-1]) if assistant_messages else None

    recent_messages = [
        TurnMessage(
            role="user" if isinstance(message, HumanMessage) else "assistant",
            content=_text(message)[:4000],
            message_id=str(message.id) if message.id else None,
        )
        for message in messages[-8:]
    ]

    session_memory = ""
    objects: list[dict[str, Any]] = []
    session_id = str(state.get("session_id") or "")
    if session_id:
        try:
            from storage import get_session_memory, list_workspace_objects  # noqa: PLC0415

            session_memory = str(get_session_memory(session_id) or "")[:3000]
            objects = list_workspace_objects(session_id=session_id, limit=6)
        except Exception:  # noqa: BLE001
            # Routing must remain available when optional durable memory is unavailable.
            session_memory = ""
            objects = []

    candidates = [card for obj in objects if (card := _workspace_card(obj)) is not None]
    active_entities: list[dict[str, Any]] = []
    seen_entities: set[str] = set()
    for card in candidates:
        for entity in card.entity_refs:
            key = json.dumps(entity, sort_keys=True, default=str)
            if key not in seen_entities:
                seen_entities.add(key)
                active_entities.append(entity)

    return TurnContextSnapshot(
        latest_user_turn=latest_user,
        previous_user_turn=previous_user,
        previous_assistant_turn=previous_assistant,
        recent_messages=recent_messages,
        session_memory=session_memory,
        workspace_candidates=candidates,
        active_entities=active_entities[:12],
        active_task=state.get("task_context") if isinstance(state.get("task_context"), dict) else None,
    )


def format_turn_context_prompt(context: dict[str, Any] | TurnContextSnapshot | None) -> str:
    if not context:
        return ""
    snapshot = context if isinstance(context, TurnContextSnapshot) else TurnContextSnapshot.model_validate(context)
    return (
        "\n\n## Current turn context\n"
        "This snapshot comes from the current LangGraph thread checkpoint. "
        "Use it to resolve references and conversation continuity.\n"
        f"{json.dumps(snapshot.model_dump(), ensure_ascii=False, default=str)}"
    )
