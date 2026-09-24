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

from utils import emit_ui_event
from workflow_activity import emit_workflow, emit_workflow_step


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
    emit_workflow_step(workflow="deck", step=step, status=status, parent_step_id=parent_step_id, payload=payload)


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
    emit_workflow(workflow="deck", parent_step_id=parent_step_id, status=status, payload=payload)


__all__ = ["emit_step", "emit_progress", "emit_workflow_terminal"]
