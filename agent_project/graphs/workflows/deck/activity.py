"""Activity emitters for the deck workflow.

Standalone copy of the DCF activity-event contract — does NOT import from
``..dcf.activity`` because that would trigger ``dcf/__init__.py`` and load
the entire DCF graph (heavy module-level LLM init) just to read activity
helpers.  Keeping these inline preserves deck/ as a fully self-contained
module per the standalone-workflow design.

Event namespace uses ``deck`` prefix so frontend grouping does not collide
with DCF activities.  Frontend ActivityTrace renders both workflows
identically (kind="workflow_step", scope="workflow").
"""

from __future__ import annotations

from typing import Any

import agent_log
from utils import emit_activity, emit_ui_event


# Map internal step-status strings to the ActivityStatus literal.
_ACTIVITY_STATUS_MAP: dict[str, str] = {
    "start": "started",
    "complete": "completed",
    "skipped": "skipped",
    "awaiting_input": "awaiting_input",
    "edited": "completed",
    "approved": "completed",
    "rejected": "error",
    "fallback": "completed",
    "warning": "completed",
    "error": "error",
}


def emit_step(
    step: str,
    status: str,
    parent_step_id: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Emit a deck workflow substep as a unified activity event.

    Stable ``activity_id`` keyed by ``(parent_step_id, step)`` ensures the
    ``started`` and terminal events merge into one entry on the frontend.
    """
    activity_status = _ACTIVITY_STATUS_MAP.get(status, "completed")
    summary = ""
    meta: dict[str, Any] | None = None
    if payload:
        if "summary_line" in payload:
            summary = str(payload.pop("summary_line"))
        meta = dict(payload)

    if status == "start":
        # Re-use the DCF logger helpers — they take (step, parent, summary)
        # and are workflow-agnostic in practice.
        agent_log.dcf_step_start(step, parent_step_id, summary)
    else:
        agent_log.dcf_step_done(step, parent_step_id, summary, activity_status)

    emit_activity(
        activity_id=f"deck_{parent_step_id}_{step}",
        kind="workflow_step",
        name=f"workflow:deck:{step}",
        scope="workflow",
        status=activity_status,
        step_id=parent_step_id,
        parent_activity_id=f"workflow_deck_{parent_step_id}",
        summary=summary or None,
        meta=meta,
        error=str(payload.get("error")) if status in {"rejected", "error"} and payload else None,
    )


def emit_progress(message: str) -> None:
    """Emit a chat-visible progress token during deck execution."""
    emit_ui_event({"type": "chat_token", "token": message})


def emit_workflow_terminal(
    *,
    parent_step_id: str,
    status: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Emit a terminal ``kind="workflow"`` span for the whole deck run."""
    summary = None
    meta = dict(payload) if payload else None
    if payload and "summary_line" in payload:
        summary = str(payload.pop("summary_line"))
    emit_activity(
        activity_id=f"workflow_deck_{parent_step_id}",
        kind="workflow",
        name="workflow:deck",
        scope="workflow",
        status=status,  # type: ignore[arg-type]
        step_id=parent_step_id,
        summary=summary,
        meta=meta,
    )


__all__ = ["emit_step", "emit_progress", "emit_workflow_terminal"]
