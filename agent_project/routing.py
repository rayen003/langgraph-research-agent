"""Semantic route contracts for top-level graph control."""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


RouteLevel = Literal["direct", "tool", "small_task", "workflow", "case"]
LatencyClass = Literal["instant", "short", "medium", "long", "background"]
ObjectPolicy = Literal["none", "ephemeral", "task_result", "artifact", "case"]
SpeechAct = Literal["answer", "context_recall", "fresh_lookup", "continue", "create_artifact", "research_project"]
ContextReference = Literal["none", "previous_user_turn", "previous_assistant_turn", "recent_thread", "workspace"]
DataRequirement = Literal["none", "thread", "workspace", "fresh_external"]
RequestedOutput = Literal[
    "answer", "chart", "table", "file", "report", "pdf", "memo", "deck", "valuation"
]


class RouteDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route_level: RouteLevel
    playbook_id: str | None = None
    selected_workflow: str | None = None
    selected_case_type: str | None = None
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    latency_class: LatencyClass = "short"
    max_tool_calls: int = Field(default=0, ge=0)
    creates_objects: bool = False
    needs_confirmation: bool = False
    propose_task: bool = False
    object_policy: ObjectPolicy = "none"
    speech_act: SpeechAct = "answer"
    context_reference: ContextReference = "none"
    data_requirement: DataRequirement = "none"
    requested_outputs: list[RequestedOutput] = Field(default_factory=lambda: ["answer"])
    reason: str = ""

    @model_validator(mode="after")
    def validate_route_shape(self) -> "RouteDecision":
        if self.route_level == "workflow" and not self.selected_workflow:
            raise ValueError("workflow route requires selected_workflow")
        if self.route_level == "case" and not self.selected_case_type:
            raise ValueError("case route requires selected_case_type")
        if self.route_level in {"direct", "tool", "small_task"} and self.selected_workflow:
            raise ValueError("selected_workflow only valid for workflow route")
        if self.route_level != "case" and self.selected_case_type:
            raise ValueError("selected_case_type only valid for case route")
        return self


def parse_route_decision(raw: str | dict[str, Any]) -> RouteDecision:
    if isinstance(raw, dict):
        return RouteDecision.model_validate(raw)
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    payload = json.loads(match.group(0) if match else text)
    return RouteDecision.model_validate(payload)


def fallback_route(reason: str = "router fallback") -> RouteDecision:
    return RouteDecision(
        route_level="small_task",
        playbook_id=None,
        selected_workflow=None,
        selected_case_type=None,
        confidence=0.0,
        latency_class="short",
        max_tool_calls=4,
        creates_objects=False,
        needs_confirmation=False,
        object_policy="ephemeral",
        reason=reason,
    )


def resolved_intent_for(decision: RouteDecision) -> str:
    return "research" if decision.route_level == "small_task" else "chat"


def validate_route_payload(payload: dict[str, Any]) -> tuple[RouteDecision, str | None]:
    try:
        return RouteDecision.model_validate(payload), None
    except ValidationError as exc:
        return fallback_route("invalid router payload"), str(exc)
