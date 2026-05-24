"""HITL assumption review node."""

from __future__ import annotations

import json
import logging
from typing import Any

try:
    from langgraph.types import interrupt
except ImportError:  # LangGraph >=1.0 style
    from langgraph.types import Interrupt

    def interrupt(payload: dict):  # type: ignore[no-redef]
        raise Interrupt(payload)

from .activity import emit_step
from .state import _ASSUMPTION_FIELDS, clip_to_field_range

logger = logging.getLogger(__name__)


def review_assumptions_node(state: dict) -> dict:
    """Present assumptions for human review (approve / reject / edit).

    When ``assumption_review_mode`` is False, auto-approves.
    """
    parent_step_id = state.get("parent_step_id") or "workflow_dcf"
    if not state.get("assumption_review_mode"):
        emit_step("assumption_review", "skipped", parent_step_id)
        return {"assumptions_approved": True}

    # Build truncated evidence items for HITL payload (text capped at 400 chars)
    raw_items = (state.get("evidence_pack") or {}).get("items", [])
    evidence_items = []
    for item in raw_items:
        ev = {k: v for k, v in item.items() if k != "text"}
        if "text" in item:
            ev["text"] = str(item["text"])[:400]
        evidence_items.append(ev)

    emit_step(
        "assumption_review", "awaiting_input", parent_step_id,
        {
            "assumptions": state.get("assumptions", {}),
            "assumption_provenance": state.get("assumption_provenance", {}),
        },
    )
    decision = interrupt({
        "action": "review_assumptions",
        "workflow": "dcf",
        "message": "Approve or edit DCF assumptions before valuation.",
        "assumptions": state.get("assumptions", {}),
        "assumption_provenance": state.get("assumption_provenance", {}),
        "assumption_memo": state.get("assumption_memo"),
        "evidence_items": evidence_items,
        "scenarios": state.get("scenarios", []),
        "company_state": state.get("company_state"),
        "thesis": state.get("thesis"),
        "features": state.get("features", {}),
        "fundamentals": state.get("fundamentals", {}),
        "profile": state.get("profile", "default"),
        "profile_meta": state.get("profile_meta", {}),
        "wacc_components": state.get("wacc_components", {}),
        "choices": ["approve", "reject", "edit"],
    })
    action = str(decision.get("action") or "approve").lower()
    if action == "reject":
        emit_step("assumption_review", "rejected", parent_step_id)
        return {"assumptions_approved": False}

    if action == "edit":
        edits = decision.get("assumptions")
        if isinstance(edits, dict):
            merged = dict(state.get("assumptions", {}))
            provenance = dict(state.get("assumption_provenance", {}))
            for key, value in edits.items():
                if key not in _ASSUMPTION_FIELDS:
                    continue
                normalized = clip_to_field_range(key, float(value))
                if normalized is None:
                    continue
                merged[key] = normalized
                prior = dict(provenance.get(key) or {})
                prior["user_edited"] = True
                prior["approved_by"] = "user"
                if not prior.get("source"):
                    prior["source"] = "user_override"
                if not prior.get("evidence"):
                    prior["evidence"] = "User edited assumption during review."
                provenance[key] = prior
            emit_step(
                "assumption_review", "edited", parent_step_id,
                {"assumptions": merged, "assumption_provenance": provenance},
            )
            logger.info(
                "DCF assumption_review edited_assumptions=%s",
                json.dumps(merged, ensure_ascii=False),
            )
            return {
                "assumptions": merged,
                "assumption_provenance": provenance,
                "assumptions_approved": True,
            }

    emit_step("assumption_review", "approved", parent_step_id)
    return {"assumptions_approved": True}


def route_after_assumptions(state: dict) -> str:
    """Route to scenario runner or end the workflow."""
    from langgraph.graph import END  # noqa: PLC0415
    return "scenario_runner" if state.get("assumptions_approved") else END
