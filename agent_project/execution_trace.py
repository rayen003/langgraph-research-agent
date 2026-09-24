"""Stable execution-trace event contract for graph/UI/test instrumentation."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ExecutionStepEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["execution_step"] = "execution_step"
    stage: str
    node: str | None = None
    route_level: str | None = None
    playbook_id: str | None = None
    playbook_kind: str | None = None
    selected_workflow: str | None = None
    selected_case_type: str | None = None
    next_node: str | None = None
    allowed_tools: list[str] = Field(default_factory=list)
    tool_calls: list[str] = Field(default_factory=list)
    object_ids: list[str] = Field(default_factory=list)
    citation_ids: list[str] = Field(default_factory=list)
    latency_class: str | None = None
    status: str | None = None
    confidence: float | None = None
    max_tool_calls: int | None = None
    creates_objects: bool | None = None
    needs_confirmation: bool | None = None
    object_policy: str | None = None
    reason: str | None = None


def make_execution_step(stage: str, **kwargs: Any) -> dict[str, Any]:
    event = ExecutionStepEvent(stage=stage, node=kwargs.pop("node", stage), **kwargs)
    return event.model_dump()
