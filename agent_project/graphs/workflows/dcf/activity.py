"""DCF workflow activity-emission helpers (unified contract)."""

from __future__ import annotations

from typing import Any

from utils import emit_ui_event
from workflow_activity import emit_workflow, emit_workflow_step

def emit_step(
    step: str,
    status: str,
    parent_step_id: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Emit a DCF workflow substep as a unified activity event.

    Stable ``activity_id`` keyed by ``(parent_step_id, step)`` ensures the
    ``started`` and terminal events merge into one entry on the frontend.

    Prefers ``summary_line`` from payload for human-readable display;
    falls back to well-known keys (ticker, rows, implied_share_price).
    """
    normalized = dict(payload or {})
    if "summary_line" not in normalized:
        if "ticker" in normalized:
            normalized["summary_line"] = f"ticker={normalized['ticker']}"
        elif "rows" in normalized:
            normalized["summary_line"] = f"{normalized['rows']} rows"
        elif "implied_share_price" in normalized:
            normalized["summary_line"] = f"implied ${normalized['implied_share_price']:.2f}"
    emit_workflow_step(workflow="dcf", step=step, status=status, parent_step_id=parent_step_id, payload=normalized)


def emit_review_substep(
    step: str,
    status: str,
    workflow_parent_step_id: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Emit a sub-step nested inside the review_subgraph activity.

    Produces ``parent_activity_id = dcf_{workflow_parent_step_id}_review_subgraph``
    so the frontend groups review_deep_dive / synthesize_adjustments *inside*
    the review_subgraph row rather than at the top-level DCF group.
    """
    emit_workflow_step(
        workflow="dcf", step=step, status=status, parent_step_id=workflow_parent_step_id,
        payload=payload, parent_activity_id=f"dcf_{workflow_parent_step_id}_review_subgraph",
    )


def emit_progress(message: str) -> None:
    """Emit a chat-visible progress token during DCF execution.

    In chat mode, the frontend appends these tokens to the streaming
    assistant message, making DCF progress visible inline.
    """
    emit_ui_event({"type": "chat_token", "token": message})


def emit_workflow_terminal(
    *,
    parent_step_id: str,
    status: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Emit a terminal ``kind="workflow"`` span for the whole DCF run.

    Carries ``confidence_label`` and ``flag_count`` so the frontend can
    render trust signals without parsing the full output payload.
    """
    normalized = dict(payload or {})
    if "summary_line" not in normalized and "implied_share_price" in normalized:
        normalized["summary_line"] = f"implied ${normalized['implied_share_price']:.2f}"
    emit_workflow(workflow="dcf", parent_step_id=parent_step_id, status=status, payload=normalized)
