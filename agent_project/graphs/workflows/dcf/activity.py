"""DCF workflow activity-emission helpers (unified contract)."""

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
}


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
    activity_status = _ACTIVITY_STATUS_MAP.get(status, "completed")
    summary = ""
    meta: dict[str, Any] | None = None
    if payload:
        # Prefer explicit summary_line, fall back to auto-generated summary
        if "summary_line" in payload:
            summary = str(payload.pop("summary_line"))
        elif "ticker" in payload:
            summary = f"ticker={payload['ticker']}"
        elif "rows" in payload:
            summary = f"{payload['rows']} rows"
        elif "implied_share_price" in payload:
            summary = f"implied ${payload['implied_share_price']:.2f}"
        meta = dict(payload)

    # ── Terminal log ────────────────────────────────────────────────────────
    if status == "start":
        agent_log.dcf_step_start(step, parent_step_id, summary)
    else:
        agent_log.dcf_step_done(step, parent_step_id, summary, activity_status)

    emit_activity(
        activity_id=f"dcf_{parent_step_id}_{step}",
        kind="workflow_step",
        name=f"workflow:dcf:{step}",
        scope="workflow",
        status=activity_status,
        step_id=parent_step_id,
        parent_activity_id=f"workflow_dcf_{parent_step_id}",
        summary=summary or None,
        meta=meta,
        error=str(payload.get("error")) if status == "rejected" and payload else None,
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
    summary = None
    meta = dict(payload) if payload else None
    if payload and "implied_share_price" in payload:
        summary = f"implied ${payload['implied_share_price']:.2f}"
    elif payload and "summary_line" in payload:
        summary = str(payload.pop("summary_line"))
    emit_activity(
        activity_id=f"workflow_dcf_{parent_step_id}",
        kind="workflow",
        name="workflow:dcf",
        scope="workflow",
        status=status,  # type: ignore[arg-type]
        step_id=parent_step_id,
        summary=summary,
        confidence_label=payload.get("confidence_label") if payload else None,
        flag_count=payload.get("flag_count") if payload else None,
        meta=meta,
    )
