"""Typed contracts and graph state for investment memo workflow."""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field


class MemoSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["workspace_object", "document", "manual_text"]
    title: str
    summary: str = ""
    content: str = ""
    object_id: str | None = None
    version_id: str | None = None
    doc_id: str | None = None
    source_refs: list[dict[str, Any]] = Field(default_factory=list)


class MemoBrief(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    company_name: str | None = None
    ticker: str | None = None
    audience: Literal["investment_committee", "portfolio_manager", "client", "internal"] = "investment_committee"
    objective: str = "Produce decision-ready investment memo."
    focus_areas: list[str] = Field(default_factory=list)
    required_sections: list[str] = Field(default_factory=lambda: [
        "Executive summary", "Investment thesis", "Valuation", "Catalysts", "Risks", "Recommendation",
    ])
    recommendation_required: bool = True
    hitl_mode: Literal["disabled", "review"] = "review"


class MemoDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    executive_summary: str
    sections: dict[str, str]
    recommendation: str
    source_version_ids: list[str] = Field(default_factory=list)
    source_refs: list[dict[str, Any]] = Field(default_factory=list)
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    limitations: list[str] = Field(default_factory=list)


class MemoOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow: Literal["memo"] = "memo"
    status: Literal["complete", "rejected"]
    memo_id: str
    memo_version_id: str | None = None
    context_version_id: str | None = None
    approved_draft_version_id: str | None = None
    title: str
    markdown: str
    markdown_path: str | None = None
    source_object_ids: list[str] = Field(default_factory=list)
    source_version_ids: list[str] = Field(default_factory=list)
    source_refs: list[dict[str, Any]] = Field(default_factory=list)
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    limitations: list[str] = Field(default_factory=list)


class MemoState(TypedDict, total=False):
    thread_id: str
    session_id: str
    parent_step_id: str
    sources: list[dict[str, Any]]
    brief: dict[str, Any]
    context_version_id: str | None
    draft: dict[str, Any]
    draft_approved: bool
    review_feedback: str | None
    approved_draft_version_id: str | None
    memo_version_id: str | None
    output: dict[str, Any]

