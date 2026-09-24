"""Versioned durable boundaries for research planning, execution, and reports."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from storage import upsert_workspace_object
from utils import get_run_dir


def _thread_id(state: dict[str, Any]) -> str:
    return str(state.get("thread_id") or get_run_dir().name)


def persist_research_context(state: dict[str, Any]) -> dict[str, Any]:
    thread_id = _thread_id(state)
    objective = str(state.get("objective") or "Research task")
    return upsert_workspace_object({
        "object_id": f"research_context:{thread_id}",
        "object_type": "research_context",
        "schema_ref": "research.ResearchContext",
        "schema_version": "0.1",
        "title": f"Research context: {objective[:80]}",
        "status": "complete",
        "session_id": state.get("session_id") or "",
        "thread_id": thread_id,
        "run_id": thread_id,
        "created_by": "workflow:research",
        "updated_by": "workflow:research",
        "tags": ["research", "context"],
        "summary": objective[:220],
        "search_text": objective,
        "payload": {
            "objective": objective,
            "memory_context": state.get("memory_context") or {},
            "user_settings": state.get("user_settings") or {},
        },
    })


def _persist_plan(
    state: dict[str, Any],
    plan: dict[str, Any],
    *,
    actor_id: str,
    action_type: str,
    decision: str | None = None,
) -> dict[str, Any]:
    thread_id = _thread_id(state)
    context_version_id = state.get("research_context_version_id")
    return upsert_workspace_object(
        {
            "object_id": f"research_plan:{thread_id}",
            "object_type": "research_plan",
            "schema_ref": "research.Plan",
            "schema_version": "0.1",
            "title": f"Research plan: {str(plan.get('query') or '')[:80]}",
            "status": "complete" if plan.get("status") == "completed" else str(plan.get("status") or "draft"),
            "session_id": state.get("session_id") or "",
            "thread_id": thread_id,
            "run_id": thread_id,
            "created_by": actor_id,
            "updated_by": actor_id,
            "source_object_ids": [f"research_context:{thread_id}"] if context_version_id else [],
            "source_version_ids": [context_version_id] if context_version_id else [],
            "tags": ["research", "plan", str(plan.get("status") or "draft")],
            "summary": f"{len(plan.get('steps') or [])} research steps.",
            "search_text": str(plan.get("query") or ""),
            "payload": {"plan": plan, "decision": decision},
        },
        action_type=action_type,
        action_metadata={"decision": decision} if decision else {},
    )


def persist_draft_plan(state: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    return _persist_plan(state, plan, actor_id="workflow:research", action_type="drafted")


def persist_approved_plan(
    state: dict[str, Any],
    plan: dict[str, Any],
    *,
    actor_id: str,
    decision: str,
) -> dict[str, Any]:
    return _persist_plan(state, plan, actor_id=actor_id, action_type="approved" if decision == "yes" else "edited", decision=decision)


def persist_step_result(
    state: dict[str, Any],
    *,
    step: dict[str, Any],
    tool_result_ids: list[str],
) -> dict[str, Any]:
    thread_id = _thread_id(state)
    plan_version_id = state.get("approved_plan_version_id")
    dependencies = [
        (state.get("step_result_version_ids") or {}).get(step_id)
        for step_id in (step.get("depends_on") or [])
    ]
    source_versions = [value for value in [plan_version_id, *dependencies] if value]
    return upsert_workspace_object({
        "object_id": f"research_step:{thread_id}:{step['id']}",
        "object_type": "research_step_result",
        "schema_ref": "research.StepResult",
        "schema_version": "0.1",
        "title": str(step.get("description") or step["id"]),
        "status": "complete",
        "session_id": state.get("session_id") or "",
        "thread_id": thread_id,
        "task_id": str(step["id"]),
        "run_id": thread_id,
        "created_by": "workflow:research",
        "updated_by": "workflow:research",
        "source_object_ids": [f"research_plan:{thread_id}"],
        "source_version_ids": source_versions,
        "tags": ["research", "step-result"],
        "summary": str(step.get("result") or "")[:220],
        "search_text": f"{step.get('description', '')} {step.get('result', '')}",
        "payload": {"step": step, "tool_result_ids": tool_result_ids},
    })


def persist_research_report(
    state: dict[str, Any],
    *,
    content: str,
    report_path: str | Path,
) -> dict[str, Any]:
    thread_id = _thread_id(state)
    plan_version_id = state.get("approved_plan_version_id")
    step_versions = list((state.get("step_result_version_ids") or {}).values())
    source_versions = [value for value in [plan_version_id, *step_versions] if value]
    return upsert_workspace_object({
        "object_id": f"research_report:{thread_id}",
        "object_type": "research_report",
        "schema_ref": "research.Report",
        "schema_version": "0.1",
        "title": str(state.get("objective") or "Research report")[:100],
        "status": "complete",
        "session_id": state.get("session_id") or "",
        "thread_id": thread_id,
        "run_id": thread_id,
        "created_by": "workflow:research",
        "updated_by": "workflow:research",
        "source_object_ids": [
            f"research_plan:{thread_id}",
            *[f"research_step:{thread_id}:{step_id}" for step_id in (state.get("step_result_version_ids") or {})],
        ],
        "source_version_ids": source_versions,
        "artifact_paths": [str(report_path)],
        "tags": ["research", "report"],
        "summary": " ".join(content.split())[:220],
        "search_text": f"{state.get('objective', '')} {content}",
        "payload": {
            "objective": state.get("objective") or "",
            "content": content,
            "report_path": str(report_path),
            "approved_plan_version_id": plan_version_id,
            "step_result_version_ids": state.get("step_result_version_ids") or {},
        },
    })

