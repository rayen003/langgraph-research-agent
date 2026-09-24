"""Retrievable object envelope shared by agents, tools, storage, and UI."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


ObjectStatus = Literal["draft", "pending", "running", "blocked", "complete", "failed", "archived"]
ObjectVisibility = Literal["private", "session", "case", "shared"]
SourceType = Literal["document", "api", "web", "manual_note", "kg_node", "artifact"]
ObjectRelation = Literal[
    "depends_on",
    "derived_from",
    "uses_source",
    "produces",
    "supersedes",
    "chronological_after",
    "cites",
    "answers",
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ObjectReference(BaseModel):
    """Directed edge to another retrievable object."""

    model_config = ConfigDict(extra="forbid")

    object_id: str = Field(min_length=1)
    object_type: str | None = None
    relation: ObjectRelation = "depends_on"
    sequence: int = Field(default=0, ge=0)
    required: bool = True
    note: str | None = None


class EntityCard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    name: str
    ticker: str | None = None


class SourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(min_length=1)
    source_type: SourceType
    title: str
    provider: str | None = None
    object_id: str | None = None
    url: str | None = None
    period: str | None = None
    retrieved_at: str | None = None
    quality_score: float | None = Field(default=None, ge=0.0, le=1.0)
    license_status: Literal["approved", "needs_review", "restricted", "unknown"] = "unknown"


class AgentObject(BaseModel):
    """Stable object shape every agent can index before reading full payload."""

    model_config = ConfigDict(extra="allow")

    object_id: str = Field(min_length=1)
    object_type: str = Field(min_length=1)
    schema_ref: str | None = None
    schema_version: str = "0.1"
    title: str = Field(min_length=1)
    summary: str = ""
    search_text: str = ""
    status: ObjectStatus = "draft"
    tags: list[str] = Field(default_factory=list)
    visibility: ObjectVisibility = "session"
    session_id: str | None = None
    thread_id: str | None = None
    case_id: str | None = None
    task_id: str | None = None
    run_id: str | None = None
    source_message_id: str | None = None
    created_by: str | None = None
    updated_by: str | None = None
    entity_refs: list[EntityCard] = Field(default_factory=list)
    source_refs: list[SourceRef] = Field(default_factory=list)
    source_version_ids: list[str] = Field(default_factory=list)
    references: list[ObjectReference] = Field(default_factory=list)
    kg_node_ids: list[str] = Field(default_factory=list)
    artifact_paths: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    quality: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)

    def sorted_references(self, relation: ObjectRelation | None = None) -> list[ObjectReference]:
        refs = [ref for ref in self.references if relation is None or ref.relation == relation]
        return sorted(refs, key=lambda ref: (ref.sequence, ref.relation, ref.object_type or "", ref.object_id))

    def dependency_ids(self) -> list[str]:
        relations = {"depends_on", "derived_from", "uses_source", "supersedes", "chronological_after"}
        return [ref.object_id for ref in self.sorted_references() if ref.relation in relations]

    def entity_cards(self) -> list[dict[str, Any]]:
        cards: list[dict[str, Any]] = []
        for entity in self.entity_refs:
            raw = entity.model_dump()
            cards.append({
                key: raw.get(key)
                for key in ("kind", "name", "ticker")
                if raw.get(key) not in (None, "")
            })
        return cards

    def search_card(self) -> dict[str, Any]:
        """Small card for search/planning. Full payload stays lazy."""

        return {
            "object_id": self.object_id,
            "object_type": self.object_type,
            "title": self.title,
            "summary": self.summary,
            "schema_ref": self.schema_ref,
            "schema_version": self.schema_version,
            "search_text": self.search_text,
            "status": self.status,
            "tags": list(self.tags),
            "entities": self.entity_cards(),
            "dependency_count": len(self.dependency_ids()),
            "confidence": self.confidence,
            "quality": dict(self.quality),
            "updated_at": self.updated_at,
        }

    def to_workspace_object(self, *, session_id: str | None = None, thread_id: str | None = None) -> dict[str, Any]:
        """Return shape accepted by storage.upsert_workspace_object()."""

        return {
            "object_id": self.object_id,
            "object_type": self.object_type,
            "schema_ref": self.schema_ref,
            "schema_version": self.schema_version,
            "title": self.title,
            "status": self.status,
            "session_id": session_id or self.session_id,
            "thread_id": thread_id or self.thread_id,
            "case_id": self.case_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "source_message_id": self.source_message_id,
            "created_by": self.created_by,
            "updated_by": self.updated_by,
            "source_object_ids": self.dependency_ids(),
            "source_version_ids": list(self.source_version_ids),
            "entity_refs": [entity.model_dump() for entity in self.entity_refs],
            "source_refs": [source.model_dump() for source in self.source_refs],
            "kg_node_ids": list(self.kg_node_ids),
            "artifact_paths": list(self.artifact_paths),
            "summary": self.summary,
            "search_text": self.search_text,
            "tags": list(self.tags),
            "confidence": self.confidence,
            "quality": dict(self.quality),
            "visibility": self.visibility,
            "payload": self.model_dump(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def walk_object_dependencies(
    root: AgentObject,
    objects_by_id: dict[str, AgentObject],
    *,
    max_depth: int = 8,
) -> list[AgentObject]:
    """Depth-first dependency walk with stable edge order and cycle guard."""

    seen: set[str] = {root.object_id}
    ordered: list[AgentObject] = []

    def visit(obj: AgentObject, depth: int) -> None:
        if depth >= max_depth:
            return
        for ref in obj.sorted_references():
            if ref.relation not in {"depends_on", "derived_from", "uses_source", "chronological_after"}:
                continue
            if ref.object_id in seen:
                continue
            target = objects_by_id.get(ref.object_id)
            if target is None:
                continue
            seen.add(ref.object_id)
            ordered.append(target)
            visit(target, depth + 1)

    visit(root, 0)
    return ordered
