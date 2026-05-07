"""Shared activity-event contract.

A single normalized event envelope (`type="activity"`) describes every
unit of agent work — chat tool calls, research tool calls, workflow
substeps, etc. — so the frontend has one model to render.

Legacy events (`tool_call_start/end/error`, `workflow_step`, ...) still
ship alongside activity events during the migration window. Once parity
is confirmed across all tools/workflows the legacy emissions can be
deleted.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

ACTIVITY_EVENT_TYPE = "activity"

# Coarse buckets — drives icon / colour / grouping in the UI.
ActivityKind = Literal[
    "tool",            # individual tool invocation (search_web, calculator, ...)
    "workflow",        # parent workflow span (e.g. DCF root)
    "workflow_step",   # substep inside a workflow
    "node",            # graph node-level activity (plan, synthesize, ...)
]

# Lifecycle states a single activity goes through.
ActivityStatus = Literal[
    "started",
    "running",      # alias for "in progress"; prefer "started" + "completed"
    "completed",
    "skipped",
    "error",
    "awaiting_input",
]

# Where the activity originated, so the UI can scope it correctly.
ActivityScope = Literal[
    "chat",            # inside a chat-mode ReAct turn
    "research",        # inside the research subgraph
    "workflow",        # inside a domain workflow (DCF, future: comps/LBO)
]


class ActivityEvent(TypedDict, total=False):
    """Canonical activity envelope sent over SSE.

    All keys except those marked Required are optional. Producers should
    use the `make_activity` helper rather than constructing this dict by
    hand to avoid drift.
    """
    type: str                 # always ACTIVITY_EVENT_TYPE
    activity_id: str          # stable id for matching start->end
    parent_activity_id: str   # nests workflow steps under their workflow
    kind: ActivityKind        # bucket for icon / grouping
    name: str                 # raw identifier (tool name, step id, ...)
    display_label: str        # optional override; UI may still re-label
    scope: ActivityScope      # research | chat | workflow
    status: ActivityStatus    # lifecycle state
    step_id: str              # research step id, or "chat" for chat scope
    started_at: float         # epoch seconds (set on the *first* emit)
    ended_at: float           # epoch seconds (set on terminal emit)
    summary: str              # one-line human readable
    args_preview: str         # short string preview of inputs
    confidence_label: str     # workflow-only; surfaces trust signal
    flag_count: int           # workflow-only; total quality flags
    error: str                # populated when status == "error"
    meta: dict[str, Any]      # free-form payload (links, ids, etc.)


def make_activity(
    *,
    activity_id: str,
    kind: ActivityKind,
    name: str,
    scope: ActivityScope,
    status: ActivityStatus,
    parent_activity_id: str | None = None,
    display_label: str | None = None,
    step_id: str | None = None,
    started_at: float | None = None,
    ended_at: float | None = None,
    summary: str | None = None,
    args_preview: str | None = None,
    confidence_label: str | None = None,
    flag_count: int | None = None,
    error: str | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an ActivityEvent payload, omitting unset fields."""
    payload: dict[str, Any] = {
        "type": ACTIVITY_EVENT_TYPE,
        "activity_id": activity_id,
        "kind": kind,
        "name": name,
        "scope": scope,
        "status": status,
    }
    if parent_activity_id:
        payload["parent_activity_id"] = parent_activity_id
    if display_label:
        payload["display_label"] = display_label
    if step_id:
        payload["step_id"] = step_id
    if started_at is not None:
        payload["started_at"] = started_at
    if ended_at is not None:
        payload["ended_at"] = ended_at
    if summary:
        payload["summary"] = summary
    if args_preview:
        payload["args_preview"] = args_preview
    if confidence_label:
        payload["confidence_label"] = confidence_label
    if flag_count is not None:
        payload["flag_count"] = flag_count
    if error:
        payload["error"] = error
    if meta:
        payload["meta"] = meta
    return payload
