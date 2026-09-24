"""Versioned durable boundaries for DCF workflow inputs, decisions, and output."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from storage import upsert_workspace_object


def _thread_id(state: dict[str, Any]) -> str:
    explicit = str(state.get("thread_id") or "").strip()
    if explicit:
        return explicit
    from utils import get_run_dir

    return get_run_dir().name


def _ticker(state: dict[str, Any]) -> str:
    return str(state.get("ticker") or "DCF").upper()


def persist_workflow_context(state: dict[str, Any]) -> dict[str, Any]:
    thread_id = _thread_id(state)
    ticker = _ticker(state)
    context = dict(state.get("workflow_context") or {})
    return upsert_workspace_object({
        "object_id": f"dcf_context:{thread_id}",
        "object_type": "workflow_context",
        "schema_ref": "workflows.dcf.WorkflowContext",
        "schema_version": "0.1",
        "title": f"{ticker} DCF workflow context",
        "status": "complete",
        "session_id": state.get("session_id") or "",
        "thread_id": thread_id,
        "run_id": state.get("kg_run_id") or thread_id,
        "created_by": "workflow:dcf",
        "updated_by": "workflow:dcf",
        "entity_refs": [{"kind": "company", "name": ticker, "ticker": ticker}],
        "tags": [ticker.lower(), "dcf", "workflow-context"],
        "summary": f"Approved execution context for {ticker} DCF.",
        "search_text": f"{ticker} DCF workflow context",
        "payload": context,
    })


def persist_approved_assumptions(
    state: dict[str, Any],
    *,
    assumptions: dict[str, Any],
    provenance: dict[str, Any],
    actor_id: str,
    decision: str,
) -> dict[str, Any]:
    thread_id = _thread_id(state)
    ticker = _ticker(state)
    context_version_id = state.get("workflow_context_version_id")
    return upsert_workspace_object(
        {
            "object_id": f"dcf_assumptions:{thread_id}",
            "object_type": "dcf_assumptions",
            "schema_ref": "workflows.dcf.ApprovedAssumptions",
            "schema_version": "0.1",
            "title": f"{ticker} approved DCF assumptions",
            "status": "complete",
            "session_id": state.get("session_id") or "",
            "thread_id": thread_id,
            "run_id": state.get("kg_run_id") or thread_id,
            "created_by": actor_id,
            "updated_by": actor_id,
            "source_object_ids": [f"dcf_context:{thread_id}"] if context_version_id else [],
            "source_version_ids": [context_version_id] if context_version_id else [],
            "entity_refs": [{"kind": "company", "name": ticker, "ticker": ticker}],
            "tags": [ticker.lower(), "dcf", "assumptions", "approved"],
            "summary": f"User-approved assumptions for {ticker} DCF.",
            "search_text": f"{ticker} approved DCF assumptions WACC terminal growth",
            "payload": {
                "assumptions": assumptions,
                "assumption_provenance": provenance,
                "decision": decision,
                "workflow_context_version_id": context_version_id,
            },
        },
        action_type="approved" if decision == "approve" else "edited",
        action_metadata={"decision": decision},
    )


def persist_dcf_result(
    state: dict[str, Any],
    *,
    payload: dict[str, Any],
    result_path: str | Path,
) -> dict[str, Any]:
    thread_id = _thread_id(state)
    ticker = _ticker(state)
    context_version_id = state.get("workflow_context_version_id")
    assumptions_version_id = state.get("approved_assumptions_version_id")
    source_versions = [value for value in (context_version_id, assumptions_version_id) if value]
    valuation = payload.get("valuation") if isinstance(payload.get("valuation"), dict) else {}
    enriched_payload = {
        **payload,
        "implied_share_price": valuation.get("implied_share_price"),
        "current_price": valuation.get("current_price"),
        "confidence_label": payload.get("confidence_label"),
        "workflow_context_version_id": context_version_id,
        "approved_assumptions_version_id": assumptions_version_id,
        "result_path": str(result_path),
    }
    return upsert_workspace_object({
        "object_id": f"dcf_run:{thread_id}",
        "object_type": "dcf_run",
        "schema_ref": "domain.cases.ValuationRunResult",
        "schema_version": "0.1",
        "title": f"{ticker} DCF run",
        "status": "complete",
        "session_id": state.get("session_id") or "",
        "thread_id": thread_id,
        "run_id": state.get("kg_run_id") or thread_id,
        "created_by": "workflow:dcf",
        "updated_by": "workflow:dcf",
        "source_object_ids": [
            object_id for object_id, version_id in (
                (f"dcf_context:{thread_id}", context_version_id),
                (f"dcf_assumptions:{thread_id}", assumptions_version_id),
            ) if version_id
        ],
        "source_version_ids": source_versions,
        "entity_refs": [{"kind": "company", "name": ticker, "ticker": ticker}],
        "artifact_paths": [str(result_path)],
        "summary": f"Implied share price {valuation.get('implied_share_price', 'n/a')}",
        "search_text": f"{ticker} DCF valuation FCFF implied share price WACC terminal growth",
        "tags": [ticker.lower(), "dcf", "valuation"],
        "quality": {"validation_status": payload.get("model_validity")},
        "payload": enriched_payload,
    })
