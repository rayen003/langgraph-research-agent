"""Durable evidence, fact, entity, and relationship contracts."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .objects import AgentObject


FactStatus = Literal["candidate", "extracted", "verified", "disputed", "superseded"]


class EvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    document_id: str
    document_version_id: str
    citation_id: str
    filename: str = ""
    page: int | None = None
    chunk_index: int | None = None
    section_path: list[str] = Field(default_factory=list)
    text_span: str = ""


class FinancialFact(AgentObject):
    object_type: Literal["financial_fact"] = "financial_fact"
    subject_id: str
    predicate: str
    value: Any = None
    value_text: str = ""
    unit: str | None = None
    currency: str | None = None
    fiscal_period: str = ""
    as_of_date: str | None = None
    publication_date: str | None = None
    fact_status: FactStatus = "candidate"
    actual_or_estimate: Literal["actual", "estimate", "guidance", "unknown"] = "unknown"
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    supersedes_fact_version_id: str | None = None
    contradicts_fact_version_ids: list[str] = Field(default_factory=list)


class EntityRecord(AgentObject):
    object_type: Literal["entity"] = "entity"
    entity_kind: str
    canonical_name: str
    aliases: list[str] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)


class RelationshipRecord(AgentObject):
    object_type: Literal["relationship"] = "relationship"
    source_entity_id: str
    relation: str
    target_entity_id: str
    valid_from: str | None = None
    valid_to: str | None = None
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)


class EvidencePack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    entity_refs: list[dict[str, Any]] = Field(default_factory=list)
    fact_refs: list[dict[str, Any]] = Field(default_factory=list)
    chunk_refs: list[dict[str, Any]] = Field(default_factory=list)
    relationship_refs: list[dict[str, Any]] = Field(default_factory=list)
    coverage: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    contradictions: list[dict[str, Any]] = Field(default_factory=list)
