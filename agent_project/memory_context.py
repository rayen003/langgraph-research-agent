"""Explicit memory retrieval for route/playbook-aware graph turns."""

from __future__ import annotations

import re
import json
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage


def _latest_user_text(messages: list[BaseMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return message.content if isinstance(message.content, str) else ""
    return ""


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[A-Za-z0-9_]{3,}", (text or "").lower())
        if token not in {"the", "and", "for", "with", "from", "this", "that", "latest", "context"}
    }


def _memory_policy(route_level: str) -> str:
    if route_level == "direct":
        return "thread_only"
    if route_level == "tool":
        return "cards"
    if route_level == "small_task":
        return "scoped"
    if route_level == "workflow":
        return "expanded"
    if route_level == "case":
        return "broad"
    return "thread_only"


def _object_limit(policy: str) -> int:
    return {
        "thread_only": 0,
        "cards": 5,
        "scoped": 10,
        "expanded": 12,
        "broad": 20,
    }[policy]


def _card(obj: dict[str, Any]) -> dict[str, Any]:
    return {
        "object_id": obj.get("object_id"),
        "object_type": obj.get("object_type"),
        "schema_ref": obj.get("schema_ref"),
        "schema_version": obj.get("schema_version") or "0.1",
        "title": obj.get("title"),
        "summary": obj.get("summary") or "",
        "search_text": obj.get("search_text") or "",
        "status": obj.get("status"),
        "tags": obj.get("tags") or [],
        "entity_refs": obj.get("entity_refs") or [],
        "source_object_ids": obj.get("source_object_ids") or [],
        "source_refs": obj.get("source_refs") or [],
        "kg_node_ids": obj.get("kg_node_ids") or [],
        "artifact_paths": obj.get("artifact_paths") or [],
        "quality": obj.get("quality") or {},
        "confidence": obj.get("confidence"),
        "updated_at": obj.get("updated_at"),
    }


def _haystack(obj: dict[str, Any]) -> str:
    parts = [
        obj.get("object_id"),
        obj.get("object_type"),
        obj.get("title"),
        obj.get("summary"),
        obj.get("search_text"),
        " ".join(obj.get("tags") or []),
    ]
    for entity in obj.get("entity_refs") or []:
        if isinstance(entity, dict):
            parts.extend([entity.get("name"), entity.get("ticker")])
    return " ".join(str(part) for part in parts if part)


def _rank_objects(query: str, objects: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    query_tokens = _tokens(query)
    scored: list[tuple[int, dict[str, Any]]] = []
    for obj in objects:
        score = len(query_tokens & _tokens(_haystack(obj)))
        if score:
            scored.append((score, obj))
    if not scored:
        return []
    max_score = max(score for score, _obj in scored)
    threshold = max_score if max_score > 1 else 1
    ranked = [
        obj
        for score, obj in sorted(scored, key=lambda item: (-item[0], str(item[1].get("updated_at") or "")))
        if score >= threshold
    ]
    return ranked[:limit]


def _expand_dependency_cards(cards: list[dict[str, Any]], *, session_id: str, max_depth: int) -> list[dict[str, Any]]:
    if max_depth <= 0:
        return []
    from storage import get_workspace_object  # noqa: PLC0415

    seen = {str(card.get("object_id")) for card in cards}
    expanded: list[dict[str, Any]] = []

    def visit(source_ids: list[str], depth: int) -> None:
        if depth > max_depth:
            return
        for object_id in source_ids:
            if object_id in seen:
                continue
            obj = get_workspace_object(object_id)
            if not obj:
                continue
            if session_id and obj.get("session_id") not in {None, "", session_id}:
                continue
            seen.add(object_id)
            card = _card(obj)
            expanded.append(card)
            visit(card.get("source_object_ids") or [], depth + 1)

    for card in cards:
        visit(card.get("source_object_ids") or [], 1)
    return expanded


def _unique(values: list[Any]) -> list[Any]:
    seen: set[str] = set()
    out: list[Any] = []
    for value in values:
        key = str(value)
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def build_memory_context(state: dict[str, Any]) -> dict[str, Any]:
    from storage import get_session_memory, list_workspace_objects  # noqa: PLC0415

    route = ((state.get("current_turn") or {}).get("route") or state.get("route_decision") or {})
    route_level = str(route.get("route_level") or "")
    # Before controller runs, retrieve a bounded set of relevant object cards.
    # Controller then sees thread memory and workspace context when choosing work.
    policy = _memory_policy(route_level) if route_level else "scoped"
    session_id = str(state.get("session_id") or "")
    query = _latest_user_text(state.get("messages") or [])
    object_limit = _object_limit(policy)

    memory_summary = get_session_memory(session_id) if session_id else ""
    objects = list_workspace_objects(session_id=session_id, limit=50) if object_limit else []
    ranked = _rank_objects(query, objects, limit=object_limit) if object_limit else []
    # Workflow follow-ups often use coreference ("that DCF", "from this").
    # Preserve recent session objects when lexical ranking has no signal.
    if not ranked and policy in {"expanded", "broad"} and re.search(
        r"\b(that|this|said|previous|prior|context|above)\b", query, re.IGNORECASE
    ):
        ranked = objects[:object_limit]
    cards = [_card(obj) for obj in ranked]
    max_depth = {"expanded": 2, "broad": 3, "scoped": 1}.get(policy, 0)
    expanded = _expand_dependency_cards(cards, session_id=session_id, max_depth=max_depth)
    all_cards = cards + expanded

    return {
        "query": query,
        "route_decision": route,
        "selected_playbook_card": {
            key: (state.get("selected_playbook") or {}).get(key)
            for key in ("id", "kind", "title", "summary")
            if (state.get("selected_playbook") or {}).get(key) is not None
        },
        "memory_policy": policy,
        "memory_summary": memory_summary,
        "retrieved_object_cards": cards,
        "expanded_dependencies": expanded,
        "source_refs": _unique([
            source
            for card in all_cards
            for source in (card.get("source_refs") or [])
        ]),
        "kg_node_ids": _unique([
            node_id
            for card in all_cards
            for node_id in (card.get("kg_node_ids") or [])
        ]),
        "artifact_paths": _unique([
            path
            for card in all_cards
            for path in (card.get("artifact_paths") or [])
        ]),
        "gaps": [],
    }


def format_memory_context_prompt(context: dict[str, Any] | None) -> str:
    if not context:
        return ""
    card_keys = {
        "object_id",
        "object_type",
        "title",
        "summary",
        "search_text",
        "status",
        "tags",
        "entity_refs",
        "source_object_ids",
        "source_refs",
        "kg_node_ids",
        "artifact_paths",
        "quality",
        "confidence",
        "updated_at",
    }
    def compact_card(card: dict[str, Any]) -> dict[str, Any]:
        return {key: card.get(key) for key in card_keys if card.get(key) not in (None, "", [], {})}

    compact = {
        "memory_policy": context.get("memory_policy"),
        "memory_summary": context.get("memory_summary") or "",
        "retrieved_object_cards": [
            compact_card(card)
            for card in context.get("retrieved_object_cards") or []
            if isinstance(card, dict)
        ],
        "expanded_dependencies": [
            compact_card(card)
            for card in context.get("expanded_dependencies") or []
            if isinstance(card, dict)
        ],
        "source_refs": context.get("source_refs") or [],
        "kg_node_ids": context.get("kg_node_ids") or [],
        "artifact_paths": context.get("artifact_paths") or [],
        "gaps": context.get("gaps") or [],
    }
    if not any(compact[key] for key in compact):
        return ""
    return (
        "\n\n## Retrieved memory context\n"
        "Use this compact retrieved context when relevant. Object cards are summaries; "
        "do not assume hidden payload fields unless a tool/workflow retrieves them.\n"
        f"{json.dumps(compact, ensure_ascii=False, default=str)}"
    )
