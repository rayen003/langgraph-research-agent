"""Canonical user-facing activity helpers for every domain workflow."""

from __future__ import annotations

import time
from typing import Any

import agent_log
from utils import emit_activity

_STATUS_MAP = {
    "start": "started",
    "running": "running",
    "complete": "completed",
    "completed": "completed",
    "skipped": "skipped",
    "awaiting_input": "awaiting_input",
    "edited": "completed",
    "approved": "completed",
    "rejected": "error",
    "fallback": "completed",
    "warning": "completed",
    "error": "error",
}
def workflow_activity_id(workflow: str, parent_step_id: str) -> str:
    return f"workflow_{workflow}_{parent_step_id}"


def workflow_step_activity_id(workflow: str, parent_step_id: str, step: str) -> str:
    return f"{workflow}_{parent_step_id}_{step}"


def _presentation(payload: dict[str, Any] | None) -> tuple[str | None, dict[str, Any] | None, dict[str, Any] | None]:
    data = dict(payload or {})
    summary = data.pop("summary_line", None)
    detail = data.pop("detail", None)
    if detail is not None and not isinstance(detail, dict):
        detail = {"notes": [str(detail)]}
    review_ref = data.pop("review_ref", None)
    if review_ref is not None and not isinstance(review_ref, dict):
        review_ref = {"id": str(review_ref)}
    return str(summary) if summary else None, detail, review_ref


def emit_workflow_step(
    *,
    workflow: str,
    step: str,
    status: str,
    parent_step_id: str,
    payload: dict[str, Any] | None = None,
    parent_activity_id: str | None = None,
) -> None:
    """Emit stable workflow step event with display and audit data separated."""
    activity_id = workflow_step_activity_id(workflow, parent_step_id, step)
    activity_status = _STATUS_MAP.get(status, "completed")
    now = time.time()
    started_at = now if activity_status in {"started", "running"} else None
    ended_at = None if activity_status in {"started", "running"} else now
    summary, detail, review_ref = _presentation(payload)
    metadata = dict(payload or {})
    metadata.pop("summary_line", None)
    metadata.pop("detail", None)
    metadata.pop("review_ref", None)

    if status == "start":
        agent_log.dcf_step_start(step, parent_step_id, summary or "")
    else:
        agent_log.dcf_step_done(step, parent_step_id, summary or "", activity_status)

    emit_activity(
        activity_id=activity_id,
        kind="workflow_step",
        name=f"workflow:{workflow}:{step}",
        scope="workflow",
        status=activity_status,  # type: ignore[arg-type]
        step_id=parent_step_id,
        parent_activity_id=parent_activity_id or workflow_activity_id(workflow, parent_step_id),
        started_at=started_at,
        ended_at=ended_at,
        summary=summary,
        error=str(metadata.get("error")) if activity_status == "error" and metadata.get("error") else None,
        detail=detail,
        review_ref=review_ref,
        meta=metadata or None,
    )


def emit_workflow(
    *,
    workflow: str,
    parent_step_id: str,
    status: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Emit workflow root event. Review content stays in separate HITL event."""
    activity_id = workflow_activity_id(workflow, parent_step_id)
    activity_status = _STATUS_MAP.get(status, status)
    now = time.time()
    started_at = now if activity_status in {"started", "running"} else None
    ended_at = None if activity_status in {"started", "running", "awaiting_input"} else now
    summary, detail, review_ref = _presentation(payload)
    metadata = dict(payload or {})
    metadata.pop("summary_line", None)
    metadata.pop("detail", None)
    metadata.pop("review_ref", None)
    emit_activity(
        activity_id=activity_id,
        kind="workflow",
        name=f"workflow:{workflow}",
        scope="workflow",
        status=activity_status,  # type: ignore[arg-type]
        step_id=parent_step_id,
        started_at=started_at,
        ended_at=ended_at,
        summary=summary,
        confidence_label=metadata.get("confidence_label"),
        flag_count=metadata.get("flag_count"),
        error=str(metadata.get("error")) if activity_status == "error" and metadata.get("error") else None,
        detail=detail,
        review_ref=review_ref,
        meta=metadata or None,
    )


__all__ = ["emit_workflow", "emit_workflow_step", "workflow_activity_id", "workflow_step_activity_id"]
